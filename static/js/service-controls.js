/**
 * Service Controls JavaScript
 * Handles AJAX interactions for service status and controls
 */

// CSRF token helper
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

const csrftoken = getCookie('csrftoken');

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
    
    try {
        const response = await fetch(url, {
            method: 'POST',
            headers: {
                'Content-Type': 'application/json',
                'X-CSRFToken': csrftoken
            }
        });
        
        const data = await response.json();
        
        if (data.success) {
            showToast(`${service}: ${data.message}`, 'success');
        } else {
            showToast(`${service}: ${data.message}`, 'error');
        }
        
        // Refresh status after test
        setTimeout(() => refreshServiceStatus(service), 1000);
        
    } catch (error) {
        showToast(`Failed to test ${service}: ${error.message}`, 'error');
    }
}

// Toggle service enabled state
async function toggleService(service, enabled) {
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

// Control scheduler (start/stop)
async function controlScheduler(action) {
    const url = `/api/services/scheduler-${action}/`;
    
    try {
        const response = await fetch(url, {
            method: 'POST',
            headers: {
                'Content-Type': 'application/json',
                'X-CSRFToken': csrftoken
            }
        });
        
        const data = await response.json();
        
        if (data.success) {
            showToast(`Scheduler: ${data.message}`, 'success');
            setTimeout(() => refreshServiceStatus('SCHEDULER'), 1000);
        } else {
            showToast(`Scheduler: ${data.message}`, 'error');
        }
        
    } catch (error) {
        showToast(`Failed to ${action} scheduler: ${error.message}`, 'error');
    }
}

// Toggle scheduler enabled state
async function toggleSchedulerEnabled(enabled) {
    const url = '/api/services/scheduler-toggle/';
    
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
            showToast(`Scheduler ${action}`, 'success');
        } else {
            showToast(`Scheduler: ${data.message}`, 'error');
            // Revert toggle on failure
            const checkbox = document.getElementById('scheduler-enabled');
            if (checkbox) {
                checkbox.checked = !enabled;
            }
        }
        
    } catch (error) {
        showToast(`Failed to toggle scheduler: ${error.message}`, 'error');
        // Revert toggle on failure
        const checkbox = document.getElementById('scheduler-enabled');
        if (checkbox) {
            checkbox.checked = !enabled;
        }
    }
}

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
