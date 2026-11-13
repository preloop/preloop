// Inject the loading indicator via JS so it never appears in static
// snapshots (e.g. Wayback Machine) where JS doesn't execute. A file rather
// than inline code so the Content-Security-Policy needs no 'unsafe-inline'
// in script-src (#2280). Loaded as a classic script at the top of <body>.
(function () {
  var el = document.createElement('div');
  el.id = 'app-loading';
  document.body.prepend(el);

  function removeLoader() {
    if (!el.parentNode) return;
    el.classList.add('fade-out');
    el.addEventListener('transitionend', function () {
      el.remove();
    });
    // Fallback if transitionend doesn't fire
    setTimeout(function () {
      el.remove();
    }, 300);
  }

  // Remove once lit-app is defined
  if (customElements.get('lit-app')) {
    removeLoader();
  } else {
    customElements.whenDefined('lit-app').then(removeLoader);
  }

  // Safety timeout — remove after 8s regardless
  setTimeout(removeLoader, 8000);
})();
