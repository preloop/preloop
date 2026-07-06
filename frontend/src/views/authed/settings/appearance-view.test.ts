import { html, fixture, expect } from '@open-wc/testing';

import '../../../components/view-header.ts';
import './appearance-view';
import type { AppearanceView } from './appearance-view';

describe('AppearanceView', () => {
  afterEach(() => {
    localStorage.clear();
  });

  it('renders the appearance header and theme options', async () => {
    const element = (await fixture(
      html`<appearance-view></appearance-view>`
    )) as AppearanceView;
    await element.updateComplete;

    const header = element.shadowRoot?.querySelector('view-header');
    expect(header?.getAttribute('headerText')).to.equal('Appearance');
    const buttons = element.shadowRoot?.querySelectorAll('sl-radio-button');
    expect(buttons?.length).to.equal(3);
  });

  it('defaults to the system theme when none stored', async () => {
    localStorage.removeItem('theme');
    const element = (await fixture(
      html`<appearance-view></appearance-view>`
    )) as AppearanceView;
    await element.updateComplete;

    expect((element as any).selectedTheme).to.equal('system');
  });

  it('reads a previously stored theme', async () => {
    localStorage.setItem('theme', 'dark');
    const element = (await fixture(
      html`<appearance-view></appearance-view>`
    )) as AppearanceView;
    await element.updateComplete;

    expect((element as any).selectedTheme).to.equal('dark');
  });

  it('persists and broadcasts a theme change', async () => {
    const element = (await fixture(
      html`<appearance-view></appearance-view>`
    )) as AppearanceView;
    await element.updateComplete;

    let dispatched: string | null = null;
    const listener = (e: Event) => {
      dispatched = (e as CustomEvent).detail.theme;
    };
    window.addEventListener('theme-change', listener);

    const group = element.shadowRoot?.querySelector('sl-radio-group') as any;
    group.value = 'light';
    (element as any).handleThemeChange({
      target: group,
    } as unknown as CustomEvent);
    await element.updateComplete;

    window.removeEventListener('theme-change', listener);

    expect((element as any).selectedTheme).to.equal('light');
    expect(localStorage.getItem('theme')).to.equal('light');
    expect(dispatched).to.equal('light');
  });
});
