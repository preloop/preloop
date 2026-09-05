import { expect, fixture, html } from '@open-wc/testing';

import './preloop-flow-preset-picker';
import {
  firstSentence,
  presetChips,
  presetGroups,
  type FlowPresetRecord,
  type PreloopFlowPresetPicker,
} from './preloop-flow-preset-picker';

const RELEASE_SECURITY_DESCRIPTION =
  'The full release-time audit in one execution: verify the CI-emitted SBOM ' +
  '(validity, minimum elements, build cross-checks, license flags), then ' +
  'match its components against public vulnerability sources (OSV.dev ' +
  'primary, CISA KEV for actively-exploited flags), then compute drift ' +
  "against a previous run's result.json when provided. Emits one combined " +
  '/workspace/result.json (preloop.cra.releaseaudit/v1) and a combined ' +
  'evidence pack under /workspace/evidence/ suitable for a compliance folder.';

const CATALOG: FlowPresetRecord[] = [
  {
    id: 'preset-001',
    name: 'Issue Triage Assistant',
    description: 'Automatically analyze new issues, suggest labels.',
    icon: 'funnel',
    trigger_event_types: ['issue.opened'],
    allowed_mcp_tools: [
      { name: 'a' },
      { name: 'b' },
      { name: 'c' },
      { name: 'd' },
      { name: 'e' },
    ],
  },
  {
    id: 'preset-002',
    name: 'Pull Request Reviewer',
    description: 'Review a pull request when it opens.',
    icon: 'code-square',
    trigger_event_types: ['pull_request_opened'],
    allowed_mcp_tools: [
      { name: 'a' },
      { name: 'b' },
      { name: 'c' },
      { name: 'd' },
    ],
    git_clone_config: { enabled: true, create_pull_request: false },
  },
  {
    id: 'preset-003',
    name: 'Observe / Eval',
    description: 'Watch a repository on a schedule.',
    icon: 'eye',
    trigger_event_types: [],
    allowed_mcp_tools: [],
  },
  {
    id: 'preset-004',
    name: 'SBOM Verify',
    description: 'Verify an SBOM.',
    icon: 'shield-check',
  },
  {
    id: 'preset-006',
    name: 'Release Security Audit',
    description: RELEASE_SECURITY_DESCRIPTION,
    icon: 'shield-lock',
  },
  {
    id: 'preset-007',
    name: 'Component Due Diligence Record',
    description: 'Record due diligence.',
    icon: 'clipboard-check',
  },
  {
    id: 'preset-011',
    name: 'Automated Issue Implementation',
    description:
      'Turn a tracker issue into a working change: read the issue, implement it.',
    icon: 'lightning',
    trigger_event_types: ['issue_labeled', 'comment_created'],
    allowed_mcp_tools: [
      { name: 'a' },
      { name: 'b' },
      { name: 'c' },
      { name: 'd' },
      { name: 'e' },
    ],
    git_clone_config: { enabled: true, create_pull_request: true },
  },
];

const ACCOUNT_PRESET: FlowPresetRecord = {
  id: 'preset-account',
  name: 'Office Reviewer',
  account_id: 'acct-1',
  description: 'An account-specific preset.',
  trigger_event_types: ['pull_request_opened'],
};

describe('presetGroups', () => {
  it('puts account presets first and keeps catalog order without a PR-reviewer hack', () => {
    const groups = presetGroups([...CATALOG, ACCOUNT_PRESET]);

    expect(groups.map((group) => group.label)).to.deep.equal([
      'Your presets',
      'Tracker automation',
      'Scheduled review',
      'Security and compliance',
    ]);
    expect(groups[0].presets.map((preset) => preset.id)).to.deep.equal([
      'preset-account',
    ]);
    expect(groups[1].presets.map((preset) => preset.name)).to.deep.equal([
      'Issue Triage Assistant',
      'Pull Request Reviewer',
      'Automated Issue Implementation',
    ]);
    expect(groups[2].presets.map((preset) => preset.name)).to.deep.equal([
      'Observe / Eval',
    ]);
    expect(groups[3].presets.map((preset) => preset.name)).to.deep.equal([
      'SBOM Verify',
      'Release Security Audit',
      'Component Due Diligence Record',
    ]);
  });
});

describe('presetChips', () => {
  it('lists chips for 001, 002, 011 and a tool-less preset', () => {
    expect(presetChips(CATALOG[0]).map((chip) => chip.label)).to.deep.equal([
      'Tracker',
      'Model',
      '5 tools',
    ]);
    expect(presetChips(CATALOG[1]).map((chip) => chip.label)).to.deep.equal([
      'Tracker',
      'Model',
      '4 tools',
      'Clones repo',
    ]);
    expect(presetChips(CATALOG[6]).map((chip) => chip.label)).to.deep.equal([
      'Tracker',
      'Model',
      '5 tools',
      'Clones repo',
      'Opens PRs',
    ]);
    expect(presetChips(CATALOG[2]).map((chip) => chip.label)).to.deep.equal([
      'Model',
    ]);
    expect(
      presetChips({ allowed_mcp_tools: [{ name: 'ask_user' }] }).map(
        (chip) => chip.label
      )
    ).to.deep.equal(['Model', '1 tool']);
  });
});

describe('firstSentence', () => {
  it('yields one sentence from the 006 description', () => {
    const sentence = firstSentence(RELEASE_SECURITY_DESCRIPTION);
    expect(sentence.startsWith('The full release-time audit')).to.be.true;
    expect(sentence.endsWith('when provided.')).to.be.true;
    expect(sentence.includes('Emits one combined')).to.be.false;
  });
});

describe('preloop-flow-preset-picker', () => {
  it('filters search and hides empty groups but keeps Blank flow', async () => {
    const el = await fixture<PreloopFlowPresetPicker>(
      html`<preloop-flow-preset-picker
        .presets=${CATALOG}
      ></preloop-flow-preset-picker>`
    );
    const search = el.shadowRoot!.querySelector('sl-input') as HTMLInputElement;
    search.value = 'sbom';
    search.dispatchEvent(new CustomEvent('sl-input', { bubbles: true }));
    await el.updateComplete;

    const text = el.shadowRoot!.textContent || '';
    expect(text).to.include('Blank flow');
    expect(text).to.include('SBOM Verify');
    expect(text).to.include('Security and compliance');
    expect(text).to.not.include('Tracker automation');
    expect(text).to.not.include('Pull Request Reviewer');
    expect(text).to.not.include('Observe / Eval');
  });

  it('marks the selected row with aria-selected and the tint class', async () => {
    const el = await fixture<PreloopFlowPresetPicker>(
      html`<preloop-flow-preset-picker
        .presets=${CATALOG}
        selectedId="preset-002"
      ></preloop-flow-preset-picker>`
    );
    const row = el.shadowRoot!.querySelector(
      '[data-preset-id="preset-002"]'
    ) as HTMLElement;
    expect(row).to.exist;
    expect(row.getAttribute('aria-selected')).to.equal('true');
    expect(row.classList.contains('selected')).to.be.true;
    expect(row.getAttribute('role')).to.equal('option');
    expect(el.shadowRoot!.querySelector('[role="listbox"]')).to.exist;
  });

  it('collapses to Started from Pull Request Reviewer. with a Change button', async () => {
    const el = await fixture<PreloopFlowPresetPicker>(
      html`<preloop-flow-preset-picker
        .presets=${CATALOG}
        selectedId="preset-002"
        collapsed
      ></preloop-flow-preset-picker>`
    );
    const text = (el.shadowRoot!.textContent || '').replace(/\s+/g, ' ');
    expect(text).to.include('Started from Pull Request Reviewer.');
    expect(text).to.include('Change');
    const change = el.shadowRoot!.querySelector('sl-button');
    expect(change).to.exist;
    expect(change!.textContent).to.contain('Change');
  });

  it('renders the missing note for an unknown preset_id', async () => {
    const el = await fixture<PreloopFlowPresetPicker>(
      html`<preloop-flow-preset-picker
        .presets=${CATALOG}
        selectedId="missing-preset"
      ></preloop-flow-preset-picker>`
    );
    expect(el.shadowRoot!.textContent).to.include(
      'That preset is no longer available.'
    );
    expect(el.shadowRoot!.querySelector('[role="listbox"]')).to.exist;
  });

  it('renders no em dash in visible copy', async () => {
    const el = await fixture<PreloopFlowPresetPicker>(
      html`<preloop-flow-preset-picker
        .presets=${[...CATALOG, ACCOUNT_PRESET]}
      ></preloop-flow-preset-picker>`
    );
    const text = el.shadowRoot!.textContent || '';
    expect(text).to.not.include('\u2014');
    expect(text).to.not.include('—');
  });
});
