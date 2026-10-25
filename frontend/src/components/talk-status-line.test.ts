import { expect, fixture, html } from '@open-wc/testing';

import './talk-status-line';
import { shortCwd } from './talk-status-line';
import type { TalkStatusLine } from './talk-status-line';
import type { ManagedAgentSummary, RuntimeSessionSummary } from '../types';

function agent(overrides: Partial<ManagedAgentSummary> = {}) {
  return {
    id: 'agent-1',
    display_name: 'Codex',
    agent_kind: 'codex',
    session_source_type: 'codex',
    control_state: 'plugin_connected',
    control_enabled: true,
    control_online: true,
    control_capabilities: ['send_text_prompt'],
    control_session_mode: 'remote',
    ...overrides,
  } as ManagedAgentSummary;
}

function session(overrides: Partial<RuntimeSessionSummary> = {}) {
  return {
    id: 'sess-1',
    latest_model_alias: 'gpt-5-codex',
    cwd: '/Users/dev/src/app',
    ...overrides,
  } as RuntimeSessionSummary;
}

async function mount(
  props: {
    agent?: ManagedAgentSummary | null;
    session?: RuntimeSessionSummary | null;
    connected?: boolean | null;
    approvals?: number;
  } = {}
): Promise<TalkStatusLine> {
  const el = await fixture<TalkStatusLine>(
    html`<talk-status-line
      .agent=${props.agent === undefined ? agent() : props.agent}
      .session=${props.session === undefined ? session() : props.session}
      .connected=${props.connected ?? true}
      .approvals=${props.approvals ?? 0}
    ></talk-status-line>`
  );
  await el.updateComplete;
  return el;
}

function field(el: TalkStatusLine, name: string): string | null {
  return (
    el
      .querySelector(`[data-testid="talk-status-${name}"]`)
      ?.textContent?.trim() ?? null
  );
}

describe('talk-status-line', () => {
  it('status line shows model, cwd and control mode', async () => {
    const el = await mount();
    // Light DOM: the words are page text, not hidden in a shadow root.
    expect(el.shadowRoot).to.equal(null);
    expect(field(el, 'model')).to.equal('gpt-5-codex');
    expect(field(el, 'cwd')).to.equal('~/src/app');
    expect(
      el.querySelector('[data-testid="talk-status-cwd"]')!.getAttribute('title')
    ).to.equal('/Users/dev/src/app');
    expect(field(el, 'mode')).to.equal('remote control');
    expect(field(el, 'connection')).to.equal('connected');
    expect(field(el, 'approvals')).to.equal(null);
  });

  it('says a local session is local and counts waiting approvals', async () => {
    const el = await mount({
      agent: agent({ control_session_mode: 'local' }),
      approvals: 2,
    });
    expect(field(el, 'mode')).to.equal('local');
    expect(field(el, 'approvals')).to.equal('2 approvals waiting');
  });

  it('shows the socket reconnecting over the agent state', async () => {
    const el = await mount({ connected: false });
    expect(field(el, 'connection')).to.equal('reconnecting');
  });

  it('leaves out what the server has not reported', async () => {
    const el = await mount({
      agent: agent({ control_enabled: false, control_online: false }),
      session: session({ latest_model_alias: null, cwd: null }),
    });
    expect(el.textContent!.trim()).to.equal('');
  });

  it('shortens home and deep paths', () => {
    expect(shortCwd('/home/dev')).to.equal('~');
    expect(shortCwd('/srv/a/b/c/d')).to.equal('…/b/c/d');
    expect(shortCwd('/srv/app')).to.equal('/srv/app');
  });
});
