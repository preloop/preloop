import type { ManagedAgentSummary, RuntimeSessionSummary } from '../types';
import { getAgentSourceLabel } from './agent-display';

export interface AgentControlState {
  state:
    | 'unsupported'
    | 'install_pending'
    | 'plugin_configured'
    | 'plugin_connected'
    | string;
  enabled: boolean;
  online: boolean;
  known: boolean;
  visible: boolean;
  label: string;
  detail: string;
  badgeVariant: 'success' | 'primary' | 'warning' | 'neutral' | 'danger';
}

export function getAgentControlState(
  agent: ManagedAgentSummary | null | undefined
): AgentControlState {
  if (!agent) {
    return {
      state: 'unsupported',
      enabled: false,
      online: false,
      known: false,
      visible: false,
      label: 'Unavailable',
      detail: 'Agent Control is not available for this agent.',
      badgeVariant: 'neutral',
    };
  }

  const capabilities = agent.control_capabilities ?? [];
  const state = agent.control_state ?? 'unsupported';
  const enabled =
    agent.control_enabled === true && capabilities.includes('send_text_prompt');
  const online = enabled && agent.control_online === true;

  if (state === 'install_pending') {
    return {
      state,
      enabled: false,
      online: false,
      known: true,
      visible: true,
      label: 'Install pending',
      detail:
        'Agent Control config was written, but the runtime plugin has not been verified yet.',
      badgeVariant: 'warning',
    };
  }

  if (!enabled) {
    return {
      state,
      enabled: false,
      online: false,
      known: agent.control_enabled !== undefined,
      visible: state !== 'unsupported',
      label: 'Unavailable',
      detail:
        'This agent does not advertise an installed Agent Control plugin.',
      badgeVariant: 'neutral',
    };
  }

  return {
    state: online ? 'plugin_connected' : state,
    enabled,
    online,
    known: true,
    visible: true,
    label: online ? 'Agent Control online' : 'Agent Control configured',
    detail: online
      ? 'This agent can receive Agent Control prompts.'
      : 'Agent Control is configured, but the plugin is not currently connected.',
    badgeVariant: online ? 'success' : 'warning',
  };
}

export function getAgentControlSessionMode(
  agent: ManagedAgentSummary | null | undefined
): 'local' | 'remote' | 'queued' | 'offline' {
  const raw = (agent?.control_session_mode || '').toLowerCase();
  if (raw === 'local' || raw === 'remote' || raw === 'queued') {
    return raw;
  }
  if (agent?.control_online) {
    return 'remote';
  }
  return 'offline';
}

export function formatAgentControlSessionLabel(
  session: RuntimeSessionSummary
): string {
  const reference = session.session_reference || session.flow_name || 'Session';
  const status = session.is_active_now
    ? 'active'
    : session.ended_at
      ? 'ended'
      : session.activity_status || 'idle';
  const lastActivity = session.last_activity_at || session.last_request_at;
  const suffix = lastActivity
    ? ` · ${new Date(lastActivity).toLocaleString()}`
    : '';
  return `${reference} (${status})${suffix}`;
}

/** Where the operator reads what Agent Control is and how a runtime gets it. */
export const AGENT_CONTROL_DOCS_URL =
  'https://docs.preloop.ai/integrations/agent-control-runtime-adapters/';

export interface AgentControlInstallHint {
  /** Can this agent kind ever run Agent Control? */
  supported: boolean;
  /** The command that installs the runtime plugin, or null when it cannot. */
  command: string | null;
  docsUrl: string;
  /** What the disabled composer says in place of the prompt. */
  placeholder: string;
  /** The line under the composer. */
  helptext: string;
}

/**
 * What a composer says when it cannot send.
 *
 * The distinction comes from the server, not from a table in the console:
 * `control_state = 'unsupported'` means the runtime has no Agent Control
 * plugin at all (Claude Desktop, Cursor), so there is nothing to install and
 * offering a command would be a lie. Anything else (configured, install
 * pending) is one `install-plugin` away, and the CLI takes the agent by the
 * name the console shows.
 */
export function getAgentControlInstallHint(
  agent: ManagedAgentSummary | null | undefined
): AgentControlInstallHint {
  const kindLabel = getAgentSourceLabel(
    agent?.agent_kind || agent?.session_source_type
  );
  const name = agent?.display_name || 'this agent';
  const state = getAgentControlState(agent);

  if (!agent || state.state === 'unsupported') {
    return {
      supported: false,
      command: null,
      docsUrl: AGENT_CONTROL_DOCS_URL,
      placeholder: `Agent Control is not available for ${kindLabel}`,
      helptext: `${kindLabel} does not run an Agent Control plugin, so Preloop can watch this agent but cannot talk to it.`,
    };
  }

  return {
    supported: true,
    command: `preloop agents install-plugin "${agent.display_name || agent.id}"`,
    docsUrl: AGENT_CONTROL_DOCS_URL,
    placeholder: `Install Agent Control to talk to ${name}`,
    helptext:
      state.state === 'install_pending'
        ? 'Agent Control config was written but the runtime plugin has not connected yet. Run it on the machine that runs the agent:'
        : 'Run this on the machine that runs the agent, then start it again:',
  };
}
