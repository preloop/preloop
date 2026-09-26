import { expect } from '@open-wc/testing';

import { renderReportMarkdown, sameEvidencePack } from './evidence-report';

describe('evidence report markdown', () => {
  it('aligns outline ids with rendered headings and ignores fenced hashes', () => {
    const source = [
      'Cover',
      '=====',
      '',
      '```',
      '# comment',
      '```',
      '',
      '#### Nested sample',
      '',
      '## `What you should do next`',
      '',
    ].join('\n');
    const { html, headings } = renderReportMarkdown(source);
    expect(headings.map((heading) => heading.text)).to.eql([
      'Cover',
      'Nested sample',
      'What you should do next',
    ]);
    for (const heading of headings) {
      expect(html).to.contain(`id="${heading.id}"`);
    }
    expect(headings.map((heading) => heading.text)).to.not.include('comment');
    expect(html).to.contain('<code>What you should do next</code>');
    const ampersand = renderReportMarkdown('## A & B\n');
    expect(ampersand.headings[0].text).to.equal('A & B');
    expect(ampersand.html).to.contain('A &amp; B');
  });

  it('treats a pack status with the same fields as unchanged', () => {
    const current = {
      status: 'available',
      sha256: 'abc',
      legal_hold: false,
      integrity: 'not_checked',
      integrity_note: 'Availability only.',
      error: null,
    };
    expect(sameEvidencePack(current, { ...current, sha256: 'abc' })).to.equal(
      true
    );
    expect(
      sameEvidencePack(current, { ...current, legal_hold: true })
    ).to.equal(false);
    expect(sameEvidencePack(null, null)).to.equal(true);
    expect(sameEvidencePack(current, null)).to.equal(false);
  });
});
