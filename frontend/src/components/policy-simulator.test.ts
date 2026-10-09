import { html, fixture, expect } from '@open-wc/testing';
import sinon from 'sinon';
import './policy-simulator';
import type { PolicySimulator } from './policy-simulator';

describe('policy simulator', () => {
  beforeEach(() => localStorage.setItem('accessToken', 'test-token'));
  afterEach(() => {
    sinon.restore();
    localStorage.removeItem('accessToken');
  });

  it('submits an unsaved rule and reports condition errors in light DOM', async () => {
    const response = {
      decision: 'require_approval',
      matched_rule: 'draft-1',
      checked_rules: [{ id: 'draft-1', matched: false, error: 'Missing path' }],
      also_matched_rule_ids: [],
    };
    const fetch = sinon.stub(window, 'fetch').resolves(
      new Response(JSON.stringify(response), {
        headers: { 'Content-Type': 'application/json' },
      })
    );
    const el = await fixture<PolicySimulator>(
      html`<policy-simulator
        .toolName=${'read_file'}
        .draftRule=${{ action: 'deny' }}
        .toolSchema=${{ properties: { path: { type: 'string', example: '/etc/../etc' } } }}
      ></policy-simulator>`
    );
    await (el as any)._simulate();
    await el.updateComplete;
    expect(el.shadowRoot).to.equal(null);
    expect(el.textContent).to.include('Require approval');
    expect(el.textContent).to.include('Missing path');
    const call = fetch
      .getCalls()
      .find((call) => call.args[0] === '/api/v1/policies/evaluate')!;
    const body = JSON.parse(call.args[1]!.body as string);
    expect(body.args).to.deep.equal({ path: '/etc/../etc' });
    expect(body.draft_rule).to.deep.equal({ action: 'deny' });
  });

  it('submits the current YAML draft and refuses non-object arguments', async () => {
    const fetch = sinon.stub(window, 'fetch').resolves(
      new Response(
        JSON.stringify({
          decision: 'allow',
          matched_rule: null,
          checked_rules: [],
          also_matched_rule_ids: [],
        })
      )
    );
    const el = await fixture<PolicySimulator>(
      html`<policy-simulator
        .toolName=${'read_file'}
        .draftYaml=${'version: "1.0"'}
      ></policy-simulator>`
    );
    (el as any)._args = '[]';
    await (el as any)._simulate();
    expect(fetch.called).to.equal(false);
    expect((el as any)._error).to.include('JSON object');
    (el as any)._args = '{}';
    await (el as any)._simulate();
    expect(
      JSON.parse(fetch.firstCall.args[1]!.body as string).draft_yaml
    ).to.equal('version: "1.0"');
  });
  it('keeps the shown result when the parent sends an equivalent draft', async () => {
    sinon.stub(window, 'fetch').resolves(
      new Response(
        JSON.stringify({
          decision: 'deny',
          checked_rules: [],
          also_matched_rule_ids: [],
        })
      )
    );
    const el = await fixture<PolicySimulator>(
      html`<policy-simulator
        .toolName=${'read_record'}
        .draftRule=${{ action: 'deny' }}
      ></policy-simulator>`
    );
    await (el as any)._simulate();
    await el.updateComplete;
    el.draftRule = { action: 'deny' };
    await el.updateComplete;
    expect(el.textContent).to.include('Deny');
    el.draftRule = { action: 'allow' };
    await el.updateComplete;
    expect((el as any)._result).to.equal(null);
  });
  it('submits only synthetic grant context and shows a grant denial reason', async () => {
    const fetch = sinon.stub(window, 'fetch').resolves(
      new Response(
        JSON.stringify({
          decision: 'deny',
          description: 'scope_not_granted',
          matched_rule: null,
          checked_rules: [],
          also_matched_rule_ids: [],
        })
      )
    );
    const el = await fixture<PolicySimulator>(
      html`<policy-simulator
        .toolName=${'read_record'}
        .draftRule=${{ action: 'allow' }}
      ></policy-simulator>`
    );
    const field = el.querySelector(
      'sl-textarea[label="Synthetic grant JSON (optional)"]'
    )! as any;
    field.value = '{"active":true,"scope":["read"]}';
    field.dispatchEvent(new Event('sl-input'));
    await (el as any)._simulate();
    await el.updateComplete;
    const body = JSON.parse(fetch.firstCall.args[1]!.body as string);
    expect(body.grant).to.deep.equal({ active: true, scope: ['read'] });
    expect(el.textContent).to.include('scope_not_granted');
    expect(el.shadowRoot).to.equal(null);
  });

  it('rejects a non-object synthetic grant without sending a request', async () => {
    const fetch = sinon.stub(window, 'fetch');
    const el = await fixture<PolicySimulator>(
      html`<policy-simulator
        .toolName=${'read_record'}
        .draftRule=${{ action: 'allow' }}
      ></policy-simulator>`
    );
    (el as any)._grant = '[]';
    await (el as any)._simulate();
    expect(fetch.called).to.equal(false);
    expect((el as any)._error).to.include(
      'Synthetic grant must be a JSON object'
    );
  });
});
