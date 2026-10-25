import { expect } from '@open-wc/testing';

import { apiKeyDisplayName, apiKeySearchQuery } from './api-key-name';

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

describe('apiKeySearchQuery', () => {
  it('maps the displayed flow key name to the stored one', () => {
    expect(apiKeySearchQuery('Flow run 9910dacd')).to.equal(
      'Flow Execution 9910dacd'
    );
    expect(apiKeySearchQuery('flow run')).to.equal('Flow Execution');
    expect(apiKeySearchQuery('Flow run 9910')).to.equal('Flow Execution 9910');
    expect(
      apiKeySearchQuery('Flow run 9910dacd-4297-4bb0-9f1e-0a1b2c3d4e5f')
    ).to.equal('Flow Execution 9910dacd-4297-4bb0-9f1e-0a1b2c3d4e5f');
  });

  it('passes other searches through', () => {
    expect(apiKeySearchQuery('deploy')).to.equal('deploy');
    expect(apiKeySearchQuery('Flow runner')).to.equal('Flow runner');
    expect(apiKeySearchQuery('Flow run helper')).to.equal('Flow run helper');
    expect(apiKeySearchQuery('Flow run deploy key')).to.equal(
      'Flow run deploy key'
    );
    expect(apiKeySearchQuery('Flow run 9910dacd notes')).to.equal(
      'Flow run 9910dacd notes'
    );
  });
});
