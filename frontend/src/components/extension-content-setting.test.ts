import { expect, fixture, html, waitUntil } from '@open-wc/testing';
import './extension-content-setting';
import type { ExtensionContentSetting } from './extension-content-setting';
import type { ExtensionContentSettings } from '../extension-content-api';

const settings = (
  change: Partial<ExtensionContentSettings> = {}
): ExtensionContentSettings => ({
  collect_enabled: true,
  notice_pending: false,
  stored_contents: 7,
  stored_bytes: 1000,
  retention_days: 90,
  updated_by_user_id: null,
  updated_at: null,
  ...change,
});

async function mount(options: {
  admin?: boolean;
  confirm?: boolean;
  initial?: ExtensionContentSettings;
}): Promise<{
  el: ExtensionContentSetting;
  saved: boolean[];
  asked: string[];
}> {
  const saved: boolean[] = [];
  const asked: string[] = [];
  const el = await fixture<ExtensionContentSetting>(
    html`<extension-content-setting
      .loader=${async () => options.initial ?? settings()}
      .accessLoader=${async () => options.admin ?? true}
      .saver=${async (change: { collect_enabled?: boolean }) => {
        saved.push(change.collect_enabled as boolean);
        return settings({
          collect_enabled: change.collect_enabled,
          stored_contents: 0,
        });
      }}
      .confirm=${async (dialog: { message: string }) => {
        asked.push(dialog.message);
        return options.confirm ?? true;
      }}
    ></extension-content-setting>`
  );
  await waitUntil(() => el.shadowRoot?.querySelector('sl-switch'));
  return { el, saved, asked };
}

const flip = async (el: ExtensionContentSetting, checked: boolean) => {
  const control = el.shadowRoot!.querySelector('sl-switch') as HTMLElement & {
    checked: boolean;
    updateComplete: Promise<unknown>;
  };
  await control.updateComplete;
  control.checked = checked;
  control.dispatchEvent(new CustomEvent('sl-change', { bubbles: true }));
  return control;
};

describe('extension-content-setting', () => {
  it('is on by default and states redaction limits, readers and retention', async () => {
    const { el } = await mount({});
    const control = el.shadowRoot!.querySelector('sl-switch') as HTMLElement & {
      checked: boolean;
    };
    expect(control.checked).to.equal(true);
    expect(control.textContent).to.contain(
      'Collect extension content for review'
    );
    const copy = el.shadowRoot!.textContent!.replace(/\s+/g, ' ');
    expect(copy).to.contain('it is not a guarantee');
    expect(copy).to.contain('90 days after the last report');
    expect(copy).to.contain('people who can approve extensions');
    expect(copy).to.contain('7 content(s) stored');
  });

  // protects=turning collection off is confirmed and says what is deleted;
  // fails_when=stored content is deleted without a question.
  it('asks before turning off, naming how much is deleted', async () => {
    const { el, saved, asked } = await mount({});
    await flip(el, false);
    await waitUntil(() => saved.length === 1);
    expect(asked[0]).to.contain('7 stored content(s)');
    expect(asked[0]).to.contain('deleted now');
    expect(saved).to.deep.equal([false]);
    await el.updateComplete;
    expect(el.shadowRoot!.textContent).to.contain('devices send no content');
  });

  it('keeps the switch on when the question is declined', async () => {
    const { el, saved } = await mount({ confirm: false });
    const control = await flip(el, false);
    await new Promise((resolve) => setTimeout(resolve, 20));
    expect(saved).to.deep.equal([]);
    expect(control.checked).to.equal(true);
  });

  it('turns back on without a question', async () => {
    const { el, saved, asked } = await mount({
      initial: settings({ collect_enabled: false, stored_contents: 0 }),
    });
    await flip(el, true);
    await waitUntil(() => saved.length === 1);
    expect(saved).to.deep.equal([true]);
    expect(asked).to.deep.equal([]);
  });

  it('is read-only for someone who cannot manage account policies', async () => {
    const { el } = await mount({ admin: false });
    const control = el.shadowRoot!.querySelector('sl-switch') as HTMLElement & {
      disabled: boolean;
    };
    expect(control.disabled).to.equal(true);
    expect(el.shadowRoot!.textContent).to.contain(
      'Only an account admin can change this.'
    );
  });
});
