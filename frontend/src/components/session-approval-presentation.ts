import { html, type LitElement } from 'lit';
import type {
  ApprovalRequest,
  FlowGatewayEvent,
  RuntimeSessionActivityItem,
} from '../types';
import type { LiveSessionState } from '../utils/live-session';
import {
  retainSessionApprovalForms,
  type SessionApprovalState,
  type SessionLiveActivity,
} from './session-live-activity';
import './session-approval-card';

/** Shared inline decision wiring; surfaces retain their reactive state and visibility. */
export function renderSessionApproval(
  host: LitElement,
  state: SessionApprovalState,
  request: ApprovalRequest
) {
  return html`<div tabindex="-1" data-approval-id=${request.id}>
    <session-approval-card
      .request=${request}
      .canDecide=${state.canDecide}
      .author=${state.author}
      .now=${state.now}
      @session-approval-updated=${(event: CustomEvent<ApprovalRequest>) => host.renderRoot.querySelector<SessionLiveActivity>('session-live-activity')?.updateRequest(event.detail)}
    ></session-approval-card>
  </div>`;
}

export function jumpSessionApproval(host: LitElement, id: string): void {
  const row = [
    ...host.renderRoot.querySelectorAll<HTMLElement>('[data-approval-id]'),
  ].find((r) => r.dataset.approvalId === id);
  row?.scrollIntoView({ block: 'center' });
  row?.focus({ preventScroll: true });
}

export function renderSessionActivity(
  host: LitElement,
  sessionId: string,
  events: FlowGatewayEvent[],
  activity: RuntimeSessionActivityItem[],
  ended: boolean,
  state: SessionApprovalState,
  setState: (state: SessionApprovalState) => void,
  liveLine?: { state: LiveSessionState | null; now: number } | null
) {
  return html`<session-live-activity
    .sessionId=${sessionId}
    .events=${events}
    .activity=${activity}
    .ended=${ended}
    .state=${liveLine?.state ?? null}
    .now=${liveLine?.now ?? 0}
    @session-approvals-changed=${(event: CustomEvent<SessionApprovalState>) => setState(retainSessionApprovalForms(state, event.detail))}
    @session-approval-jump=${(event: CustomEvent<{ id: string }>) => jumpSessionApproval(host, event.detail.id)}
    @session-live-reconcile=${() => host.dispatchEvent(new CustomEvent('session-live-reload', { bubbles: true, composed: true }))}
  ></session-live-activity>`;
}
