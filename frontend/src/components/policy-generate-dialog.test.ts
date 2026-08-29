import { html, fixture, expect, waitUntil } from '@open-wc/testing';
import sinon from 'sinon';

import './policy-generate-dialog';
import type { PolicyGenerateDialog } from './policy-generate-dialog';

describe('PolicyGenerateDialog', () => {
  let fetchStub: sinon.SinonStub;

  beforeEach(() => {
    localStorage.setItem('accessToken', 'test-access-token');
    localStorage.setItem('refreshToken', 'test-refresh-token');
    fetchStub = sinon.stub(window, 'fetch');
  });

  afterEach(() => {
    fetchStub.restore();
    localStorage.clear();
  });

  // ---------------------------------------------------------------------------
  // Helpers
  // ---------------------------------------------------------------------------

  function stubGenerate(
    yaml: string,
    warnings: string[] = [],
    currentYaml = 'version: "1.0"\nmetadata:\n  name: current'
  ) {
    fetchStub.callsFake(async (input: RequestInfo | URL) => {
      const url = typeof input === 'string' ? input : input.toString();
      if (url.endsWith('/api/v1/policies/generate')) {
        return new Response(JSON.stringify({ yaml, warnings }), {
          status: 200,
          headers: { 'Content-Type': 'application/json' },
        });
      }
      if (url.endsWith('/api/v1/policies/generate-from-audit')) {
        return new Response(JSON.stringify({ yaml, warnings }), {
          status: 200,
          headers: { 'Content-Type': 'application/json' },
        });
      }
      if (url.includes('/api/v1/policies/export')) {
        return new Response(currentYaml, {
          status: 200,
          headers: { 'Content-Type': 'application/x-yaml' },
        });
      }
      if (url.endsWith('/api/v1/policies/diff')) {
        return new Response(
          JSON.stringify({
            summary: '1 added',
            has_changes: yaml !== currentYaml,
          }),
          {
            status: 200,
            headers: { 'Content-Type': 'application/json' },
          }
        );
      }
      return new Response(JSON.stringify({}), {
        status: 200,
        headers: { 'Content-Type': 'application/json' },
      });
    });
  }

  function stubGenerateError(status: number, detail: string) {
    fetchStub.callsFake(
      async () =>
        new Response(JSON.stringify({ detail }), {
          status,
          headers: { 'Content-Type': 'application/json' },
        })
    );
  }

  // ---------------------------------------------------------------------------
  // Rendering
  // ---------------------------------------------------------------------------

  it('renders the dialog when open', async () => {
    stubGenerate('');
    const el = (await fixture(
      html`<policy-generate-dialog .open=${true}></policy-generate-dialog>`
    )) as PolicyGenerateDialog;
    await el.updateComplete;

    const dialog = el.shadowRoot!.querySelector('sl-dialog');
    expect(dialog).to.exist;
    expect(dialog!.getAttribute('label')).to.equal('Describe a change');
  });

  it('has prompt and audit tabs', async () => {
    stubGenerate('');
    const el = (await fixture(
      html`<policy-generate-dialog .open=${true}></policy-generate-dialog>`
    )) as PolicyGenerateDialog;
    await el.updateComplete;

    const tabs = el.shadowRoot!.querySelectorAll('sl-tab');
    const panels = Array.from(tabs).map((t) => t.getAttribute('panel'));
    expect(panels).to.include('prompt');
    expect(panels).to.include('audit');
  });

  it('generate button is disabled when prompt is empty', async () => {
    stubGenerate('');
    const el = (await fixture(
      html`<policy-generate-dialog .open=${true}></policy-generate-dialog>`
    )) as PolicyGenerateDialog;
    await el.updateComplete;

    const generateBtn = Array.from(
      el.shadowRoot!.querySelectorAll('sl-button')
    ).find((b) => b.textContent?.trim()?.includes('Generate'));
    expect(generateBtn).to.exist;
    expect(generateBtn!.hasAttribute('disabled')).to.be.true;
  });

  // ---------------------------------------------------------------------------
  // Prompt generation flow
  // ---------------------------------------------------------------------------

  it('generates policy from prompt successfully', async () => {
    const yamlContent = 'version: "1.0"\nmetadata:\n  name: test';
    stubGenerate(yamlContent, ['minor warning']);

    const el = (await fixture(
      html`<policy-generate-dialog .open=${true}></policy-generate-dialog>`
    )) as PolicyGenerateDialog;
    await el.updateComplete;

    // Set prompt text
    (el as any)._prompt = 'require approval for bash';
    el.requestUpdate();
    await el.updateComplete;

    // Click Generate
    const generateBtn = Array.from(
      el.shadowRoot!.querySelectorAll('sl-button')
    ).find((b) => b.textContent?.trim()?.includes('Generate'));
    generateBtn!.click();

    await waitUntil(
      () => (el as any)._generatedYaml !== '',
      'YAML was not generated'
    );
    await el.updateComplete;

    expect((el as any)._generatedYaml).to.equal(yamlContent);
    expect((el as any)._warnings).to.deep.equal(['minor warning']);

    const diff = el.shadowRoot!.querySelector('.yaml-diff pre');
    expect(diff).to.exist;
    expect(diff!.textContent).to.contain('--- a/policies.yaml');
    expect(
      el.shadowRoot!.querySelector('.yaml-preview pre')?.textContent
    ).to.equal(yamlContent);

    // Verify fetch was called with correct body
    const generateCall = fetchStub
      .getCalls()
      .find((c) => String(c.args[0]).endsWith('/api/v1/policies/generate'));
    expect(generateCall).to.exist;
    const body = JSON.parse(
      (generateCall!.args[1] as RequestInit).body as string
    );
    expect(body.prompt).to.equal('require approval for bash');
    expect(body.include_current_config).to.be.true;
  });

  // ---------------------------------------------------------------------------
  // Error handling
  // ---------------------------------------------------------------------------

  it('shows error alert when generation fails', async () => {
    stubGenerateError(400, 'No AI model configured');

    const el = (await fixture(
      html`<policy-generate-dialog .open=${true}></policy-generate-dialog>`
    )) as PolicyGenerateDialog;
    await el.updateComplete;

    (el as any)._prompt = 'deny all';
    el.requestUpdate();
    await el.updateComplete;

    const generateBtn = Array.from(
      el.shadowRoot!.querySelectorAll('sl-button')
    ).find((b) => b.textContent?.trim()?.includes('Generate'));
    generateBtn!.click();

    await waitUntil(() => (el as any)._error !== '', 'Error was not set');
    await el.updateComplete;

    expect((el as any)._error).to.include('No AI model configured');

    const errorAlert = el.shadowRoot!.querySelector(
      'sl-alert[variant="danger"]'
    );
    expect(errorAlert).to.exist;
  });

  // ---------------------------------------------------------------------------
  // Audit tab generation
  // ---------------------------------------------------------------------------

  it('generates policy from audit logs with date range', async () => {
    const yamlContent = 'version: "1.0"\nmetadata:\n  name: audit-based';
    stubGenerate(yamlContent);

    const el = (await fixture(
      html`<policy-generate-dialog .open=${true}></policy-generate-dialog>`
    )) as PolicyGenerateDialog;
    await el.updateComplete;

    // Switch to audit tab
    (el as any)._activeTab = 'audit';
    (el as any)._startDate = '2026-01-01';
    (el as any)._endDate = '2026-02-01';
    el.requestUpdate();
    await el.updateComplete;

    // Click Generate
    const generateBtn = Array.from(
      el.shadowRoot!.querySelectorAll('sl-button')
    ).find((b) => b.textContent?.trim()?.includes('Generate'));
    generateBtn!.click();

    await waitUntil(
      () => (el as any)._generatedYaml !== '',
      'YAML was not generated'
    );

    // Verify the audit endpoint was called
    const auditCall = fetchStub
      .getCalls()
      .find((c) =>
        String(c.args[0]).endsWith('/api/v1/policies/generate-from-audit')
      );
    expect(auditCall).to.exist;
    const body = JSON.parse((auditCall!.args[1] as RequestInit).body as string);
    expect(body.start_date).to.equal('2026-01-01');
    expect(body.end_date).to.equal('2026-02-01');
  });

  // ---------------------------------------------------------------------------
  // Apply policy event
  // ---------------------------------------------------------------------------

  it('dispatches policy-apply event when Save is clicked', async () => {
    const yamlContent = 'version: "1.0"';
    stubGenerate(yamlContent);

    const el = (await fixture(
      html`<policy-generate-dialog .open=${true}></policy-generate-dialog>`
    )) as PolicyGenerateDialog;
    await el.updateComplete;

    // Simulate a successful generation
    (el as any)._generatedYaml = yamlContent;
    el.requestUpdate();
    await el.updateComplete;

    // Capture the custom event
    let eventDetail: any = null;
    el.addEventListener('policy-apply', ((e: CustomEvent) => {
      eventDetail = e.detail;
    }) as EventListener);

    const applyBtn = Array.from(
      el.shadowRoot!.querySelectorAll('sl-button')
    ).find((b) => b.textContent?.trim() === 'Save');
    expect(applyBtn).to.exist;
    applyBtn!.click();

    expect(eventDetail).to.not.be.null;
    expect(eventDetail.yaml).to.equal(yamlContent);
  });

  // ---------------------------------------------------------------------------
  // Close resets state
  // ---------------------------------------------------------------------------

  it('resets state when dialog is closed', async () => {
    stubGenerate('');
    const el = (await fixture(
      html`<policy-generate-dialog .open=${true}></policy-generate-dialog>`
    )) as PolicyGenerateDialog;
    await el.updateComplete;

    // Set some state
    (el as any)._prompt = 'something';
    (el as any)._generatedYaml = 'version: "1.0"';
    (el as any)._error = 'some error';
    (el as any)._warnings = ['warn'];
    el.requestUpdate();
    await el.updateComplete;

    // Close the dialog
    (el as any)._handleClose();
    await el.updateComplete;

    expect((el as any)._prompt).to.equal('');
    expect((el as any)._generatedYaml).to.equal('');
    expect((el as any)._error).to.equal('');
    expect((el as any)._warnings).to.deep.equal([]);
    expect(el.open).to.be.false;
  });

  it('shows a unified YAML diff before Save and does not apply on Discard', async () => {
    const current = 'version: "1.0"\nmetadata:\n  name: current';
    const yamlContent = 'version: "1.0"\nmetadata:\n  name: edited';
    stubGenerate(yamlContent, [], current);

    const el = (await fixture(
      html`<policy-generate-dialog
        .open=${true}
        .currentYaml=${current}
      ></policy-generate-dialog>`
    )) as PolicyGenerateDialog;
    await el.updateComplete;

    let applied = false;
    el.addEventListener('policy-apply', () => {
      applied = true;
    });

    (el as any)._prompt = 'add a PII deny rule';
    el.requestUpdate();
    await el.updateComplete;
    const generateBtn = Array.from(
      el.shadowRoot!.querySelectorAll('sl-button')
    ).find((b) => b.textContent?.trim()?.includes('Generate'));
    generateBtn!.click();
    await waitUntil(
      () => (el as any)._unifiedDiff !== '',
      'diff was not computed'
    );
    await el.updateComplete;

    expect(
      el.shadowRoot!.querySelector('.yaml-diff pre')?.textContent
    ).to.contain('+  name: edited');
    const saveBtn = Array.from(
      el.shadowRoot!.querySelectorAll('sl-button')
    ).find((b) => b.textContent?.trim() === 'Save');
    expect(saveBtn).to.exist;

    const discardBtn = Array.from(
      el.shadowRoot!.querySelectorAll('sl-button')
    ).find((b) => b.textContent?.trim() === 'Discard');
    discardBtn!.click();
    await el.updateComplete;

    expect(applied).to.be.false;
    expect((el as any)._generatedYaml).to.equal('');
    expect(el.shadowRoot!.querySelector('.yaml-diff')).to.not.exist;
  });

  it('links to Models settings when no default model is configured', async () => {
    stubGenerateError(
      400,
      'No AI models configured. Add a default model at /console/ai-models'
    );
    const el = (await fixture(
      html`<policy-generate-dialog .open=${true}></policy-generate-dialog>`
    )) as PolicyGenerateDialog;
    await el.updateComplete;
    (el as any)._prompt = 'deny pii';
    el.requestUpdate();
    await el.updateComplete;
    const generateBtn = Array.from(
      el.shadowRoot!.querySelectorAll('sl-button')
    ).find((b) => b.textContent?.trim()?.includes('Generate'));
    generateBtn!.click();
    await waitUntil(() => (el as any)._error !== '', 'Error was not set');
    await el.updateComplete;
    const link = el.shadowRoot!.querySelector('a.models-link');
    expect(link?.getAttribute('href')).to.equal('/console/ai-models');
  });
});
