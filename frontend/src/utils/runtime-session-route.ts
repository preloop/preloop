/**
 * `/console/runtime-sessions/<id>` is the URL people guess and paste. The
 * sessions page reads the selection from `?sessionId=`, so the path form
 * redirects there and keeps any other query parameters and the hash.
 */
export function runtimeSessionPathRedirect(
  sessionId: string,
  search = '',
  hash = ''
): string {
  const params = new URLSearchParams(search);
  params.set('sessionId', sessionId);
  return `/console/runtime-sessions?${params.toString()}${hash}`;
}
