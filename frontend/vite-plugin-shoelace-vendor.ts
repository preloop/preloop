import * as fs from 'fs';
import * as path from 'path';
import type { Plugin, ResolvedConfig } from 'vite';

/**
 * Serves Shoelace's theme stylesheets, autoloader, component chunks and icon
 * set from the console's own origin under `/vendor/shoelace/`.
 *
 * They used to come from a public CDN, pinned to an older Shoelace than the
 * one bundled from `node_modules`. On an air-gapped or egress-restricted
 * self-hosted install the CDN is unreachable, and the console rendered
 * without its theme and icons: unstyled buttons, invisible switches, empty
 * icon slots. The CDN also added a third-party request to every console page
 * load and let the themed components drift from the bundled ones.
 *
 * The files are copied from the installed package, so the version always
 * matches `package.json`. The autoloader derives Shoelace's base path from its
 * own `src`, which points icons and lazily loaded components here as well.
 */
export const SHOELACE_VENDOR_BASE = '/vendor/shoelace/';

/** Top-level entries of the package's `cdn/` build that the browser fetches. */
const SHIPPED_ENTRIES = [
  'shoelace-autoloader.js',
  'themes',
  'components',
  'chunks',
  'assets',
  'translations',
  'utilities',
];

const CONTENT_TYPES: Record<string, string> = {
  '.css': 'text/css; charset=utf-8',
  '.js': 'text/javascript; charset=utf-8',
  '.svg': 'image/svg+xml',
  '.json': 'application/json; charset=utf-8',
};

/** Type declarations and source maps are never requested by the browser. */
export function isShippedFile(relativePath: string): boolean {
  const normalized = relativePath.split(path.sep).join('/');
  const [top] = normalized.split('/');
  if (!SHIPPED_ENTRIES.includes(top)) return false;
  return !normalized.endsWith('.d.ts') && !normalized.endsWith('.map');
}

/**
 * Maps a request path under the vendor base to a file in the package, or
 * `null` when it is outside the base, escapes the package directory, or is
 * not one of the shipped files.
 */
export function resolveVendorRequest(
  sourceDir: string,
  requestPath: string
): string | null {
  const pathname = requestPath.split(/[?#]/)[0];
  if (!pathname.startsWith(SHOELACE_VENDOR_BASE)) return null;
  let relative: string;
  try {
    relative = decodeURIComponent(pathname.slice(SHOELACE_VENDOR_BASE.length));
  } catch {
    return null;
  }
  const root = path.resolve(sourceDir);
  const resolved = path.resolve(root, relative);
  if (!resolved.startsWith(root + path.sep)) return null;
  if (!isShippedFile(path.relative(root, resolved))) return null;
  return resolved;
}

function copyShipped(sourceDir: string, targetDir: string): number {
  let copied = 0;
  const walk = (relativeDir: string) => {
    const absoluteDir = path.join(sourceDir, relativeDir);
    for (const entry of fs.readdirSync(absoluteDir, { withFileTypes: true })) {
      const relative = path.join(relativeDir, entry.name);
      if (entry.isDirectory()) {
        if (relativeDir === '' && !SHIPPED_ENTRIES.includes(entry.name)) {
          continue;
        }
        walk(relative);
      } else if (isShippedFile(relative)) {
        const target = path.join(targetDir, relative);
        fs.mkdirSync(path.dirname(target), { recursive: true });
        fs.copyFileSync(path.join(sourceDir, relative), target);
        copied += 1;
      }
    }
  };
  walk('');
  return copied;
}

export function shoelaceVendorPlugin(sourceDir: string): Plugin {
  let config: ResolvedConfig;
  return {
    name: 'preloop-shoelace-vendor',
    configResolved(resolved) {
      config = resolved;
    },
    configureServer(server) {
      server.middlewares.use((req, res, next) => {
        const file = req.url ? resolveVendorRequest(sourceDir, req.url) : null;
        if (!file || !fs.existsSync(file) || !fs.statSync(file).isFile()) {
          next();
          return;
        }
        const type = CONTENT_TYPES[path.extname(file)];
        if (type) res.setHeader('Content-Type', type);
        fs.createReadStream(file).pipe(res);
      });
    },
    writeBundle() {
      const target = path.resolve(
        config.root,
        config.build.outDir,
        SHOELACE_VENDOR_BASE.replace(/^\/|\/$/g, '')
      );
      const copied = copyShipped(sourceDir, target);
      config.logger.info(
        `[shoelace-vendor] copied ${copied} files to ${path.relative(config.root, target)}`
      );
    },
  };
}
