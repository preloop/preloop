import type { BrandConfig } from './brand-config';

const screenshotDirectory = '/assets/screenshots/quickstart/dark/';
const responsiveScreenshots = new Set(
  [
    'agent_bubble',
    'audit_page',
    'cost_page',
    'dashboard',
    'rules_configured',
  ].map((name) => `${screenshotDirectory}${name}.png`)
);

function responsiveDerivatives(original: string): {
  src: string;
  width: number;
}[] {
  if (!responsiveScreenshots.has(original)) return [];
  const stem = original.slice(0, -4);
  return [800, 1600].map((width) => ({
    src: `${stem}-${width}.webp`,
    width,
  }));
}

/** Display derivatives for bundled stills; custom branding and animation pass through. */
export function landingImageSources(original: string): {
  src: string;
  srcset?: string;
  width?: number;
  height?: number;
} {
  const derivatives = responsiveDerivatives(original);
  if (!derivatives.length) return { src: original };
  return {
    src: derivatives[0].src,
    srcset: [...derivatives, { src: original, width: 3200 }]
      .map(({ src, width }) => `${src} ${width}w`)
      .join(', '),
    width: 3200,
    height: 1900,
  };
}

function isRootRelativeAssetPath(assetPath: string): boolean {
  return (
    assetPath.startsWith('/') &&
    !assetPath.startsWith('//') &&
    !/^https?:/i.test(assetPath)
  );
}

/**
 * Root-relative image paths the landing page will request from ``public/``.
 *
 * Hero and feature placeholders are baked into index.html at build time; a
 * missing file 404s on the live site with a broken-image icon.
 */
export function collectLandingPublicAssetPaths(brand: BrandConfig): string[] {
  const paths: string[] = [];
  const heroImage = brand.landing?.hero?.image;
  if (heroImage) {
    paths.push(heroImage);
  }
  for (const feature of brand.landing?.features || []) {
    if (feature.placeholderImg) {
      paths.push(feature.placeholderImg);
    }
  }
  const displayPaths = paths.flatMap((original) => [
    original,
    ...responsiveDerivatives(original).map(({ src }) => src),
  ]);
  return [...new Set(displayPaths)].filter(isRootRelativeAssetPath);
}
