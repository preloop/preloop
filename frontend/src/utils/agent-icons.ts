import { html, type TemplateResult } from 'lit';
import { getAgentKindPresentation } from './agent-kinds';

export function renderAgentIcon(
  sourceType: string | null | undefined,
  style: string = ''
): TemplateResult {
  const presentation = getAgentKindPresentation(sourceType);
  const logo = presentation?.logo;
  if (logo) {
    return html`<sl-icon src="${logo}" style="${style}"></sl-icon>`;
  }
  return html`<sl-icon
    name="${presentation?.icon || 'robot'}"
    style="${style}"
  ></sl-icon>`;
}
