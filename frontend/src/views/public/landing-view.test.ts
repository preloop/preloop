import { html, fixture, expect } from '@open-wc/testing';
import sinon from 'sinon';
import './landing-view';
import {
  LandingView,
  parseHeroVideoUrl,
  buildHeroVideoEmbedUrl,
} from './landing-view';

const BRAND_CONFIG: any = {
  name: 'Test Brand',
  domain: 'test.example.com',
  edition: 'saas',
  company: { legal_name: 'Test Co', address: '123 Test', city: 'Test' },
  branding: {
    logo_light: '/logo.svg',
    logo_dark: '/logo-dark.svg',
    favicon: '/favicon.ico',
    primary_color: '#000',
    gradient_product: '',
    gradient_ai: '',
  },
  social: { twitter: '', linkedin: '', instagram: '' },
};

const tick = (ms = 150) => new Promise((r) => setTimeout(r, ms));

const JSON_CONTENT = {
  hero: {
    title: 'JSON Hero Title',
    lead: 'JSON hero lead copy.',
    cta_primary: 'Get Started',
  },
  features: [{ title: 'Feature One', text: 'Does things.' }],
  faqs: [{ q: 'Question?', a: 'Answer.' }],
};

function stubFetch(content: unknown = JSON_CONTENT) {
  return sinon
    .stub(window, 'fetch')
    .callsFake(async (input: RequestInfo | URL) => {
      const url = String(input);
      if (url.includes('/landing-content.json')) {
        return new Response(JSON.stringify(content), { status: 200 });
      }
      if (url.includes('/api/v1/features')) {
        return new Response(JSON.stringify({ features: {} }), { status: 200 });
      }
      return new Response('{}', { status: 200 });
    });
}

describe('LandingView', () => {
  let fetchStub: sinon.SinonStub;

  beforeEach(() => {
    (window as any).BRAND_CONFIG = BRAND_CONFIG;
  });

  afterEach(() => {
    fetchStub.restore();
    delete (window as any).BRAND_CONFIG;
  });

  it('keeps slotted hero marketing copy in the light DOM (crawlable)', async () => {
    fetchStub = stubFetch();
    const el = (await fixture(html`
      <landing-view>
        <h1 slot="hero-title">Govern Every AI Agent</h1>
        <p slot="hero-lead">The MCP governance layer for teams.</p>
      </landing-view>
    `)) as LandingView;
    await tick();
    await el.updateComplete;

    // Slotted content remains in the light DOM where crawlers can read it.
    const lightTitle = el.querySelector('[slot="hero-title"]');
    expect(lightTitle, 'hero title in light DOM').to.exist;
    expect(lightTitle?.textContent).to.contain('Govern Every AI Agent');
    // And the component projects it into its rendered hero.
    expect((el as any)._heroTitle).to.contain('Govern Every AI Agent');
  });

  it('renders the default get-started marketing section', async () => {
    fetchStub = stubFetch();
    const el = (await fixture(
      html`<landing-view></landing-view>`
    )) as LandingView;
    await tick();
    await el.updateComplete;
    expect(el.shadowRoot?.querySelector('#get-started')).to.exist;
    expect(el.shadowRoot?.textContent).to.contain(
      'Turbocharge your AI Workflow with MCP'
    );
  });

  it('loads hero and feature content from JSON on client navigation', async () => {
    fetchStub = stubFetch();
    const el = (await fixture(
      html`<landing-view></landing-view>`
    )) as LandingView;
    await tick();
    await el.updateComplete;
    expect((el as any)._heroTitle).to.equal('JSON Hero Title');
    expect((el as any)._featureSlides.length).to.equal(1);
    expect(
      fetchStub
        .getCalls()
        .some((c) => String(c.args[0]).includes('/landing-content.json'))
    ).to.be.true;
  });

  it('handles empty content without rendering feature cards', async () => {
    fetchStub = stubFetch({ hero: {}, features: [], faqs: [] });
    const el = (await fixture(
      html`<landing-view></landing-view>`
    )) as LandingView;
    await tick();
    await el.updateComplete;
    expect((el as any)._featureSlides.length).to.equal(0);
    expect((el as any)._faqs.length).to.equal(0);
  });
});

describe('parseHeroVideoUrl', () => {
  it('parses a watch URL with a playlist into video + list ids', () => {
    expect(
      parseHeroVideoUrl(
        'https://www.youtube.com/watch?v=Y_geb2Or8zM&list=PLr2Jp0c-Qn2hoYL3aRZGUtBjTCVygWIXt'
      )
    ).to.deep.equal({
      videoId: 'Y_geb2Or8zM',
      listId: 'PLr2Jp0c-Qn2hoYL3aRZGUtBjTCVygWIXt',
    });
  });

  it('parses a watch URL without a playlist into a video-only target', () => {
    expect(
      parseHeroVideoUrl('https://www.youtube.com/watch?v=Y_geb2Or8zM')
    ).to.deep.equal({ videoId: 'Y_geb2Or8zM', listId: null });
  });

  it('parses a bare playlist URL into a list-only target', () => {
    expect(
      parseHeroVideoUrl('https://www.youtube.com/playlist?list=PLabc123_-')
    ).to.deep.equal({ videoId: null, listId: 'PLabc123_-' });
  });

  it('parses youtu.be short links', () => {
    expect(
      parseHeroVideoUrl('https://youtu.be/Y_geb2Or8zM?list=PLabc123')
    ).to.deep.equal({ videoId: 'Y_geb2Or8zM', listId: 'PLabc123' });
  });

  it('parses existing embed URLs, including the videoseries form', () => {
    expect(
      parseHeroVideoUrl('https://www.youtube-nocookie.com/embed/Y_geb2Or8zM')
    ).to.deep.equal({ videoId: 'Y_geb2Or8zM', listId: null });
    expect(
      parseHeroVideoUrl(
        'https://www.youtube.com/embed/videoseries?list=PLabc123'
      )
    ).to.deep.equal({ videoId: null, listId: 'PLabc123' });
  });

  it('rejects empty, malformed, non-YouTube, and id-less URLs', () => {
    expect(parseHeroVideoUrl('')).to.be.null;
    expect(parseHeroVideoUrl('not a url')).to.be.null;
    expect(parseHeroVideoUrl('https://vimeo.com/12345')).to.be.null;
    expect(parseHeroVideoUrl('https://www.youtube.com/watch')).to.be.null;
  });
});

describe('buildHeroVideoEmbedUrl', () => {
  it('builds a nocookie video+list embed with autoplay', () => {
    expect(
      buildHeroVideoEmbedUrl({ videoId: 'Y_geb2Or8zM', listId: 'PLabc123' })
    ).to.equal(
      'https://www.youtube-nocookie.com/embed/Y_geb2Or8zM?list=PLabc123&autoplay=1'
    );
  });

  it('builds a videoseries embed for list-only targets', () => {
    expect(
      buildHeroVideoEmbedUrl({ videoId: null, listId: 'PLabc123' })
    ).to.equal(
      'https://www.youtube-nocookie.com/embed/videoseries?list=PLabc123&autoplay=1'
    );
  });

  it('builds a plain video embed for video-only targets', () => {
    expect(
      buildHeroVideoEmbedUrl({ videoId: 'Y_geb2Or8zM', listId: null })
    ).to.equal('https://www.youtube-nocookie.com/embed/Y_geb2Or8zM?autoplay=1');
  });

  it('returns null for a null target', () => {
    expect(buildHeroVideoEmbedUrl(null)).to.be.null;
  });
});

describe('LandingView hero video', () => {
  let fetchStub: sinon.SinonStub;

  const HERO_WITH_VIDEO = {
    hero: {
      title: 'Hero',
      lead: 'Lead',
      image: '/assets/screenshots/quickstart/dark/agent_bubble.png',
      image_alt: 'Product screenshot',
      video_playlist_url:
        'https://www.youtube.com/watch?v=Y_geb2Or8zM&list=PLr2Jp0c-Qn2hoYL3aRZGUtBjTCVygWIXt',
    },
    features: [],
    faqs: [],
  };

  beforeEach(() => {
    (window as any).BRAND_CONFIG = BRAND_CONFIG;
  });

  afterEach(() => {
    fetchStub.restore();
    delete (window as any).BRAND_CONFIG;
  });

  it('renders no play button when the knob is absent (default hero)', async () => {
    fetchStub = stubFetch({
      hero: { title: 'Hero', image: '/img.png', image_alt: 'alt' },
      features: [],
      faqs: [],
    });
    const el = (await fixture(
      html`<landing-view></landing-view>`
    )) as LandingView;
    await tick();
    await el.updateComplete;

    expect(el.shadowRoot?.querySelector('.hero-visual img')).to.exist;
    expect(el.shadowRoot?.querySelector('.hero-video-play')).to.not.exist;
    expect(el.shadowRoot?.querySelector('iframe')).to.not.exist;
  });

  it('renders no play button for an unparseable knob value', async () => {
    fetchStub = stubFetch({
      hero: {
        title: 'Hero',
        image: '/img.png',
        video_playlist_url: 'not a url',
      },
      features: [],
      faqs: [],
    });
    const el = (await fixture(
      html`<landing-view></landing-view>`
    )) as LandingView;
    await tick();
    await el.updateComplete;

    expect(el.shadowRoot?.querySelector('.hero-video-play')).to.not.exist;
  });

  it('renders the play button over the screenshot with no YouTube URL before click', async () => {
    fetchStub = stubFetch(HERO_WITH_VIDEO);
    const el = (await fixture(
      html`<landing-view></landing-view>`
    )) as LandingView;
    await tick();
    await el.updateComplete;

    const playBtn = el.shadowRoot?.querySelector(
      '.hero-video-play'
    ) as HTMLButtonElement;
    expect(playBtn).to.exist;
    expect(playBtn.getAttribute('aria-label')).to.contain('Play');
    // Poster is still the real product screenshot.
    const img = el.shadowRoot?.querySelector(
      '.hero-visual--video img'
    ) as HTMLImageElement;
    expect(img).to.exist;
    expect(img.getAttribute('src')).to.equal(
      HERO_WITH_VIDEO.hero.image.replace('.png', '-800.webp')
    );
    expect(img.srcset).to.contain(`${HERO_WITH_VIDEO.hero.image} 3200w`);
    // Click-to-load: nothing in the shadow DOM references YouTube yet.
    expect(el.shadowRoot?.querySelector('iframe')).to.not.exist;
    expect(el.shadowRoot?.innerHTML).to.not.contain('youtube');
  });

  it('swaps in the nocookie iframe on click and restores the image on close', async () => {
    fetchStub = stubFetch(HERO_WITH_VIDEO);
    const el = (await fixture(
      html`<landing-view></landing-view>`
    )) as LandingView;
    await tick();
    await el.updateComplete;

    const playBtn = el.shadowRoot?.querySelector(
      '.hero-video-play'
    ) as HTMLButtonElement;
    playBtn.click();
    await el.updateComplete;

    const iframe = el.shadowRoot?.querySelector(
      'iframe.hero-video-frame'
    ) as HTMLIFrameElement;
    expect(iframe).to.exist;
    expect(iframe.getAttribute('src')).to.equal(
      'https://www.youtube-nocookie.com/embed/Y_geb2Or8zM?list=PLr2Jp0c-Qn2hoYL3aRZGUtBjTCVygWIXt&autoplay=1'
    );
    expect(el.shadowRoot?.querySelector('.hero-video-play')).to.not.exist;

    const closeBtn = el.shadowRoot?.querySelector(
      '.hero-video-close'
    ) as HTMLButtonElement;
    expect(closeBtn).to.exist;
    closeBtn.click();
    await el.updateComplete;

    expect(el.shadowRoot?.querySelector('iframe')).to.not.exist;
    expect(el.shadowRoot?.querySelector('.hero-video-play')).to.exist;
    expect(el.shadowRoot?.querySelector('.hero-visual--video img')).to.exist;
  });

  it('reads the knob from the SSR hero-video slot', async () => {
    fetchStub = stubFetch();
    const el = (await fixture(html`
      <landing-view>
        <div
          slot="hero-image"
          data-src="/img.png"
          data-alt="Product screenshot"
        ></div>
        <div
          slot="hero-video"
          data-url="https://www.youtube.com/watch?v=Y_geb2Or8zM&list=PLabc123"
        ></div>
      </landing-view>
    `)) as LandingView;
    await tick();
    await el.updateComplete;

    expect(el.shadowRoot?.querySelector('.hero-video-play')).to.exist;
    expect(el.shadowRoot?.querySelector('iframe')).to.not.exist;
  });
});

describe('LandingView hero CTA row and footer disclaimer', () => {
  let fetchStub: sinon.SinonStub;

  const HERO_WITH_INSTALL_AND_LEGAL = {
    hero: {
      title: 'Hero',
      lead: 'Lead',
      install_command: 'curl -fsSL https://example.com/install | sh',
      install_caption: 'macOS, Linux, and WSL.',
      cta_secondary: 'Book a Demo',
      cta_secondary_url: '/request-demo',
      trust_tags: ['Apache-2.0', 'Self-hostable'],
      image: '/img.png',
      image_alt: 'Product screenshot',
    },
    features: [],
    faqs: [{ q: 'Question?', a: 'Answer.' }],
    legal_disclaimer:
      'Preloop is not a law firm and does not provide legal advice.',
  };

  beforeEach(() => {
    (window as any).BRAND_CONFIG = BRAND_CONFIG;
  });

  afterEach(() => {
    fetchStub.restore();
    delete (window as any).BRAND_CONFIG;
  });

  it('renders the secondary CTA below the hero grid, not in the copy column', async () => {
    fetchStub = stubFetch(HERO_WITH_INSTALL_AND_LEGAL);
    const el = (await fixture(
      html`<landing-view></landing-view>`
    )) as LandingView;
    await tick();
    await el.updateComplete;

    const hero = el.shadowRoot?.querySelector('.hero');
    const inner = hero?.querySelector('.hero-inner');
    const cta = hero?.querySelector('.hero-secondary-cta');
    expect(cta, 'secondary CTA container exists').to.exist;
    expect(
      cta?.classList.contains('section-container'),
      'CTA row reuses the shared section width'
    ).to.be.true;
    expect(hero?.contains(cta as Node)).to.be.true;
    expect(
      inner?.contains(cta as Node),
      'CTA is not inside the two-column grid'
    ).to.be.false;
    expect(
      inner?.nextElementSibling,
      'CTA is the row immediately below the grid'
    ).to.equal(cta);
    expect(el.shadowRoot?.querySelector('.hero-content .hero-secondary-cta')).to
      .not.exist;
    const demoBtn = cta?.querySelector('sl-button');
    expect(demoBtn?.getAttribute('data-track')).to.equal('cta_demo_hero');
    expect((cta?.textContent || '').replace(/\s+/g, ' ')).to.contain(
      'Want a guided tour first?'
    );
  });

  it('keeps the disclaimer out of the FAQ and renders it in the footer', async () => {
    fetchStub = stubFetch(HERO_WITH_INSTALL_AND_LEGAL);
    const el = (await fixture(
      html`<landing-view></landing-view>`
    )) as LandingView;
    await tick();
    await el.updateComplete;

    const faq = el.shadowRoot?.querySelector('.faq-section');
    expect(faq).to.exist;
    expect(faq?.nextElementSibling?.classList.contains('legal-disclaimer')).to
      .be.false;
    expect(el.shadowRoot?.querySelector('main .legal-disclaimer')).to.not.exist;
    expect(el.shadowRoot?.querySelector('.faq-section + p.legal-disclaimer')).to
      .not.exist;

    const footer = el.shadowRoot?.querySelector('app-footer');
    expect(footer).to.exist;
    await (footer as HTMLElement & { updateComplete: Promise<unknown> })
      .updateComplete;
    const disclaimer = footer?.shadowRoot?.querySelector('.legal-disclaimer');
    expect(disclaimer, 'disclaimer present in the footer').to.exist;
    expect((disclaimer?.textContent || '').trim()).to.equal(
      HERO_WITH_INSTALL_AND_LEGAL.legal_disclaimer
    );
  });
});

describe('LandingView responsive screenshots', () => {
  const original = '/assets/screenshots/quickstart/dark/agent_bubble.png';
  const feature = '/assets/screenshots/quickstart/dark/cost_page.png';
  let fetchStub: sinon.SinonStub;

  beforeEach(() => {
    (window as any).BRAND_CONFIG = BRAND_CONFIG;
  });

  afterEach(() => {
    fetchStub.restore();
    delete (window as any).BRAND_CONFIG;
  });

  for (const source of ['JSON', 'slotted HTML']) {
    it(`uses responsive previews and original lightbox links from ${source}`, async () => {
      fetchStub = stubFetch({
        hero: { title: 'Hero', image: original, image_alt: 'Active agents' },
        features: [
          { title: 'Costs', text: 'Track spending.', placeholderImg: feature },
        ],
        faqs: [],
      });
      const el = (await fixture(
        source === 'JSON'
          ? html`<landing-view></landing-view>`
          : html`
              <landing-view>
                <h1 slot="hero-title">Hero</h1>
                <div
                  slot="hero-image"
                  data-src=${original}
                  data-alt="Active agents"
                ></div>
                <div
                  slot="feature-0"
                  data-title="Costs"
                  data-text="Track spending."
                  data-img=${feature}
                ></div>
              </landing-view>
            `
      )) as LandingView;
      await tick();
      await el.updateComplete;
      const hero =
        el.shadowRoot!.querySelector<HTMLImageElement>('.hero-visual img')!;
      const preview = el.shadowRoot!.querySelector<HTMLImageElement>(
        '.feature-stacked-image'
      )!;
      expect(hero.getAttribute('src')).to.equal(
        original.replace('.png', '-800.webp')
      );
      expect(hero.srcset).to.contain('1600.webp 1600w');
      expect(hero.srcset).to.contain(`${original} 3200w`);
      expect(hero.sizes).to.contain('1150px');
      expect(hero.loading).to.equal('eager');
      expect(preview.loading).to.equal('lazy');
      expect(preview.srcset).to.contain('cost_page-800.webp 800w');
      const link = hero.closest('a')!;
      expect(link.getAttribute('href')).to.equal(original);
      expect(link.hasAttribute('aria-hidden')).to.be.false;
      expect(preview.closest('a')!.getAttribute('href')).to.equal(feature);
      // Enter on an anchor dispatches a click, opening the same full-size lightbox.
      link.click();
      await el.updateComplete;
      expect(
        el.shadowRoot!.querySelector('.lightbox-image')?.getAttribute('src')
      ).to.equal(original);
      // Modified clicks keep the browser's native new-tab behavior.
      const modifiedClick = new MouseEvent('click', {
        ctrlKey: true,
        cancelable: true,
      });
      let intercepted = true;
      link.addEventListener(
        'click',
        (event) => {
          intercepted = event.defaultPrevented;
          event.preventDefault(); // Keep the test page in place after checking the handler.
        },
        { once: true }
      );
      link.dispatchEvent(modifiedClick);
      expect(intercepted).to.be.false;
    });
  }

  it('opens carousel stills at full size and keeps video thumbnails playable', async () => {
    fetchStub = stubFetch({
      hero: { title: 'Hero' },
      features_layout: 'carousel',
      features: [
        { title: 'Still', text: 'Screenshot', placeholderImg: feature },
        {
          title: 'Video',
          text: 'Video',
          placeholderImg: original,
          videoUrl: 'https://www.youtube.com/watch?v=Y_geb2Or8zM',
        },
      ],
    });
    const el = await fixture<LandingView>(html`<landing-view></landing-view>`);
    await tick();
    await el.updateComplete;
    const links = el.shadowRoot!.querySelectorAll<HTMLAnchorElement>(
      'sl-carousel-item:not([data-clone]) .image-placeholder'
    );
    const stillLink = Array.from(links).find(
      (link) => link.getAttribute('href') === feature
    )!;
    const videoLink = Array.from(links).find((link) =>
      link.getAttribute('href')!.startsWith('https://www.youtube.com/')
    )!;
    expect(stillLink).to.exist;
    stillLink.click();
    await el.updateComplete;
    expect(
      el.shadowRoot!.querySelector('.lightbox-image')?.getAttribute('src')
    ).to.equal(feature);
    let videoClickIntercepted = true;
    videoLink.addEventListener(
      'click',
      (event) => {
        videoClickIntercepted = event.defaultPrevented;
        event.preventDefault();
      },
      { once: true }
    );
    videoLink.dispatchEvent(
      new MouseEvent('click', { metaKey: true, cancelable: true })
    );
    expect(videoClickIntercepted).to.be.false;
    videoLink.click();
    await el.updateComplete;
    expect(
      el.shadowRoot!.querySelector('.video-wrapper iframe')?.getAttribute('src')
    ).to.contain('Y_geb2Or8zM');
  });

  it('keeps custom brand images usable without guessed derivatives', async () => {
    fetchStub = stubFetch({
      hero: { title: 'Hero', image: '/custom.png' },
      features: [],
    });
    const el = await fixture<LandingView>(html`<landing-view></landing-view>`);
    await tick();
    await el.updateComplete;
    const image = el.shadowRoot!.querySelector('.hero-visual img')!;
    expect(image.getAttribute('src')).to.equal('/custom.png');
    expect(image.hasAttribute('srcset')).to.be.false;
    expect(image.hasAttribute('sizes')).to.be.false;
  });
});
