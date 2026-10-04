import { expect, fixture, html } from '@open-wc/testing';

import './not-found-view';
import { notFoundAction, type NotFoundView } from './not-found-view';

describe('NotFoundView', () => {
  it('explains the miss and offers one way back', async () => {
    const el = (await fixture(
      html`<not-found-view></not-found-view>`
    )) as NotFoundView;

    expect(el.shadowRoot!.textContent).to.contain('Page not found');
    expect(el.shadowRoot!.querySelector('sl-button')!.getAttribute('href')).to
      .exist;
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
