(function () {
  function csrfToken() {
    var el = document.querySelector('meta[name="csrf-token"]');
    return el ? el.getAttribute('content') : '';
  }

  document.body.addEventListener('htmx:configRequest', function (evt) {
    evt.detail.headers['X-CSRFToken'] = csrfToken();
  });

  window.toast = function (message, kind) {
    var region = document.getElementById('toast-region');
    if (!region || !message) return;
    var el = document.createElement('div');
    el.setAttribute('role', 'alert');
    el.className = 'lora-toast lora-toast-' + (kind || 'info');
    el.textContent = message;
    region.appendChild(el);
    setTimeout(function () { el.remove(); }, 3500);
  };

  window.refreshClaimBody = function (bodyUrl) {
    window.htmx.ajax('GET', bodyUrl, { target: '#claim-body', swap: 'outerHTML' });
  };
})();
