import { expect } from '@open-wc/testing';
import { BrandConfig } from './brand-config';
import { collectLandingPublicAssetPaths } from './brand-landing-assets';

function brandWithImages(partial: {
  heroImage?: string;
  placeholders?: string[];
}): BrandConfig {
  return {
    landing: {
      hero: {
        title: '',
        lead: '',
        cta_primary: '',
        cta_secondary: '',
        cta_secondary_url: '',
        image: partial.heroImage,
      },
      features: (partial.placeholders || []).map((placeholderImg) => ({
        title: 'Onboard the agents you already run with one command',
        text: '',
        videoUrl: '',
        placeholderImg,
      })),
    },
  } as BrandConfig;
}

describe('collectLandingPublicAssetPaths', () => {
  it('includes the hero shot and feature placeholders', () => {
    const paths = collectLandingPublicAssetPaths(
      brandWithImages({
        heroImage: '/assets/screenshots/quickstart/dark/agent_bubble.png',
        placeholders: [
          '/assets/screenshots/quickstart/dark/cost_page.png',
          '/assets/screenshots/quickstart/dark/agents-onboarding.webp',
        ],
      })
    );

    expect(paths).to.include(
      '/assets/screenshots/quickstart/dark/agent_bubble.png'
    );
    expect(paths).to.include(
      '/assets/screenshots/quickstart/dark/agents-onboarding.webp'
    );
  });

  it('drops empty, remote, and protocol-relative URLs', () => {
    const paths = collectLandingPublicAssetPaths(
      brandWithImages({
        heroImage: 'https://cdn.example/hero.png',
        placeholders: ['', '//cdn.example/x.png', '/assets/ok.png'],
      })
    );

    expect(paths).to.deep.equal(['/assets/ok.png']);
  });
});
