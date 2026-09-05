import { expect } from '@open-wc/testing';

import {
  hostnameFromUrl,
  parseAllowedHosts,
  resolveAllowedHosts,
} from './vite-allowed-hosts';

describe('parseAllowedHosts', () => {
  it('returns undefined for empty input', () => {
    expect(parseAllowedHosts(undefined)).to.equal(undefined);
    expect(parseAllowedHosts('')).to.equal(undefined);
    expect(parseAllowedHosts('   ')).to.equal(undefined);
  });

  it('treats true/all as allow any host', () => {
    expect(parseAllowedHosts('true')).to.equal(true);
    expect(parseAllowedHosts('all')).to.equal(true);
  });

  it('splits a comma-separated list', () => {
    expect(parseAllowedHosts('tuvok.preloop.ai, other.example')).to.deep.equal([
      'tuvok.preloop.ai',
      'other.example',
    ]);
  });
});

describe('hostnameFromUrl', () => {
  it('ignores localhost and loopback', () => {
    expect(hostnameFromUrl('http://localhost:8000')).to.equal(undefined);
    expect(hostnameFromUrl('http://127.0.0.1:8000')).to.equal(undefined);
  });

  it('returns a public hostname', () => {
    expect(hostnameFromUrl('https://tuvok.preloop.ai/api')).to.equal(
      'tuvok.preloop.ai'
    );
  });

  it('returns undefined for invalid URLs', () => {
    expect(hostnameFromUrl('not a url')).to.equal(undefined);
  });
});

describe('resolveAllowedHosts', () => {
  it('prefers an explicit allow-all flag', () => {
    expect(resolveAllowedHosts({ allowedHosts: 'all' })).to.equal(true);
  });

  it('unions explicit hosts with HMR and API hostnames', () => {
    expect(
      resolveAllowedHosts({
        allowedHosts: 'console.internal',
        hmrHost: 'tuvok.preloop.ai',
        apiUrl: 'https://tuvok.preloop.ai',
      })
    ).to.have.members(['console.internal', 'tuvok.preloop.ai']);
  });

  it('allows any host for Docker Compose when nothing else is set', () => {
    expect(resolveAllowedHosts({ apiProxyTarget: 'http://api:8000' })).to.equal(
      true
    );
  });

  it('keeps an explicit list even in Docker Compose', () => {
    expect(
      resolveAllowedHosts({
        allowedHosts: 'tuvok.preloop.ai',
        apiProxyTarget: 'http://api:8000',
      })
    ).to.deep.equal(['tuvok.preloop.ai']);
  });

  it('returns undefined for a local laptop Vite session', () => {
    expect(
      resolveAllowedHosts({
        apiUrl: 'http://localhost:8000',
        apiProxyTarget: 'http://127.0.0.1:8000',
      })
    ).to.equal(undefined);
  });
});
