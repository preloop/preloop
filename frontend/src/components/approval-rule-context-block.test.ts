import { html, fixture, expect } from '@open-wc/testing';
import './approval-rule-context-block.ts';
import type { ApprovalRuleContextBlock } from './approval-rule-context-block';
import type { ApprovalRuleContext } from '../types';

/**
 * The point of this block is that an approver can tell a boundary case from a
 * mid-band one. So the tests care about exactly two things: the rule text is
 * shown verbatim when we have it, and nothing at all is shown when we do not.
 */
describe('ApprovalRuleContextBlock', () => {
  const boundaryRule: ApprovalRuleContext = {
    source: 'tool_access_rule',
    decision: 'require_approval',
    rule_id: '3f1d9b4e-0000-4000-8000-000000000001',
    rule_name: 'Large refunds need a human',
    expression: 'args.amount >= 5000',
    expression_type: 'cel',
    priority: 10,
    referenced_args: ['amount'],
    tool_configuration_id: '3f1d9b4e-0000-4000-8000-000000000002',
  };

  async function mount(
    context: ApprovalRuleContext | null,
    compact = false
  ): Promise<ApprovalRuleContextBlock> {
    const element = (await fixture(html`
      <approval-rule-context-block
        .ruleContext=${context}
        ?compact=${compact}
      ></approval-rule-context-block>
    `)) as ApprovalRuleContextBlock;
    await element.updateComplete;
    return element;
  }

  /**
   * Presence as a boolean. The test runner cannot serialize a DOM node into a
   * failure report and hangs instead, so assertions never hold one.
   */
  function has(element: ApprovalRuleContextBlock, selector: string): boolean {
    return (
      element.shadowRoot?.querySelector(selector) !== null &&
      element.shadowRoot?.querySelector(selector) !== undefined
    );
  }

  it('shows the rule name and the expression that fired', async () => {
    const element = await mount(boundaryRule);

    const text = element.shadowRoot?.textContent || '';
    expect(text).to.contain('Why this needs approval');
    expect(text).to.contain('Large refunds need a human');

    expect(has(element, '.rule-expression'), 'expression is rendered').to.be
      .true;
    const expression = element.shadowRoot?.querySelector('.rule-expression');
    // Verbatim: a reworded threshold is a different rule.
    expect(expression?.textContent?.trim()).to.equal('args.amount >= 5000');
  });

  it('names the arguments the rule inspects and its priority', async () => {
    const element = await mount(boundaryRule);

    const tags = [
      ...(element.shadowRoot?.querySelectorAll('sl-tag') || []),
    ].map((tag) => tag.textContent?.replace(/\s+/g, ' ').trim());
    expect(tags).to.contain('Priority 10');
    expect(tags).to.contain('Checks amount');
  });

  it('flags when lower-priority rules also matched', async () => {
    const element = await mount({
      ...boundaryRule,
      also_matched_rule_ids: [
        '3f1d9b4e-0000-4000-8000-000000000003',
        '3f1d9b4e-0000-4000-8000-000000000004',
      ],
    });

    const text = (element.shadowRoot?.textContent || '').replace(/\s+/g, ' ');
    expect(text).to.contain('2 lower-priority rules also matched');
  });

  it('says "rule" not "rules" when exactly one also matched', async () => {
    const element = await mount({
      ...boundaryRule,
      also_matched_rule_ids: ['3f1d9b4e-0000-4000-8000-000000000003'],
    });

    const text = (element.shadowRoot?.textContent || '').replace(/\s+/g, ' ');
    expect(text).to.contain('1 lower-priority rule also matched');
    expect(text).to.not.contain('rules also matched');
  });

  it('renders nothing at all when there is no rule context', async () => {
    const element = await mount(null);

    expect(has(element, '.rule-block'), 'no block is rendered').to.be.false;
    expect((element.shadowRoot?.textContent || '').trim()).to.equal('');
  });

  it('renders nothing when the context carries no rule name', async () => {
    // Defensive: a malformed payload must not produce an empty accusing box.
    const element = await mount({
      source: 'tool_access_rule',
      decision: 'require_approval',
      rule_name: '',
    } as ApprovalRuleContext);

    expect(has(element, '.rule-block'), 'no block is rendered').to.be.false;
  });

  it('explains default gating without inventing a rule expression', async () => {
    const element = await mount({
      source: 'tool_default_workflow',
      decision: 'require_approval',
      rule_name: 'Default policy',
      explanation:
        'No access rule matched. This tool is configured to require approval for every call, whatever the arguments are.',
    });

    const text = (element.shadowRoot?.textContent || '').replace(/\s+/g, ' ');
    expect(text).to.contain('require approval for every call');
    expect(has(element, '.rule-expression'), 'no expression invented').to.be
      .false;
  });

  it('shows the rule name only in compact mode', async () => {
    const element = await mount(boundaryRule, true);

    const text = element.shadowRoot?.textContent || '';
    expect(text).to.contain('Large refunds need a human');
    // A clipped expression reads as a different rule, so list rows omit it.
    expect(has(element, '.rule-expression'), 'compact hides the expression').to
      .be.false;
    expect(text).to.not.contain('Why this needs approval');
    const compact = element.shadowRoot?.querySelector('.compact');
    expect(compact?.getAttribute('title')).to.equal('args.amount >= 5000');
  });

  it('links to the tool the rule gates, not the whole catalogue', async () => {
    const element = (await fixture(html`
      <approval-rule-context-block
        .ruleContext=${boundaryRule}
        .toolName=${'issue_refund'}
      ></approval-rule-context-block>
    `)) as ApprovalRuleContextBlock;
    await element.updateComplete;

    const link = element.shadowRoot?.querySelector('.rule-link');
    expect(link?.getAttribute('href')).to.equal(
      '/console/tools#tool=issue_refund'
    );
  });

  it('falls back to the Tools page when the tool is unknown', async () => {
    const element = await mount(boundaryRule);

    const link = element.shadowRoot?.querySelector('.rule-link');
    expect(link?.getAttribute('href')).to.equal('/console/tools');
  });

  it('renders nothing in compact mode without a context', async () => {
    const element = await mount(null, true);

    expect(has(element, '.compact'), 'nothing rendered').to.be.false;
  });
});
