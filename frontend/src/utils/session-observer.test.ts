import { expect } from '@open-wc/testing';

import {
  formatSessionIdLabel,
  looksLikeFilePath,
  normalizeObservedSession,
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
