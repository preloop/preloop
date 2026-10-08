import { html, fixture, expect, waitUntil } from '@open-wc/testing';
import './subaccount-access-dialog';
import type { SubaccountAccessDialog } from './subaccount-access-dialog';
import {
  mockApi,
  signInForTest,
  type MockApi,
} from '../../../test-helpers/capability-api';
import { resetConfirmDialogForTests } from '../../../components/confirm-dialog';
import { answerConfirmDialog } from '../../../utils/test-confirm-dialog';

const SUBS = '/api/v1/accounts/acc-root/subaccounts';
const GRANTS = '/api/v1/accounts/acc-root/access-grants';

describe('subaccount-access-dialog', () => {
  let api: MockApi;
  beforeEach(() => signInForTest());
  afterEach(() => {
    api?.restore();
    localStorage.clear();
    resetConfirmDialogForTests();
  });

  async function open(grants: unknown[] = []) {
    api = mockApi({
      capabilities: ['account_hierarchy'],
      routes: [
        {
          path: SUBS,
          body: {
            items: [
              { id: 'sub-a', name: 'North' },
              { id: 'sub-b', name: 'South' },
            ],
          },
        },
        { path: GRANTS, body: { items: grants } },
        {
          method: 'POST',
          path: GRANTS,
          status: 201,
          body: (call) => ({ id: 'grant-new', ...(call.body as object) }),
        },
        {
          method: 'DELETE',
          path: /^\/api\/v1\/accounts\/acc-root\/access-grants\//,
          status: 204,
        },
      ],
    });
    const el = await fixture<SubaccountAccessDialog>(
      html`<subaccount-access-dialog></subaccount-access-dialog>`
    );
    await el.show({ type: 'user', id: 'user-1', label: 'Alice Example' });
    await el.updateComplete;
    return el;
  }

  const button = (el: SubaccountAccessDialog, label: string) =>
    [...el.shadowRoot!.querySelectorAll('sl-button')].find(
      (b) => b.textContent?.trim() === label
    ) as HTMLElement;

  async function chooseSelected(el: SubaccountAccessDialog) {
    const target = el.shadowRoot!.querySelector('#target') as HTMLElement & {
      value: string;
    };
    target.value = 'selected';
    target.dispatchEvent(new Event('sl-change'));
    await el.updateComplete;
  }

  it('grants access in selected subaccounts', async () => {
    const el = await open();
    await chooseSelected(el);
    const south = el.shadowRoot!.querySelector(
      'sl-checkbox[data-subaccount="sub-b"]'
    ) as HTMLElement & { checked: boolean };
    expect(south).to.exist;
    south.checked = true;
    south.dispatchEvent(new Event('sl-change'));
    await el.updateComplete;

    button(el, 'Grant access').click();
    await waitUntil(
      () => api.calls.some((c) => c.method === 'POST' && c.path === GRANTS),
      'no grant created'
    );
    const post = api.calls.find(
      (c) => c.method === 'POST' && c.path === GRANTS
    )!;
    expect(post.body).to.eql({
      subject_type: 'user',
      subject_id: 'user-1',
      level: 'read',
      target: 'selected',
      subaccount_ids: ['sub-b'],
    });
  });

  it('asks for a subaccount before granting in selected ones', async () => {
    const el = await open();
    await chooseSelected(el);
    button(el, 'Grant access').click();
    await el.updateComplete;
    expect(
      el.shadowRoot!.querySelector('[role="alert"]')?.textContent
    ).to.contain('Choose at least one subaccount');
    expect(
      api.calls.some((c) => c.method === 'POST' && c.path === GRANTS)
    ).to.equal(false);
  });

  it('keeps the grant when the revoke is cancelled', async () => {
    const el = await open([
      {
        id: 'grant-1',
        subject_type: 'user',
        subject_id: 'user-1',
        level: 'admin',
        target: 'all',
      },
    ]);
    await waitUntil(
      () => el.shadowRoot!.querySelector('[data-testid="subject-grants"]'),
      'grants did not load'
    );
    button(el, 'Revoke').click();
    const asked = await answerConfirmDialog(false);
    expect(asked).to.contain("Alice Example's admin access");
    await new Promise((r) => setTimeout(r, 50));
    expect(api.calls.some((c) => c.method === 'DELETE')).to.equal(false);
  });
});
