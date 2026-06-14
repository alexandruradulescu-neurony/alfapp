/**
 * Service Controls JavaScript
 * Handles AJAX interactions for service status and controls
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

// Try to get CSRF token from multiple sources
let csrftoken = getCookie('csrftoken');
if (!csrftoken) {
    // Try to get from meta tag
    const metaTag = document.querySelector('meta[name="csrf-token"]');
    if (metaTag) {
        csrftoken = metaTag.getAttribute('content');
    }
}
if (!csrftoken) {
    // Try to get from hidden input (Django forms add this)
    const input = document.querySelector('input[name="csrfmiddlewaretoken"]');
    if (input) {
        csrftoken = input.value;
    }
}
if (!csrftoken) {
    // Try to get from our custom hidden input
    const input = document.getElementById('csrf_token');
    if (input) {
        csrftoken = input.value;
    }
}

// Debug log
console.log('Service Controls loaded, CSRF token:', csrftoken ? csrftoken.substring(0, 10) + '...' : 'MISSING');

// Show toast notification
function showToast(message, type = 'info') {
    const toast = document.getElementById('serviceToast');
    const toastMessage = document.getElementById('toastMessage');
    
    toastMessage.textContent = message;
    
    // Update alert type
    toast.classList.remove('alert-info', 'alert-success', 'alert-error', 'alert-warning');
    if (type === 'success') {
        toast.classList.add('alert-success');
    } else if (type === 'error') {
        toast.classList.add('alert-error');
    } else if (type === 'warning') {
        toast.classList.add('alert-warning');
    } else {
        toast.classList.add('alert-info');
    }
    
    // Show toast
    toast.classList.remove('alert-hidden', 'opacity-0');
    toast.classList.add('opacity-100');
    
    // Auto-hide after 3 seconds
    setTimeout(() => hideToast(), 3000);
}

// Hide toast
function hideToast() {
    const toast = document.getElementById('serviceToast');
    toast.classList.add('alert-hidden', 'opacity-0');
    toast.classList.remove('opacity-100');
}

// Test a service connection
async function testService(service) {
    const url = `/api/services/${service}/test/`;
    
    console.log(`Testing ${service}...`);
    
    try {
        const response = await fetch(url, {
            method: 'POST',
            headers: {
                'Content-Type': 'application/json',
                'X-CSRFToken': csrftoken
            }
        });
        
        console.log(`Response status: ${response.status}`);
        
        if (response.status === 403) {
            showToast('Authentication required. Please log in.', 'error');
            return;
        }
        
        const data = await response.json();
        
        if (data.success) {
            showToast(`${service}: ${data.message}`, 'success');
        } else {
            showToast(`${service}: ${data.message}`, 'error');
        }
        
        // Refresh status after test
        setTimeout(() => refreshServiceStatus(service), 1000);
        
    } catch (error) {
        console.error(`Error testing ${service}:`, error);
        showToast(`Failed to test ${service}: ${error.message}`, 'error');
    }
}

// Toggle service enabled state
async function toggleService(service, enabled) {
    // Sidebar is a special case - it doesn't have a real backend service
    if (service === 'SIDEBAR') {
        showToast('Sidebar authentication enabled/disabled', 'success');
        return;
    }
    
    const url = `/api/services/${service}/toggle/`;
    
    try {
        const response = await fetch(url, {
            method: 'POST',
            headers: {
                'Content-Type': 'application/json',
                'X-CSRFToken': csrftoken
            },
            body: JSON.stringify({ enabled: enabled })
        });
        
        const data = await response.json();
        
        if (data.success) {
            const action = enabled ? 'enabled' : 'disabled';
            showToast(`${service} ${action}`, 'success');
        } else {
            showToast(`${service}: ${data.message}`, 'error');
            // Revert toggle on failure
            const checkbox = document.querySelector(`input[data-service="${service}"]`);
            if (checkbox) {
                checkbox.checked = !enabled;
            }
        }
        
    } catch (error) {
        showToast(`Failed to toggle ${service}: ${error.message}`, 'error');
        // Revert toggle on failure
        const checkbox = document.querySelector(`input[data-service="${service}"]`);
        if (checkbox) {
            checkbox.checked = !enabled;
        }
    }
}

// Toggle a SystemSettings boolean automation switch — instant, no Save needed.
// The checkbox's element id must equal the flag name.
async function toggleSettingFlag(flag, enabled) {
    try {
        const response = await fetch('/api/services/settings-flag/', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json', 'X-CSRFToken': csrftoken },
            body: JSON.stringify({ flag: flag, enabled: enabled })
        });
        const data = await response.json();
        if (data.success) {
            showToast(data.message || `${flag} updated`, 'success');
        } else {
            showToast(`${flag}: ${data.error || data.message || 'failed'}`, 'error');
            const cb = document.getElementById(flag);
            if (cb) cb.checked = !enabled;
        }
    } catch (error) {
        showToast(`Failed to update ${flag}: ${error.message}`, 'error');
        const cb = document.getElementById(flag);
        if (cb) cb.checked = !enabled;
    }
}

// The "Scheduled Jobs" master switch reuses the generic toggleService('SCHEDULER', …)
// path; there is no in-process scheduler to start/stop (jobs run via Railway cron).

// Refresh single service status
async function refreshServiceStatus(service) {
    try {
        const response = await fetch(`/api/services/status/${service}/`);
        const data = await response.json();
        
        // Find the card for this service
        const card = document.querySelector(`input[data-service="${service}"]`)?.closest('.card');
        if (!card) return;
        
        // Update badge
        const badge = card.querySelector('.badge');
        if (badge) {
            badge.className = `badge badge-${data.status_color} gap-1`;
            badge.innerHTML = `<i class="bi bi-circle-fill text-[0.4rem]"></i>${data.status_name}`;
        }
        
        // Update last checked time
        const timeEl = card.querySelector('p.text-xs');
        if (timeEl && data.last_checked) {
            const date = new Date(data.last_checked);
            const formatted = date.toLocaleString('en-US', { month: 'short', day: 'numeric', hour: '2-digit', minute: '2-digit' });
            timeEl.textContent = `Last checked: ${formatted}`;
        }
        
    } catch (error) {
        console.error(`Failed to refresh ${service} status:`, error);
    }
}

// Refresh all service statuses
async function refreshAllStatuses() {
    const services = ['AI', 'IMAP', 'ZENDESK', 'PAYPAL', 'SCHEDULER', 'SCREENSHOT'];
    
    showToast('Checking all service statuses...', 'info');
    
    for (const service of services) {
        await refreshServiceStatus(service);
    }
    
    showToast('All service statuses refreshed', 'success');
}

// Auto-refresh statuses every 2 minutes
document.addEventListener('DOMContentLoaded', function() {
    // Initial refresh after 1 second
    setTimeout(refreshAllStatuses, 1000);
    
    // Auto-refresh every 2 minutes
    setInterval(refreshAllStatuses, 120000);
});
