import { expect } from '@open-wc/testing';
import sinon from 'sinon';
import { registerPasskey } from './passkeys';

describe('Passkey registration on an address the browser refuses', () => {
  afterEach(() => {
    sinon.restore();
    localStorage.clear();
  });

  // protects=a console opened at an IP address explains why passkeys cannot
  // work there (WebAuthn forbids IP addresses as relying party ids);
  // fails_when=the browser's "invalid domain" error is shown raw or silently
  // dropped; no_claim=server and authenticator are stubbed.
  it('says to open the console at a domain name', async () => {
    localStorage.setItem('accessToken', 'synthetic');
    sinon.stub(window, 'fetch').resolves(
      new Response(
        JSON.stringify({
          options: {
            challenge: 'AAAA',
            rp: { id: '127.0.0.1', name: 'Preloop' },
            user: { id: 'AAAA', name: 'jane@example.com', displayName: 'Jane' },
            pubKeyCredParams: [{ type: 'public-key', alg: -7 }],
          },
          challenge_token: 'synthetic-challenge',
        }),
        { status: 200, headers: { 'Content-Type': 'application/json' } }
      )
    );
    sinon
      .stub(navigator.credentials, 'create')
      .rejects(new DOMException('This is an invalid domain.', 'SecurityError'));
    let message = '';
    try {
      await registerPasskey();
    } catch (error) {
      message = (error as Error).message;
    }
    expect(message).to.contain('domain name');
    expect(message).to.contain('not an IP address');
  });
});
