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

  function rule(id: string, action: string, priority: number) {
    return {
      id,
      action,
      condition_expression: null,
      condition_type: 'cel',
      priority,
      description: null,
      is_enabled: true,
      approval_workflow_id: null,
    };
  }

  it('names rule actions in sentence case with the shared colours', async () => {
    const el = (await fixture(html`
      <governance-rule-set-editor
        .toolName=${'pay'}
        .rules=${[
          rule('r1', 'deny', 1),
          rule('r2', 'require_approval', 2),
          rule('r3', 'allow', 3),
        ]}
        .workflows=${[]}
        .features=${{}}
      ></governance-rule-set-editor>
    `)) as GovernanceRuleSetEditor;
    await el.updateComplete;

    const labels = Array.from(
      el.shadowRoot!.querySelectorAll('.rule-action-label')
    );
    expect(labels.map((label) => label.textContent?.trim())).to.deep.equal([
      'Deny',
      'Require approval',
      'Allow',
    ]);
    // Require approval is amber here, as on Policies, Approvals and Audit.
    expect(labels[1].classList.contains('warning')).to.equal(true);

    // Screen readers hear which rule a button acts on, not just "button".
    const names = Array.from(
      el.shadowRoot!.querySelectorAll(
        '.rule-actions sl-icon-button[name="pencil"], .rule-actions sl-icon-button[name="trash"]'
      )
    ).map((button) => button.getAttribute('label'));
    expect(names).to.deep.equal([
      'Edit rule 1',
      'Delete rule 1',
      'Edit rule 2',
      'Delete rule 2',
      'Edit rule 3',
      'Delete rule 3',
    ]);
  });
  it('moves rules using Alt+arrows and explicit buttons with bounded ends', async () => {
    const el = await fixture<GovernanceRuleSetEditor>(
      html`<governance-rule-set-editor
        .rules=${[rule('a', 'allow', 0), rule('b', 'deny', 1)]}
      ></governance-rule-set-editor>`
    );
    let detail: any;
    el.addEventListener('reorder-rules', (event: Event) => {
      detail = (event as CustomEvent).detail;
    });
    const rows = el.shadowRoot!.querySelectorAll('.rule-item');
    rows[1].dispatchEvent(
      new KeyboardEvent('keydown', {
        key: 'ArrowUp',
        altKey: true,
        bubbles: true,
      })
    );
    expect(detail.reorderedRules).to.deep.equal([
      { id: 'b', priority: 0 },
      { id: 'a', priority: 1 },
    ]);
    const up = el.shadowRoot!.querySelector(
      'sl-icon-button[name="arrow-up"]'
    ) as any;
    expect(up.disabled).to.equal(true);
    const down = el.shadowRoot!.querySelector(
      'sl-icon-button[name="arrow-down"]'
    ) as any;
    down.click();
    expect(detail.reorderedRules[0].id).to.equal('b');
    const allDown = el.shadowRoot!.querySelectorAll(
      'sl-icon-button[name="arrow-down"]'
    );
    expect((allDown[1] as any).disabled).to.equal(true);
  });

  it('forwards save settlement and keeps the editor open', async () => {
    const el = await fixture<GovernanceRuleSetEditor>(
      html`<governance-rule-set-editor></governance-rule-set-editor>`
    );
    const editor = el as any;
    editor._showRuleEditor = true;
    const resolve = () => {};
    const reject = () => {};
    let detail: any;
    el.addEventListener('save-rule', (event: Event) => {
      detail = (event as CustomEvent).detail;
    });
    editor._handleSaveRule(
      new CustomEvent('save-rule', {
        detail: { rule: null, formData: {}, resolve, reject },
      })
    );
    expect(detail.resolve).to.equal(resolve);
    expect(detail.reject).to.equal(reject);
    expect(editor._showRuleEditor).to.equal(true);
  });
});
