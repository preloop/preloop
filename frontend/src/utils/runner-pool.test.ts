import { expect } from '@open-wc/testing';

import {
  ANY_ONLINE_RUNNER_LABEL,
  PRELOOP_HOSTED_LABEL,
  SERVER_RUNNER_POOL,
  buildRunnerPoolOptions,
  describeNextRunnerPool,
} from './runner-pool';

const OFFICE = {
  name: 'office-mac',
  labels: ['local', 'gpu'],
  status: 'online',
};

describe('runner-pool options', () => {
  it('lists the default and hosted sentinels plus online names and labels', () => {
    const options = buildRunnerPoolOptions([
      OFFICE,
      { name: 'offline-box', labels: ['spare'], status: 'offline' },
    ]);
    expect(options[0]).to.deep.equal({
      value: '',
      label: ANY_ONLINE_RUNNER_LABEL,
    });
    expect(options[1]).to.deep.equal({
      value: SERVER_RUNNER_POOL,
      label: PRELOOP_HOSTED_LABEL,
    });
    expect(options.map((option) => option.value)).to.include.members([
      'office-mac',
      'local',
      'gpu',
    ]);
    expect(options.map((option) => option.value)).to.not.include('offline-box');
    expect(options.map((option) => option.value)).to.not.include('spare');
  });
});

describe('runner-pool next-execution hint', () => {
  it('prefers an explicit flow pool', () => {
    expect(
      describeNextRunnerPool({
        flowPool: 'gpu',
        accountPool: 'server',
        runners: [OFFICE],
      })
    ).to.equal('Next execution will use gpu.');
  });

  it('uses the hosted sentinel on the flow', () => {
    expect(
      describeNextRunnerPool({
        flowPool: 'server',
        accountPool: 'office-mac',
        runners: [OFFICE],
      })
    ).to.equal('Next execution will use Preloop hosted.');
  });

  it('falls back to the account default', () => {
    expect(
      describeNextRunnerPool({
        flowPool: null,
        accountPool: 'office-mac',
        runners: [OFFICE],
      })
    ).to.equal('Next execution will use office-mac (account default).');
  });

  it('uses any online private runner when nothing is pinned', () => {
    expect(
      describeNextRunnerPool({
        flowPool: '',
        accountPool: null,
        runners: [OFFICE],
      })
    ).to.equal(
      'Next execution will use any online private runner (currently office-mac).'
    );
  });

  it('uses hosted when no private runner is online', () => {
    expect(
      describeNextRunnerPool({
        flowPool: null,
        accountPool: null,
        runners: [{ name: 'office-mac', status: 'offline' }],
      })
    ).to.equal(
      'Next execution will use Preloop hosted. No private runner is online.'
    );
  });

  it('does not put an em dash in user-facing hint text', () => {
    const hint = describeNextRunnerPool({
      flowPool: null,
      accountPool: 'server',
      runners: [OFFICE],
    });
    expect(hint).to.equal(
      'Next execution will use Preloop hosted (account default).'
    );
    expect(hint).to.not.contain('—');
  });
});
