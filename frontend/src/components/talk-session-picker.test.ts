import { expect, fixture, html, waitUntil } from '@open-wc/testing';
import sinon from 'sinon';
import { setViewport } from '@web/test-runner-commands';
import './talk-session-picker';
import type { TalkSessionPicker } from './talk-session-picker';
import type { RuntimeSessionSummary } from '../types';

function session(
  id: string,
  overrides: Partial<RuntimeSessionSummary> = {}
): RuntimeSessionSummary {
  return {
    id,
    title: `Session ${id}`,
    first_prompt: `Opening request ${id}`,
    cwd: '/workspace/catalog',
    activity_status: 'idle',
    ...overrides,
  } as RuntimeSessionSummary;
}

describe('Talk session finder', () => {
  afterEach(async () => {
    sinon.restore();
    localStorage.removeItem('accessToken');
    await setViewport({ width: 1024, height: 768 });
  });
  it('uses the tree, labels states, and selects a child in one action', async () => {
    const el = await fixture<TalkSessionPicker>(
      html`<talk-session-picker
        .sessions=${[
          session('parent'),
          session('child', {
            parent_session_id: 'parent',
            activity_status: 'active_now',
          }),
          session('ended', {
            ended_at: '2026-01-01',
            activity_status: 'ended',
          }),
        ]}
      ></talk-session-picker>`
    );
    expect(el.textContent).to.contain('Idle');
    expect(el.textContent).to.contain('Ended');
    expect(el.querySelector('[data-session-id="child"]')).to.equal(null);
    const toggle = el.querySelector('sl-button[aria-expanded]') as HTMLElement;
    toggle.click();
    await el.updateComplete;
    const selected = sinon.spy();
    el.addEventListener('talk-session-select', selected);
    (el.querySelector('[data-session-id="child"]') as HTMLElement).click();
    expect(selected).to.have.been.calledOnce;
    expect(selected.firstCall.args[0].detail.sessionId).to.equal('child');
    expect(el.textContent).to.contain('Subagent of Session parent');
    expect(el.textContent).to.contain('/workspace/catalog');
    expect(el.textContent).to.contain('Opening request child');
  });
  it('searches the agent with a POST body and reveals content matches with parent context', async () => {
    localStorage.setItem('accessToken', 'synthetic-token');
    const fetch = sinon.stub(window, 'fetch').resolves(
      new Response(
        JSON.stringify({
          items: [session('child', { parent_session_id: 'parent' })],
          related_sessions: [session('parent')],
          matching_snippets: { child: 'Middle <mark>quasar</mark> request' },
          has_more: false,
        }),
        { status: 200 }
      )
    );
    const el = await fixture<TalkSessionPicker>(
      html`<talk-session-picker agentId="example-agent"></talk-session-picker>`
    );
    const input = el.querySelector('sl-input')!;
    input.value = 'quasar';
    input.dispatchEvent(new Event('sl-input'));
    await waitUntil(() =>
      Boolean(el.querySelector('[data-session-id="child"]'))
    );
    expect(fetch.firstCall.args[0]).to.equal('/api/v1/runtime-sessions/find');
    expect(JSON.parse(String(fetch.firstCall.args[1]?.body))).to.include({
      agent: 'example-agent',
      query: 'quasar',
    });
    expect(el.textContent).to.contain('Middle quasar request');
    expect(el.querySelector('[data-session-id="parent"]')).to.exist;
  });
  it('keeps New session separate from a selected session', async () => {
    const el = await fixture<TalkSessionPicker>(
      html`<talk-session-picker
        .sessions=${[session('old')]}
        .currentSessionId=${'old'}
      ></talk-session-picker>`
    );
    const selected = sinon.spy(),
      created = sinon.spy();
    el.addEventListener('talk-session-select', selected);
    el.addEventListener('talk-session-new', created);
    (
      el.querySelector('[data-testid="talk-new-session"]') as HTMLElement
    ).click();
    expect(created).to.have.been.calledOnce;
    expect(selected).not.to.have.been.called;
  });
  it('reveals the selected child and keeps the menu inside a phone viewport', async () => {
    await setViewport({ width: 375, height: 667 });
    const el = await fixture<TalkSessionPicker>(
      html`<talk-session-picker
        .sessions=${[session('parent'), session('child', { parent_session_id: 'parent' })]}
        .currentSessionId=${'child'}
      ></talk-session-picker>`
    );
    await el.show();
    expect(
      el
        .querySelector('[data-session-id="child"]')
        ?.getAttribute('aria-current')
    ).to.equal('true');
    const menu = el.querySelector('.talk-session-menu') as HTMLElement;
    expect(menu.getBoundingClientRect().width).to.be.at.most(375);
    expect(getComputedStyle(menu).boxSizing).to.equal('border-box');
  });

  it('ignores a slow result after the query changes', async () => {
    localStorage.setItem('accessToken', 'synthetic-token');
    let completeOld!: (value: Response) => void;
    const fetch = sinon.stub(window, 'fetch');
    fetch.onFirstCall().returns(
      new Promise<Response>((resolve) => {
        completeOld = resolve;
      })
    );
    fetch
      .onSecondCall()
      .resolves(
        new Response(
          JSON.stringify({ items: [session('new')], has_more: false }),
          { status: 200 }
        )
      );
    const el = await fixture<TalkSessionPicker>(
      html`<talk-session-picker agentId="example-agent"></talk-session-picker>`
    );
    const input = el.querySelector('sl-input')!;
    input.value = 'old';
    input.dispatchEvent(new Event('sl-input'));
    await waitUntil(() => fetch.calledOnce);
    input.value = 'new';
    input.dispatchEvent(new Event('sl-input'));
    await waitUntil(() => !!el.querySelector('[data-session-id="new"]'));
    completeOld(
      new Response(
        JSON.stringify({ items: [session('old')], has_more: false }),
        { status: 200 }
      )
    );
    await new Promise((resolve) => setTimeout(resolve, 50));
    expect(el.querySelector('[data-session-id="old"]')).to.equal(null);
    expect(el.querySelector('[data-session-id="new"]')).to.exist;
  });
});
