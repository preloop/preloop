import { expect } from '@open-wc/testing';
import { billingAttribution } from './billing-attribution';
import { billingStatusLabel } from './billing-status';

describe('billing display contracts', () => {
  it('names the captured resolved path and model, never inferring from an alias', () => {
    expect(billingAttribution({ billing_path: 'allowance' })).to.equal(
      'Billed to: allowance'
    );
    expect(
      billingAttribution({
        billing_path: 'your_key',
        billing_model_name: 'Own example',
        billing_model_id: 'model-1',
      })
    ).to.equal('Billed to: your key (Own example · model-1)');
    expect(
      billingAttribution({ model_alias: 'hosted-looking-alias' })
    ).to.equal('Billing path not recorded');
  });
  it('replaces provider codes with human status labels', () => {
    expect(
      ['trialing', 'active', 'past_due', 'pending_cancellation'].map(
        billingStatusLabel
      )
    ).to.deep.equal([
      'Trial',
      'Active',
      'Payment overdue',
      'Pending cancellation',
    ]);
    expect(billingStatusLabel('unknown_status')).to.equal('Unknown status');
  });
});
