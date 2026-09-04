export const SERVER_RUNNER_POOL = 'server';
export const AUTO_RUNNER_POOL = 'auto';

export const ANY_ONLINE_RUNNER_LABEL = 'Any online private runner (default)';
export const PRELOOP_HOSTED_LABEL = 'Preloop hosted';

export interface RunnerPoolOption {
  value: string;
  label: string;
}

export interface RunnerPoolSource {
  name?: string | null;
  labels?: string[] | null;
  status?: string | null;
}

export function isOnlineRunner(runner: RunnerPoolSource): boolean {
  const status = (runner.status || '').toLowerCase();
  return status === 'online' || status === 'busy';
}

export function buildRunnerPoolOptions(
  runners: RunnerPoolSource[]
): RunnerPoolOption[] {
  const options: RunnerPoolOption[] = [
    { value: '', label: ANY_ONLINE_RUNNER_LABEL },
    { value: SERVER_RUNNER_POOL, label: PRELOOP_HOSTED_LABEL },
  ];
  const seen = new Set<string>(['', SERVER_RUNNER_POOL, AUTO_RUNNER_POOL]);
  for (const runner of runners) {
    if (!isOnlineRunner(runner)) {
      continue;
    }
    const name = (runner.name || '').trim();
    if (name && !seen.has(name)) {
      seen.add(name);
      options.push({ value: name, label: name });
    }
    for (const raw of runner.labels || []) {
      const label = String(raw).trim();
      if (label && !seen.has(label)) {
        seen.add(label);
        options.push({ value: label, label: `Label: ${label}` });
      }
    }
  }
  return options;
}

export function describeNextRunnerPool(args: {
  flowPool?: string | null;
  accountPool?: string | null;
  runners: RunnerPoolSource[];
}): string {
  const onlineNames = args.runners
    .filter(isOnlineRunner)
    .map((runner) => (runner.name || '').trim())
    .filter(Boolean);
  const explicit = (args.flowPool || '').trim();
  if (explicit.toLowerCase() === SERVER_RUNNER_POOL) {
    return 'Next execution will use Preloop hosted.';
  }
  if (explicit && explicit.toLowerCase() !== AUTO_RUNNER_POOL) {
    return `Next execution will use ${explicit}.`;
  }
  const account = (args.accountPool || '').trim();
  if (!explicit && account.toLowerCase() === SERVER_RUNNER_POOL) {
    return 'Next execution will use Preloop hosted (account default).';
  }
  if (!explicit && account && account.toLowerCase() !== AUTO_RUNNER_POOL) {
    return `Next execution will use ${account} (account default).`;
  }
  if (onlineNames.length > 0) {
    return `Next execution will use any online private runner (currently ${onlineNames.join(', ')}).`;
  }
  return 'Next execution will use Preloop hosted. No private runner is online.';
}
