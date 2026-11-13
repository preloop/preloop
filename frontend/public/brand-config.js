// Expose the build-time brand configuration as window.BRAND_CONFIG (#2280).
// vite-plugin-brand writes it into a JSON data block in <head>; this classic
// script reads it before any module runs. A file rather than inline code so
// the Content-Security-Policy needs no 'unsafe-inline' in script-src.
(function () {
  var block = document.getElementById('preloop-brand-config');
  if (!block) return;
  try {
    window.BRAND_CONFIG = JSON.parse(block.textContent || '{}');
  } catch (error) {
    console.error('Brand configuration is not valid JSON', error);
  }
})();
