/**
 * The rule list under a tool. One label for one act: the policies page, this
 * editor and the tool rule dialog all said something different ("Add rule",
 * "Add Rule", "Add Access Rule - pay") for adding a rule.
 */
import { html, fixture, expect } from '@open-wc/testing';
import './governance-rule-set-editor';
import type { GovernanceRuleSetEditor } from './governance-rule-set-editor';

describe('GovernanceRuleSetEditor', () => {
  it('offers to add a rule in sentence case', async () => {
    const el = (await fixture(html`
      <governance-rule-set-editor
        .toolName=${'pay'}
        .rules=${[]}
        .workflows=${[]}
        .features=${{}}
      ></governance-rule-set-editor>
    `)) as GovernanceRuleSetEditor;
    await el.updateComplete;

    const label = Array.from(
      el.shadowRoot!.querySelectorAll('.rules-footer sl-button')
    ).map((button) => button.textContent?.trim());
    expect(label).to.deep.equal(['Add rule']);
  });
});
