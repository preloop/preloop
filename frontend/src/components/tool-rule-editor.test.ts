import { html, fixture, expect } from '@open-wc/testing';

import './tool-rule-editor';
import type { ToolRuleEditor } from './tool-rule-editor';

describe('ToolRuleEditor', () => {
  it('renders dialog when open', async () => {
    const el = (await fixture(
      html`<tool-rule-editor
        .open=${true}
        .workflows=${[]}
        .features=${{}}
      ></tool-rule-editor>`
    )) as ToolRuleEditor;

    await el.updateComplete;

    const dialog = el.shadowRoot?.querySelector('sl-dialog');
    expect(dialog).to.exist;
    expect(dialog?.hasAttribute('open') || (dialog as any).open).to.be.true;
  });

  it('renders action cards for deny, require_approval, and allow', async () => {
    const el = (await fixture(
      html`<tool-rule-editor
        .open=${true}
        .workflows=${[]}
        .features=${{}}
      ></tool-rule-editor>`
    )) as ToolRuleEditor;

    await el.updateComplete;

    const actionCards = el.shadowRoot?.querySelectorAll('.action-card');
    expect(actionCards).to.have.lengthOf(3);

    const labels = Array.from(actionCards!).map(
      (c) => c.querySelector('.action-label')?.textContent
    );
    expect(labels).to.include('Deny');
    expect(labels).to.include('Require Approval');
    expect(labels).to.include('Allow');
  });

  it('dispatches close event when Cancel is clicked', async () => {
    const el = (await fixture(
      html`<tool-rule-editor
        .open=${true}
        .workflows=${[]}
        .features=${{}}
      ></tool-rule-editor>`
    )) as ToolRuleEditor;

    await el.updateComplete;

    let closeFired = false;
    el.addEventListener('close', () => {
      closeFired = true;
    });

    const cancelBtn = el.shadowRoot?.querySelector(
      'sl-button[variant="default"]'
    ) as HTMLElement;
    expect(cancelBtn).to.exist;
    cancelBtn.click();

    await el.updateComplete;
    expect(closeFired).to.be.true;
  });

  it('dispatches save-rule event when Add Rule is clicked', async () => {
    const el = (await fixture(
      html`<tool-rule-editor
        .open=${true}
        .workflows=${[]}
        .features=${{}}
      ></tool-rule-editor>`
    )) as ToolRuleEditor;

    await el.updateComplete;

    let saveDetail: { rule: unknown; formData: unknown } | null = null;
    el.addEventListener('save-rule', ((e: CustomEvent) => {
      saveDetail = e.detail;
    }) as EventListener);

    const saveBtn = el.shadowRoot?.querySelector(
      'sl-button[variant="primary"]'
    ) as HTMLElement;
    expect(saveBtn).to.exist;
    saveBtn.click();

    await el.updateComplete;
    expect(saveDetail).to.exist;
    expect(saveDetail?.formData).to.exist;
    expect((saveDetail?.formData as any).action).to.equal('deny');
  });

  it('shows Edit label when editing existing rule', async () => {
    const existingRule = {
      id: 'rule-1',
      account_id: 'acc-1',
      tool_configuration_id: 'cfg-1',
      action: 'allow' as const,
      condition_expression: null,
      condition_type: 'simple' as const,
      priority: 0,
      description: null,
      is_enabled: true,
      approval_workflow_id: null,
    };

    const el = (await fixture(
      html`<tool-rule-editor
        .open=${true}
        .rule=${existingRule}
        .workflows=${[]}
        .features=${{}}
      ></tool-rule-editor>`
    )) as ToolRuleEditor;

    await el.updateComplete;

    const dialog = el.shadowRoot?.querySelector('sl-dialog');
    expect(dialog?.getAttribute('label')).to.include('Edit');
  });

  it('builds a matches expression for the regex operator', async () => {
    const el = (await fixture(
      html`<tool-rule-editor
        .open=${true}
        .workflows=${[]}
        .features=${{}}
      ></tool-rule-editor>`
    )) as ToolRuleEditor;

    await el.updateComplete;

    const expr = (el as any)._buildConditionExpression(
      'command',
      'matches',
      'rm -rf|git push --force'
    );
    expect(expr).to.equal('args.command.matches("rm -rf|git push --force")');
  });

  it('escapes quotes and backslashes inside a regex value', async () => {
    const el = (await fixture(
      html`<tool-rule-editor
        .open=${true}
        .workflows=${[]}
        .features=${{}}
      ></tool-rule-editor>`
    )) as ToolRuleEditor;

    await el.updateComplete;

    const expr = (el as any)._buildConditionExpression(
      'command',
      'matches',
      'foo"bar\\baz'
    );
    expect(expr).to.equal('args.command.matches("foo\\"bar\\\\baz")');
  });

  it('translates a path pattern into an anchored regex', async () => {
    const el = (await fixture(
      html`<tool-rule-editor
        .open=${true}
        .workflows=${[]}
        .features=${{}}
      ></tool-rule-editor>`
    )) as ToolRuleEditor;

    await el.updateComplete;

    expect(
      (el as any)._buildConditionExpression('file_path', 'glob', '.github/**')
    ).to.equal('args.file_path.matches("(^|/)\\\\.github/.*$")');
    expect(
      (el as any)._buildConditionExpression('file_path', 'glob', '*.env')
    ).to.equal('args.file_path.matches("(^|/)[^/]*\\\\.env$")');
  });

  it('parses an existing matches expression back into the builder', async () => {
    const existingRule = {
      id: 'rule-1',
      account_id: 'acc-1',
      tool_configuration_id: 'cfg-1',
      action: 'deny' as const,
      condition_expression: 'args.command.matches("rm -rf|git push --force")',
      condition_type: 'simple' as const,
      priority: 0,
      description: null,
      is_enabled: true,
      approval_workflow_id: null,
    };

    const el = (await fixture(
      html`<tool-rule-editor
        .open=${true}
        .rule=${existingRule}
        .workflows=${[]}
        .features=${{}}
      ></tool-rule-editor>`
    )) as ToolRuleEditor;

    await el.updateComplete;

    expect((el as any)._simpleField).to.equal('command');
    expect((el as any)._simpleOperator).to.equal('matches');
    expect((el as any)._simpleValue).to.equal('rm -rf|git push --force');

    const parsed = (el as any)._parseCelExpression(
      'args.file_path.matches("(^|/)\\\\.github/.*$")'
    );
    expect(parsed).to.exist;
    expect(parsed.conditions).to.have.lengthOf(1);
    expect(parsed.conditions[0].operator).to.equal('matches');
    expect(parsed.conditions[0].field).to.equal('file_path');
    expect(parsed.conditions[0].value).to.equal('(^|/)\\.github/.*$');
  });

  it('keeps matches available in simple mode and marks condition_type simple', async () => {
    const el = (await fixture(
      html`<tool-rule-editor
        .open=${true}
        .workflows=${[]}
        .features=${{}}
      ></tool-rule-editor>`
    )) as ToolRuleEditor;

    await el.updateComplete;

    const matchesOption = el.shadowRoot?.querySelector(
      'sl-option[value="matches"]'
    );
    const globOption = el.shadowRoot?.querySelector('sl-option[value="glob"]');
    expect(matchesOption).to.exist;
    expect(matchesOption?.textContent?.trim()).to.equal('matches regex');
    expect(globOption).to.exist;
    expect(globOption?.textContent?.trim()).to.equal('matches path pattern');

    (el as any)._simpleField = 'command';
    (el as any)._simpleOperator = 'matches';
    (el as any)._simpleValue = 'rm -rf';
    await el.updateComplete;

    const hint = el.shadowRoot?.querySelector('.advanced-conditions-hint');
    expect(hint).to.exist;
    expect(hint?.textContent).to.include('Advanced conditions');

    let saveDetail: { rule: unknown; formData: unknown } | null = null;
    el.addEventListener('save-rule', ((e: CustomEvent) => {
      saveDetail = e.detail;
    }) as EventListener);

    (el as any)._handleSave();
    await el.updateComplete;

    expect(saveDetail).to.exist;
    const formData = saveDetail?.formData as {
      condition_type: string;
      condition_expression: string;
    };
    expect(formData.condition_type).to.equal('simple');
    expect(formData.condition_expression).to.equal(
      'args.command.matches("rm -rf")'
    );
  });
});
