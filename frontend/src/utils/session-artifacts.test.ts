import { expect } from '@open-wc/testing';
import sinon from 'sinon';

import {
  MAX_CONCURRENT_ARTIFACT_LOADS,
  acquireSessionArtifact,
  MAX_EAGER_ARTIFACT_PREVIEWS,
  activeSessionArtifactLoads,
  claimEagerArtifactPreview,
  releaseEagerArtifactPreview,
  releaseSessionArtifact,
} from './session-artifacts';

describe('session artifact loads (#1690)', () => {
  let fetchStub: sinon.SinonStub;
  let pending: Array<() => void>;

  beforeEach(() => {
    localStorage.setItem('accessToken', 'test-access-token');
    pending = [];
    fetchStub = sinon.stub(window, 'fetch').callsFake(
      () =>
        new Promise<Response>((resolve) => {
          pending.push(() =>
            resolve(new Response(new Blob(['x'], { type: 'image/png' })))
          );
        })
    );
  });

  afterEach(() => {
    fetchStub.restore();
    localStorage.clear();
  });

  const flush = () => new Promise((resolve) => setTimeout(resolve, 0));

  it('keeps at most two artifact downloads in flight', async () => {
    const ids = ['a1', 'a2', 'a3', 'a4', 'a5'];
    const loads = ids.map((id) => acquireSessionArtifact('s-limit', id));
    await flush();

    expect(fetchStub.callCount).to.equal(MAX_CONCURRENT_ARTIFACT_LOADS);
    expect(activeSessionArtifactLoads()).to.equal(2);

    while (pending.length) {
      pending.shift()!();
      await flush();
      await flush();
    }
    const results = await Promise.all(loads);
    expect(results.every((r) => r.status === 'ok')).to.equal(true);
    expect(fetchStub.callCount).to.equal(ids.length);
    expect(activeSessionArtifactLoads()).to.equal(0);
    ids.forEach((id) => releaseSessionArtifact('s-limit', id));
  });

  it('skips a queued download released before it started', async () => {
    const first = acquireSessionArtifact('s-cancel', 'b1');
    const second = acquireSessionArtifact('s-cancel', 'b2');
    const third = acquireSessionArtifact('s-cancel', 'b3');
    await flush();
    releaseSessionArtifact('s-cancel', 'b3');

    while (pending.length) {
      pending.shift()!();
      await flush();
      await flush();
    }
    await Promise.all([first, second]);
    const skipped = await third;

    expect(skipped.status).to.equal('error');
    expect(fetchStub.callCount).to.equal(2);
    releaseSessionArtifact('s-cancel', 'b1');
    releaseSessionArtifact('s-cancel', 'b2');
  });

  it('reuses the bytes when a preview is released and mounted again', async () => {
    // A re-render that destroys and recreates a row released the last hold
    // and fetched the same artifact again: 8 fetches of one screencast
    // session on prod (#1690).
    const first = acquireSessionArtifact('s-remount', 'r1');
    await flush();
    pending.shift()!();
    const loaded = await first;
    expect(loaded.status).to.equal('ok');
    releaseSessionArtifact('s-remount', 'r1');

    const again = await acquireSessionArtifact('s-remount', 'r1');
    expect(again).to.deep.equal(loaded);
    expect(fetchStub.callCount).to.equal(1);
    releaseSessionArtifact('s-remount', 'r1');
  });

  it('allows two eager previews per session, then asks for a click', () => {
    expect(MAX_EAGER_ARTIFACT_PREVIEWS).to.equal(2);
    expect(claimEagerArtifactPreview('s-eager', 'e1')).to.equal(true);
    expect(claimEagerArtifactPreview('s-eager', 'e2')).to.equal(true);
    expect(claimEagerArtifactPreview('s-eager', 'e3')).to.equal(false);
    // The same artifact keeps its claim across remounts.
    expect(claimEagerArtifactPreview('s-eager', 'e1')).to.equal(true);
    // Another session has its own budget.
    expect(claimEagerArtifactPreview('s-eager-2', 'e3')).to.equal(true);
    // A released claim frees the slot.
    releaseEagerArtifactPreview('s-eager', 'e1');
    expect(claimEagerArtifactPreview('s-eager', 'e3')).to.equal(true);
  });
});
