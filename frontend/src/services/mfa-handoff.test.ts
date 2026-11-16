import { expect } from '@open-wc/testing';
import {
  enrollmentOnly,
  safeCliRedirect,
  takeRedirectMfaChallenge,
} from './mfa';

describe('MFA browser handoff', () => {
  // protects=only loopback receives completed CLI credentials; fails_when=remote URL accepted; no_claim=server validates signed state separately.
  it('restricts callbacks to literal loopback addresses', () => {
    for (const url of [
      'http://127.0.0.1:4321/callback',
      'http://localhost:4321/callback',
      'http://[::1]:4321/callback',
    ])
      expect(safeCliRedirect(url)).to.equal(url);
    for (const url of [
      'https://evil.example/callback',
      'https://localhost.evil.example',
      'javascript:alert(1)',
      '//localhost/callback',
      'http://name@localhost/callback',
      'http://localhost\\@evil.example',
    ])
      expect(safeCliRedirect(url)).to.equal(null);
  });

  // protects=challenge is not left in browser address/storage; fails_when=handoff can be read twice; no_claim=backend signature validates it.
  it('consumes a redirect challenge once', () => {
    const previous = location.href;
    try {
      history.replaceState(
        {},
        '',
        '/login#mfa_challenge=synthetic&cli_redirect_uri=http%3A%2F%2Flocalhost%3A4321%2Fcallback'
      );
      expect(takeRedirectMfaChallenge()).to.deep.equal({
        challenge: 'synthetic',
        cliRedirect: 'http://localhost:4321/callback',
      });
      expect(location.hash).to.equal('');
      expect(takeRedirectMfaChallenge()).to.equal(null);
    } finally {
      history.replaceState({}, '', previous);
    }
  });

  // protects=signed callback comparison keeps the original URI spelling;
  // fails_when=browser canonicalization changes the value sent to verification; no_claim=server validates the signature.
  it('preserves the exact callback hint for server comparison', () => {
    const previous = location.href;
    try {
      history.replaceState(
        {},
        '',
        '/login#mfa_challenge=synthetic&cli_redirect_uri=http%3A%2F%2Flocalhost%3A80'
      );
      expect(takeRedirectMfaChallenge()?.cliRedirect).to.equal(
        'http://localhost:80'
      );
    } finally {
      history.replaceState({}, '', previous);
    }
  });

  // protects=enrollment routing understands bounded boolean; fails_when=malformed token crashes; no_claim=this hint never authorizes API access.
  it('reads enrollment scope as a presentation hint', () => {
    expect(
      enrollmentOnly(
        'header.' + btoa(JSON.stringify({ enroll_only: true })) + '.signature'
      )
    ).to.equal(true);
    expect(
      enrollmentOnly(
        'header.' + btoa(JSON.stringify({ enroll_only: 'true' })) + '.signature'
      )
    ).to.equal(false);
    expect(enrollmentOnly('broken')).to.equal(false);
  });
});
