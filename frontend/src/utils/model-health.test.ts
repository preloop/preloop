import { expect } from '@open-wc/testing';
import {
  failsOften,
  modelFailureShareText,
  modelHealthLabel,
  modelHealthVariant,
} from './model-health';

describe('model health rule (#1956)', () => {
  it('reads Attention for an open claim, whatever the counts', () => {
    expect(
      modelHealthLabel({
        needsAttention: true,
        totalRequests: 0,
        failedRequests: 0,
      })
    ).to.equal('Attention');
  });

  it('reads Idle with no requests in the window', () => {
    expect(
      modelHealthLabel({
        needsAttention: false,
        totalRequests: 0,
        failedRequests: 0,
      })
    ).to.equal('Idle');
  });

  it('turns Acknowledged at a 5% failure rate', () => {
    expect(failsOften(4, 100)).to.equal(false);
    expect(failsOften(5, 100)).to.equal(true);
    expect(
      modelHealthLabel({
        needsAttention: false,
        totalRequests: 358,
        failedRequests: 215,
      })
    ).to.equal('Acknowledged');
    expect(
      modelHealthLabel({
        needsAttention: false,
        totalRequests: 100,
        failedRequests: 4,
      })
    ).to.equal('Healthy');
  });

  it('colours only Healthy green', () => {
    expect(modelHealthVariant('Healthy')).to.equal('success');
    expect(modelHealthVariant('Attention')).to.equal('warning');
    expect(modelHealthVariant('Acknowledged')).to.equal('neutral');
    expect(modelHealthVariant('Idle')).to.equal('neutral');
  });

  it('states the failure share only above the threshold', () => {
    expect(modelFailureShareText(50, 73, '30d')).to.equal(
      '50 of 73 requests failed in 30d'
    );
    expect(modelFailureShareText(1500, 2000, '')).to.equal(
      '1,500 of 2,000 requests failed'
    );
    expect(modelFailureShareText(1, 100, '30d')).to.equal('');
  });
});
