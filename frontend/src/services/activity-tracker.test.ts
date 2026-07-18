import { expect } from '@open-wc/testing';

import { ActivityTracker } from './activity-tracker';

const DISABLE_TELEMETRY_KEY = 'preloop:disableTelemetry';

describe('activity-tracker telemetry opt-out', () => {
  beforeEach(() => {
    localStorage.removeItem(DISABLE_TELEMETRY_KEY);
  });

  afterEach(() => {
    localStorage.removeItem(DISABLE_TELEMETRY_KEY);
  });

  it('tracks by default', () => {
    expect(new ActivityTracker().isEnabled()).to.be.true;
  });

  it('opts out when the disable flag is set', () => {
    // Browser-side counterpart to PRELOOP_DISABLE_TELEMETRY: internal
    // browsing must be excludable from the admin funnel.
    localStorage.setItem(DISABLE_TELEMETRY_KEY, 'true');
    expect(new ActivityTracker().isEnabled()).to.be.false;
  });

  it('only honours an exact "true" value', () => {
    localStorage.setItem(DISABLE_TELEMETRY_KEY, 'false');
    expect(new ActivityTracker().isEnabled()).to.be.true;
  });
});
