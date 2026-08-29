/**
 * Vite `server.allowedHosts` for the console when it is reached by a
 * public hostname (Docker Compose behind nginx, remote git checkouts).
 */

export function parseAllowedHosts(
  value: string | undefined
): true | string[] | undefined {
  if (!value) {
    return undefined;
  }
  const trimmed = value.trim();
  if (trimmed === 'true' || trimmed === 'all') {
    return true;
  }
  const hosts = trimmed
    .split(',')
    .map((host) => host.trim())
    .filter(Boolean);
  return hosts.length ? hosts : undefined;
}

export function hostnameFromUrl(value: string | undefined): string | undefined {
  if (!value) {
    return undefined;
  }
  try {
    const hostname = new URL(value).hostname;
    if (
      hostname &&
      hostname !== 'localhost' &&
      hostname !== '127.0.0.1' &&
      hostname !== '::1'
    ) {
      return hostname;
    }
  } catch {
    return undefined;
  }
  return undefined;
}

export function resolveAllowedHosts(input: {
  allowedHosts?: string;
  additionalHosts?: string;
  hmrHost?: string;
  apiUrl?: string;
  apiProxyTarget?: string;
}): true | string[] | undefined {
  const explicit = parseAllowedHosts(
    input.allowedHosts || input.additionalHosts
  );
  if (explicit === true) {
    return true;
  }
  const hosts = new Set<string>(Array.isArray(explicit) ? explicit : []);
  if (input.hmrHost) {
    hosts.add(input.hmrHost);
  }
  const fromApi = hostnameFromUrl(input.apiUrl);
  if (fromApi) {
    hosts.add(fromApi);
  }
  // Docker Compose console is reached through a public Host header.
  if (input.apiProxyTarget === 'http://api:8000' && hosts.size === 0) {
    return true;
  }
  return hosts.size ? [...hosts] : undefined;
}
