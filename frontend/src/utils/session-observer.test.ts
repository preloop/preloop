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
