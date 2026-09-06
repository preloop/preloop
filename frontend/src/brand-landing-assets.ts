import { BrandConfig } from './brand-config';

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
  return [...new Set(paths)].filter(isRootRelativeAssetPath);
}
