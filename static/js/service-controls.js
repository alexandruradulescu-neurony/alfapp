/**
 * Service Controls JavaScript
 * Wires the settings page's Test buttons and instant switches via addEventListener
 * (no inline handlers — robust under any Content-Security-Policy).
 */

// CSRF token helper - try multiple sources
function getCookie(name) {
    let cookieValue = null;
    if (document.cookie && document.cookie !== '') {
        const cookies = document.cookie.split(';');
        for (let i = 0; i < cookies.length; i++) {
            const cookie = cookies[i].trim();
            if (cookie.substring(0, name.length + 1) === (name + '=')) {
                cookieValue = decodeURIComponent(cookie.substring(name.length + 1));
                break;
            }
        }
    }
    return cookieValue;
}

// Resolve the CSRF token from any of the usual places.
let csrftoken = getCookie('csrftoken');
if (!csrftoken) {
    const metaTag = document.querySelector('meta[name="csrf-token"]');
    if (metaTag) csrftoken = metaTag.getAttribute('content');
}
if (!csrftoken) {
    const input = document.querySelector('input[name="csrfmiddlewaretoken"]');
    if (input) csrftoken = input.value;
}
if (!csrftoken) {
    const input = document.getElementById('csrf_token');
    if (input) csrftoken = input.value;
}

console.log('Service Controls loaded. CSRF token:', csrftoken ? 'present' : 'MISSING');

// Show toast notification
function showToast(message, type = 'info') {
    const toast = document.getElementById('serviceToast');
    const toastMessage = document.getElementById('toastMessage');
    if (!toast || !toastMessage) return;

    toastMessage.textContent = message;
    toast.classList.remove('alert-info', 'alert-success', 'alert-error', 'alert-warning');
    toast.classList.add(
        type === 'success' ? 'alert-success'
        : type === 'error' ? 'alert-error'
        : type === 'warning' ? 'alert-warning'
        : 'alert-info');
    toast.classList.remove('alert-hidden', 'opacity-0');
    toast.classList.add('opacity-100');
    setTimeout(hideToast, 3000);
}

function hideToast() {
    const toast = document.getElementById('serviceToast');
    if (!toast) return;
    toast.classList.add('alert-hidden', 'opacity-0');
    toast.classList.remove('opacity-100');
}

// Test a connection. The status badge reflects the stored result on next page load.
async function testService(service) {
    showToast(`Testing ${service}…`, 'info');
    try {
        const response = await fetch(`/api/services/${service}/test/`, {
            method: 'POST',
            headers: { 'Content-Type': 'application/json', 'X-CSRFToken': csrftoken },
        });
        if (response.status === 401 || response.status === 403) {
            showToast('Manager access required.', 'error');
            return;
        }
        const data = await response.json();
        showToast(`${service}: ${data.message || (data.success ? 'OK' : 'failed')}`,
                  data.success ? 'success' : 'error');
    } catch (error) {
        showToast(`Failed to test ${service}: ${error.message}`, 'error');
    }
}

function revertToggle(selector, enabled) {
    const cb = document.querySelector(selector);
    if (cb) cb.checked = !enabled;
}

// ServiceStatus enable flag (the Scheduled-jobs master switch).
async function toggleService(service, enabled) {
    try {
        const response = await fetch(`/api/services/${service}/toggle/`, {
            method: 'POST',
            headers: { 'Content-Type': 'application/json', 'X-CSRFToken': csrftoken },
            body: JSON.stringify({ enabled: enabled }),
        });
        const data = await response.json();
        if (data.success) {
            showToast(data.message || `${service} ${enabled ? 'enabled' : 'disabled'}`, 'success');
        } else {
            showToast(`${service}: ${data.message || 'failed'}`, 'error');
            revertToggle(`[data-service-toggle="${service}"]`, enabled);
        }
    } catch (error) {
        showToast(`Failed to toggle ${service}: ${error.message}`, 'error');
        revertToggle(`[data-service-toggle="${service}"]`, enabled);
    }
}

// SystemSettings boolean automation switch — instant, no Save.
async function toggleSettingFlag(flag, enabled) {
    try {
        const response = await fetch('/api/services/settings-flag/', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json', 'X-CSRFToken': csrftoken },
            body: JSON.stringify({ flag: flag, enabled: enabled }),
        });
        const data = await response.json();
        if (data.success) {
            showToast(data.message || `${flag} updated`, 'success');
        } else {
            showToast(`${flag}: ${data.error || data.message || 'failed'}`, 'error');
            revertToggle(`#${flag}`, enabled);
        }
    } catch (error) {
        showToast(`Failed to update ${flag}: ${error.message}`, 'error');
        revertToggle(`#${flag}`, enabled);
    }
}

// Wire everything once the DOM is ready (no inline onclick/onchange anywhere).
document.addEventListener('DOMContentLoaded', function () {
    document.querySelectorAll('[data-test]').forEach(function (btn) {
        btn.addEventListener('click', function () { testService(btn.dataset.test); });
    });
    document.querySelectorAll('[data-service-toggle]').forEach(function (el) {
        el.addEventListener('change', function () { toggleService(el.dataset.serviceToggle, el.checked); });
    });
    document.querySelectorAll('[data-flag-toggle]').forEach(function (el) {
        el.addEventListener('change', function () { toggleSettingFlag(el.dataset.flagToggle, el.checked); });
    });
    document.querySelectorAll('[data-toast-close]').forEach(function (el) {
        el.addEventListener('click', hideToast);
    });
});
