import { expect, fixture, html } from '@open-wc/testing';
import '@shoelace-style/shoelace/dist/components/badge/badge.js';
import '@shoelace-style/shoelace/dist/components/tooltip/tooltip.js';

import {
  FAILURE_CATEGORY_META,
  failureCategoryBreakdown,
  failureCategoryChipLabel,
  failureCategoryCounts,
  failureCategoryLabel,
  failureCategoryTooltip,
  renderFailureCategoryChip,
} from './failure-category';

describe('failure-category', () => {
  it('covers the vocabulary the server stores', () => {
    expect(Object.keys(FAILURE_CATEGORY_META)).to.have.members([
      'runner_conflict',
      'runner_error',
      'model_transient',
      'model_auth',
      'provider_billing',
      'model_quota',
      'model_config',
      'no_confirmation',
      'tool_error',
      'agent_error',
      'timeout',
      'cancelled',
      'unknown',
    ]);
  });

  it('gives every category a one line tooltip', () => {
    for (const [key, meta] of Object.entries(FAILURE_CATEGORY_META)) {
      expect(meta.tooltip, key).to.have.length.greaterThan(20);
      expect(meta.tooltip, key).to.not.contain('\n');
      // House rule: no em dashes in anything a person reads.
      expect(meta.tooltip, key).to.not.contain('—');
      expect(meta.label, key).to.equal(meta.label.toLowerCase());
    }
  });

  it('humanises a known category', () => {
    expect(failureCategoryLabel('model_transient')).to.equal('model transient');
    expect(failureCategoryChipLabel('no_confirmation')).to.equal(
      'No confirmation'
    );
  });

  it('humanises a category it has never seen rather than dropping it', () => {
    expect(failureCategoryLabel('quantum_flux')).to.equal('quantum flux');
    expect(failureCategoryTooltip('quantum_flux')).to.contain('quantum flux');
  });

  it('says nothing at all when the field is absent', () => {
    expect(failureCategoryLabel(null)).to.equal('');
    expect(failureCategoryLabel(undefined)).to.equal('');
    expect(failureCategoryLabel('  ')).to.equal('');
    expect(failureCategoryTooltip(null)).to.equal('');
    expect(failureCategoryChipLabel(null)).to.equal('');
  });

  it('counts categories, most common first', () => {
    expect(
      failureCategoryCounts([
        'model_transient',
        'no_confirmation',
        'model_transient',
        'model_transient',
        'no_confirmation',
        null,
      ])
    ).to.deep.equal([
      { category: 'model_transient', count: 3 },
      { category: 'no_confirmation', count: 2 },
    ]);
  });

  it('breaks a count of failures into what it is made of', () => {
    expect(
      failureCategoryBreakdown([
        'model_transient',
        'model_transient',
        'model_transient',
        'no_confirmation',
        'no_confirmation',
      ])
    ).to.equal('3 model transient, 2 no confirmation');
  });

  it('breaks ties by the vocabulary order, not by insertion', () => {
    expect(failureCategoryBreakdown(['timeout', 'runner_conflict'])).to.equal(
      '1 runner conflict, 1 timeout'
    );
  });

  it('has an empty breakdown when nothing carries a category', () => {
    expect(failureCategoryBreakdown([null, undefined, ''])).to.equal('');
  });

  it('renders a soft neutral chip with its tooltip', async () => {
    const el = await fixture(
      html`<div>${renderFailureCategoryChip('runner_conflict')}</div>`
    );
    const badge = el.querySelector('sl-badge');
    expect(badge).to.exist;
    expect(badge?.getAttribute('variant')).to.equal('neutral');
    expect(badge?.classList.contains('chip')).to.be.true;
    // Red is stated once, in the status pill this chip sits after.
    expect(badge?.classList.contains('solid')).to.be.false;
    expect(badge?.textContent?.trim()).to.equal('Runner conflict');
    const tooltip = el.querySelector('sl-tooltip');
    expect(tooltip?.getAttribute('content')).to.contain('same name');
  });

  it('labels a provider billing refusal and never promises a retry', () => {
    expect(failureCategoryChipLabel('provider_billing')).to.equal(
      'Provider billing'
    );
    const tooltip = failureCategoryTooltip('provider_billing');
    expect(tooltip).to.contain('billing or quota');
    expect(tooltip).to.contain('topped up');
    expect(tooltip).to.not.contain('usually works on a retry');
  });

  it('drops the model transient retry promise when the page saw a 4xx', () => {
    expect(failureCategoryTooltip('model_transient')).to.contain(
      'usually works on a retry'
    );
    const doubtful = failureCategoryTooltip('model_transient', {
      retryDoubtful: true,
    });
    expect(doubtful).to.not.contain('usually works on a retry');
    expect(doubtful).to.contain('4xx');
  });

  it('renders nothing when the run carries no category', async () => {
    const el = await fixture(
      html`<div>${renderFailureCategoryChip(undefined)}</div>`
    );
    expect(el.querySelector('sl-badge')).to.not.exist;
  });
});
