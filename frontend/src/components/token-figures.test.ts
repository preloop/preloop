/**
 * Behaviour contract for the console's token figures.
 *
 * Every list leads with tokens, so this one component decides what a token
 * figure looks like: compact counts, input and output named separately, and
 * a cache segment that is silent when nobody measured the cache.
 */
import { expect, fixture, html } from '@open-wc/testing';
import './token-figures';
import type { TokenFigures } from './token-figures';
import {
  cacheSplitOf,
  formatCacheHitRate,
  formatTokenCount,
  sumTokenUsage,
  tokenFiguresTitle,
} from './token-figures';
import type { GatewayTokenUsage } from '../types';

const USAGE: GatewayTokenUsage = {
  prompt_tokens: 12400,
  completion_tokens: 3100,
  total_tokens: 15500,
  input_tokens: 12400,
  output_tokens: 3100,
  cache_read_tokens: 8200,
  cache_write_tokens: 300,
  uncached_input_tokens: 3900,
  cache_hit_ratio: 0.6777,
};

function text(el: TokenFigures): string {
  return (el.shadowRoot?.textContent || '').replace(/\s+/g, ' ').trim();
}

describe('formatTokenCount', () => {
  it('renders counts under 1000 whole and larger ones compact', () => {
    expect(formatTokenCount(0)).to.equal('0');
    expect(formatTokenCount(999)).to.equal('999');
    expect(formatTokenCount(1000)).to.equal('1K');
    expect(formatTokenCount(12400)).to.equal('12.4K');
    expect(formatTokenCount(3_140_000)).to.equal('3.1M');
  });

  it('treats a missing count as nothing rather than throwing', () => {
    expect(formatTokenCount(null)).to.equal('0');
    expect(formatTokenCount(undefined)).to.equal('0');
  });
});

describe('formatCacheHitRate', () => {
  it('rounds to whole percent', () => {
    expect(formatCacheHitRate(0.6777)).to.equal('68%');
    expect(formatCacheHitRate(1)).to.equal('100%');
    expect(formatCacheHitRate(0)).to.equal('0%');
  });

  it('reports an unknown rate as null, never as zero percent', () => {
    expect(formatCacheHitRate(null)).to.equal(null);
    expect(formatCacheHitRate(undefined)).to.equal(null);
  });
});

describe('cacheSplitOf', () => {
  it('derives the rate when the aggregate only sent counts', () => {
    const split = cacheSplitOf({
      prompt_tokens: 1000,
      completion_tokens: 100,
      total_tokens: 1100,
      cache_read_tokens: 800,
      uncached_input_tokens: 200,
    });
    expect(split?.hit).to.equal(800);
    expect(split?.miss).to.equal(200);
    expect(split?.ratio).to.equal(0.8);
  });

  it('is null when no request reported a cache split', () => {
    expect(
      cacheSplitOf({
        prompt_tokens: 1000,
        completion_tokens: 100,
        total_tokens: 1100,
      })
    ).to.equal(null);
  });
});

describe('sumTokenUsage', () => {
  it('adds the counts and recomputes the rate from the merged counts', () => {
    // One aggregate reported a cache split, the other did not. Averaging the
    // two rates would state a hit rate over traffic nobody measured; the
    // merged rate is read over covered input only.
    const total = sumTokenUsage([
      {
        prompt_tokens: 1000,
        completion_tokens: 100,
        total_tokens: 1100,
        cache_read_tokens: 800,
        cache_write_tokens: 50,
        uncached_input_tokens: 200,
        cache_hit_ratio: 0.8,
      },
      {
        prompt_tokens: 3000,
        completion_tokens: 900,
        total_tokens: 3900,
      },
    ]);
    expect(total?.input_tokens).to.equal(4000);
    expect(total?.prompt_tokens).to.equal(4000);
    expect(total?.output_tokens).to.equal(1000);
    expect(total?.completion_tokens).to.equal(1000);
    expect(total?.total_tokens).to.equal(5000);
    expect(total?.cache_read_tokens).to.equal(800);
    expect(total?.cache_write_tokens).to.equal(50);
    expect(total?.uncached_input_tokens).to.equal(200);
    // 800 of the 1000 input tokens anyone measured came from cache. The
    // 3000 unmeasured ones are not misses.
    expect(total?.cache_hit_ratio).to.equal(0.8);
    expect(formatCacheHitRate(total?.cache_hit_ratio)).to.equal('80%');
  });

  it('leaves the rate unknown when nothing reported a cache split', () => {
    const total = sumTokenUsage([
      { prompt_tokens: 10, completion_tokens: 5, total_tokens: 15 },
      { prompt_tokens: 20, completion_tokens: 5, total_tokens: 25 },
    ]);
    expect(total?.input_tokens).to.equal(30);
    expect(total?.cache_hit_ratio).to.equal(null);
    expect(cacheSplitOf(total)).to.equal(null);
  });

  it('is null when there is nothing at all to state', () => {
    expect(sumTokenUsage([])).to.equal(null);
    expect(sumTokenUsage([null, undefined])).to.equal(null);
  });
});

describe('token-figures', () => {
  it('states input and output separately, compact, units named once', async () => {
    const el = await fixture<TokenFigures>(
      html`<token-figures .usage=${USAGE}></token-figures>`
    );
    expect(text(el)).to.contain('12.4K in');
    expect(text(el)).to.contain('3.1K out');
  });

  it('summarises the cache as a hit rate, and expands to hits and misses', async () => {
    const el = await fixture<TokenFigures>(
      html`<token-figures .usage=${USAGE}></token-figures>`
    );
    expect(text(el)).to.contain('cache 68% hit');

    el.expanded = true;
    await el.updateComplete;
    expect(text(el)).to.contain('8.2K hit');
    expect(text(el)).to.contain('3.9K miss');
  });

  it('omits the cache segment when the providers reported nothing', async () => {
    const el = await fixture<TokenFigures>(
      html`<token-figures
        .usage=${{
          prompt_tokens: 400,
          completion_tokens: 100,
          total_tokens: 500,
        }}
      ></token-figures>`
    );
    expect(text(el)).to.contain('400 in');
    expect(text(el)).to.not.contain('cache');
    expect(text(el)).to.not.contain('0%');
  });

  it('carries the exact numbers in a tooltip and in title', async () => {
    const el = await fixture<TokenFigures>(
      html`<token-figures .usage=${USAGE}></token-figures>`
    );
    const expected = tokenFiguresTitle(USAGE);
    expect(expected).to.contain('12,400 input tokens');
    expect(expected).to.contain('3,100 output tokens');
    expect(expected).to.contain('8,200 input tokens read from cache');
    expect(expected).to.contain('3,900 not cached');
    expect(expected).to.contain('300 written to cache');

    const tooltip = el.shadowRoot?.querySelector('sl-tooltip');
    expect(tooltip?.getAttribute('content')).to.equal(expected);
    expect(
      el.shadowRoot?.querySelector('.figures')?.getAttribute('title')
    ).to.equal(expected);
  });

  it('says nothing for a row with no usage, rather than a measured zero', async () => {
    const el = await fixture<TokenFigures>(
      html`<token-figures .usage=${null}></token-figures>`
    );
    expect(text(el)).to.equal('-');
    expect(
      el.shadowRoot?.querySelector('.empty')?.getAttribute('title')
    ).to.equal('No token usage recorded');
    expect(tokenFiguresTitle(null)).to.equal('No token usage recorded');
  });

  it('drops the cache segment where a column is too narrow for it', async () => {
    const el = await fixture<TokenFigures>(
      html`<token-figures .usage=${USAGE} hide-cache></token-figures>`
    );
    expect(text(el)).to.contain('12.4K in');
    expect(text(el)).to.not.contain('cache');
  });
});
