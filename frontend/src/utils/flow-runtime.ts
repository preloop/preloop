import { getAgentKindPresentation } from './agent-kinds';

/**
 * What a flow's `agent_type` is called on screen. Shared by the flow page
 * and the execution page, so a run's strip never prints the raw runtime id
 * ("deepseek", which reads as the model provider) where the flow page says
 * "DeepSeek Harness".
 */
const FLOW_RUNTIME_LABELS: Record<string, string> = {
  codex: 'Codex CLI',
  gemini: 'Gemini CLI',
  opencode: 'OpenCode',
  pi: 'Pi',
  deepseek: 'DeepSeek Harness',
};

/** The label for a flow's `agent_type`. */
export function flowRuntimeLabel(agentType: string | null | undefined): string {
  const raw = (agentType || '').trim();
  if (!raw) return 'Unknown';
  return (
    FLOW_RUNTIME_LABELS[raw.toLowerCase()] ||
    getAgentKindPresentation(raw)?.label ||
    raw
  );
}
