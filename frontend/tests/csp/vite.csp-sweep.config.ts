/**
 * Production build for the CSP violation sweep (#2280).
 *
 * The open-source brand is self-hosted, so its build has no marketing landing
 * page, blog, `/vs/` or `/resources/` pages. The sweep must load all four
 * under the enforced policy, so this config builds the same source with a
 * SaaS-edition copy of the open-source brand and a fixture content tree
 * (tests/csp/fixture-content/cspsweep) holding one page of each kind.
 * Everything else (plugins, bundling, index.html) is the real vite.config.ts.
 */
import * as fs from 'fs';
import * as path from 'path';
import { fileURLToPath } from 'url';
import * as yaml from 'js-yaml';
import { defineConfig, type PluginOption, type UserConfig } from 'vite';
import baseConfig from '../../vite.config';
import { brandPlugin } from '../../vite-plugin-brand';

export const SWEEP_BRAND = 'cspsweep';
const here = fileURLToPath(new URL('.', import.meta.url));
const frontend = path.resolve(here, '..', '..');
export const SWEEP_DIST = path.join(frontend, 'dist-csp-sweep');

function writeSweepBrands(): string {
  const source = yaml.load(
    fs.readFileSync(path.join(frontend, 'brands.yaml'), 'utf-8')
  ) as { brands: Record<string, Record<string, unknown>> };
  const brand = { ...source.brands.preloop, edition: 'saas' };
  const generated = path.join(here, '.generated');
  fs.mkdirSync(generated, { recursive: true });
  const file = path.join(generated, 'brands.yaml');
  fs.writeFileSync(file, yaml.dump({ brands: { [SWEEP_BRAND]: brand } }));
  return file;
}

function isBrandPlugin(plugin: PluginOption): boolean {
  return (
    typeof plugin === 'object' &&
    plugin !== null &&
    !Array.isArray(plugin) &&
    'name' in plugin &&
    plugin.name === 'vite-plugin-brand'
  );
}

const base = baseConfig as UserConfig;
const plugins = (base.plugins ?? []).flat() as PluginOption[];
const brandIndex = plugins.findIndex(isBrandPlugin);
if (brandIndex < 0) {
  throw new Error('vite.config.ts no longer registers vite-plugin-brand');
}
plugins.splice(
  brandIndex,
  1,
  brandPlugin(SWEEP_BRAND, {
    configPath: writeSweepBrands(),
    contentPath: path.join(here, 'fixture-content'),
    outDir: SWEEP_DIST,
  })
);

export default defineConfig({
  ...base,
  root: frontend,
  build: { ...base.build, outDir: SWEEP_DIST, emptyOutDir: true },
  plugins,
});
