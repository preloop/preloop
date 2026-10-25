/**
 * The console has one page-title size, `--console-text-h1` (26px / 600,
 * DESIGN.md "Type scale"). Four pages drew their own: 28px/700, 36px/500,
 * 28px, 36px/700 (U-13).
 */
import { expect, fixture, html } from '@open-wc/testing';
import '../public/not-found-view';
import { NotFoundView } from '../public/not-found-view';
import { NotificationPreferencesView } from './notification-preferences-view';
import { ApprovalView } from './approval-view';

function cssText(styles: unknown): string {
  const list = Array.isArray(styles) ? styles.flat(Infinity) : [styles];
  return list
    .map((style) => (style as { cssText?: string })?.cssText || '')
    .join('\n');
}

describe('console page titles use the one h1 size (U-13)', () => {
  it('renders the not-found title at 26px / 600', async () => {
    const el = await fixture<NotFoundView>(
      html`<div style="--console-text-h1: 26px">
        <not-found-view></not-found-view>
      </div>`
    );
    const view = el.querySelector('not-found-view') as NotFoundView;
    await view.updateComplete;
    const h1 = view.shadowRoot!.querySelector('h1') as HTMLElement;
    expect(getComputedStyle(h1).fontSize).to.equal('26px');
    expect(getComputedStyle(h1).fontWeight).to.equal('600');
  });

  it('points the Notifications and approval titles at the token', () => {
    for (const view of [NotificationPreferencesView, ApprovalView]) {
      const text = cssText((view as unknown as { styles: unknown }).styles);
      expect(text).to.match(
        /\.header h1\s*\{[^}]*font-size:\s*var\(--console-text-h1/
      );
    }
  });
});

describe('list headers are sentence case (U-13)', () => {
  it('drops the uppercase header from the shared list table styles', async () => {
    const { listTableStyles } = await import('../../table/list-table-styles');
    const text = (listTableStyles as unknown as { cssText: string }).cssText;
    const sortButton = /\.sort-button\s*\{([^}]*)\}/.exec(text)?.[1] || '';
    expect(sortButton).to.not.contain('text-transform');
    expect(sortButton).to.contain('--console-text-meta');
  });
});

describe('audit rows are hairline rows (U-06)', () => {
  it('draws no filled box per audit group', async () => {
    const { AuditView } = await import('./audit-view');
    const list = (AuditView as unknown as { styles: unknown }).styles;
    const text = (Array.isArray(list) ? list.flat(Infinity) : [list])
      .map((style) => (style as { cssText?: string })?.cssText || '')
      .join('\n');
    const group = /\.timeline-group\s*\{([^}]*)\}/.exec(text)?.[1] || '';
    expect(group).to.not.contain('background');
    expect(group).to.contain('border-bottom');
    expect(group).to.contain('--console-hairline');
  });
});

describe('labels are soft chips, not paint (U-14)', () => {
  it('renders Built-in, Agent and the device platform without a solid pill', async () => {
    const sources = await Promise.all(
      [
        '/src/views/authed/settings/ai-models-view.ts',
        '/src/views/authed/settings/api-keys-view.ts',
        '/src/views/authed/notification-preferences-view.ts',
      ].map(async (path) => [path, await (await fetch(path)).text()] as const)
    );
    for (const [path, code] of sources) {
      // Every sl-badge in these views carries the chip recipe.
      const badges = code.match(/<sl-badge(?![^>]*class=)[^>]*>/g) || [];
      expect(badges, path).to.deep.equal([]);
    }
  });
});

describe('attention category chips are borderless (U-14)', () => {
  it('keeps the boxed strip chip style off the category chips', async () => {
    const { AttentionView } = await import('./attention-view');
    const list = (AttentionView as unknown as { styles: unknown }).styles;
    const text = (Array.isArray(list) ? list.flat(Infinity) : [list])
      .map((style) => (style as { cssText?: string })?.cssText || '')
      .join('\n');
    expect(text).to.not.match(/(^|\n)\s*\.chip\s*\{[^}]*border:\s*1px/);
  });
});
