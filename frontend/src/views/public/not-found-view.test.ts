import { expect, fixture, html } from '@open-wc/testing';

import './not-found-view';
import { notFoundAction, type NotFoundView } from './not-found-view';
import { pageTitle } from '../../utils/page-title';

describe('NotFoundView', () => {
  it('explains the miss and offers one way back', async () => {
    const el = (await fixture(
      html`<not-found-view></not-found-view>`
    )) as NotFoundView;

    expect(el.shadowRoot!.textContent).to.contain('Page not found');
    expect(el.shadowRoot!.querySelector('sl-button')!.getAttribute('href')).to
      .exist;
  });

  describe('tab title', () => {
    let originalTitle: string;
    let originalUrl: string;

    beforeEach(() => {
      originalTitle = document.title;
      originalUrl = window.location.href;
      // The tab still carries the console page the reader came from.
      document.title = 'Agents';
    });

    afterEach(() => {
      document.title = originalTitle;
      window.history.replaceState(null, '', originalUrl);
    });

    it('names the console 404 in the tab instead of the previous page', async () => {
      window.history.replaceState(null, '', '/console/does-not-exist');
      await fixture(html`<not-found-view></not-found-view>`);
      expect(document.title).to.equal(pageTitle('Page not found'));
    });

    it('leaves the served title alone on a public 404', async () => {
      // On a public path the app shell restores the served title; this
      // page does not retitle the tab there.
      window.history.replaceState(null, '', '/consoles');
      await fixture(html`<not-found-view></not-found-view>`);
      expect(document.title).to.equal('Agents');
    });
  });

  describe('notFoundAction', () => {
    it('leads back to the Overview inside the console', () => {
      // The console 404 renders inside the shell, so "the console" is where
      // the reader already is.
      expect(notFoundAction('/console/agnets', true)).to.eql({
        href: '/console',
        label: 'Back to Overview',
      });
    });

    it('offers the console to a signed-in visitor on a public path', () => {
      expect(notFoundAction('/agents', true)).to.eql({
        href: '/console',
        label: 'Go to the console',
      });
    });

    it('sends an anonymous visitor home, not to a sign-in screen', () => {
      expect(notFoundAction('/agents', false)).to.eql({
        href: '/',
        label: 'Go to the home page',
      });
      // A path that merely starts with the letters is not the console.
      expect(notFoundAction('/consoles', false).href).to.equal('/');
    });
  });
});
