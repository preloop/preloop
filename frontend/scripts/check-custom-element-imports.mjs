/**
 * Fail when a module renders a console custom element it does not import.
 *
 * A Lit template that uses `<view-header>` without importing
 * `components/view-header` still works when some earlier page happened to
 * register the element, so the bug hides during in-app navigation. A deep
 * link or a reload then renders the element as an unknown tag: only its
 * slotted children show, and the page loses its title and description.
 * Thirteen views shipped that way. Importing every element a module renders
 * makes each route correct on a cold load.
 *
 * Only elements defined under src/components are checked. Test files are not
 * scanned. A module that loads an element lazily on purpose lists it in
 * LAZY_BY_DESIGN with the reason.
 */
import { readdir, readFile } from 'node:fs/promises';
import path from 'node:path';
import { fileURLToPath } from 'node:url';

const src = path.resolve(
  path.dirname(fileURLToPath(import.meta.url)),
  '../src'
);

/** `file -> tags` rendered without a static import, deliberately. */
const LAZY_BY_DESIGN = new Map([
  // lit-app imports the public header and footer once for every public route.
  ['views/public/landing-view.ts', ['app-header']],
  ['views/public/pricing-view.ts', ['app-header', 'app-footer']],
  ['views/public/static-view.ts', ['app-header', 'app-footer']],
]);

/** Comments mention tags in prose; only code can render one. */
const stripComments = (text) =>
  text.replace(/\/\*[\s\S]*?\*\//g, '').replace(/^\s*\/\/.*$/gm, '');

async function walk(dir) {
  const entries = await readdir(dir, { withFileTypes: true });
  const files = [];
  for (const entry of entries) {
    const full = path.join(dir, entry.name);
    if (entry.isDirectory()) {
      if (entry.name === 'node_modules') continue;
      files.push(...(await walk(full)));
    } else if (/\.ts$/.test(entry.name) && !/\.test\.ts$/.test(entry.name)) {
      files.push(full);
    }
  }
  return files;
}

const stripExt = (file) => file.replace(/\.(ts|js)$/, '');

export async function findMissingImports() {
  const files = await walk(src);
  const sources = new Map();
  for (const file of files) sources.set(file, await readFile(file, 'utf8'));

  const definitions = new Map();
  for (const [file, text] of sources) {
    if (!file.startsWith(path.join(src, 'components'))) continue;
    for (const match of text.matchAll(/@customElement\('([a-z0-9-]+)'\)/g)) {
      definitions.set(match[1], stripExt(file));
    }
  }

  const problems = [];
  for (const [file, raw] of sources) {
    // Build-time HTML string builders (SEO pages, pricing SSR) never render
    // through Lit, so they register nothing and need nothing registered.
    if (!/from ['"]lit['"]/.test(raw)) continue;
    const text = stripComments(raw);
    const imported = new Set();
    for (const match of text.matchAll(
      /(?:import|from)\s*\(?\s*['"](\.[^'"]+)['"]/g
    )) {
      imported.add(stripExt(path.resolve(path.dirname(file), match[1])));
    }
    const relative = path.relative(src, file);
    for (const [tag, module] of definitions) {
      if (module === stripExt(file)) continue;
      if (!new RegExp(`<${tag}[\\s>]`).test(text)) continue;
      if (imported.has(module)) continue;
      if (LAZY_BY_DESIGN.get(relative)?.includes(tag)) continue;
      problems.push(
        `${relative}: renders <${tag}> without importing ${path.relative(src, module)}`
      );
    }
  }
  return problems;
}

if (process.argv[1] === fileURLToPath(import.meta.url)) {
  const problems = await findMissingImports();
  if (problems.length) {
    console.error(problems.join('\n'));
    console.error(
      `\n${problems.length} custom element(s) rendered without an import. ` +
        'Import the component module next to the template that renders it.'
    );
    process.exit(1);
  }
}
