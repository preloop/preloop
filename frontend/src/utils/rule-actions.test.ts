import { expect } from '@open-wc/testing';

import {
  RULE_ACTION_META,
  ruleActionLabel,
  ruleActionMeta,
} from './rule-actions';

describe('rule-actions', () => {
  it('names every backend action in sentence case', () => {
    expect(ruleActionLabel('allow')).to.equal('Allow');
    expect(ruleActionLabel('deny')).to.equal('Deny');
    expect(ruleActionLabel('require_approval')).to.equal('Require approval');
    expect(ruleActionLabel('notify')).to.equal('Notify');
    expect(ruleActionLabel('redact')).to.equal('Redact');
  });

  it('colours require approval as a warning everywhere', () => {
    expect(ruleActionMeta('require_approval').variant).to.equal('warning');
    expect(ruleActionMeta('allow').variant).to.equal('success');
    expect(ruleActionMeta('deny').variant).to.equal('danger');
  });

  it('never shows raw snake_case for an unknown action', () => {
    const meta = ruleActionMeta('rate_limit');
    expect(meta.label).to.equal('Rate limit');
    expect(meta.variant).to.equal('neutral');
    expect(ruleActionLabel('')).to.equal('Unknown');
    expect(ruleActionLabel(undefined)).to.equal('Unknown');
  });

  it('gives every known action an icon', () => {
    for (const meta of Object.values(RULE_ACTION_META)) {
      expect(meta.icon).to.be.a('string').and.not.equal('');
    }
  });
});
