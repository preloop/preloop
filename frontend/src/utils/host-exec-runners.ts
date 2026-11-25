/**
 * Which runners can run a host CLI (Cursor, Copilot, ...) for a flow, read
 * from what each runner advertises: its host execution profiles and its
 * harness inventory. The flow form uses this to offer "Cursor on my runner"
 * without asking the user for a profile name.
 */
import type { RunnerRecord } from '../api';
import { isOnlineRunner, runnerSelectionKind } from './runner-pool';

export interface HostExecRunnerChoice {
  runner: RunnerRecord;
  /** Runner pool token that targets exactly this runner. */
  token: string;
  /** Host execution profile on that runner that runs the harness. */
  profile: string;
  online: boolean;
}

/** A runner whose inventory lists the harness but cannot run it yet. */
export interface HostExecRunnerBlocker {
  runner: RunnerRecord;
  reason: 'signed_out' | 'disabled' | 'no_profile';
}

function profilesFor(runner: RunnerRecord, harness: string): string[] {
  const names: string[] = [];
  for (const item of runner.capabilities?.host_exec_profiles || []) {
    const name = (item?.name || '').trim();
    const caps = Array.isArray(item?.capabilities) ? item.capabilities : [];
    if (name && caps.includes(harness)) {
      names.push(name);
    }
  }
  return names;
}

function inventoryEntry(runner: RunnerRecord, harness: string) {
  return (runner.harness_inventory?.entries || []).find(
    (entry) => entry.harness === harness
  );
}

/** Use the immutable ID: names can collide with labels or reserved pools. */
export function runnerToken(runner: RunnerRecord): string {
  return runner.id;
}

/**
 * Runners that advertise a host profile for the harness, online first.
 *
 * The profile the inventory generated for the harness is preferred, since
 * that is the one `preloop runner` wrote for the signed-in CLI.
 */
export function hostExecRunnerChoices(
  runners: RunnerRecord[],
  harness: string
): HostExecRunnerChoice[] {
  const choices: HostExecRunnerChoice[] = [];
  for (const runner of runners) {
    const entry = inventoryEntry(runner, harness);
    const profiles = profilesFor(runner, harness);
    if (
      profiles.length === 0 ||
      entry?.login_state === 'signed_out' ||
      entry?.enabled === false
    )
      continue;
    const generated = (
      inventoryEntry(runner, harness)?.generated_profile || ''
    ).trim();
    const profile = profiles.includes(generated) ? generated : profiles[0];
    choices.push({
      runner,
      token: runnerToken(runner),
      profile,
      online: isOnlineRunner(runner),
    });
  }
  return choices.sort((a, b) => Number(b.online) - Number(a.online));
}

/**
 * Runners whose inventory reports the harness but that advertise no profile
 * for it, so the form can say why they are not offered.
 */
export function hostExecRunnerBlockers(
  runners: RunnerRecord[],
  harness: string
): HostExecRunnerBlocker[] {
  const blockers: HostExecRunnerBlocker[] = [];
  for (const runner of runners) {
    const entry = inventoryEntry(runner, harness);
    if (
      !entry ||
      (profilesFor(runner, harness).length > 0 &&
        entry.login_state !== 'signed_out' &&
        entry.enabled !== false)
    )
      continue;
    blockers.push({
      runner,
      reason:
        entry.login_state === 'signed_out'
          ? 'signed_out'
          : entry.enabled === false
            ? 'disabled'
            : 'no_profile',
    });
  }
  return blockers;
}

/** The choice the flow currently targets, matched by runner token or id. */
export function selectedHostExecChoice(
  choices: HostExecRunnerChoice[],
  runnerPool: string | null | undefined
): HostExecRunnerChoice | undefined {
  const key = (runnerPool || '').trim().toLowerCase();
  if (runnerSelectionKind(key) !== 'private') return undefined;
  const matches = choices.filter(
    (choice) =>
      choice.token.toLowerCase() === key ||
      choice.runner.id.toLowerCase() === key ||
      (choice.runner.name || '').trim().toLowerCase() === key
  );
  return matches.length === 1 ? matches[0] : undefined;
}
