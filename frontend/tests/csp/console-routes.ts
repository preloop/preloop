/**
 * The console's route table, read from source for the CSP violation sweep
 * (#2280).
 *
 * The sweep must visit every route the router knows, including routes added
 * after it was written, so the list is derived from `src/components/lit-app.ts`
 * (the `const routes: Route[] = [...]` table) and `src/lazy-routes.ts`
 * (`CAPABILITY_ROUTES`) instead of being typed out here. The reader is a small
 * scanner over the object literals: it tracks `path:` keys, `children:` arrays
 * and `redirect:` entries, and skips strings and comments.
 */
import * as fs from 'fs';
import * as path from 'path';
import { fileURLToPath } from 'url';

export interface SweepRoute {
  /** The route pattern as the router compiles it, e.g. `/console/agents/:agentId`. */
  pattern: string;
  /** A concrete URL for the pattern: params become `csp-sweep`, `(.*)` becomes a missing page. */
  url: string;
}

const FRONTEND = fileURLToPath(new URL('../..', import.meta.url));

interface ObjectFrame {
  kind: 'object';
  prefix: string;
  path: string | null;
  redirect: boolean;
  hasChildren: boolean;
  pendingKey: string | null;
}

interface ArrayFrame {
  kind: 'array';
  prefix: string;
}

type Frame = ObjectFrame | ArrayFrame;

function joinPath(prefix: string, child: string): string {
  const joined = `${prefix}/${child}`.replace(/\/+/g, '/');
  return joined.length > 1 ? joined.replace(/\/$/, '') : joined;
}

/**
 * Collect leaf route patterns from the array literal that starts at `start`
 * (the index of its `[`). Routes with children contribute their children;
 * redirects are skipped (their target is visited on its own).
 */
export function readRouteArray(
  source: string,
  start: number,
  basePrefix = ''
): string[] {
  const patterns: string[] = [];
  const stack: Frame[] = [];
  let i = start;
  let lastIdentifier = '';
  while (i < source.length) {
    const char = source[i];
    const next = source[i + 1];
    if (char === '/' && next === '/') {
      i = source.indexOf('\n', i);
      if (i < 0) break;
      continue;
    }
    if (char === '/' && next === '*') {
      i = source.indexOf('*/', i + 2) + 2;
      continue;
    }
    if (char === "'" || char === '"' || char === '`') {
      let j = i + 1;
      while (j < source.length && source[j] !== char) {
        if (source[j] === '\\') j += 1;
        j += 1;
      }
      const literal = source.slice(i + 1, j);
      const top = stack[stack.length - 1];
      if (top?.kind === 'object' && top.pendingKey === 'path') {
        top.path = literal;
      }
      if (top?.kind === 'object') top.pendingKey = null;
      i = j + 1;
      continue;
    }
    if (/[A-Za-z_$]/.test(char)) {
      let j = i;
      while (j < source.length && /[\w$]/.test(source[j])) j += 1;
      lastIdentifier = source.slice(i, j);
      i = j;
      continue;
    }
    const top = stack[stack.length - 1];
    if (char === ':' && top?.kind === 'object' && lastIdentifier) {
      top.pendingKey = lastIdentifier;
      if (lastIdentifier === 'redirect') top.redirect = true;
    } else if (char === '[') {
      let prefix = basePrefix;
      if (top?.kind === 'object') {
        prefix =
          top.pendingKey === 'children' && top.path !== null
            ? joinPath(top.prefix, top.path)
            : top.prefix;
        if (top.pendingKey === 'children') top.hasChildren = true;
        top.pendingKey = null;
      } else if (top) {
        prefix = top.prefix;
      }
      stack.push({ kind: 'array', prefix });
    } else if (char === '{') {
      const prefix = top ? top.prefix : basePrefix;
      if (top?.kind === 'object') top.pendingKey = null;
      stack.push({
        kind: 'object',
        prefix,
        path: null,
        redirect: false,
        hasChildren: false,
        pendingKey: null,
      });
    } else if (char === '}') {
      const frame = stack.pop();
      if (
        frame?.kind === 'object' &&
        frame.path !== null &&
        !frame.redirect &&
        !frame.hasChildren &&
        // Only frames that sit directly in a route array are routes; objects
        // nested elsewhere (an `action` body, a config literal) are not.
        stack[stack.length - 1]?.kind === 'array'
      ) {
        patterns.push(joinPath(frame.prefix, frame.path));
      }
    } else if (char === ']') {
      stack.pop();
      if (stack.length === 0) break;
    } else if (char === ',' && top?.kind === 'object') {
      top.pendingKey = null;
    }
    if (!/\s/.test(char)) lastIdentifier = '';
    i += 1;
  }
  return patterns;
}

function concreteUrl(pattern: string): string {
  return pattern
    .replace(/\(\.\*\)/g, 'csp-sweep-missing-page')
    .replace(/:[A-Za-z]+/g, 'csp-sweep');
}

/** Every leaf route of the console router, public and authenticated. */
export function consoleRoutes(): SweepRoute[] {
  const litApp = fs.readFileSync(
    path.join(FRONTEND, 'src', 'components', 'lit-app.ts'),
    'utf-8'
  );
  const marker = 'const routes: Route[] = [';
  const start = litApp.indexOf(marker);
  if (start < 0) throw new Error(`lit-app.ts no longer contains "${marker}"`);
  const patterns = readRouteArray(litApp, start + marker.length - 1);

  const lazy = fs.readFileSync(
    path.join(FRONTEND, 'src', 'lazy-routes.ts'),
    'utf-8'
  );
  const lazyMarker = 'CAPABILITY_ROUTES: readonly CapabilityRoute[] = [';
  const lazyStart = lazy.indexOf(lazyMarker);
  if (lazyStart < 0) {
    throw new Error(`lazy-routes.ts no longer contains "${lazyMarker}"`);
  }
  patterns.push(
    ...readRouteArray(lazy, lazyStart + lazyMarker.length - 1, '/console')
  );

  const seen = new Set<string>();
  return patterns
    .filter((pattern) => {
      if (seen.has(pattern)) return false;
      seen.add(pattern);
      return true;
    })
    .map((pattern) => ({ pattern, url: concreteUrl(pattern) }));
}
