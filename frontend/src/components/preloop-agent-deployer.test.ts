import { html, fixture, expect } from '@open-wc/testing';
import sinon from 'sinon';

import './preloop-agent-deployer';
import type { PreloopAgentDeployer } from './preloop-agent-deployer';

/**
 * The deployer is the body of the "Deploy Governed Agent" dialog and of the
 * deploy path inside the onboarding wizard. It had no tests at all, so this
 * file pins what the redesign relies on: the step counter (including the
 * offset the wizard hands it), one primary action per step, the review block
 * before an irreversible action, the back routing between substeps, and the
 * phone layout.
 */
describe('PreloopAgentDeployer', () => {
  let fetchStub: sinon.SinonStub;

  const MODELS = [
    {
      id: 'm1',
      name: 'GPT-4o',
      provider_name: 'openai',
      model_kind: 'llm',
      model_identifier: 'gpt-4o',
    },
    {
      id: 'm2',
      name: 'Claude Sonnet',
      provider_name: 'anthropic',
      model_kind: 'llm',
      model_identifier: 'claude-sonnet',
    },
  ];

  beforeEach(() => {
    localStorage.setItem('accessToken', 'test-access-token');
    fetchStub = sinon.stub(window, 'fetch');
    fetchStub.callsFake(
      async () =>
        new Response(JSON.stringify(MODELS), {
          status: 200,
          headers: { 'Content-Type': 'application/json' },
        })
    );
  });

  afterEach(() => {
    fetchStub.restore();
    localStorage.clear();
  });

  const PHONE_WIDTH = 390;

  async function mount(
    attrs: { hideBack?: boolean; stepOffset?: number; width?: number } = {}
  ): Promise<PreloopAgentDeployer> {
    const host = (await fixture(html`
      <div style="width: ${attrs.width || 900}px;">
        <preloop-agent-deployer
          ?hide-back-button=${attrs.hideBack ?? false}
          .stepOffset=${attrs.stepOffset ?? 0}
          .aiModels=${MODELS}
          .isEnterprise=${true}
          .isAdmin=${true}
        ></preloop-agent-deployer>
      </div>
    `)) as HTMLElement;
    const el = host.querySelector(
      'preloop-agent-deployer'
    ) as PreloopAgentDeployer;
    await el.updateComplete;
    return el;
  }

  function stepText(el: PreloopAgentDeployer): string {
    return (
      el.shadowRoot?.querySelector('.wizard-step-count')?.textContent || ''
    )
      .replace(/\s+/g, ' ')
      .trim();
  }

  function cardByText(
    el: PreloopAgentDeployer,
    text: string
  ): HTMLElement | undefined {
    return (
      Array.from(
        el.shadowRoot?.querySelectorAll('.wizard-option-button') || []
      ) as HTMLElement[]
    ).find((b) => b.textContent?.includes(text));
  }

  async function goTo(
    el: PreloopAgentDeployer,
    step: string
  ): Promise<PreloopAgentDeployer> {
    (el as any).deploySubStep = step;
    el.requestUpdate();
    await el.updateComplete;
    return el;
  }

  it('numbers its own steps and draws a rail only where the total is known', async () => {
    const el = await mount({ hideBack: true });
    expect(stepText(el)).to.equal('Step 1');
    expect(el.shadowRoot?.querySelector('.wizard-step-rail')).to.equal(null);

    cardByText(el, 'Deploy on an existing host')!.click();
    await el.updateComplete;
    expect((el as any).deploySubStep).to.equal('existing-host-method');
    expect(stepText(el)).to.equal('Step 2');

    cardByText(el, 'SSH access')!.click();
    await el.updateComplete;
    expect((el as any).deploySubStep).to.equal('ssh-config');
    expect(stepText(el)).to.contain('Step 3 of 3');
    const rail = el.shadowRoot?.querySelector(
      '.wizard-step-rail'
    ) as HTMLElement;
    expect(rail.children.length).to.equal(3);
    expect(rail.querySelectorAll('.done').length).to.equal(3);
  });

  it('continues the wizard count when the wizard hands over mid-path', async () => {
    // The wizard reaches the deployer after choose + deploy-type, so the first
    // deployer screen is step 3, not step 1 all over again.
    const el = await mount({ stepOffset: 2 });
    expect(stepText(el)).to.equal('Step 3');
    await goTo(el, 'ssh-config');
    expect(stepText(el)).to.contain('Step 5 of 5');
  });

  it('hides Back on the first step when the host owns the exit', async () => {
    const hidden = await mount({ hideBack: true });
    expect(hidden.shadowRoot?.querySelector('.wizard-back')).to.equal(null);

    const shown = await mount({ hideBack: false });
    expect(shown.shadowRoot?.querySelector('.wizard-back')).to.exist;

    let cancelled = false;
    shown.addEventListener('deploy-cancel', () => (cancelled = true));
    (shown.shadowRoot?.querySelector('.wizard-back') as HTMLElement).click();
    await shown.updateComplete;
    expect(cancelled, 'first-step Back cancels').to.equal(true);
  });

  it('routes Back from a leaf step to the branch above it', async () => {
    const el = await mount({ hideBack: true });
    await goTo(el, 'ssh-config');
    (el.shadowRoot?.querySelector('.wizard-back') as HTMLElement).click();
    await el.updateComplete;
    expect((el as any).deploySubStep).to.equal('existing-host-method');

    await goTo(el, 'cli-install');
    (el.shadowRoot?.querySelector('.wizard-back') as HTMLElement).click();
    await el.updateComplete;
    expect((el as any).deploySubStep).to.equal('existing-host-method');

    await goTo(el, 'fresh-vm-premium');
    (el.shadowRoot?.querySelector('.wizard-back') as HTMLElement).click();
    await el.updateComplete;
    expect((el as any).deploySubStep).to.equal('agent-host');
  });

  it('keeps one primary action per step and blocks deploy until SSH is complete', async () => {
    const el = await mount({ hideBack: true });
    await goTo(el, 'ssh-config');

    const actions = el.shadowRoot?.querySelector(
      '.wizard-actions'
    ) as HTMLElement;
    const primaries = actions.querySelectorAll('sl-button[variant="primary"]');
    expect(primaries.length).to.equal(1);
    expect(primaries[0].textContent?.trim()).to.equal('Deploy agent');
    expect(
      (primaries[0] as HTMLElement & { disabled: boolean }).disabled
    ).to.equal(true);

    (el as any).sshHost = '192.168.1.100';
    (el as any).sshUsername = 'ubuntu';
    (el as any).sshPassword = 'secret';
    el.requestUpdate();
    await el.updateComplete;
    const enabled = el.shadowRoot?.querySelector(
      '.wizard-actions sl-button[variant="primary"]'
    ) as HTMLElement & { disabled: boolean };
    expect(enabled.disabled).to.equal(false);
  });

  it('summarises the deployment before the button that starts it', async () => {
    const el = await mount({ hideBack: true });
    (el as any).sshHost = '192.168.1.100';
    (el as any).sshUsername = 'ubuntu';
    (el as any).deployModel = 'm1';
    await goTo(el, 'ssh-config');

    const summary = (
      el.shadowRoot?.querySelector('.wizard-summary')?.textContent || ''
    ).replace(/\s+/g, ' ');
    expect(summary).to.contain('ubuntu@192.168.1.100:22');
    expect(summary).to.contain('GPT-4o');
    expect(summary).to.contain('Password');

    await goTo(el, 'fresh-vm-premium');
    const vmSummary = (
      el.shadowRoot?.querySelector('.wizard-summary')?.textContent || ''
    ).replace(/\s+/g, ' ');
    expect(vmSummary).to.contain('Standard (2 vCPU, 4GB RAM)');
    expect(vmSummary).to.contain('VNC');
  });

  it('renders the provisioning log from theme tokens and finishes with one action', async () => {
    const el = await mount({ hideBack: true });
    (el as any).isBooting = true;
    (el as any).bootLogs = [
      '[ssh] Connecting to target host...',
      'SUCCESS: Persistent governed agent node successfully activated via SSH!',
    ];
    el.requestUpdate();
    await el.updateComplete;

    const log = el.shadowRoot?.querySelector('.boot-log') as HTMLElement;
    expect(log, 'log block').to.exist;
    expect(log.getAttribute('role')).to.equal('log');
    expect(log.querySelectorAll('.boot-log-line').length).to.equal(2);
    expect(log.querySelectorAll('.boot-log-line.success').length).to.equal(1);
    // No inline colour literals survive on the log or its lines.
    expect(log.getAttribute('style')).to.equal(null);

    let done = false;
    el.addEventListener('deploy-wizard-done', () => (done = true));
    const finish = el.shadowRoot?.querySelector(
      '.wizard-actions sl-button[variant="primary"]'
    ) as HTMLElement;
    expect(finish.textContent?.trim()).to.equal('View the connected agent');
    finish.click();
    await el.updateComplete;
    expect(done).to.equal(true);
  });

  it('does not overflow horizontally at 390px on any step', async () => {
    const steps = [
      'agent-host',
      'existing-host-method',
      'cli-install',
      'ssh-config',
      'fresh-vm-premium',
    ];
    for (const step of steps) {
      const el = await mount({ hideBack: true, width: PHONE_WIDTH });
      await goTo(el, step);
      const shell = el.shadowRoot?.querySelector(
        '.wizard-shell'
      ) as HTMLElement;
      expect(shell.scrollWidth, `${step}: shell`).to.be.at.most(
        PHONE_WIDTH + 1
      );
      const right = el.getBoundingClientRect().right;
      Array.from(el.shadowRoot?.querySelectorAll('*') || []).forEach((node) => {
        const rect = (node as HTMLElement).getBoundingClientRect();
        if (rect.width === 0) {
          return;
        }
        expect(
          Math.round(rect.right),
          `${step}: ${(node as HTMLElement).className || node.tagName}`
        ).to.be.at.most(Math.round(right) + 1);
      });
      Array.from(
        el.shadowRoot?.querySelectorAll('.command-code') || []
      ).forEach((code) => {
        const box = code as HTMLElement;
        expect(box.scrollWidth, `${step}: command block`).to.be.at.most(
          box.clientWidth + 1
        );
      });
    }
  });
});
