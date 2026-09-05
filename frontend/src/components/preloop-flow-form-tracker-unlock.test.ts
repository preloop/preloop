import { expect } from '@open-wc/testing';

/**
 * Spec #145 test 9: flow-form embeds add-tracker-modal but must not open
 * the unlock review dialog (v1 scoped to trackers-view only).
 */
describe('PreloopFlowForm tracker unlock scoping', () => {
  it('does not import or render unlocked-tools-review-dialog', async () => {
    const res = await fetch(
      new URL('./preloop-flow-form.ts', import.meta.url).href
    );
    expect(res.ok).to.be.true;
    const source = await res.text();
    expect(source).to.include('add-tracker-modal');
    expect(source).to.include('@tracker-added=${this.handleTrackerAdded}');
    expect(source).to.not.include('unlocked-tools-review-dialog');
    expect(source).to.not.include('unlocked_tool_names');
  });
});
