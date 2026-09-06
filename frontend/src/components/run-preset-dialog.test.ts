import { expect, aTimeout } from '@open-wc/testing';
import sinon from 'sinon';

import {
  openRunPresetDialog,
  resetRunPresetDialogForTests,
  type RunPresetDialog,
} from './run-preset-dialog';

const issueId = '22222222-2222-2222-2222-222222222222';

function dialogElement(): RunPresetDialog {
  const element = document.body.querySelector(
    'run-preset-dialog'
  ) as RunPresetDialog | null;
  expect(element, 'run-preset-dialog singleton is mounted').to.exist;
  return element as RunPresetDialog;
}

async function clickFooter(label: string) {
  const element = dialogElement();
  await element.updateComplete;
  const button = [
    ...(element.shadowRoot?.querySelectorAll('sl-button') || []),
  ].find((candidate) => candidate.textContent?.trim() === label);
  expect(button, `footer button "${label}" exists`).to.exist;
  (button as HTMLElement).click();
}

describe('RunPresetDialog', () => {
  let fetchStub: sinon.SinonStub;

  beforeEach(() => {
    localStorage.setItem('accessToken', 'test-access-token');
  });

  afterEach(() => {
    fetchStub?.restore();
    resetRunPresetDialogForTests();
    document.body
      .querySelectorAll('sl-alert')
      .forEach((alert) => alert.remove());
    localStorage.clear();
  });

  it('first run shows create copy and sends confirm_create', async () => {
    const bodies: unknown[] = [];
    fetchStub = sinon.stub(window, 'fetch').callsFake(async (input, init) => {
      const url = String(input);
      if (!url.includes('/api/v1/flows/run-preset')) {
        return new Response('{}', { status: 200 });
      }
      const parsed = JSON.parse(String(init?.body || '{}'));
      bodies.push(parsed);
      if (!parsed.confirm_create) {
        return new Response(
          JSON.stringify({
            detail: {
              code: 'flow_missing',
              flow_name: 'Automated Issue Implementation',
            },
          }),
          { status: 409 }
        );
      }
      return new Response(
        JSON.stringify({
          execution_id: 'exec-1',
          flow_id: 'flow-1',
          flow_name: 'Automated Issue Implementation',
          flow_created: true,
          execution_url: '/console/flows/executions/exec-1',
        }),
        { status: 200 }
      );
    });

    void openRunPresetDialog({
      presetSlug: 'automated-issue-implementation',
      target: { kind: 'issue', issue_id: issueId },
      issueKey: 'ALP-9',
    });
    await aTimeout(20);
    const element = dialogElement();
    await element.updateComplete;

    const createDialog = element.shadowRoot?.querySelector('sl-dialog');
    expect(createDialog?.getAttribute('label')).to.equal(
      'Create the implementer flow?'
    );
    expect(element.shadowRoot?.textContent).to.contain(
      'This account has no Automated Issue Implementation flow yet.'
    );
    expect(element.shadowRoot?.textContent).to.contain('then run it on ALP-9');

    await clickFooter('Create and run');
    await aTimeout(20);
    expect(bodies.length).to.equal(2);
    expect((bodies[0] as { confirm_create: boolean }).confirm_create).to.equal(
      false
    );
    expect((bodies[1] as { confirm_create: boolean }).confirm_create).to.equal(
      true
    );
  });

  it('existing flow shows run copy', async () => {
    fetchStub = sinon.stub(window, 'fetch').callsFake(async () => {
      return new Response(
        JSON.stringify({
          execution_id: null,
          flow_id: 'flow-1',
          flow_name: 'Automated Issue Implementation',
          flow_created: false,
          execution_url: null,
        }),
        { status: 200 }
      );
    });

    void openRunPresetDialog({
      presetSlug: 'automated-issue-implementation',
      target: { kind: 'issue', issue_id: issueId },
      issueKey: 'ALP-9',
    });
    await aTimeout(20);
    const element = dialogElement();
    await element.updateComplete;

    const runDialog = element.shadowRoot?.querySelector('sl-dialog');
    expect(runDialog?.getAttribute('label')).to.equal(
      'Run Automated Issue Implementation on ALP-9?'
    );
    expect(runDialog?.getAttribute('label')).to.not.contain(
      'Create the implementer flow?'
    );
    const run = [
      ...(element.shadowRoot?.querySelectorAll('sl-button') || []),
    ].find((candidate) => candidate.textContent?.trim() === 'Run');
    expect(run).to.exist;
  });

  it('422 renders model alert', async () => {
    fetchStub = sinon.stub(window, 'fetch').callsFake(async (input, init) => {
      const url = String(input);
      if (!url.includes('/api/v1/flows/run-preset')) {
        return new Response('{}', { status: 200 });
      }
      const parsed = JSON.parse(String(init?.body || '{}'));
      if (!parsed.confirm_create) {
        return new Response(
          JSON.stringify({
            execution_id: null,
            flow_id: 'flow-1',
            flow_name: 'Automated Issue Implementation',
            flow_created: false,
            execution_url: null,
          }),
          { status: 200 }
        );
      }
      return new Response(
        JSON.stringify({
          detail:
            "This preset runs on the 'codex' agent, which needs an AI model",
        }),
        { status: 422 }
      );
    });

    void openRunPresetDialog({
      presetSlug: 'automated-issue-implementation',
      target: { kind: 'issue', issue_id: issueId },
      issueKey: 'ALP-9',
    });
    await aTimeout(20);
    await clickFooter('Run');
    await aTimeout(20);
    const element = dialogElement();
    await element.updateComplete;

    const alert = element.shadowRoot?.querySelector('sl-alert');
    expect(alert).to.exist;
    expect(alert?.getAttribute('variant')).to.equal('warning');
    expect(element.shadowRoot?.textContent?.replace(/\s+/g, ' ')).to.contain(
      'No usable AI model for this preset. Add one under Models, then try again.'
    );
    const models = element.shadowRoot?.querySelector(
      'sl-button[href="/console/ai-models"]'
    );
    expect(models).to.exist;
    expect(models?.textContent).to.contain('Models');
  });

  it('batch triage probe posts targets and uses triage copy', async () => {
    const bodies: unknown[] = [];
    fetchStub = sinon.stub(window, 'fetch').callsFake(async (input, init) => {
      const url = String(input);
      if (!url.includes('/api/v1/flows/run-preset')) {
        return new Response('{}', { status: 200 });
      }
      const parsed = JSON.parse(String(init?.body || '{}'));
      bodies.push(parsed);
      return new Response(
        JSON.stringify({
          execution_id: null,
          flow_id: 'flow-1',
          flow_name: 'Issue Triage Assistant',
          flow_created: false,
          execution_url: null,
        }),
        { status: 200 }
      );
    });

    await openRunPresetDialog({
      presetSlug: 'issue-triage-assistant',
      targets: [
        { kind: 'issue', issue_id: issueId },
        { kind: 'issue', issue_id: '33333333-3333-3333-3333-333333333333' },
      ],
      issueKey: '2 issues',
      role: 'triage',
    });
    const element = dialogElement();
    await element.updateComplete;
    const dialog = element.shadowRoot?.querySelector('sl-dialog');
    expect(dialog?.getAttribute('label')).to.equal(
      'Run Issue Triage Assistant on 2 issues?'
    );
    expect(bodies[0]).to.deep.include({
      preset_slug: 'issue-triage-assistant',
      confirm_create: false,
    });
    expect((bodies[0] as { targets: unknown }).targets).to.have.length(2);
    expect((bodies[0] as { target?: unknown }).target).to.equal(undefined);
  });
  for (const anyCreated of [false, true]) {
    it(`shows batch failures and preserves existing run links (created=${anyCreated})`, async () => {
      const results = [
        ...(anyCreated
          ? [
              {
                issue_id: issueId,
                execution_id: 'existing-run',
                execution_status: 'PENDING',
                execution_url: '/console/flows/executions/existing-run',
                error: 'View the existing run before retrying.',
              },
            ]
          : []),
        {
          issue_id: 'missing',
          error: 'Issue not found <script>unsafe</script>',
        },
      ];
      fetchStub = sinon
        .stub(window, 'fetch')
        .callsFake(async (_input, init) => {
          const confirm = JSON.parse(String(init?.body || '{}')).confirm_create;
          return new Response(
            JSON.stringify({
              execution_id: anyCreated ? 'existing-run' : null,
              execution_url: anyCreated
                ? '/console/flows/executions/existing-run'
                : null,
              flow_id: 'triage-flow',
              flow_name: 'Issue Triage Assistant',
              flow_created: false,
              results: confirm ? results : [],
            }),
            { status: 200 }
          );
        });
      await openRunPresetDialog({
        presetSlug: 'issue-triage-assistant',
        targets: results.map((item) => ({
          kind: 'issue',
          issue_id: item.issue_id,
        })),
        issueKey: 'selected issues',
        role: 'triage',
      });
      await clickFooter('Run');
      await aTimeout(30);
      const alert = document.body.querySelector('sl-alert');
      expect(alert).to.exist;
      expect(alert?.getAttribute('variant')).to.equal('warning');
      expect(alert?.textContent).to.contain(
        'Issue not found <script>unsafe</script>'
      );
      expect(alert?.querySelector('script')).not.to.exist;
      expect(alert?.textContent).not.to.contain('Run started');
      expect(alert?.textContent).to.contain(
        anyCreated ? '1 run created' : 'No runs were created'
      );
      if (anyCreated) expect(alert?.textContent).to.contain('View run');
    });
  }
});
