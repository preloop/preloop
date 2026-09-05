import { expect } from '@open-wc/testing';

import {
  ACCOUNT_AUTO_OPTION_LABEL,
  ACCOUNT_AUTO_RESOLVED,
  ACCOUNT_HOSTED_RESOLVED,
  AUTO_RUNNER_POOL,
  FLOW_AUTO_OVERRIDE_LABEL,
  HOSTED_ONLY_EXHAUSTED_LABEL,
  HOSTED_ONLY_LABEL,
  SERVER_RUNNER_POOL,
  buildRunnerPoolGroups,
  describeNextRunnerPool,
  isSelectableToken,
  resolveAccountPoolLabel,
} from './runner-pool';

const OFFICE = {
  name: 'office-mac',
  labels: ['local', 'gpu'],
  status: 'online',
};

const LAB = {
  name: 'lab-1',
  labels: ['gpu'],
  status: 'online',
};

const OFFLINE = {
  name: 'lab-offline',
  labels: ['spare'],
  status: 'offline',
};

function flattenValues(
  groups: ReturnType<typeof buildRunnerPoolGroups>
): string[] {
  return groups.flatMap((group) => group.options.map((option) => option.value));
}

function flattenLabels(
  groups: ReturnType<typeof buildRunnerPoolGroups>
): string[] {
  return groups.flatMap((group) => group.options.map((option) => option.label));
}

describe('runner-pool options', () => {
  it('flow context, account default auto: first row is inherit and has no separate auto row', () => {
    const groups = buildRunnerPoolGroups({
      runners: [OFFICE],
      context: 'flow',
      accountPool: null,
    });
    const first = groups[0].options[0];
    expect(first).to.deep.equal({
      value: '',
      label: `Account default: ${ACCOUNT_AUTO_RESOLVED}`,
    });
    expect(flattenValues(groups)).to.not.include(AUTO_RUNNER_POOL);
    expect(flattenLabels(groups)).to.not.include(FLOW_AUTO_OVERRIDE_LABEL);
  });

  it('flow context, account default server: inherit hosted and offer explicit auto', () => {
    const groups = buildRunnerPoolGroups({
      runners: [OFFICE],
      context: 'flow',
      accountPool: SERVER_RUNNER_POOL,
    });
    const primary = groups[0].options;
    expect(primary[0]).to.deep.equal({
      value: '',
      label: `Account default: ${ACCOUNT_HOSTED_RESOLVED}`,
    });
    expect(primary[1]).to.deep.equal({
      value: AUTO_RUNNER_POOL,
      label: FLOW_AUTO_OVERRIDE_LABEL,
    });
    expect(primary[2].value).to.equal(SERVER_RUNNER_POOL);
    expect(primary[2].label).to.equal(HOSTED_ONLY_LABEL);
  });

  it('account context: first row is auto default and has no empty inherit row', () => {
    const groups = buildRunnerPoolGroups({
      runners: [OFFICE],
      context: 'account',
      accountPool: null,
    });
    expect(groups[0].options[0]).to.deep.equal({
      value: AUTO_RUNNER_POOL,
      label: ACCOUNT_AUTO_OPTION_LABEL,
    });
    expect(flattenValues(groups)).to.not.include('');
  });

  it('includes labels from offline runners and sorts by online count then name', () => {
    const groups = buildRunnerPoolGroups({
      runners: [OFFLINE, OFFICE, LAB],
      context: 'flow',
      accountPool: null,
    });
    const labelGroup = groups.find(
      (group) => group.label === 'Runners by label'
    );
    expect(labelGroup).to.exist;
    expect(labelGroup?.options.map((option) => option.value)).to.deep.equal([
      'gpu',
      'local',
      'spare',
    ]);
    expect(labelGroup?.options.map((option) => option.label)).to.deep.equal([
      'Runners labelled gpu (2 online)',
      'Runners labelled local (1 online)',
      'Runners labelled spare (0 online)',
    ]);
  });

  it('excludes labels with whitespace from selectable options', () => {
    const groups = buildRunnerPoolGroups({
      runners: [
        { name: 'office-mac', labels: ['office gpu', 'gpu'], status: 'online' },
      ],
      context: 'flow',
    });
    const values = flattenValues(groups);
    expect(values).to.include('gpu');
    expect(values).to.not.include('office gpu');
    expect(isSelectableToken('office gpu')).to.equal(false);
    expect(isSelectableToken('gpu')).to.equal(true);
  });

  it('lists specific runners online first with a status suffix', () => {
    const groups = buildRunnerPoolGroups({
      runners: [OFFLINE, OFFICE, { name: 'zebra', status: 'offline' }],
      context: 'flow',
    });
    const runnerGroup = groups.find(
      (group) => group.label === 'Specific runner'
    );
    expect(runnerGroup).to.exist;
    expect(runnerGroup?.options.map((option) => option.label)).to.deep.equal([
      'office-mac (online)',
      'lab-offline (offline)',
      'zebra (offline)',
    ]);
  });

  it('treats a stored runner id as registered and selects that runner', () => {
    const groups = buildRunnerPoolGroups({
      runners: [{ ...OFFICE, id: 'runner-office-1' }],
      context: 'flow',
      current: 'runner-office-1',
    });
    expect(flattenValues(groups)).to.include('runner-office-1');
    expect(
      flattenLabels(groups).some((label) => label.includes('(not registered)'))
    ).to.equal(false);
    const runnerGroup = groups.find(
      (group) => group.label === 'Specific runner'
    );
    expect(runnerGroup?.options[0]).to.deep.equal({
      value: 'runner-office-1',
      label: 'office-mac (online)',
    });
  });

  it('appends the current unknown value as not registered', () => {
    const groups = buildRunnerPoolGroups({
      runners: [OFFICE],
      context: 'flow',
      current: 'legacy-pin',
    });
    const last = groups[groups.length - 1].options[0];
    expect(last).to.deep.equal({
      value: 'legacy-pin',
      label: 'legacy-pin (not registered)',
    });
  });

  it('skips the not registered row when the current value has whitespace', () => {
    const groups = buildRunnerPoolGroups({
      runners: [OFFICE],
      context: 'flow',
      current: 'office gpu',
    });
    const labels = flattenLabels(groups);
    expect(labels.some((label) => label.includes('(not registered)'))).to.equal(
      false
    );
    expect(flattenValues(groups)).to.not.include('office gpu');
  });

  it('disables the hosted row when hosted minutes are exhausted', () => {
    const groups = buildRunnerPoolGroups({
      runners: [OFFICE],
      context: 'flow',
      hostedMinutesLeft: 0,
    });
    const hosted = groups[0].options.find(
      (option) => option.value === SERVER_RUNNER_POOL
    );
    expect(hosted).to.deep.equal({
      value: SERVER_RUNNER_POOL,
      label: HOSTED_ONLY_EXHAUSTED_LABEL,
      disabled: true,
    });
  });
});

describe('runner-pool next-run hint', () => {
  it('describes an explicit hosted flow', () => {
    expect(
      describeNextRunnerPool({
        flowPool: 'server',
        accountPool: 'office-mac',
        runners: [OFFICE],
      })
    ).to.equal('Next run: Preloop hosted.');
  });

  it('describes auto when private runners are online', () => {
    expect(
      describeNextRunnerPool({
        flowPool: 'auto',
        accountPool: 'server',
        runners: [OFFICE, LAB],
      })
    ).to.equal(
      'Next run: a private runner (lab-1, office-mac online). Falls back to Preloop hosted when none is free.'
    );
  });

  it('describes auto when no private runner is online', () => {
    expect(
      describeNextRunnerPool({
        flowPool: '',
        accountPool: null,
        runners: [OFFLINE],
      })
    ).to.equal('Next run: Preloop hosted. No private runner is online.');
  });

  it('describes a labelled pool with nothing online', () => {
    expect(
      describeNextRunnerPool({
        flowPool: 'gpu',
        accountPool: null,
        runners: [OFFLINE, { name: 'box', labels: ['gpu'], status: 'offline' }],
      })
    ).to.equal(
      'Next run: a runner labelled gpu. None is online, so the run queues for up to 15 minutes, then fails.'
    );
  });

  it('describes a named runner that is offline', () => {
    expect(
      describeNextRunnerPool({
        flowPool: 'office-mac',
        accountPool: null,
        runners: [{ name: 'office-mac', status: 'offline' }],
      })
    ).to.equal(
      'Next run: office-mac. It is offline, so the run queues for up to 15 minutes, then fails.'
    );
  });

  it('does not put an em dash in any produced label or hint', () => {
    const groups = buildRunnerPoolGroups({
      runners: [OFFICE, LAB, OFFLINE],
      context: 'flow',
      accountPool: 'server',
      current: 'missing-pin',
      hostedMinutesLeft: 0,
    });
    const hints = [
      describeNextRunnerPool({
        flowPool: null,
        accountPool: 'server',
        runners: [OFFICE],
        hostedMinutesLeft: 340,
      }),
      describeNextRunnerPool({
        flowPool: 'auto',
        accountPool: null,
        runners: [OFFICE, LAB],
        hostedMinutesLeft: 0,
      }),
      describeNextRunnerPool({
        flowPool: 'gpu',
        accountPool: null,
        runners: [OFFICE],
      }),
      describeNextRunnerPool({
        flowPool: null,
        accountPool: 'office-mac',
        runners: [OFFICE],
      }),
      resolveAccountPoolLabel('gpu', [OFFICE]),
    ];
    const produced = [...flattenLabels(groups), ...hints];
    for (const text of produced) {
      expect(text, text).to.not.contain('\u2014');
    }
    expect(hints[0]).to.equal(
      'Next run: Preloop hosted. (account default) Hosted minutes left: 340.'
    );
    expect(hints[1]).to.equal(
      'Next run: a private runner (lab-1, office-mac online). No hosted minutes left, so the run queues if none is free.'
    );
  });

  it('does not restate hosted minutes left when the exhausted clause is already present', () => {
    expect(
      describeNextRunnerPool({
        flowPool: 'auto',
        accountPool: null,
        runners: [OFFICE, LAB],
        hostedMinutesLeft: 0,
      })
    ).to.equal(
      'Next run: a private runner (lab-1, office-mac online). No hosted minutes left, so the run queues if none is free.'
    );
  });

  it('does not claim the next run is hosted when hosted minutes are exhausted', () => {
    expect(
      describeNextRunnerPool({
        flowPool: 'server',
        accountPool: null,
        runners: [OFFICE],
        hostedMinutesLeft: 0,
      })
    ).to.equal('Next run: Preloop hosted, but no hosted minutes are left.');
    expect(
      describeNextRunnerPool({
        flowPool: 'auto',
        accountPool: null,
        runners: [OFFLINE],
        hostedMinutesLeft: 0,
      })
    ).to.equal(
      'Next run: Preloop hosted, but no hosted minutes are left. No private runner is online.'
    );
  });
});
