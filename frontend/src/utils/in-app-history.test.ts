import { expect } from '@open-wc/testing';

import {
  IN_APP_DEPTH_KEY,
  hasInAppHistory,
  historyStateForNavigation,
  inAppDepth,
} from './in-app-history';

describe('in-app history', () => {
  const startUrl = window.location.pathname + window.location.search;

  afterEach(() => {
    window.history.replaceState(null, '', startUrl);
  });

  it('reads the depth stamped on a history state', () => {
    expect(inAppDepth(null)).to.equal(0);
    expect(inAppDepth({})).to.equal(0);
    expect(inAppDepth({ [IN_APP_DEPTH_KEY]: 'x' })).to.equal(0);
    expect(inAppDepth({ [IN_APP_DEPTH_KEY]: -2 })).to.equal(0);
    expect(inAppDepth({ [IN_APP_DEPTH_KEY]: 3 })).to.equal(3);
  });

  it('deepens on push and keeps the depth on replace', () => {
    expect(historyStateForNavigation('push', null)).to.deep.equal({
      [IN_APP_DEPTH_KEY]: 1,
    });
    expect(
      historyStateForNavigation('push', { [IN_APP_DEPTH_KEY]: 1 })
    ).to.deep.equal({ [IN_APP_DEPTH_KEY]: 2 });
    expect(
      historyStateForNavigation('replace', { [IN_APP_DEPTH_KEY]: 2 })
    ).to.deep.equal({ [IN_APP_DEPTH_KEY]: 2 });
    expect(historyStateForNavigation('replace', null)).to.equal(null);
  });

  it('has no in-app history on a page loaded directly', () => {
    window.history.replaceState(null, '', '/console/agents/agent-1');
    expect(hasInAppHistory()).to.equal(false);
  });

  it('has in-app history after the router pushed an entry', () => {
    window.history.pushState(
      historyStateForNavigation('push'),
      '',
      '/console/agents/agent-1'
    );
    expect(hasInAppHistory()).to.equal(true);
  });
});
