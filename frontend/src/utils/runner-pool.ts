export const SERVER_RUNNER_POOL = 'server';
export const AUTO_RUNNER_POOL = 'auto';

export const ACCOUNT_AUTO_RESOLVED =
  'Auto (private runners first, then Preloop hosted)';
export const ACCOUNT_HOSTED_RESOLVED = 'Preloop hosted only';
export const FLOW_AUTO_OVERRIDE_LABEL =
  'Auto: private runners first, then Preloop hosted';
export const ACCOUNT_AUTO_OPTION_LABEL =
  'Auto (default): private runners first, then Preloop hosted';
export const HOSTED_ONLY_LABEL = 'Preloop hosted only';
export const HOSTED_ONLY_EXHAUSTED_LABEL =
  'Preloop hosted only (no hosted minutes left)';

export interface RunnerPoolOption {
  value: string;
  label: string;
  disabled?: boolean;
}

export interface RunnerPoolGroup {
  label?: string;
  options: RunnerPoolOption[];
}

export interface RunnerPoolSource {
  id?: string | null;
  name?: string | null;
  labels?: string[] | null;
  status?: string | null;
}

export interface BuildRunnerPoolGroupsArgs {
  runners: RunnerPoolSource[];
  context: 'flow' | 'account';
  accountPool?: string | null;
  current?: string | null;
  hostedMinutesLeft?: number | null;
}

function normalizedPool(value?: string | null): string {
  return (value ?? '').trim();
}

function poolKey(value?: string | null): string {
  return normalizedPool(value).toLowerCase();
}

export function isOnlineRunner(runner: RunnerPoolSource): boolean {
  const status = (runner.status || '').toLowerCase();
  return status === 'online' || status === 'busy';
}

export function isSelectableToken(value: string): boolean {
  return Boolean(value) && !/\s/.test(value);
}

export function isAccountDefaultAuto(accountPool?: string | null): boolean {
  const key = poolKey(accountPool);
  return key === '' || key === AUTO_RUNNER_POOL;
}

function isAutoToken(value?: string | null): boolean {
  return poolKey(value) === AUTO_RUNNER_POOL;
}

function isServerToken(value?: string | null): boolean {
  return poolKey(value) === SERVER_RUNNER_POOL;
}

function runnerName(runner: RunnerPoolSource): string {
  return (runner.name || '').trim();
}

function findRunnerByToken(
  token: string,
  runners: RunnerPoolSource[]
): RunnerPoolSource | undefined {
  const key = token.toLowerCase();
  return runners.find((runner) => {
    const name = runnerName(runner).toLowerCase();
    const id = (runner.id || '').trim().toLowerCase();
    return name === key || (id !== '' && id === key);
  });
}

function hasLabel(token: string, runners: RunnerPoolSource[]): boolean {
  const key = token.toLowerCase();
  return runners.some((runner) =>
    (runner.labels || []).some(
      (raw) => String(raw).trim().toLowerCase() === key
    )
  );
}

function classifyToken(
  token: string,
  runners: RunnerPoolSource[]
): 'auto' | 'server' | 'label' | 'name' {
  if (isAutoToken(token) || token === '') {
    return 'auto';
  }
  if (isServerToken(token)) {
    return 'server';
  }
  if (findRunnerByToken(token, runners)) {
    return 'name';
  }
  if (hasLabel(token, runners)) {
    return 'label';
  }
  return 'name';
}

export function resolveAccountPoolLabel(
  accountPool?: string | null,
  runners: RunnerPoolSource[] = []
): string {
  const token = normalizedPool(accountPool);
  if (isAccountDefaultAuto(token)) {
    return ACCOUNT_AUTO_RESOLVED;
  }
  if (isServerToken(token)) {
    return ACCOUNT_HOSTED_RESOLVED;
  }
  const runner = findRunnerByToken(token, runners);
  if (runner) {
    return `runner ${runnerName(runner) || token}`;
  }
  if (hasLabel(token, runners)) {
    return `runners labelled ${token}`;
  }
  return `runner ${token}`;
}

function hostedOption(hostedMinutesLeft?: number | null): RunnerPoolOption {
  const exhausted = hostedMinutesLeft === 0;
  return {
    value: SERVER_RUNNER_POOL,
    label: exhausted ? HOSTED_ONLY_EXHAUSTED_LABEL : HOSTED_ONLY_LABEL,
    disabled: exhausted || undefined,
  };
}

function collectLabelRows(runners: RunnerPoolSource[]): RunnerPoolOption[] {
  const counts = new Map<string, { label: string; online: number }>();
  for (const runner of runners) {
    for (const raw of runner.labels || []) {
      const label = String(raw).trim();
      if (!isSelectableToken(label)) {
        continue;
      }
      const key = label.toLowerCase();
      const current = counts.get(key) || { label, online: 0 };
      if (isOnlineRunner(runner)) {
        current.online += 1;
      }
      counts.set(key, current);
    }
  }
  return [...counts.values()]
    .sort((left, right) => {
      if (right.online !== left.online) {
        return right.online - left.online;
      }
      return left.label.localeCompare(right.label);
    })
    .map((entry) => ({
      value: entry.label,
      label: `Runners labelled ${entry.label} (${entry.online} online)`,
    }));
}

function collectRunnerRows(runners: RunnerPoolSource[]): RunnerPoolOption[] {
  const seen = new Set<string>();
  const rows: { runner: RunnerPoolSource; name: string }[] = [];
  for (const runner of runners) {
    const name = runnerName(runner);
    if (!isSelectableToken(name)) {
      continue;
    }
    const key = name.toLowerCase();
    if (seen.has(key)) {
      continue;
    }
    seen.add(key);
    rows.push({ runner, name });
  }
  rows.sort((left, right) => {
    const leftOnline = isOnlineRunner(left.runner) ? 0 : 1;
    const rightOnline = isOnlineRunner(right.runner) ? 0 : 1;
    if (leftOnline !== rightOnline) {
      return leftOnline - rightOnline;
    }
    return left.name.localeCompare(right.name);
  });
  return rows.map(({ runner, name }) => ({
    value: name,
    label: `${name} (${isOnlineRunner(runner) ? 'online' : 'offline'})`,
  }));
}

function optionValues(groups: RunnerPoolGroup[]): Set<string> {
  const values = new Set<string>(['', AUTO_RUNNER_POOL, SERVER_RUNNER_POOL]);
  for (const group of groups) {
    for (const option of group.options) {
      values.add(option.value);
    }
  }
  return values;
}

function isKnownCurrent(current: string, values: Set<string>): boolean {
  if (values.has(current)) {
    return true;
  }
  const key = current.toLowerCase();
  for (const value of values) {
    if (value.toLowerCase() === key) {
      return true;
    }
  }
  return false;
}

export function buildRunnerPoolGroups(
  args: BuildRunnerPoolGroupsArgs
): RunnerPoolGroup[] {
  const runners = args.runners || [];
  const groups: RunnerPoolGroup[] = [];
  const primary: RunnerPoolOption[] = [];

  if (args.context === 'flow') {
    primary.push({
      value: '',
      label: `Account default: ${resolveAccountPoolLabel(args.accountPool, runners)}`,
    });
    if (!isAccountDefaultAuto(args.accountPool)) {
      primary.push({
        value: AUTO_RUNNER_POOL,
        label: FLOW_AUTO_OVERRIDE_LABEL,
      });
    }
  } else {
    primary.push({
      value: AUTO_RUNNER_POOL,
      label: ACCOUNT_AUTO_OPTION_LABEL,
    });
  }
  primary.push(hostedOption(args.hostedMinutesLeft));
  groups.push({ options: primary });

  const labelRows = collectLabelRows(runners);
  if (labelRows.length > 0) {
    groups.push({ label: 'Runners by label', options: labelRows });
  }

  const runnerRows = collectRunnerRows(runners);
  if (runnerRows.length > 0) {
    groups.push({ label: 'Specific runner', options: runnerRows });
  }

  const current = normalizedPool(args.current);
  if (
    current &&
    isSelectableToken(current) &&
    !isKnownCurrent(current, optionValues(groups))
  ) {
    groups.push({
      options: [{ value: current, label: `${current} (not registered)` }],
    });
  }

  return groups;
}

function onlinePrivateNames(runners: RunnerPoolSource[]): string[] {
  return runners
    .filter(isOnlineRunner)
    .map(runnerName)
    .filter(Boolean)
    .sort((left, right) => left.localeCompare(right));
}

function onlineLabelCount(token: string, runners: RunnerPoolSource[]): number {
  const key = token.toLowerCase();
  return runners.filter(
    (runner) =>
      isOnlineRunner(runner) &&
      (runner.labels || []).some(
        (raw) => String(raw).trim().toLowerCase() === key
      )
  ).length;
}

function withInherited(text: string, inherited: boolean): string {
  if (!inherited) {
    return text;
  }
  const period = text.indexOf('.');
  if (period === -1) {
    return `${text} (account default)`;
  }
  return `${text.slice(0, period + 1)} (account default)${text.slice(period + 1)}`;
}

function withHostedMinutes(
  text: string,
  hostedMinutesLeft?: number | null,
  hostedCapable?: boolean
): string {
  if (!hostedCapable || typeof hostedMinutesLeft !== 'number') {
    return text;
  }
  if (
    hostedMinutesLeft === 0 &&
    text.includes('No hosted minutes left, so the run queues if none is free.')
  ) {
    return text;
  }
  return `${text} Hosted minutes left: ${hostedMinutesLeft}.`;
}

export function describeNextRunnerPool(args: {
  flowPool?: string | null;
  accountPool?: string | null;
  runners: RunnerPoolSource[];
  hostedMinutesLeft?: number | null;
}): string {
  const runners = args.runners || [];
  const flow = normalizedPool(args.flowPool);
  const inherited = flow === '';
  const effective = inherited
    ? normalizedPool(args.accountPool) || AUTO_RUNNER_POOL
    : flow;
  const kind = classifyToken(effective, runners);
  const hostedCapable = kind === 'auto' || kind === 'server';
  const exhaustedAuto =
    kind === 'auto' && args.hostedMinutesLeft === 0
      ? 'No hosted minutes left, so the run queues if none is free.'
      : 'Falls back to Preloop hosted when none is free.';

  let text = '';
  if (kind === 'server') {
    text = 'Next run: Preloop hosted.';
  } else if (kind === 'auto') {
    const names = onlinePrivateNames(runners);
    if (names.length > 0) {
      text = `Next run: a private runner (${names.join(', ')} online). ${exhaustedAuto}`;
    } else {
      text = 'Next run: Preloop hosted. No private runner is online.';
    }
  } else if (kind === 'label') {
    const online = onlineLabelCount(effective, runners);
    if (online > 0) {
      text = `Next run: a runner labelled ${effective} (${online} online).`;
    } else {
      text =
        `Next run: a runner labelled ${effective}. None is online, so the ` +
        'run queues for up to 15 minutes, then fails.';
    }
  } else {
    const runner = findRunnerByToken(effective, runners);
    const display = runner ? runnerName(runner) || effective : effective;
    if (runner && isOnlineRunner(runner)) {
      text = `Next run: ${display}.`;
    } else {
      text =
        `Next run: ${display}. It is offline, so the run queues for up to ` +
        '15 minutes, then fails.';
    }
  }

  return withHostedMinutes(
    withInherited(text, inherited && kind !== 'auto'),
    args.hostedMinutesLeft,
    hostedCapable
  );
}
