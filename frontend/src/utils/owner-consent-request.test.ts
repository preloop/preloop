import { expect } from '@open-wc/testing';
import sinon from 'sinon';
import { ownerConsentRequest } from './owner-consent-request';

const json = (value: unknown, status = 200) =>
  new Response(JSON.stringify(value), { status });
const pending = () =>
  json(
    {
      detail: {
        status: 'pending',
        approval_id: 'approval-1',
        message: 'Waiting for runner owner',
      },
    },
    409
  );
const request = {
  method: 'POST',
  body: JSON.stringify({ message: 'Example task' }),
};

describe('ownerConsentRequest', () => {
  it('sends an immutable UUID intent and reuses it after approval', async () => {
    const fetcher = sinon.stub();
    fetcher.onCall(0).resolves(pending());
    fetcher.onCall(1).resolves(json({ status: 'pending' }));
    fetcher.onCall(2).resolves(json({ status: 'approved' }));
    fetcher.onCall(3).resolves(json({ started: true }, 202));
    const response = await ownerConsentRequest(fetcher, '/start', request, {
      pollIntervalMs: 0,
    });
    expect(response.status).to.equal(202);
    const original = fetcher.firstCall.args[1] as RequestInit;
    expect(JSON.parse(original.body as string).consent_request_id).to.match(
      /^[0-9a-f-]{36}$/
    );
    expect(fetcher.lastCall.args[1].body).to.equal(original.body);
    expect(request.body).to.equal(JSON.stringify({ message: 'Example task' }));
  });

  for (const status of ['declined', 'expired', 'consumed']) {
    it(`retains the backend refusal after ${status} consent`, async () => {
      const refusal = json(
        {
          detail: {
            status,
            message: 'Owner consent does not authorize this session',
          },
        },
        403
      );
      const fetcher = sinon.stub();
      fetcher.onCall(0).resolves(pending());
      fetcher.onCall(1).resolves(json({ status }));
      fetcher.onCall(2).resolves(refusal);
      expect(await ownerConsentRequest(fetcher, '/start', request)).to.equal(
        refusal
      );
      expect(fetcher.lastCall.args[1].body).to.equal(
        fetcher.firstCall.args[1].body
      );
    });
  }

  it('generates a UUID when the console uses an insecure HTTP context', async () => {
    const uuid = sinon.stub(crypto, 'randomUUID').value(undefined);
    try {
      const fetcher = sinon.stub().resolves(json({ started: true }, 202));
      await ownerConsentRequest(fetcher, '/start', request);
      const body = JSON.parse(fetcher.firstCall.args[1].body as string);
      expect(body.consent_request_id).to.match(
        /^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/
      );
    } finally {
      uuid.restore();
    }
  });

  it('does not poll unrelated conflict responses', async () => {
    const conflict = json({ detail: { code: 'runner_offline' } }, 409);
    const fetcher = sinon.stub().resolves(conflict);
    expect(await ownerConsentRequest(fetcher, '/start', request)).to.equal(
      conflict
    );
    expect(fetcher.calledOnce).to.equal(true);
  });

  it('stops at the local follow limit and preserves the pending response', async () => {
    const response = pending();
    const fetcher = sinon.stub().resolves(response);
    expect(
      await ownerConsentRequest(fetcher, '/start', request, { maxWaitMs: 0 })
    ).to.equal(response);
    expect(fetcher.calledOnce).to.equal(true);
  });

  it('respects the approval expiration before retrying', async () => {
    const fetcher = sinon.stub();
    fetcher.onCall(0).resolves(pending());
    fetcher.onCall(1).resolves(
      json({
        status: 'pending',
        expires_at: new Date(Date.now() - 1000).toISOString(),
      })
    );
    fetcher.onCall(2).resolves(json({ detail: { status: 'expired' } }, 403));
    expect(
      (await ownerConsentRequest(fetcher, '/start', request)).status
    ).to.equal(403);
    expect(fetcher.callCount).to.equal(3);
  });

  it('aborts a pending wait without dispatching a retry', async () => {
    const controller = new AbortController();
    const fetcher = sinon.stub();
    fetcher.onCall(0).resolves(pending());
    fetcher.onCall(1).callsFake(async () => {
      setTimeout(() => controller.abort(), 0);
      return json({ status: 'pending' });
    });
    let error: unknown;
    try {
      await ownerConsentRequest(fetcher, '/start', {
        ...request,
        signal: controller.signal,
      });
    } catch (caught) {
      error = caught;
    }
    expect(error).to.be.instanceOf(DOMException);
    expect((error as DOMException).name).to.equal('AbortError');
    expect(fetcher.callCount).to.equal(2);
  });

  it('stops polling when the status endpoint refuses access', async () => {
    const refused = json({ detail: 'Not authorized' }, 403);
    const fetcher = sinon.stub();
    fetcher.onCall(0).resolves(pending());
    fetcher.onCall(1).resolves(refused);
    expect(await ownerConsentRequest(fetcher, '/start', request)).to.equal(
      refused
    );
    expect(fetcher.callCount).to.equal(2);
  });
});
