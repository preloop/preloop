import { expect } from '@open-wc/testing';
import sinon from 'sinon';
import {
  beginOverviewLoad,
  beginOverviewShell,
  closeOverviewShell,
  readOverviewInput,
  invalidateOverviewLoads,
  type OverviewLoad,
} from './overview-bootstrap';

const params = {
  start_date: '2026-10-01T00:00:00.000Z',
  prior_start: '2026-09-01T00:00:00.000Z',
  prior_end: '2026-10-01T00:00:00.000Z',
  now: '2026-10-28T00:00:00.000Z',
};

describe('Overview response stream', () => {
  let load: OverviewLoad;
  let stream: ReadableStreamDefaultController<Uint8Array>;
  const send = (value: object) =>
    stream.enqueue(new TextEncoder().encode(JSON.stringify(value) + '\n'));
  beforeEach(() => {
    localStorage.setItem('accessToken', 'tenant-a');
    sinon.stub(window, 'fetch').resolves(
      new Response(
        new ReadableStream({
          start(controller) {
            stream = controller;
          },
        })
      )
    );
    load = beginOverviewLoad(params);
  });
  afterEach(() => {
    load.close();
    localStorage.removeItem('accessToken');
    sinon.restore();
  });

  it('delivers ready fields before slow inputs and preserves denial responses', async () => {
    send({ paths: ['/api/v1/agents?limit=100', '/api/v1/users'] });
    const ready = load.read('/api/v1/agents?limit=100', 'tenant-a');
    send({
      url: '/api/v1/agents?limit=100',
      status: 200,
      body: '{"items":[]}',
      headers: { 'content-type': 'application/json' },
    });
    expect(await (await ready)!.json()).to.eql({ items: [] });
    const forbidden = load.read('/api/v1/users', 'tenant-a');
    send({
      url: '/api/v1/users',
      status: 403,
      body: '{"detail":"denied"}',
      headers: { 'content-type': 'application/json' },
    });
    expect((await forbidden)!.status).to.equal(403);
    expect(await load.read('/api/v1/unrelated', 'tenant-a')).to.equal(null);
  });

  it('never returns prior-user results after authentication changes', async () => {
    send({ paths: ['/api/v1/agents'] });
    const result = load.read('/api/v1/agents', 'tenant-a');
    localStorage.setItem('accessToken', 'tenant-b');
    send({
      url: '/api/v1/agents',
      status: 200,
      body: '{"secret":"a"}',
      headers: {},
    });
    expect(
      await result.then(
        () => 'resolved',
        (error) => error.name
      )
    ).to.equal('AbortError');
    expect(await load.read('/api/v1/agents', 'tenant-b')).to.equal(null);
  });

  it('invalidates snapshot responses after writes or authentication cache resets', async () => {
    send({ paths: ['/api/v1/agents'] });
    send({
      url: '/api/v1/agents',
      status: 200,
      body: '{"items":[]}',
      headers: {},
    });
    expect((await load.read('/api/v1/agents', 'tenant-a'))!.status).to.equal(
      200
    );
    invalidateOverviewLoads();
    expect(await load.read('/api/v1/agents', 'tenant-a')).to.equal(null);
  });

  it('keeps unrelated queries outside the snapshot cancellation scope', async () => {
    send({ paths: ['/api/v1/agents?limit=100'] });
    await load.read('/api/v1/not-in-manifest', 'tenant-a');
    const unrelated = load.read('/api/v1/agents?limit=20', 'tenant-a');
    invalidateOverviewLoads();
    expect(await unrelated).to.equal(null);
  });

  it('routes shell inputs using the server manifest without a client path list', async () => {
    load.close();
    const previousUrl = window.location.href;
    history.replaceState(null, '', '/console');
    (window.fetch as sinon.SinonStub).resolves(
      new Response(
        new ReadableStream({
          start(controller) {
            stream = controller;
          },
        })
      )
    );
    try {
      beginOverviewShell();
      send({ paths: ['/api/v1/new-server-owned-shell-input'] });
      send({
        url: '/api/v1/new-server-owned-shell-input',
        status: 200,
        body: '{"ready":true}',
        headers: {},
      });
      const response = await readOverviewInput(
        '/api/v1/new-server-owned-shell-input',
        'tenant-a'
      );
      expect(await response!.json()).to.eql({ ready: true });
    } finally {
      closeOverviewShell();
      history.replaceState(null, '', previousUrl);
    }
  });

  it('releases pending readers when the view disconnects', async () => {
    send({ paths: ['/api/v1/agents'] });
    await load.read('/api/v1/not-in-manifest', 'tenant-a');
    const result = load.read('/api/v1/agents', 'tenant-a');
    load.close();
    expect(
      await result.then(
        () => 'resolved',
        (error) => error.name
      )
    ).to.equal('AbortError');
    expect(
      (window.fetch as sinon.SinonStub).firstCall.args[1].signal.aborted
    ).to.equal(true);
  });
});
