import imaplib
import logging
from typing import Dict, Any
from django.utils import timezone
import requests
from apps.config.models import ServiceStatus, SystemSettings

logger = logging.getLogger(__name__)


class ConnectionTester:
    """Test connections to external services and update their status."""
    
    def __init__(self):
        self.timeout = 10  # seconds
    
    def test_ai(self) -> Dict[str, Any]:
        """Test AI provider connection."""
        try:
            settings = SystemSettings.objects.get(pk=1)
            
            if not settings.ai_api_key:
                return self._update_status(
                    'AI',
                    'disconnected',
                    success=False,
                    message='API key not configured'
                )
            
            # Simple connectivity test - check if endpoint is reachable
            try:
                response = requests.get(
                    f"{settings.ai_api_base}",
                    timeout=self.timeout
                )
                # If we get any response (even 401/403), the server is reachable
                return self._update_status(
                    'AI',
                    'connected',
                    success=True,
                    message='AI provider is reachable'
                )
            except requests.RequestException:
                # Server not reachable
                return self._update_status(
                    'AI',
                    'error',
                    success=False,
                    message='AI provider server not reachable'
                )
                
        except SystemSettings.DoesNotExist:
            return self._update_status(
                'AI',
                'disconnected',
                success=False,
                message='System settings not configured'
            )
        except Exception as e:
            return self._update_status(
                'AI',
                'error',
                success=False,
                message=str(e)
            )
    
    def test_imap(self) -> Dict[str, Any]:
        """Test IMAP email server connection."""
        try:
            settings = SystemSettings.objects.get(pk=1)
            
            if not settings.imap_host or not settings.imap_user or not settings.imap_pass:
                return self._update_status(
                    'IMAP',
                    'disconnected',
                    success=False,
                    message='IMAP credentials not configured'
                )
            
            # Attempt IMAP connection
            mail = imaplib.IMAP4_SSL(settings.imap_host, timeout=self.timeout)
            mail.login(settings.imap_user, settings.imap_pass)
            mail.logout()
            
            return self._update_status(
                'IMAP',
                'connected',
                success=True,
                message='IMAP server connection successful'
            )
            
        except SystemSettings.DoesNotExist:
            return self._update_status(
                'IMAP',
                'disconnected',
                success=False,
                message='System settings not configured'
            )
        except imaplib.IMAP4.error as e:
            return self._update_status(
                'IMAP',
                'error',
                success=False,
                message=f'IMAP authentication failed: {str(e)}'
            )
        except Exception as e:
            return self._update_status(
                'IMAP',
                'error',
                success=False,
                message=str(e)
            )
    
    def test_zendesk(self) -> Dict[str, Any]:
        """Test Zendesk API connection."""
        try:
            settings = SystemSettings.objects.get(pk=1)
            
            if not settings.zd_subdomain or not settings.zd_token:
                return self._update_status(
                    'ZENDESK',
                    'disconnected',
                    success=False,
                    message='Zendesk credentials not configured'
                )
            
            # Test API access
            url = f"https://{settings.zd_subdomain}.zendesk.com/api/v2/tickets.json"
            auth = (f"{settings.zd_email}/token", settings.zd_token)
            
            response = requests.get(
                url,
                auth=auth,
                timeout=self.timeout
            )
            
            if response.status_code == 200:
                return self._update_status(
                    'ZENDESK',
                    'connected',
                    success=True,
                    message='Zendesk API connection successful'
                )
            elif response.status_code == 401:
                return self._update_status(
                    'ZENDESK',
                    'error',
                    success=False,
                    message='Invalid Zendesk credentials'
                )
            else:
                return self._update_status(
                    'ZENDESK',
                    'error',
                    success=False,
                    message=f'Zendesk API error: {response.status_code}'
                )
                
        except SystemSettings.DoesNotExist:
            return self._update_status(
                'ZENDESK',
                'disconnected',
                success=False,
                message='System settings not configured'
            )
        except requests.RequestException as e:
            return self._update_status(
                'ZENDESK',
                'error',
                success=False,
                message=str(e)
            )
    
    def test_paypal(self) -> Dict[str, Any]:
        """Test PayPal API connection."""
        try:
            settings = SystemSettings.objects.get(pk=1)
            
            if not settings.paypal_client_id or not settings.paypal_secret:
                return self._update_status(
                    'PAYPAL',
                    'disconnected',
                    success=False,
                    message='PayPal credentials not configured'
                )
            
            # Get OAuth token (PayPal API test)
            auth_url = 'https://api-m.sandbox.paypal.com/v1/oauth2/token'
            
            response = requests.post(
                auth_url,
                data={'grant_type': 'client_credentials'},
                auth=(settings.paypal_client_id, settings.paypal_secret),
                timeout=self.timeout
            )
            
            if response.status_code == 200:
                return self._update_status(
                    'PAYPAL',
                    'connected',
                    success=True,
                    message='PayPal API connection successful'
                )
            elif response.status_code == 401:
                return self._update_status(
                    'PAYPAL',
                    'error',
                    success=False,
                    message='Invalid PayPal credentials'
                )
            else:
                return self._update_status(
                    'PAYPAL',
                    'error',
                    success=False,
                    message=f'PayPal API error: {response.status_code}'
                )
                
        except SystemSettings.DoesNotExist:
            return self._update_status(
                'PAYPAL',
                'disconnected',
                success=False,
                message='System settings not configured'
            )
        except requests.RequestException as e:
            return self._update_status(
                'PAYPAL',
                'error',
                success=False,
                message=str(e)
            )
    
    def get_scheduler_status(self) -> Dict[str, Any]:
        """Report on the cron dispatcher (run_scheduled_jobs) from its heartbeat.

        There is no in-process scheduler to poll — health is inferred from the
        master switch and how recently the Railway cron last recorded a run."""
        from datetime import timedelta
        STALE_AFTER = timedelta(hours=2)   # a run older than this means the cron is silent
        try:
            status = ServiceStatus.objects.filter(service='SCHEDULER').first()
            if status and not status.is_enabled:
                return {'success': True, 'status': 'stopped',
                        'message': 'Scheduled jobs are disabled (master switch off)'}
            if not status or not status.last_checked:
                return {'success': False, 'status': 'disconnected',
                        'message': 'No scheduled run recorded yet — is the Railway cron job set up?'}
            last = status.last_checked
            stamp = last.strftime('%b %d, %H:%M UTC')
            if timezone.now() - last <= STALE_AFTER:
                if status.last_error:
                    return {'success': False, 'status': 'error',
                            'message': f'Last run {stamp} had errors: {status.last_error[:120]}'}
                return {'success': True, 'status': 'running', 'message': f'Last run {stamp}'}
            return {'success': False, 'status': 'error',
                    'message': f'No run since {stamp} — check the Railway cron job'}
        except Exception as e:
            return {'success': False, 'status': 'error', 'message': str(e)}
    
    def _update_status(
        self,
        service: str,
        status: str,
        success: bool = True,
        message: str = '',
        metadata: Dict[str, Any] = None
    ) -> Dict[str, Any]:
        """Update service status in database and return result."""
        status_obj, created = ServiceStatus.objects.get_or_create(
            service=service,
            defaults={'status': status, 'is_enabled': True}
        )
        
        status_obj.status = status
        status_obj.last_checked = timezone.now()
        if not success and message:
            status_obj.last_error = message
        elif success:
            status_obj.last_error = ''
        if metadata:
            status_obj.metadata = metadata
        status_obj.save()
        
        return {
            'success': success,
            'status': status,
            'message': message,
            'service': service
        }
    
    def test_woocommerce(self) -> Dict[str, Any]:
        """Test the WooCommerce REST API connection (read-only — never refunds).

        Hits an authenticated read endpoint so it verifies the store URL AND
        that the consumer key/secret are valid with read access.
        """
        try:
            settings = SystemSettings.objects.get(pk=1)
            url = (settings.woocommerce_store_url or '').strip().rstrip('/')
            key = (settings.woocommerce_consumer_key or '').strip()
            secret = (settings.woocommerce_consumer_secret or '').strip()
            if not (url and key and secret):
                return self._update_status(
                    'WOOCOMMERCE', 'disconnected', success=False,
                    message='WooCommerce store URL and API credentials not configured')

            response = requests.get(
                f"{url}/wp-json/wc/v3/orders",
                params={'per_page': 1},
                auth=(key, secret),
                timeout=self.timeout,
            )
            if response.status_code == 200:
                return self._update_status(
                    'WOOCOMMERCE', 'connected', success=True,
                    message='WooCommerce connection successful')
            elif response.status_code in (401, 403):
                return self._update_status(
                    'WOOCOMMERCE', 'error', success=False,
                    message='WooCommerce rejected the credentials — check the '
                            'consumer key/secret and that the key is Read/Write')
            elif response.status_code == 404:
                return self._update_status(
                    'WOOCOMMERCE', 'error', success=False,
                    message='WooCommerce REST API not found — check the store URL')
            else:
                return self._update_status(
                    'WOOCOMMERCE', 'error', success=False,
                    message=f'WooCommerce API error: HTTP {response.status_code}')
        except SystemSettings.DoesNotExist:
            return self._update_status(
                'WOOCOMMERCE', 'disconnected', success=False,
                message='System settings not configured')
        except requests.RequestException as e:
            return self._update_status(
                'WOOCOMMERCE', 'error', success=False, message=str(e))

    def test_all_services(self) -> Dict[str, Dict[str, Any]]:
        """Test all services and return results."""
        results = {
            'AI': self.test_ai(),
            'IMAP': self.test_imap(),
            'ZENDESK': self.test_zendesk(),
            'PAYPAL': self.test_paypal(),
            'SCHEDULER': self.get_scheduler_status(),
        }
        return results
