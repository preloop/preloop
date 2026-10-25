import { expect } from '@open-wc/testing';

import { apiKeyDisplayName } from './api-key-name';

describe('apiKeyDisplayName', () => {
  it('names auto-created flow keys by run with a short id', () => {
    expect(
      apiKeyDisplayName('Flow Execution 9910dacd-4297-4bb0-9f1e-0a1b2c3d4e5f')
    ).to.deep.equal({
      name: 'Flow run 9910dacd',
      fullId: '9910dacd-4297-4bb0-9f1e-0a1b2c3d4e5f',
    });
  });

  it('keeps names people chose', () => {
    expect(apiKeyDisplayName('CI deploy key')).to.deep.equal({
      name: 'CI deploy key',
      fullId: null,
    });
    expect(apiKeyDisplayName('Flow Execution notes')).to.deep.equal({
      name: 'Flow Execution notes',
      fullId: null,
    });
  });
});
