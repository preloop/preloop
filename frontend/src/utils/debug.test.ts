import { expect } from '@open-wc/testing';
import sinon from 'sinon';

import { debugLog } from './debug';

describe('debugLog', () => {
  it('does not write to the console outside a dev build', () => {
    const log = sinon.spy(console, 'log');
    try {
      debugLog('tracker token', 'secret-token-value');
      expect(log.called, 'production builds drop debugLog').to.equal(false);
    } finally {
      log.restore();
    }
  });
});
