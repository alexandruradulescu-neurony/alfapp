(function () {
  function csrfToken() {
    var el = document.querySelector('meta[name="csrf-token"]');
    return el ? el.getAttribute('content') : '';
  }

  document.body.addEventListener('htmx:configRequest', function (evt) {
    evt.detail.headers['X-CSRFToken'] = csrfToken();
  });

  // Self-contained spinner (SMIL-animated SVG) — no CSS class needed, so it
  // works regardless of the compiled Tailwind build. Inherits button colour.
  var SPINNER_SVG =
    '<svg width="16" height="16" viewBox="0 0 24 24" fill="none" ' +
    'style="display:inline-block;vertical-align:-3px;margin-right:6px">' +
    '<circle cx="12" cy="12" r="9" stroke="currentColor" stroke-width="3" stroke-opacity="0.25"/>' +
    '<path d="M21 12a9 9 0 0 0-9-9" stroke="currentColor" stroke-width="3" stroke-linecap="round">' +
    '<animateTransform attributeName="transform" type="rotate" from="0 12 12" to="360 12 12" ' +
    'dur="0.8s" repeatCount="indefinite"/></path></svg>';

  // The submit/trigger buttons to lock while an action request is in flight.
  function actionButtons(el) {
    if (el.tagName === 'FORM') {
      return el.querySelectorAll('button[type="submit"], button:not([type])');
    }
    return [el];
  }

  // Lock + show progress the instant the request starts, so a slow money
  // action (a refund waits up to ~30s for WooCommerce) can't be double-clicked
  // and the user gets immediate feedback. Original markup is restored on finish.
  function lockAction(el) {
    var busy = el.dataset.busyLabel || 'Working…';
    actionButtons(el).forEach(function (btn) {
      if (btn.disabled || btn.dataset.lockedLabel !== undefined) return;
      btn.dataset.lockedLabel = btn.innerHTML;
      btn.disabled = true;
      btn.setAttribute('aria-busy', 'true');
      btn.innerHTML = SPINNER_SVG + busy;
    });
  }

  function unlockAction(el) {
    actionButtons(el).forEach(function (btn) {
      if (btn.dataset.lockedLabel === undefined) return;
      btn.innerHTML = btn.dataset.lockedLabel;
      delete btn.dataset.lockedLabel;
      btn.disabled = false;
      btn.removeAttribute('aria-busy');
    });
  }

  document.body.addEventListener('htmx:beforeRequest', function (evt) {
    var el = evt.detail.elt;
    if (el && el.hasAttribute('data-claim-action')) lockAction(el);
  });

  // Network failure / timeout: htmx may not fire afterRequest cleanly, so
  // re-enable here too and surface an error — never leave a button stuck.
  ['htmx:sendError', 'htmx:timeout'].forEach(function (ev) {
    document.body.addEventListener(ev, function (evt) {
      var el = evt.detail.elt;
      if (!el || !el.hasAttribute('data-claim-action')) return;
      unlockAction(el);
      window.toast((el.dataset.toastErr || 'Action failed') +
        ' (no response — check before retrying)', 'error');
    });
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
    unlockAction(el);  // re-enable + restore the button now the request is done
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
