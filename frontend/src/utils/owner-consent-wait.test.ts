import { expect } from '@open-wc/testing';
import {
  WAITING_FOR_RUNNER_OWNER,
  isWaitingForRunnerOwner,
} from './execution-presentation';

describe('isWaitingForRunnerOwner', () => {
  it('reads a queued run held for the owner as a wait', () => {
    expect(
      isWaitingForRunnerOwner({
        status: 'PENDING',
        error_message: WAITING_FOR_RUNNER_OWNER,
      })
    ).to.equal(true);
  });

  it('leaves finished runs and other messages alone', () => {
    expect(
      isWaitingForRunnerOwner({
        status: 'FAILED',
        error_message: WAITING_FOR_RUNNER_OWNER,
      })
    ).to.equal(false);
    expect(
      isWaitingForRunnerOwner({ status: 'PENDING', error_message: 'Other' })
    ).to.equal(false);
  });
});
