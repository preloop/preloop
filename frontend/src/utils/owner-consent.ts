/**
 * Owner consent settings: the one place their labels and help text live, so
 * the runners table, the agent page and their tests read the same words.
 */

export type FlowExecutionConsent = 'ask_me' | 'always' | 'never';
export type SessionStartConsent = 'ask_me' | 'allow' | 'deny';

export interface ConsentOption<T extends string> {
  value: T;
  label: string;
  help: string;
}

export const FLOW_CONSENT_LABEL = 'Flow runs on this runner';

export const FLOW_CONSENT_OPTIONS: ConsentOption<FlowExecutionConsent>[] = [
  {
    value: 'ask_me',
    label: 'Ask me each time',
    help: 'You get an approval request before a flow runs here. The run waits until you answer or the request times out.',
  },
  {
    value: 'always',
    label: 'Allow without asking',
    help: 'Flows assigned to this runner start right away.',
  },
  {
    value: 'never',
    label: 'Never run flows',
    help: 'Flows are not scheduled on this runner. Your own sessions still work.',
  },
];

export const SESSION_CONSENT_LABEL = 'Sessions started by others';

export const SESSION_CONSENT_OPTIONS: ConsentOption<SessionStartConsent>[] = [
  {
    value: 'ask_me',
    label: 'Ask me first',
    help: 'When another account member starts a new session on this agent, you get an approval request first. Sessions already running are not affected.',
  },
  {
    value: 'allow',
    label: 'Allow',
    help: 'Account members can start new sessions on this agent without asking.',
  },
  {
    value: 'deny',
    label: "Don't allow",
    help: 'Only you can start new sessions on this agent.',
  },
];

/** Who may change the setting, said once for the read-only state. */
export const FLOW_CONSENT_OWNER_ONLY = 'Only the runner owner can change this.';
export const SESSION_CONSENT_OWNER_ONLY =
  'Only the agent owner can change this.';

export function consentOption<T extends string>(
  options: ConsentOption<T>[],
  value: string | null | undefined
): ConsentOption<T> {
  return options.find((option) => option.value === value) ?? options[0];
}
