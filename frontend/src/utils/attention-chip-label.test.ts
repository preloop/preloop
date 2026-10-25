import { expect } from '@open-wc/testing';
import { attentionChipLabel, type AttentionItem } from './attention';

const item = (overrides: Partial<AttentionItem>): AttentionItem => ({
  id: 'x:1',
  kind: 'flow',
  severity: 'warning',
  title: 'Pull Request Reviewer',
  detail: '',
  href: '/console',
  at: null,
  fingerprint: 'f',
  dismissable: true,
  ...overrides,
});

/**
 * The Overview strip has room for three short chips at 1440. "Global ·
 * monthly · Soft limit excee…" was cut mid-word, so a chip names the thing
 * and the fact, and the sentence lives in its title.
 */
describe('attentionChipLabel', () => {
  it('names a budget by the limit it crossed', () => {
    expect(
      attentionChipLabel(
        item({
          kind: 'budget',
          title: 'Global · monthly',
          detail: 'Soft limit exceeded ($120.00 / $100.00)',
        })
      )
    ).to.equal('Budget over soft limit');
    expect(
      attentionChipLabel(
        item({
          kind: 'budget',
          title: 'Team · weekly',
          detail: 'Hard limit reached ($50.00 / $50.00)',
        })
      )
    ).to.equal('Budget at hard limit');
  });

  it('gives a flow or a model its name and failure count', () => {
    expect(
      attentionChipLabel(
        item({
          detail:
            '11 failed: 6 model transient, 5 runner conflict · latest 2h ago',
        })
      )
    ).to.equal('Pull Request Reviewer: 11 failed');
    expect(
      attentionChipLabel(
        item({ detail: '3 failed runs in 2d · latest 1h ago · 4m' })
      )
    ).to.equal('Pull Request Reviewer: 3 failed');
    expect(
      attentionChipLabel(item({ detail: 'Failed: runner conflict · 1h ago' }))
    ).to.equal('Pull Request Reviewer: failed');
    expect(
      attentionChipLabel(
        item({
          kind: 'model',
          title: 'gpt-5',
          detail: '4 failed requests · last 5m ago',
        })
      )
    ).to.equal('gpt-5: 4 failed');
  });

  it('gives an agent its name and first reason', () => {
    expect(
      attentionChipLabel(
        item({
          kind: 'agent',
          title: 'build-host',
          detail: 'Live check failed · 2 failed requests in last session',
        })
      )
    ).to.equal('build-host: Live check failed');
  });

  it('keeps the title alone for everything else', () => {
    expect(
      attentionChipLabel(
        item({
          kind: 'extension',
          title: 'GlitchTip intake failed',
          detail: 'Last delivery returned 503',
        })
      )
    ).to.equal('GlitchTip intake failed');
  });
});
