/**
 * Local servers for the CSP violation sweep (#2280).
 *
 * `startConsoleServer` serves a production build the way the console nginx
 * does (prerendered page first, SPA shell otherwise) and sends the enforced
 * policy on every response. The policy text is read from
 * frontend/docker/nginx.conf.template, the same string nginx sends, with the
 * script-src and connect-src sources filled in the way
 * docker/15-preloop-csp.envsh and the Helm chart fill them. With analytics
 * on, HTML responses get the tags the Helm chart's sub_filter injects, read
 * from helm/preloop/templates/configmap-nginx.yaml.
 *
 * `startAnalyticsStub` is a stand-in tracker on another origin: it serves a
 * Plausible-style script and records the events the browser posts to it. No
 * real analytics host is contacted.
 */
import * as fs from 'fs';
import * as http from 'http';
import type { AddressInfo } from 'net';
import * as path from 'path';
import { fileURLToPath } from 'url';

const REPO = fileURLToPath(new URL('../../..', import.meta.url));
const NGINX_TEMPLATE = path.join(
  REPO,
  'frontend',
  'docker',
  'nginx.conf.template'
);
const HELM_CONFIGMAP = path.join(
  REPO,
  'helm',
  'preloop',
  'templates',
  'configmap-nginx.yaml'
);

export interface RunningServer {
  origin: string;
  close: () => Promise<void>;
}

/** The enforced policy nginx sends, with extra script and connect sources. */
export function enforcedPolicy(
  extraScriptSrc: string[] = [],
  extraConnectSrc: string[] = []
): string {
  const template = fs.readFileSync(NGINX_TEMPLATE, 'utf-8');
  const match = /map "" \$preloop_csp_policy \{\s*default "([^"]+)";/.exec(
    template
  );
  if (!match) {
    throw new Error('no $preloop_csp_policy map in nginx.conf.template');
  }
  return match[1]
    .replace(
      '${PRELOOP_CSP_SCRIPT_SRC}',
      ["'self'", ...extraScriptSrc].join(' ')
    )
    .replace(
      '${PRELOOP_CSP_CONNECT_SRC}',
      ["'self'", 'ws:', 'wss:', 'data:', ...extraConnectSrc].join(' ')
    );
}

/** The `</head>` replacement the Helm chart injects for built-in analytics. */
export function helmAnalyticsTags(domain: string, scriptUrl: string): string {
  const configmap = fs.readFileSync(HELM_CONFIGMAP, 'utf-8');
  const match =
    /sub_filter '<\/head>' '(<script src="\/analytics-queue\.js">[^']*?)<\/head>';/.exec(
      configmap
    );
  if (!match) {
    throw new Error('no built-in analytics sub_filter in configmap-nginx.yaml');
  }
  return match[1]
    .replace('{{ .Values.analytics.domain }}', domain)
    .replace('{{ .Values.analytics.scriptUrl }}', scriptUrl);
}

const TYPES: Record<string, string> = {
  '.html': 'text/html; charset=utf-8',
  '.js': 'text/javascript; charset=utf-8',
  '.mjs': 'text/javascript; charset=utf-8',
  '.css': 'text/css; charset=utf-8',
  '.json': 'application/json',
  '.svg': 'image/svg+xml',
  '.png': 'image/png',
  '.jpg': 'image/jpeg',
  '.webp': 'image/webp',
  '.woff2': 'font/woff2',
  '.woff': 'font/woff',
  '.ico': 'image/x-icon',
  '.md': 'text/markdown; charset=utf-8',
  '.txt': 'text/plain; charset=utf-8',
  '.xml': 'application/xml',
  '.mp4': 'video/mp4',
  '.webm': 'video/webm',
};

function fileIn(dist: string, relative: string): string | null {
  const full = path.join(dist, relative);
  if (!full.startsWith(dist)) return null;
  return fs.existsSync(full) && fs.statSync(full).isFile() ? full : null;
}

/** nginx's try_files order for the console, see nginx.conf.template. */
function resolve(dist: string, pathname: string): string {
  const direct = fileIn(dist, pathname);
  if (direct) return direct;
  const prerendered = [
    /^\/(vs|resources|blog)\/([a-z0-9][a-z0-9-]*)\/?$/,
    /^\/()([a-z0-9][a-z0-9-]*)$/,
  ];
  for (const pattern of prerendered) {
    const match = pattern.exec(pathname);
    if (match) {
      const page = fileIn(
        dist,
        match[1] ? `${match[1]}/${match[2]}.html` : `${match[2]}.html`
      );
      if (page) return page;
    }
  }
  if (pathname === '/blog') {
    const index = fileIn(dist, 'blog/index.html');
    if (index) return index;
  }
  return path.join(dist, 'index.html');
}

function listen(server: http.Server): Promise<string> {
  return new Promise((done) => {
    server.listen(0, '127.0.0.1', () => {
      const { port } = server.address() as AddressInfo;
      done(`http://127.0.0.1:${port}`);
    });
  });
}

function closer(server: http.Server): () => Promise<void> {
  return () =>
    new Promise((done) => {
      server.closeAllConnections();
      server.close(() => done());
    });
}

export async function startConsoleServer(options: {
  dist: string;
  policy: string;
  analyticsTags?: string;
}): Promise<RunningServer> {
  const dist = path.resolve(options.dist);
  if (!fs.existsSync(path.join(dist, 'index.html'))) {
    throw new Error(`${dist} has no index.html; run the sweep build first`);
  }
  const server = http.createServer((request, response) => {
    const url = new URL(request.url || '/', 'http://localhost');
    const file = resolve(dist, decodeURIComponent(url.pathname));
    const type = TYPES[path.extname(file)] || 'application/octet-stream';
    let body: Buffer | string = fs.readFileSync(file);
    if (type.startsWith('text/html') && options.analyticsTags) {
      body = body
        .toString('utf-8')
        .replace('</head>', `${options.analyticsTags}</head>`);
    }
    response.writeHead(200, {
      'Content-Type': type,
      'Content-Security-Policy': options.policy,
      'Cache-Control': 'no-store',
    });
    response.end(body);
  });
  const origin = await listen(server);
  return { origin, close: closer(server) };
}

export interface AnalyticsEvent {
  name: string;
  domain: string;
  url: string;
}

/** A Plausible-style tracker: queue replay, pageview, custom events. */
const TRACKER = `(function () {
  var script = document.currentScript;
  var origin = new URL(script.src).origin;
  var domain = script.getAttribute('data-domain');
  function send(name) {
    fetch(origin + '/api/event', {
      method: 'POST',
      body: JSON.stringify({ name: name, domain: domain, url: location.href }),
      keepalive: true,
    });
  }
  var queued = (window.plausible && window.plausible.q) || [];
  window.plausible = function (name) { send(name); };
  send('pageview');
  for (var i = 0; i < queued.length; i++) send(queued[i][0]);
})();
`;

export async function startAnalyticsStub(): Promise<
  RunningServer & { events: AnalyticsEvent[] }
> {
  const events: AnalyticsEvent[] = [];
  const server = http.createServer((request, response) => {
    const url = new URL(request.url || '/', 'http://localhost');
    if (request.method === 'GET' && url.pathname === '/js/script.js') {
      response.writeHead(200, { 'Content-Type': 'text/javascript' });
      response.end(TRACKER);
      return;
    }
    if (request.method === 'POST' && url.pathname === '/api/event') {
      let raw = '';
      request.on('data', (chunk) => (raw += chunk));
      request.on('end', () => {
        try {
          events.push(JSON.parse(raw) as AnalyticsEvent);
        } catch {
          // A malformed body is not an event; the test sees none.
        }
        response.writeHead(202, { 'Access-Control-Allow-Origin': '*' });
        response.end('ok');
      });
      return;
    }
    response.writeHead(404);
    response.end();
  });
  const origin = await listen(server);
  return { origin, events, close: closer(server) };
}
