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
    if (bodyUrl) window.htmx.ajax('GET', bodyUrl, { target: '#claim-body', swap: 'outerHTML' });
  };

  // System-action buttons (the cross-app JSON endpoints) declare their intent
  // with data-* attributes instead of an eval'd hx-on handler — the production
  // CSP forbids unsafe-eval, so hx-on / Alpine expressions silently die. This
  // listener lives in an external file (no eval needed) and reads those
  // attributes to refresh / toast / redirect / close after the request.
  //
  // Contract (set on the element that fires the htmx request):
  //   data-claim-action            marker — required for this handler to act
  //   data-refresh-body="<url>"    GET this url and swap #claim-body on success
  //   data-toast-ok="<msg>"        success toast
  //   data-toast-err="<msg>"       failure toast (default: "Action failed")
  //   data-toast-indeterminate="<msg>"  shown on HTTP 502 (e.g. refund timeout)
  //   data-redirect="<url>"        navigate here on success (e.g. after delete)
  //   data-close-dialog="<id>"     <dialog>.close() this element id on success
  document.body.addEventListener('htmx:afterRequest', function (evt) {
    var el = evt.detail.elt;
    if (!el || !el.hasAttribute('data-claim-action')) return;
    var d = el.dataset;
    var ok = evt.detail.successful;
    var status = evt.detail.xhr ? evt.detail.xhr.status : 0;
    if (ok) {
      if (d.redirect) { window.location.href = d.redirect; return; }
      if (d.closeDialog) {
        var dlg = document.getElementById(d.closeDialog);
        if (dlg && dlg.close) dlg.close();
      }
      if (d.refreshBody) window.refreshClaimBody(d.refreshBody);
      if (d.toastOk) window.toast(d.toastOk, 'success');
    } else if (status === 502 && d.toastIndeterminate) {
      window.toast(d.toastIndeterminate, 'warning');
    } else {
      window.toast(d.toastErr || 'Action failed', 'error');
    }
  });

  // Refund modal: lock the amount to the full remaining sum when "Full" is
  // chosen, free it for "Partial". Called from an inline onchange (CSP-safe).
  window.loraRefundType = function (form) {
    if (!form) return;
    var full = form.querySelector('input[name="refund_type"][value="FULL"]');
    var amount = form.querySelector('input[name="amount"]');
    if (!amount) return;
    if (full && full.checked) {
      if (amount.dataset.remaining) amount.value = amount.dataset.remaining;
      amount.readOnly = true;
    } else {
      amount.readOnly = false;
    }
  };
})();
