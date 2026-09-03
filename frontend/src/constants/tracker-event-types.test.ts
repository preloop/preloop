import { expect } from '@open-wc/testing';

import {
  getTrackerEventOptions,
  GITLAB_TRACKER_EVENTS,
} from './tracker-event-types';

describe('getTrackerEventOptions', () => {
  it('returns full GitLab event list including merge request updated', () => {
    const events = getTrackerEventOptions('gitlab');
    expect(events).to.deep.equal(GITLAB_TRACKER_EVENTS);
    expect(events.some((event) => event.value === 'merge_request_updated')).to
      .be.true;
    expect(events.some((event) => event.value === 'issue_updated')).to.be.true;
    expect(events.some((event) => event.value === 'deployment')).to.be.true;
  });
});
