// Queue web-analytics events fired before the tracker script loads (#2280).
// The Helm chart injects this file ahead of the tracker tag when
// analytics.enabled is set. It is a file rather than inline code so the
// console's Content-Security-Policy needs no 'unsafe-inline' in script-src.
window.plausible =
  window.plausible ||
  function () {
    (window.plausible.q = window.plausible.q || []).push(arguments);
  };
