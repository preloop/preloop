import { expect } from '@open-wc/testing';

import {
  getAgentKindPresentation,
  isCliOnboardableAgentKind,
  normalizeAgentKind,
} from './agent-kinds';

describe('agent kinds', () => {
  it('identifies Pi and DeepSeek Harness independently of model providers', () => {
    expect(getAgentKindPresentation('copilot_cli')?.label).to.equal(
      'Copilot CLI'
    );
    expect(getAgentKindPresentation('pi')?.label).to.equal('Pi');
    expect(getAgentKindPresentation('deepseek')?.label).to.equal(
      'DeepSeek Harness'
    );
  });
  it('folds case and separators into one key', () => {
    expect(normalizeAgentKind('Gemini CLI')).to.equal('gemini_cli');
    expect(normalizeAgentKind('gemini-cli')).to.equal('gemini_cli');
    expect(getAgentKindPresentation('Claude Code')?.label).to.equal(
      'Claude Code'
    );
  });

  it('knows which kinds the CLI can onboard', () => {
    for (const kind of [
      'claude_code',
      'Gemini CLI',
      'codex',
      'opencode',
      'hermes',
      'pi',
      'deepseek',
      'cursor',
      'windsurf',
      'vscode',
      'copilot_cli',
      'desktop_agent',
    ]) {
      expect(isCliOnboardableAgentKind(kind), kind).to.equal(true);
    }
  });

  // Anything the CLI cannot discover on a machine answers false, including
  // kinds nobody has taught the console about: printing a command that cannot
  // work is worse than printing none.
  it('answers false for custom, external and unknown kinds', () => {
    for (const kind of [
      'custom',
      'external_agent',
      'langgraph',
      '',
      null,
      undefined,
    ]) {
      expect(isCliOnboardableAgentKind(kind as string), String(kind)).to.equal(
        false
      );
    }
  });
});
