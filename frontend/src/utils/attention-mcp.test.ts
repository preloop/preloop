import { expect } from '@open-wc/testing';
import sinon from 'sinon';
import { invalidateApiCaches } from '../api';
import {
  decideMCPFinding,
  loadMCPFindings,
  type MCPFinding,
} from '../mcp-findings-api';
import { deriveAttentionItems } from './attention';

const finding: MCPFinding = {
  id: 'finding-1',
  server_id: 'registry-1',
  registry_revision: '2026-10-16T12:00:00',
  evidence_class: 'configured_route',
  source: 'account_registry',
  approval_state: 'unknown',
  state: 'open',
  first_seen_at: '2026-10-16T12:00:00Z',
  last_seen_at: '2026-10-16T12:00:00Z',
  ignored_until: null,
  agent_id: null,
  person_id: null,
  limitation:
    'Only configured routes and completed routed calls are attributable.',
};

describe('MCP inventory attention', () => {
  afterEach(() => {
    sinon.restore();
    invalidateApiCaches();
    localStorage.clear();
  });
  it('keeps configuration distinct from observed calls and hides resolved evidence', () => {
    const items = deriveAttentionItems({
      mcpFindings: [
        finding,
        { ...finding, id: 'call-1', evidence_class: 'observed_call' },
        { ...finding, id: 'resolved', state: 'resolved' },
      ],
    }).items.filter((item) => item.kind === 'mcp');
    expect(items).to.have.length(2);
    expect(items.map((item) => item.title)).to.include(
      'Configured MCP route needs inventory review'
    );
    expect(items.map((item) => item.title)).to.include(
      'Observed MCP call needs inventory review'
    );
    expect(items.every((item) => !item.dismissable)).to.equal(true);
    expect(items[0].detail).to.include('unknown');
  });
  it('does not request findings without the capability', async () => {
    localStorage.setItem('accessToken', 'synthetic');
    const fetch = sinon.stub(window, 'fetch').resolves(
      new Response(JSON.stringify({ features: {} }), {
        status: 200,
        headers: { 'Content-Type': 'application/json' },
      })
    );
    expect(await loadMCPFindings()).to.eql([]);
    expect(
      fetch
        .getCalls()
        .some((call) => String(call.args[0]).includes('/mcp-findings'))
    ).to.equal(false);
  });
  it('writes explicit inventory-only approval and expiring ignore scopes', async () => {
    localStorage.setItem('accessToken', 'synthetic');
    const fetch = sinon
      .stub(window, 'fetch')
      .callsFake(async () => new Response('{}', { status: 200 }));
    await decideMCPFinding('finding-1', 'approve');
    expect(JSON.parse(fetch.firstCall.args[1]?.body as string)).to.eql({
      scope: 'account_registry_revision',
    });
    await decideMCPFinding('finding-1', 'ignore');
    const body = JSON.parse(fetch.secondCall.args[1]?.body as string);
    expect(body.scope).to.equal('finding');
    expect(new Date(body.expires_at).getTime()).to.be.greaterThan(Date.now());
  });
});

it('opens the exact resolved MCP finding from a weekly report link', () => {
  const items = deriveAttentionItems({
    focusedItemId: 'mcp:resolved',
    mcpFindings: [
      { ...finding, id: 'resolved', state: 'resolved' },
      { ...finding, id: 'other-resolved', state: 'resolved' },
    ],
  }).items.filter((item) => item.kind === 'mcp');
  expect(items).to.have.length(1);
  expect(items[0].id).to.equal('mcp:resolved');
  expect(items[0].title).to.equal('MCP inventory finding resolved');
  expect(items[0].severity).to.equal('low');
});
