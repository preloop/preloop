import { expect } from '@open-wc/testing';

import {
  cleanSessionSubject,
  shortenHomePaths,
  stripBillingHeader,
} from './session-subject';

describe('cleanSessionSubject', () => {
  it('drops the harness billing header line', () => {
    expect(
      cleanSessionSubject(
        'x-anthropic-billing-header: cc_version=2.1.310.b2b; cc_entrypoint=cli;\nFix the login redirect'
      )
    ).to.equal('Fix the login redirect');
  });

  it('drops a billing header that shares the line with the prompt', () => {
    expect(
      cleanSessionSubject(
        'x-anthropic-billing-header: cc_version=2.1.310.b2b; cc_entrypoint=cli; You are Claude Code'
      )
    ).to.equal('You are Claude Code');
  });

  it('removes known wrapper blocks and keeps the real prompt', () => {
    expect(
      cleanSessionSubject(
        '<system-reminder>Codebase instructions follow</system-reminder>\n' +
          '<local-command-caveat>Caveat: not sent to you</local-command-caveat>\n' +
          'Add a retry to the webhook sender'
      )
    ).to.equal('Add a retry to the webhook sender');
  });

  it('removes an unterminated wrapper opener rather than the whole text', () => {
    expect(
      cleanSessionSubject('<system-reminder> [SYSTEM NOTIFICATION] background')
    ).to.equal(null);
  });

  it('shortens home directory paths', () => {
    expect(
      cleanSessionSubject(
        'Read /Users/alex/git/acme/brief/prompt.md and run it'
      )
    ).to.equal('Read ~/git/acme/brief/prompt.md and run it');
    expect(shortenHomePaths('cat /home/sam/notes.txt')).to.equal(
      'cat ~/notes.txt'
    );
  });

  it('leaves ordinary prompts and unknown tags alone', () => {
    expect(cleanSessionSubject('Render <my-widget> in the header')).to.equal(
      'Render <my-widget> in the header'
    );
    expect(cleanSessionSubject('Exploring the Talk')).to.equal(
      'Exploring the Talk'
    );
  });

  it('returns null when nothing but harness text remains', () => {
    expect(
      cleanSessionSubject(
        'x-anthropic-billing-header: cc_version=2.1; cc_entrypoint=cli;'
      )
    ).to.equal(null);
    expect(cleanSessionSubject('   ')).to.equal(null);
    expect(cleanSessionSubject(null)).to.equal(null);
  });
});

describe('stripBillingHeader', () => {
  it('drops only the header and keeps wrappers in previews', () => {
    expect(
      stripBillingHeader(
        'x-anthropic-billing-header: cc_version=2.1; cc_entrypoint=cli;\n<system-reminder>x</system-reminder>'
      )
    ).to.equal('<system-reminder>x</system-reminder>');
    expect(stripBillingHeader('You are a helpful agent')).to.equal(
      'You are a helpful agent'
    );
  });
});
