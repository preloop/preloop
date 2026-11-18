import { expect } from '@open-wc/testing';
import { rememberSessionEnd, takeSessionEndNotice } from './session-end-notice';

describe('Session end notice', () => {
  afterEach(() => sessionStorage.clear());

  // protects=policy and reset sign-outs explain themselves once; fails_when=
  // notice missing, repeated, or an unknown value is shown; no_claim=server.
  it('maps the server reason to one notice, read once', () => {
    rememberSessionEnd('sign_in_policy');
    expect(takeSessionEndNotice()).to.equal(
      'Your administrator now requires a second sign-in step. Sign in with your password to set it up.'
    );
    expect(takeSessionEndNotice()).to.equal('');
    rememberSessionEnd('sign_in_reset');
    expect(takeSessionEndNotice()).to.equal(
      'An administrator reset your sign-in. Sign in with your password and set it up again.'
    );
    rememberSessionEnd('<b>anything</b>');
    expect(takeSessionEndNotice()).to.equal('');
  });
});
