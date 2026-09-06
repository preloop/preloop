import { html, fixture, expect } from '@open-wc/testing';

import './attribution-line';
import { attributionParts, type AttributionLine } from './attribution-line';

/** An approval that knows everything about its caller. */
const FULL = {
  agent: { id: 'agent-1', name: 'Claude Code (laptop)', kind: 'claude_code' },
  api_key: { id: 'key-1', name: 'claude-code-laptop' },
  session: { id: 'session-1', subject: 'feature/attribution' },
  flow_execution: {
    id: 'exec-1',
    flow_id: 'flow-1',
    flow_name: 'Nightly audit',
  },
};

/** A plain API key calling the gate: no agent, no session, no run. */
const KEY_ONLY = {
  api_key: { id: 'key-2', name: 'ci-deploy' },
};

async function lineOf(source: unknown) {
  const element = (await fixture(
    html`<attribution-line .source=${source}></attribution-line>`
  )) as AttributionLine;
  await element.updateComplete;
  return element;
}

function textOf(element: AttributionLine): string {
  return element.shadowRoot!.textContent!.replace(/\s+/g, ' ').trim();
}

function hrefsOf(element: AttributionLine): (string | null)[] {
  return Array.from(element.shadowRoot!.querySelectorAll('a')).map((a) =>
    a.getAttribute('href')
  );
}

describe('attributionParts', () => {
  it('names and links all four parts when all four are known', () => {
    expect(attributionParts(FULL)).to.deep.equal([
      {
        key: 'agent',
        label: 'Agent',
        text: 'Claude Code (laptop)',
        href: '/console/agents/agent-1',
        title: 'claude_code · agent-1',
      },
      {
        key: 'key',
        label: 'Key',
        text: 'claude-code-laptop',
        href: '/console/settings/api-keys/key-1',
        title: 'key-1',
      },
      {
        key: 'session',
        label: 'Session',
        text: 'feature/attribution',
        href: '/console/runtime-sessions?sessionId=session-1',
        title: 'session-1',
      },
      {
        key: 'flow',
        label: 'Flow run',
        text: 'Nightly audit',
        href: '/console/flows/executions/exec-1',
        title: 'exec-1',
      },
    ]);
  });

  it('omits the parts nobody can name', () => {
    expect(attributionParts(KEY_ONLY).map((part) => part.key)).to.deep.equal([
      'key',
    ]);
  });

  it('shortens an id to eight characters rather than printing a UUID', () => {
    const parts = attributionParts({
      managed_agent_id: '3f2a9c14-6b7d-4e58-9a01-77b1c0d2e3f4',
      runtime_session_id: 'a1b2c3d4-e5f6-4788-9a0b-1c2d3e4f5a6b',
    });
    expect(parts.map((part) => part.text)).to.deep.equal([
      '3f2a9c14',
      'a1b2c3d4',
    ]);
    // The link still carries the whole id: only the label is shortened.
    expect(parts[0].href).to.equal(
      '/console/agents/3f2a9c14-6b7d-4e58-9a01-77b1c0d2e3f4'
    );
  });

  it('never falls back to a generic label when an id exists', () => {
    const parts = attributionParts({
      managed_agent_id: 'agent-7',
      managed_agent_name: null,
      tool_args: { _preloop_source: 'claude_code' },
    });
    expect(parts[0].text).to.equal('agent-7');
    expect(parts[0].text).to.not.equal('AI agent');
  });

  it('falls back to the adapter only when there is no id at all', () => {
    const parts = attributionParts({
      tool_args: { _preloop_source: 'cursor' },
    });
    expect(parts).to.have.length(1);
    expect(parts[0].text).to.equal('Cursor');
    expect(parts[0].href).to.equal(undefined);
  });

  it('says nothing about a caller it knows nothing about', () => {
    expect(attributionParts({})).to.deep.equal([]);
    expect(attributionParts(null)).to.deep.equal([]);
  });
});

describe('attribution-line', () => {
  it('renders every known part with its link', async () => {
    const element = await lineOf(FULL);

    const text = textOf(element);
    expect(text).to.contain('Agent Claude Code (laptop)');
    expect(text).to.contain('Key claude-code-laptop');
    expect(text).to.contain('Session feature/attribution');
    expect(text).to.contain('Flow run Nightly audit');
    expect(hrefsOf(element)).to.deep.equal([
      '/console/agents/agent-1',
      '/console/settings/api-keys/key-1',
      '/console/runtime-sessions?sessionId=session-1',
      '/console/flows/executions/exec-1',
    ]);
  });

  it('renders one part, with no empty labels, for a key-only caller', async () => {
    const element = await lineOf(KEY_ONLY);

    expect(textOf(element)).to.equal('Key ci-deploy');
    expect(hrefsOf(element)).to.deep.equal([
      '/console/settings/api-keys/key-2',
    ]);
  });

  it('renders nothing at all when nothing is known', async () => {
    const element = await lineOf({});
    expect(element.shadowRoot!.querySelector('.line')).to.equal(null);
  });

  it('drops the labels in compact mode but keeps the links', async () => {
    const element = (await fixture(
      html`<attribution-line compact .source=${FULL}></attribution-line>`
    )) as AttributionLine;
    await element.updateComplete;

    expect(textOf(element)).to.not.contain('Agent');
    expect(textOf(element)).to.contain('Claude Code (laptop)');
    expect(hrefsOf(element)).to.have.length(4);
  });

  it('keeps a link click off the row underneath it', async () => {
    const element = await lineOf(FULL);
    // Capture phase, so navigation is cancelled before the anchor sees it and
    // the test runner is not taken to /console/agents/agent-1.
    const cancel = (event: Event) => event.preventDefault();
    document.addEventListener('click', cancel, true);
    let bubbled = 0;
    element.addEventListener('click', () => {
      bubbled += 1;
    });

    try {
      const link = element.shadowRoot!.querySelector('a')!;
      link.dispatchEvent(
        new MouseEvent('click', {
          bubbles: true,
          composed: true,
          cancelable: true,
        })
      );
    } finally {
      document.removeEventListener('click', cancel, true);
    }

    expect(bubbled).to.equal(0);
  });
});
