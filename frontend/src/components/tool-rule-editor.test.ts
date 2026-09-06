import { html, fixture, expect } from '@open-wc/testing';

import './tool-rule-editor';
import {
  globToAnchoredRegex,
  unescapeCelString,
  type ToolRuleEditor,
} from './tool-rule-editor';

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
    expect(labels).to.include('Require approval');
    expect(labels).to.include('Allow');
  });

  it('defaults a new rule to Require approval, not Deny (B-T3)', async () => {
    const el = (await fixture(
      html`<tool-rule-editor
        .open=${true}
        .workflows=${[]}
        .features=${{}}
      ></tool-rule-editor>`
    )) as ToolRuleEditor;

    await el.updateComplete;

    const selected = el.shadowRoot?.querySelector('.action-card.selected');
    expect(
      selected?.querySelector('.action-label')?.textContent?.trim()
    ).to.equal('Require approval');
    expect(selected?.classList.contains('approval')).to.be.true;
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
    expect((saveDetail?.formData as any).action).to.equal('require_approval');
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
    expect(unescapeCelString('a\\nb')).to.equal('a\\nb');
    expect(unescapeCelString('\\d+')).to.equal('\\d+');
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
    expect(
      (el as any)._buildConditionExpression('file_path', 'glob', '**/*.env')
    ).to.equal('args.file_path.matches("(^|/)(?:.*/)?[^/]*\\\\.env$")');
    const envRegex = new RegExp(globToAnchoredRegex('**/*.env'));
    expect(envRegex.test('.env')).to.be.true;
    expect(envRegex.test('a/b/.env')).to.be.true;
    const oneChar = new RegExp(globToAnchoredRegex('src/?'));
    expect(oneChar.test('src/a')).to.be.true;
    expect(oneChar.test('src/ab')).to.be.false;
    expect(oneChar.test('src/a/b')).to.be.false;
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
    expect(
      (el as any)._parseSingleCondition(
        'args.command.matches("foo\\"bar\\\\baz")'
      ).value
    ).to.equal('foo"bar\\baz');
  });

  it('keeps matches available in simple mode and saves the matches expression', async () => {
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

    let saveDetail = null as { rule: unknown; formData: unknown } | null;
    el.addEventListener('save-rule', ((e: CustomEvent) => {
      saveDetail = e.detail;
    }) as EventListener);

    (el as any)._handleSave();
    await el.updateComplete;

    expect(saveDetail).to.not.equal(null);
    const formData = (saveDetail as { rule: unknown; formData: unknown })
      .formData as {
      condition_type: string;
      condition_expression: string;
    };
    expect(formData.condition_type).to.equal('cel');
    expect(formData.condition_expression).to.equal(
      'args.command.matches("rm -rf")'
    );
  });

  it('falls back to a Path pattern sentence when the glob description is empty', async () => {
    const el = (await fixture(
      html`<tool-rule-editor
        .open=${true}
        .workflows=${[]}
        .features=${{}}
      ></tool-rule-editor>`
    )) as ToolRuleEditor;

    await el.updateComplete;

    (el as any)._simpleField = 'file_path';
    (el as any)._simpleOperator = 'glob';
    (el as any)._simpleValue = '.github/**';
    (el as any)._description = '';
    await el.updateComplete;

    let saveDetail = null as { rule: unknown; formData: unknown } | null;
    el.addEventListener('save-rule', ((e: CustomEvent) => {
      saveDetail = e.detail;
    }) as EventListener);

    (el as any)._handleSave();
    await el.updateComplete;

    expect(saveDetail).to.not.equal(null);
    const formData = (saveDetail as { rule: unknown; formData: unknown })
      .formData as { description: string };
    expect(formData.description).to.equal('Path pattern: .github/**');
  });

  it('does not label a catch-all as a path pattern when the field is empty', async () => {
    const el = (await fixture(
      html`<tool-rule-editor
        .open=${true}
        .workflows=${[]}
        .features=${{}}
      ></tool-rule-editor>`
    )) as ToolRuleEditor;

    await el.updateComplete;

    (el as any)._simpleField = '';
    (el as any)._simpleOperator = 'glob';
    (el as any)._simpleValue = '.github/**';
    (el as any)._description = '';
    await el.updateComplete;

    let saveDetail = null as { rule: unknown; formData: unknown } | null;
    el.addEventListener('save-rule', ((e: CustomEvent) => {
      saveDetail = e.detail;
    }) as EventListener);

    (el as any)._handleSave();
    await el.updateComplete;

    expect(saveDetail).to.not.equal(null);
    const formData = (saveDetail as { rule: unknown; formData: unknown })
      .formData as {
      description: string | null;
      condition_expression: string | null;
    };
    expect(formData.condition_expression).to.equal(null);
    expect(formData.description).to.equal(null);
  });

  it('blocks save when the regex does not compile', async () => {
    const el = (await fixture(
      html`<tool-rule-editor
        .open=${true}
        .workflows=${[]}
        .features=${{}}
      ></tool-rule-editor>`
    )) as ToolRuleEditor;

    await el.updateComplete;

    (el as any)._simpleField = 'command';
    (el as any)._simpleOperator = 'matches';
    (el as any)._simpleValue = '[';
    await el.updateComplete;

    let saveDetail = null as { rule: unknown; formData: unknown } | null;
    el.addEventListener('save-rule', ((e: CustomEvent) => {
      saveDetail = e.detail;
    }) as EventListener);

    (el as any)._handleSave();
    await el.updateComplete;

    expect(saveDetail).to.equal(null);
    expect((el as any)._error).to.equal('That regex does not compile.');
  });
});
