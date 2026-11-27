import { expect, fixture, html } from '@open-wc/testing';
import './runner-flow-approval-context';
import {
  formatTimeLimit,
  type RunnerFlowApprovalContextBlock,
} from './runner-flow-approval-context';
import type { RunnerFlowApprovalContext } from '../types';

const context: RunnerFlowApprovalContext = {
  runner: {
    name: 'Work laptop',
    device: { hostname: 'laptop.example.com', os: 'macOS', arch: 'arm64' },
  },
  harness: 'Codex CLI',
  model: 'example-model',
  flow: { id: 'flow-1', name: 'Nightly review', href: '/console/flows/flow-1' },
  trigger: { kind: 'schedule', label: 'Scheduled run' },
  repositories: [
    { repository: 'https://example.com/acme/app.git', branch: 'main' },
  ],
  directory: 'App checkout',
  publication: {
    can_push: true,
    label: 'Can push a branch and open a pull request',
  },
  limits: { timeout_seconds: 5400 },
  scope: 'this_run',
  scope_label: 'Approving allows this run only.',
};

function field(el: RunnerFlowApprovalContextBlock, name: string): string {
  return (
    el.shadowRoot!.querySelector(`[data-field="${name}"]`)?.textContent ?? ''
  )
    .replace(/\s+/g, ' ')
    .trim();
}

describe('runner-flow-approval-context', () => {
  it('shows the device, harness, flow, trigger, checkout, publication, limit and scope', async () => {
    const el = await fixture<RunnerFlowApprovalContextBlock>(
      html`<runner-flow-approval-context
        .context=${context}
      ></runner-flow-approval-context>`
    );
    expect(field(el, 'runner')).to.equal(
      'Work laptop · laptop.example.com · macOS'
    );
    expect(field(el, 'agent')).to.equal('Codex CLI · example-model');
    expect(field(el, 'flow')).to.equal('Nightly review');
    expect(
      el
        .shadowRoot!.querySelector('[data-field="flow"] a')!
        .getAttribute('href')
    ).to.equal('/console/flows/flow-1');
    expect(field(el, 'trigger')).to.equal('Scheduled run');
    expect(field(el, 'repositories')).to.equal(
      'https://example.com/acme/app.git (main)'
    );
    expect(field(el, 'directory')).to.equal('App checkout');
    expect(field(el, 'publication')).to.contain('pull request');
    expect(field(el, 'limits')).to.equal('1h 30m');
    expect(field(el, 'scope')).to.contain('this run only');
  });

  it('omits facts the snapshot does not carry', async () => {
    const el = await fixture<RunnerFlowApprovalContextBlock>(
      html`<runner-flow-approval-context
        .context=${{ runner: { name: 'ci-box' } }}
      ></runner-flow-approval-context>`
    );
    expect(field(el, 'runner')).to.equal('ci-box');
    for (const name of ['agent', 'flow', 'repositories', 'limits']) {
      expect(el.shadowRoot!.querySelector(`[data-field="${name}"]`)).to.equal(
        null
      );
    }
  });

  it('renders one line in compact mode', async () => {
    const el = await fixture<RunnerFlowApprovalContextBlock>(
      html`<runner-flow-approval-context
        compact
        .context=${context}
      ></runner-flow-approval-context>`
    );
    expect(el.shadowRoot!.textContent!.replace(/\s+/g, ' ').trim()).to.equal(
      'Nightly review → Codex CLI · example-model → Work laptop · laptop.example.com · macOS'
    );
  });

  it('formats time limits', () => {
    expect(formatTimeLimit(30)).to.equal('30s');
    expect(formatTimeLimit(900)).to.equal('15m');
    expect(formatTimeLimit(7200)).to.equal('2h');
  });
});
