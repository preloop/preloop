/** A stored return URL never grants authorization or leaves this origin. */
export function safeLoginReturn(value: string | null): string | null {
  if (!value || !value.startsWith('/') || value.startsWith('//')) return null;
  // Reject encoded path separators and controls too: browsers/routers differ
  // in when they decode them. Query data can contain encoded ordinary text.
  let decoded: string;
  try {
    decoded = decodeURIComponent(value);
  } catch {
    return null;
  }
  if (/[\\\u0000-\u001f\u007f]/.test(decoded) || decoded.startsWith('//'))
    return null;
  const url = new URL(value, window.location.origin);
  return url.origin === window.location.origin ? value : null;
}

export function consumeLoginReturn(): string | null {
  const value = localStorage.getItem('loginRedirect');
  localStorage.removeItem('loginRedirect');
  return safeLoginReturn(value);
}
