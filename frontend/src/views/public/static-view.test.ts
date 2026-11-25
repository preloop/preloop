import { expect, fixture, html, waitUntil } from '@open-wc/testing';
import sinon from 'sinon';
import type { BrandRuntimeConfig } from '../../brand-config';
import './static-view';
import type { StaticView } from './static-view';

describe('static content on a host with an SPA fallback', () => {
  const config = window as Window & { BRAND_CONFIG?: BrandRuntimeConfig };
  const saved = config.BRAND_CONFIG;
  const docsUrl = 'https://docs.example.test/';
  beforeEach(() => {
    config.BRAND_CONFIG = { ...saved, docs_url: docsUrl } as BrandRuntimeConfig;
  });
  afterEach(() => {
    if (saved) config.BRAND_CONFIG = saved;
    else delete config.BRAND_CONFIG;
    sinon.restore();
  });

  async function load(src: string, body: string): Promise<StaticView> {
    sinon.stub(window, 'fetch').resolves(new Response(body));
    const view = await fixture<StaticView>(
      html`<static-view .src=${src}></static-view>`
    );
    await waitUntil(() => !view.shadowRoot!.querySelector('sl-spinner'));
    return view;
  }

  it('offers the configured documentation when the host serves its app shell', async () => {
    const view = await load(
      '/content/docs.md',
      '<!doctype html><html><body><lit-app></lit-app></body></html>'
    );
    expect(view.shadowRoot!.querySelector('lit-app')).not.to.exist;
    expect(view.shadowRoot!.querySelector('a')?.href).to.equal(docsUrl);
  });

  it('renders published Markdown, including inline markup', async () => {
    const view = await load(
      '/content/docs.md',
      '# Quick start\n\nUse <kbd>Enter</kbd>.'
    );
    expect(view.shadowRoot!.querySelector('h1')?.textContent).to.equal(
      'Quick start'
    );
    expect(view.shadowRoot!.querySelector('kbd')?.textContent).to.equal(
      'Enter'
    );
  });

  it('shows a content error when a missing blog fragment returns the app shell', async () => {
    const view = await load(
      '/content/blog/missing.html',
      '<!doctype html><html><body><lit-app></lit-app></body></html>'
    );
    expect(view.shadowRoot!.querySelector('lit-app')).not.to.exist;
    expect(view.shadowRoot!.querySelector('sl-alert')).to.exist;
  });

  it('keeps a published article fragment intact', async () => {
    const view = await load(
      '/content/blog/example.html',
      '<article><h1>Release notes</h1></article>'
    );
    expect(view.shadowRoot!.querySelector('article h1')?.textContent).to.equal(
      'Release notes'
    );
  });

  it('shows the existing content error for other missing pages', async () => {
    const view = await load(
      '/content/terms.md',
      '<html lang="en"><body><lit-app></lit-app></body></html>'
    );
    expect(view.shadowRoot!.querySelector('sl-alert')).to.exist;
    expect(view.shadowRoot!.querySelector('lit-app')).not.to.exist;
  });
});
