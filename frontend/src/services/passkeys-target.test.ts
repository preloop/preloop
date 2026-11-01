import { expect } from '@open-wc/testing';
import sinon from 'sinon';
import { signInWithPasskey } from './passkeys';

describe('Account-specific passkey sign-in', () => {
  afterEach(() => sinon.restore());

  for (const accountId of [undefined, '11111111-1111-4111-8111-111111111111']) {
    it(`keeps the challenge target ${accountId ? 'bound on both requests' : 'absent for normal sign-in'}`, async () => {
      const fetch = sinon.stub(window, 'fetch');
      fetch.onFirstCall().resolves(
        new Response(
          JSON.stringify({
            options: { challenge: 'YQ' },
            challenge_token: 'synthetic-challenge',
          })
        )
      );
      fetch.onSecondCall().resolves(
        new Response(
          JSON.stringify({
            access_token: 'synthetic-access',
            refresh_token: 'synthetic-refresh',
          })
        )
      );
      sinon.stub(navigator.credentials, 'get').resolves({
        id: 'key',
        rawId: new ArrayBuffer(1),
        type: 'public-key',
        response: {
          clientDataJSON: new ArrayBuffer(1),
          authenticatorData: new ArrayBuffer(1),
          signature: new ArrayBuffer(1),
          userHandle: null,
        },
        getClientExtensionResults: () => ({}),
      } as unknown as PublicKeyCredential);
      await signInWithPasskey(accountId);
      const options = fetch.firstCall.args[1]!;
      const verify = JSON.parse(String(fetch.secondCall.args[1]!.body));
      if (accountId) {
        expect(JSON.parse(String(options.body))).to.deep.equal({
          account_id: accountId,
        });
        expect(verify.account_id).to.equal(accountId);
      } else {
        expect(options.body).to.equal(undefined);
        expect(verify).not.to.have.property('account_id');
      }
      expect(verify.challenge_token).to.equal('synthetic-challenge');
    });
  }
});
