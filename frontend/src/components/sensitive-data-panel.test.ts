/**
 * The Sensitive data tab: the form drives the generated YAML, a stored
 * policy renders back into the form, the test box shows what the API found
 * and what would be stored, and a bad JSON path stops Save inline.
 */
import { expect, fixture, html, waitUntil } from '@open-wc/testing';
import sinon from 'sinon';
import { parse } from 'yaml';
import './sensitive-data-panel.ts';
import type { SensitiveDataPanel } from './sensitive-data-panel';

const TYPES = {
  types: [
    {
      id: 'email',
      label: 'Email address',
      description: 'Mailbox addresses.',
      example: 'a@example.com',
      locales: [],
      checksum: false,
      builtin: true,
    },
    {
      id: 'iban',
      label: 'IBANs',
      description: 'International bank account numbers.',
      example: 'DE89 3704 0044 0532 0130 00',
      locales: [],
      checksum: true,
      builtin: true,
    },
  ],
  default_types: ['email', 'iban'],
};

const POLICY = `version: "1.0"
sensitive_data:
  later_option: keep
  rules:
    - id: console-deny
      on: [tool.args]
      types: [iban]
      action: deny
  reference_only:
    - id: patient-tools
      scope: {tools: [get_patient_record]}
      keep_fields: ["$.consent_id"]
      approver_view: redacted
`;

function json(body: unknown, status = 200) {
  return new Response(JSON.stringify(body), {
    status,
    headers: { 'Content-Type': 'application/json' },
  });
}

describe('sensitive-data-panel', () => {
  let sandbox: sinon.SinonSandbox;
  let fetchStub: sinon.SinonStub;

  beforeEach(() => {
    sandbox = sinon.createSandbox();
    localStorage.setItem('accessToken', 'test-access-token');
    localStorage.setItem('refreshToken', 'test-refresh-token');
    fetchStub = sandbox.stub(window, 'fetch');
    fetchStub.callsFake(async (input: RequestInfo | URL) => {
      const url = String(input);
      if (url.includes('/sensitive-data/types')) return json(TYPES);
      return json({}, 404);
    });
  });

  afterEach(() => {
    sandbox.restore();
    localStorage.removeItem('accessToken');
    localStorage.removeItem('refreshToken');
  });

  async function mount(policyYaml = 'version: "1.0"\n') {
    const el = await fixture<SensitiveDataPanel>(
      html`<sensitive-data-panel
        .policyYaml=${policyYaml}
      ></sensitive-data-panel>`
    );
    await waitUntil(() => el.shadowRoot!.querySelector('[data-type="email"]'));
    return el;
  }

  const $ = <T extends Element>(el: SensitiveDataPanel, selector: string) =>
    el.shadowRoot!.querySelector<T>(selector)!;

  function generated(el: SensitiveDataPanel) {
    return parse($(el, '[data-testid="sensitive-yaml"]').textContent || '');
  }

  it('turns a checked type and action into the YAML block', async () => {
    const el = await mount();
    $<HTMLInputElement>(el, '#type-email').click();
    await el.updateComplete;
    const redact = $<HTMLInputElement>(
      el,
      'input[name="action-email"][value="redact"]'
    );
    redact.click();
    await el.updateComplete;
    expect(generated(el)).to.deep.equal({
      sensitive_data: {
        rules: [
          {
            id: 'console-redact',
            on: ['model.request', 'model.response', 'tool.args', 'tool.result'],
            types: ['email'],
            action: 'redact',
          },
        ],
      },
    });
    expect($(el, '[data-testid="sensitive-summary"]').textContent).to.contain(
      'Email address matches are stored as [REDACTED:email].'
    );
  });

  it('renders a stored policy and writes it back unchanged', async () => {
    const el = await mount(POLICY);
    expect($<HTMLInputElement>(el, '#type-iban').checked).to.equal(true);
    expect($<HTMLInputElement>(el, '#type-email').checked).to.equal(false);
    expect(
      $<HTMLInputElement>(el, 'input[name="action-iban"][value="deny"]').checked
    ).to.equal(true);
    expect(
      $<HTMLInputElement>(el, 'input[aria-label="Field to keep 1"]').value
    ).to.equal('$.consent_id');
    expect(generated(el)).to.deep.equal(
      parse(POLICY).sensitive_data && {
        sensitive_data: parse(POLICY).sensitive_data,
      }
    );
    expect(
      el.shadowRoot!.querySelector('[data-testid="sensitive-diff"]')
    ).to.equal(null);
  });

  it('saves the full policy with only sensitive_data changed', async () => {
    const el = await mount(POLICY);
    const saved = new Promise<string>((resolve) =>
      el.addEventListener('sensitive-data-save', (e) =>
        resolve((e as CustomEvent).detail.yaml)
      )
    );
    $<HTMLInputElement>(el, '#type-email').click();
    await el.updateComplete;
    $<HTMLButtonElement>(el, '[data-testid="sensitive-save"]').click();
    const doc = parse(await saved);
    expect(doc.version).to.equal('1.0');
    expect(doc.sensitive_data.later_option).to.equal('keep');
    expect(doc.sensitive_data.rules.map((r: any) => r.id)).to.deep.equal([
      'console-deny',
      'console-notify',
    ]);
  });

  it('flags an invalid JSON path inline and does not save', async () => {
    const el = await mount(POLICY);
    const spy = sinon.spy();
    el.addEventListener('sensitive-data-save', spy);
    const input = $<HTMLInputElement>(
      el,
      'input[aria-label="Field to keep 1"]'
    );
    input.value = 'consent id';
    input.dispatchEvent(new Event('input'));
    await el.updateComplete;
    const fresh = $<HTMLInputElement>(
      el,
      'input[aria-label="Field to keep 1"]'
    );
    expect(fresh.getAttribute('aria-invalid')).to.equal('true');
    expect(el.shadowRoot!.textContent).to.contain(
      'Use a path like $.consent_id'
    );
    $<HTMLButtonElement>(el, '[data-testid="sensitive-save"]').click();
    await el.updateComplete;
    expect(spy.called).to.equal(false);
  });

  it('renders the spans the test API returns and the stored form', async () => {
    fetchStub.callsFake(
      async (input: RequestInfo | URL, init?: RequestInit) => {
        const url = String(input);
        if (url.includes('/sensitive-data/types')) return json(TYPES);
        if (url.includes('/sensitive-data/test')) {
          expect(JSON.parse(String(init?.body)).types).to.deep.equal(['email']);
          return json({
            matches: [{ type: 'email', start: 5, end: 18, confidence: 1 }],
            types_found: ['email'],
            count: 1,
            redacted_preview: 'mail [REDACTED:email]',
          });
        }
        return json({}, 404);
      }
    );
    const el = await mount();
    $<HTMLInputElement>(el, '#type-email').click();
    await el.updateComplete;
    $<HTMLInputElement>(
      el,
      'input[name="action-email"][value="redact"]'
    ).click();
    const textarea = $<HTMLTextAreaElement>(el, '#sd-test-text');
    textarea.value = 'mail a@example.com';
    textarea.dispatchEvent(new Event('input'));
    $<HTMLButtonElement>(el, '[data-testid="sensitive-test"]').click();
    await waitUntil(() =>
      el.shadowRoot!.querySelector('[data-testid="sensitive-test-result"]')
    );
    const mark = $(el, 'mark[data-type="email"]');
    expect(mark.textContent).to.contain('a@example.com');
    expect($(el, '[data-testid="sensitive-stored"]').textContent).to.equal(
      'mail [REDACTED:email]'
    );
  });

  it('shows the empty and error states of the test box', async () => {
    let fail = false;
    fetchStub.callsFake(async (input: RequestInfo | URL) => {
      const url = String(input);
      if (url.includes('/sensitive-data/types')) return json(TYPES);
      if (fail) return json({ detail: 'Unknown PII types' }, 422);
      return json({ matches: [], types_found: [], count: 0 });
    });
    const el = await mount();
    $<HTMLButtonElement>(el, '[data-testid="sensitive-test"]').click();
    await el.updateComplete;
    expect(
      $(el, '[data-testid="sensitive-test-error"]').textContent
    ).to.contain('Paste some sample text first.');
    const textarea = $<HTMLTextAreaElement>(el, '#sd-test-text');
    // Same cap as the test endpoint, so a long paste never earns a 422.
    expect(textarea.maxLength).to.equal(20000);
    textarea.value = 'nothing here';
    textarea.dispatchEvent(new Event('input'));
    $<HTMLButtonElement>(el, '[data-testid="sensitive-test"]').click();
    await waitUntil(() =>
      el.shadowRoot!.querySelector('[data-testid="sensitive-test-empty"]')
    );
    fail = true;
    $<HTMLButtonElement>(el, '[data-testid="sensitive-test"]').click();
    await waitUntil(() =>
      el.shadowRoot!.querySelector('[data-testid="sensitive-test-error"]')
    );
    expect(
      $(el, '[data-testid="sensitive-test-error"]').textContent
    ).to.contain('Unknown PII types');
  });

  it('moves the action when a selected custom pattern is renamed', async () => {
    const el = await mount();
    const buttons = Array.from(
      el.shadowRoot!.querySelectorAll<HTMLButtonElement>('button')
    );
    buttons.find((b) => b.textContent!.trim() === 'Add pattern')!.click();
    await el.updateComplete;
    const nameInput = () =>
      $<HTMLInputElement>(el, 'input[placeholder="employee_id"]');
    nameInput().value = 'emp';
    nameInput().dispatchEvent(new Event('input'));
    const regex = el.shadowRoot!.querySelectorAll<HTMLInputElement>(
      '.entry input[type="text"]'
    )[1];
    regex.value = 'E\\d+';
    regex.dispatchEvent(new Event('input'));
    await el.updateComplete;
    $<HTMLInputElement>(el, '#type-emp').click();
    await el.updateComplete;
    nameInput().value = 'employee_code';
    nameInput().dispatchEvent(new Event('input'));
    await el.updateComplete;
    const block = generated(el).sensitive_data;
    expect(block.rules[0].types).to.deep.equal(['employee_code']);
    expect(el.shadowRoot!.querySelector('#type-emp')).to.equal(null);
    expect($<HTMLInputElement>(el, '#type-employee_code').checked).to.equal(
      true
    );
  });

  it('labels every form control', async () => {
    const el = await mount(POLICY);
    $<HTMLInputElement>(el, '#type-email').click();
    await el.updateComplete;
    const controls = el.shadowRoot!.querySelectorAll(
      'input, select, textarea, button'
    );
    for (const control of Array.from(controls)) {
      const labelled =
        control.getAttribute('aria-label') ||
        control.closest('label') ||
        (control.id &&
          el.shadowRoot!.querySelector(`label[for="${control.id}"]`)) ||
        (control.tagName === 'BUTTON' && control.textContent!.trim());
      expect(!!labelled, control.outerHTML).to.equal(true);
    }
  });
});
