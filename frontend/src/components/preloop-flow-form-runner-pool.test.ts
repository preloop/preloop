import {
  expect,
  fixture,
  fixtureCleanup,
  html,
  oneEvent,
} from '@open-wc/testing';
import sinon, { SinonSandbox } from 'sinon';
import './preloop-flow-form.ts';
import type { PreloopFlowForm } from './preloop-flow-form';

let source: string;

before(async () => {
  const res = await fetch(
    new URL('./preloop-flow-form.ts', import.meta.url).href
  );
  expect(res.ok).to.be.true;
  source = await res.text();
});

describe('PreloopFlowForm runner pool field', () => {
  it('renders a runner pool select and custom label input', () => {
    expect(source).to.include('label="Runner pool"');
    expect(source).to.include('Or type a runner label');
    expect(source).to.include('renderRunnerPoolField()');
    expect(source).to.include('runner_pool: this.normalizedFlowRunnerPool()');
  });

  it('shows a next-execution hint from current online runners', () => {
    expect(source).to.include('describeNextRunnerPool');
    expect(source).to.include('runner-pool-hint');
  });
});

describe('PreloopFlowForm runner pool behaviour', () => {
  let sandbox: SinonSandbox;

  beforeEach(() => {
    localStorage.setItem('accessToken', 'test-access-token');
    localStorage.setItem('refreshToken', 'test-refresh-token');
    sandbox = sinon.createSandbox();
    sandbox.stub(window, 'fetch').callsFake(async (url: any) => {
      const target = String(url);
      if (target.includes('/api/v1/runners')) {
        return new Response(
          JSON.stringify([
            {
              id: '11111111-1111-4111-8111-111111111111',
              name: 'office-mac',
              labels: ['local'],
              status: 'online',
            },
          ])
        );
      }
      if (target.includes('/api/v1/account/details')) {
        return new Response(
          JSON.stringify({
            id: 'acct-1',
            organization_name: 'Example Org',
            default_runner_pool: null,
            created_at: '2026-09-04T00:00:00Z',
            updated_at: '2026-09-04T00:00:00Z',
          })
        );
      }
      if (target.includes('/api/v1/agents')) {
        return new Response(JSON.stringify({ items: [] }));
      }
      return new Response(JSON.stringify([]));
    });
  });

  afterEach(() => {
    fixtureCleanup();
    sandbox.restore();
    localStorage.clear();
  });

  const mount = async (flow: Record<string, unknown>) => {
    const element = await fixture<PreloopFlowForm>(
      html`<preloop-flow-form .flow=${flow}></preloop-flow-form>`
    );
    while ((element as any)._loadingReferenceData) {
      await new Promise((resolve) => setTimeout(resolve, 10));
    }
    await element.updateComplete;
    return element;
  };

  const submit = async (element: PreloopFlowForm) => {
    const submitted = oneEvent(element, 'flow-submit');
    void (element as any).handleFormSubmit(new Event('submit'));
    const event = await submitted;
    return event.detail.flow;
  };

  it('lists online runners and submits a selected pool', async () => {
    const element = await mount({
      id: 'flow-1',
      name: 'Review',
      prompt_template: 'review',
      agent_type: 'codex',
    });
    expect(element.shadowRoot?.textContent).to.contain(
      'Any online private runner (default)'
    );
    expect(element.shadowRoot?.textContent).to.contain('Preloop hosted');
    expect(element.shadowRoot?.textContent).to.contain('office-mac');
    expect(element.shadowRoot?.textContent).to.contain(
      'Next execution will use any online private runner (currently office-mac).'
    );

    const select = element.shadowRoot?.querySelector(
      'sl-select[label="Runner pool"]'
    ) as HTMLSelectElement;
    expect(select).to.exist;
    select.value = 'office-mac';
    select.dispatchEvent(new CustomEvent('sl-change'));
    await element.updateComplete;

    const payload = await submit(element);
    expect(payload.runner_pool).to.equal('office-mac');
  });

  it('submits a typed label and hosted sentinel', async () => {
    const element = await mount({
      id: 'flow-1',
      name: 'Review',
      prompt_template: 'review',
      agent_type: 'codex',
      runner_pool: 'server',
    });
    expect(element.shadowRoot?.textContent).to.contain(
      'Next execution will use Preloop hosted.'
    );

    const custom = element.shadowRoot?.querySelector(
      'sl-input[label="Or type a runner label"]'
    ) as HTMLInputElement;
    custom.value = 'gpu';
    custom.dispatchEvent(new CustomEvent('sl-input'));
    await element.updateComplete;

    const payload = await submit(element);
    expect(payload.runner_pool).to.equal('gpu');
  });
});
