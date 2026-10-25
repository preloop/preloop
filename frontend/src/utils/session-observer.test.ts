import { expect } from '@open-wc/testing';

import {
  formatSessionIdLabel,
  looksLikeFilePath,
  normalizeObservedSession,
  normalizeObservedSessions,
} from './session-observer';

describe('session-observer titles', () => {
  it('detects config file paths used as session references', () => {
    expect(looksLikeFilePath('/Users/dimo/.openclaw/openclaw.json')).to.be.true;
    expect(looksLikeFilePath('/Users/dimo/.claude/settings.json')).to.be.true;
    expect(looksLikeFilePath('C:\\Users\\dimo\\.claude\\settings.json')).to.be
      .true;
    expect(looksLikeFilePath('claude-session-42')).to.be.false;
    expect(looksLikeFilePath('Agent Control new session')).to.be.false;
  });

  it('falls back to a truncated session id instead of config paths', () => {
    const session = normalizeObservedSession({
      id: 'runtime-session-empty',
      runtime_session_id: 'runtime-session-empty',
      session_source_type: 'openclaw',
      session_source_id: 'mini-ab09f5011623',
      session_reference: '/Users/dimo/.openclaw/openclaw.json',
      request_count: 3,
    });

    expect(session.title).to.equal('Openclaw runtime-');
    expect(session.title).to.not.contain('/Users/dimo');
  });

  it('prefers explicit session titles over references', () => {
    const session = normalizeObservedSession({
      id: 'runtime-session-1',
      runtime_session_id: 'runtime-session-1',
      session_source_type: 'claude_code',
      session_reference: '/Users/dimo/.claude/settings.json',
      title: 'Heartbeat Poll Monitoring',
      request_count: 11,
    });

    expect(session.title).to.equal('Heartbeat Poll Monitoring');
  });

  it('formats short session ids without truncation', () => {
    expect(formatSessionIdLabel('abc12345')).to.equal('abc12345');
    expect(formatSessionIdLabel('runtime-session-empty')).to.equal('runtime-');
  });
});

describe('session-observer merge', () => {
  it('adds two rows for one session and recomputes the cache rate', () => {
    // Two sources describe the same session (the runtime session list and
    // the gateway breakdown). Counts add; the hit rate is read again from
    // the merged counts rather than averaged, which is what sumTokenUsage
    // does for every other list.
    const merged = normalizeObservedSessions([
      {
        id: 'session-1',
        total_requests: 2,
        estimated_cost: 0.1,
        token_usage: {
          prompt_tokens: 1000,
          completion_tokens: 100,
          total_tokens: 1100,
          cache_read_tokens: 800,
          uncached_input_tokens: 200,
          cache_hit_ratio: 0.8,
        },
      },
      {
        id: 'session-1',
        total_requests: 1,
        estimated_cost: 0.05,
        token_usage: {
          prompt_tokens: 1000,
          completion_tokens: 100,
          total_tokens: 1100,
          cache_read_tokens: 200,
          uncached_input_tokens: 800,
          cache_hit_ratio: 0.2,
        },
      },
    ]);

    expect(merged.length).to.equal(1);
    expect(merged[0].totalRequests).to.equal(3);
    expect(merged[0].tokenUsage.input_tokens).to.equal(2000);
    expect(merged[0].tokenUsage.output_tokens).to.equal(200);
    expect(merged[0].tokenUsage.total_tokens).to.equal(2200);
    expect(merged[0].tokenUsage.cache_read_tokens).to.equal(1000);
    expect(merged[0].tokenUsage.cache_hit_ratio).to.equal(0.5);
  });

  it('leaves the merged rate unknown when neither row measured the cache', () => {
    const merged = normalizeObservedSessions([
      {
        id: 'session-2',
        token_usage: { prompt_tokens: 10, completion_tokens: 5 },
      },
      {
        id: 'session-2',
        token_usage: { prompt_tokens: 20, completion_tokens: 5 },
      },
    ]);

    expect(merged[0].tokenUsage.input_tokens).to.equal(30);
    expect(merged[0].tokenUsage.cache_hit_ratio).to.equal(null);
  });
});

describe('session-observer notes', () => {
  it('reads the note count and the newest author off the row', () => {
    const session = normalizeObservedSession({
      id: 'session-noted',
      note_count: 2,
      latest_note_author_display: 'Reviewer',
      latest_note_author_auth_method: 'agent',
      latest_note_at: '2026-03-09T20:00:00Z',
    });

    expect(session.noteCount).to.equal(2);
    expect(session.latestNoteAuthorDisplay).to.equal('Reviewer');
    expect(session.latestNoteAuthorAuthMethod).to.equal('agent');
    expect(session.latestNoteAt).to.equal('2026-03-09T20:00:00Z');
  });

  it('reads a row with no note fields as a session nobody noted', () => {
    const session = normalizeObservedSession({ id: 'session-quiet' });

    expect(session.noteCount).to.equal(0);
    expect(session.latestNoteAuthorDisplay).to.equal(null);
    expect(session.latestNoteAuthorAuthMethod).to.equal(null);
    expect(session.latestNoteAt).to.equal(null);
  });

  it('takes the count once when two rows describe one session', () => {
    // Requests add across sources; a note count is already per session, so
    // merging must not double it.
    const merged = normalizeObservedSessions([
      { id: 'session-3', total_requests: 1 },
      {
        id: 'session-3',
        total_requests: 1,
        note_count: 2,
        latest_note_author_display: 'Jane Doe',
        latest_note_author_auth_method: 'jwt',
      },
    ]);

    expect(merged.length).to.equal(1);
    expect(merged[0].totalRequests).to.equal(2);
    expect(merged[0].noteCount).to.equal(2);
    expect(merged[0].latestNoteAuthorDisplay).to.equal('Jane Doe');
  });
});

describe('spawn aliases', () => {
  it('uses the spawn alias as the primary label even after a title arrives', () => {
    const session = normalizeObservedSession({
      id: 'worker-session',
      alias: 'brief-api',
      title: 'Investigate the API regression',
      runtime_principal_name: 'Claude Code',
    });
    expect(session.title).to.equal('brief-api');
  });
});

describe('session owner links', () => {
  it('links a session to its agent, and a flow session to its flow', () => {
    const agent = normalizeObservedSession({
      id: 'session-1',
      agent_name: 'Codex CLI',
      agent_href: '/console/agents/agent-1',
    });
    expect(agent.agentHref).to.equal('/console/agents/agent-1');
    expect(agent.agentName).to.equal('Codex CLI');
    const flow = normalizeObservedSession({
      id: 'session-2',
      flow_id: 'flow-1',
      flow_name: 'Pull Request Reviewer',
    });
    expect(flow.flowHref).to.equal('/console/flows/flow-1');
    const legacy = normalizeObservedSession({
      id: 'session-3',
      managed_agent_id: 'agent-2',
      managed_agent_name: 'Claude Code',
    });
    expect(legacy.agentHref).to.equal('/console/agents/agent-2');
  });
});

describe('session-observer subject hygiene', () => {
  it('cleans harness preambles out of a derived title', () => {
    const session = normalizeObservedSession({
      id: 'rs-hygiene-1',
      session_source_type: 'claude_code',
      title:
        'x-anthropic-billing-header: cc_version=2.1.310.b2b; cc_entrypoint=cli;\nRead /Users/alex/git/acme/prompt.md',
    });
    expect(session.title).to.equal('Read ~/git/acme/prompt.md');
  });

  it('skips a title that is only harness text and uses the next field', () => {
    const session = normalizeObservedSession({
      id: 'rs-hygiene-2',
      session_source_type: 'claude_code',
      title: '<system-reminder>Background task finished</system-reminder>',
      runtime_session_name: 'Claude Code',
    });
    expect(session.title).to.equal('Claude Code');
  });
});
