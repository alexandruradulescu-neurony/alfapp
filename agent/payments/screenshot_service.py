"""
Zendesk Screenshot Capture Service for LORA.
Uses Playwright for browser-based screenshot capture of Zendesk tickets.
"""

import logging
import os
import tempfile
from datetime import datetime
from pathlib import Path
from typing import Optional, Tuple

from django.conf import settings
from django.core.files.base import ContentFile

from apps.config.models import SystemSettings
from apps.payments.models import Dispute, DisputeScreenshot, DisputeActivityLog

logger = logging.getLogger(__name__)

# Playwright imports - will be imported lazily to avoid errors if not installed
_playwright = None
_async_playwright = None


def _get_playwright():
    """Lazy import of Playwright to avoid errors if not installed."""
    global _playwright, _async_playwright
    if _playwright is None:
        try:
            from playwright.sync_api import sync_playwright as _playwright
            logger.debug("Playwright imported successfully")
        except ImportError:
            logger.error("Playwright not installed. Run: pip install playwright && playwright install")
            raise ImportError("Playwright is required for screenshot capture. Install with: pip install playwright && playwright install")
    return _playwright


def _authenticate_to_zendesk(page, subdomain: str, email: str, password: str) -> bool:
    """
    Authenticate to Zendesk using browser.
    
    Args:
        page: Playwright page object
        subdomain: Zendesk subdomain
        email: Agent email
        password: Agent password
        
    Returns:
        True if authentication successful, False otherwise
    """
    try:
        login_url = f"https://{subdomain}.zendesk.com/access/legacy"
        logger.info(f"Navigating to Zendesk login: {login_url}")
        
        page.goto(login_url, timeout=30000)
        page.wait_for_load_state('networkidle')
        
        # Check if already logged in by looking for agent interface elements
        if _is_logged_in(page):
            logger.info("Already authenticated to Zendesk")
            return True
        
        # Try to find and fill login form
        # Zendesk uses various selectors, try multiple approaches
        try:
            # Try new Zendesk login form
            email_input = page.locator('input[type="email"], input[name="email"], input[id*="email"]')
            if email_input.count() > 0:
                email_input.first.fill(email)
                logger.debug("Filled email field")
                
                # Click continue/next if present
                continue_btn = page.locator('button[type="submit"], input[type="submit"], button:has-text("Continue"), button:has-text("Next")')
                if continue_btn.count() > 0:
                    continue_btn.first.click()
                    page.wait_for_timeout(2000)  # Wait for password field to appear
            
            # Fill password
            password_input = page.locator('input[type="password"], input[name="password"], input[id*="password"]')
            if password_input.count() > 0:
                password_input.first.fill(password)
                logger.debug("Filled password field")
            
            # Submit login form
            submit_btn = page.locator('button[type="submit"], input[type="submit"], button:has-text("Sign in"), button:has-text("Log in")')
            if submit_btn.count() > 0:
                submit_btn.first.click()
                logger.debug("Submitted login form")
                
                # Wait for navigation after login
                page.wait_for_load_state('networkidle', timeout=30000)
                
        except Exception as e:
            logger.warning(f"Could not interact with login form: {e}")
            # Try alternative: navigate directly to tickets page which may trigger login
            page.goto(f"https://{subdomain}.zendesk.com/agent/tickets", timeout=30000)
            page.wait_for_load_state('networkidle')
        
        # Check if logged in after authentication attempt
        if _is_logged_in(page):
            logger.info("Successfully authenticated to Zendesk")
            return True
        else:
            logger.warning("Authentication may have failed - could not verify logged-in state")
            return False
            
    except Exception as e:
        logger.error(f"Error during Zendesk authentication: {e}")
        return False


def _is_logged_in(page) -> bool:
    """
    Check if the page shows a logged-in Zendesk agent interface.
    
    Args:
        page: Playwright page object
        
    Returns:
        True if logged in, False otherwise
    """
    try:
        # Look for elements that indicate logged-in state
        indicators = [
            'a[href*="/agent/tickets"]',  # Tickets navigation
            '[data-test-id="agent-header"]',  # Agent header
            '.agent-header',  # Agent header class
            'nav[class*="agent"]',  # Agent navigation
            '[class*="sidebar"]',  # Sidebar navigation
        ]
        
        for selector in indicators:
            try:
                if page.locator(selector).count() > 0:
                    return True
            except Exception:
                continue
        
        # Also check we're not on a login page
        current_url = page.url
        if 'login' in current_url.lower() or 'access' in current_url.lower():
            return False
            
        return False
        
    except Exception as e:
        logger.warning(f"Error checking login state: {e}")
        return False


def _capture_screenshot(page, ticket_id: str, output_path: str) -> bool:
    """
    Capture full-page screenshot of Zendesk ticket.
    
    Args:
        page: Playwright page object
        ticket_id: Zendesk ticket ID
        output_path: Path to save screenshot
        
    Returns:
        True if successful, False otherwise
    """
    try:
        # Navigate to ticket page
        system_settings = SystemSettings.get_instance()
        subdomain = system_settings.zd_subdomain
        ticket_url = f"https://{subdomain}.zendesk.com/agent/tickets/{ticket_id}"
        
        logger.info(f"Navigating to ticket: {ticket_url}")
        page.goto(ticket_url, timeout=30000, wait_until='networkidle')
        
        # Wait for ticket content to load
        page.wait_for_selector('[class*="ticket"]', timeout=10000)
        page.wait_for_timeout(2000)  # Extra wait for dynamic content
        
        # Capture full-page screenshot
        page.screenshot(path=output_path, full_page=True)
        
        logger.info(f"Screenshot captured successfully: {output_path}")
        return True
        
    except Exception as e:
        logger.error(f"Error capturing screenshot: {e}")
        return False


def capture_zendesk_screenshots(dispute_id: int, auto_retry: bool = True, max_retries: int = 2) -> Tuple[bool, str]:
    """
    Capture screenshots of Zendesk ticket for a dispute.
    
    This function:
    1. Fetches the Dispute record and validates it has zd_ticket_id
    2. Authenticates to Zendesk via browser using credentials from SystemSettings
    3. Navigates to the ticket page
    4. Captures a full-page screenshot
    5. Saves as DisputeScreenshot record
    6. Updates dispute status: MATCHED -> GATHERING_DATA -> DOCUMENTS_READY
    7. Logs action to DisputeActivityLog
    
    Args:
        dispute_id: The Django Dispute record ID
        auto_retry: If True, retry on failure (default True)
        max_retries: Maximum number of retry attempts (default 2)
        
    Returns:
        Tuple of (success: bool, message: str)
    """
    try:
        # 1. Fetch and validate Dispute record
        dispute = Dispute.objects.filter(id=dispute_id).first()
        
        if not dispute:
            error_msg = f"Dispute #{dispute_id} not found"
            logger.error(error_msg)
            return False, error_msg
        
        if not dispute.zd_ticket_id:
            error_msg = f"Dispute #{dispute_id} has no Zendesk ticket ID (zd_ticket_id)"
            logger.warning(error_msg)
            return False, error_msg
        
        logger.info(f"Starting screenshot capture for Dispute #{dispute_id} (Zendesk ticket: {dispute.zd_ticket_id})")
        
        # Get Zendesk credentials from SystemSettings
        system_settings = SystemSettings.get_instance()
        subdomain = system_settings.zd_subdomain
        email = system_settings.zd_agent_email
        password = system_settings.zd_agent_password
        
        if not all([subdomain, email, password]):
            error_msg = "Zendesk browser credentials not configured in SystemSettings (zd_subdomain, zd_agent_email, zd_agent_password)"
            logger.error(error_msg)
            return False, error_msg
        
        # Attempt screenshot capture with retry logic
        attempt = 0
        last_error = None
        
        while attempt <= max_retries:
            if attempt > 0:
                logger.info(f"Retry attempt {attempt}/{max_retries} for Dispute #{dispute_id}")
            
            try:
                success, message = _capture_screenshot_for_dispute(
                    dispute=dispute,
                    subdomain=subdomain,
                    email=email,
                    password=password,
                )
                
                if success:
                    # Update dispute status progression
                    _update_dispute_status(dispute)
                    
                    # Log the activity
                    DisputeActivityLog.objects.create(
                        dispute=dispute,
                        action='SCREENSHOTS_CAPTURED',
                        details=f"Zendesk ticket {dispute.zd_ticket_id} screenshot captured. {message}",
                    )
                    
                    logger.info(f"Screenshot capture completed for Dispute #{dispute_id}: {message}")
                    return True, message
                else:
                    last_error = message
                    logger.warning(f"Screenshot capture failed: {message}")
                    
            except Exception as e:
                last_error = str(e)
                logger.error(f"Screenshot capture error: {e}")
            
            attempt += 1
            
            if attempt <= max_retries and auto_retry:
                # Wait before retry
                import time
                time.sleep(2)
        
        # All retries exhausted
        error_msg = f"Screenshot capture failed after {max_retries} retries: {last_error}"
        logger.error(error_msg)
        
        # Log the failure
        DisputeActivityLog.objects.create(
            dispute=dispute,
            action='SCREENSHOTS_CAPTURED',
            details=f"FAILED: {error_msg}",
        )
        
        return False, error_msg
        
    except Exception as e:
        error_msg = f"Unexpected error in capture_zendesk_screenshots: {e}"
        logger.exception(error_msg)
        return False, error_msg


def _capture_screenshot_for_dispute(
    dispute: Dispute,
    subdomain: str,
    email: str,
    password: str,
) -> Tuple[bool, str]:
    """
    Internal function to capture screenshot for a dispute using Playwright.
    
    Args:
        dispute: Dispute object
        subdomain: Zendesk subdomain
        email: Agent email
        password: Agent password
        
    Returns:
        Tuple of (success: bool, message: str)
    """
    playwright_func = _get_playwright()
    browser = None
    
    try:
        # Launch browser
        browser = playwright_func().chromium.launch(
            headless=True,
            args=[
                '--no-sandbox',
                '--disable-setuid-sandbox',
                '--disable-dev-shm-usage',
                '--disable-accelerated-2d-canvas',
                '--disable-gpu',
            ]
        )
        
        # Create context with realistic viewport
        context = browser.new_context(
            viewport={'width': 1920, 'height': 1080},
            user_agent='Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36',
        )
        
        page = context.new_page()
        
        # Authenticate to Zendesk
        auth_success = _authenticate_to_zendesk(page, subdomain, email, password)
        
        if not auth_success:
            return False, "Failed to authenticate to Zendesk"
        
        # Capture screenshot
        ticket_id = dispute.zd_ticket_id
        temp_dir = tempfile.gettempdir()
        temp_path = os.path.join(temp_dir, f"zendesk_ticket_{ticket_id}_{datetime.now().strftime('%Y%m%d_%H%M%S')}.png")
        
        screenshot_success = _capture_screenshot(page, ticket_id, temp_path)
        
        if not screenshot_success:
            return False, "Failed to capture screenshot"
        
        # Read the screenshot file
        with open(temp_path, 'rb') as f:
            image_content = f.read()
        
        # Create DisputeScreenshot record
        screenshot = DisputeScreenshot.objects.create(
            dispute=dispute,
            description=f"Zendesk ticket {ticket_id} - Full page screenshot",
            page_url=f"https://{subdomain}.zendesk.com/agent/tickets/{ticket_id}",
        )
        
        # Save the image file
        screenshot.image.save(
            f"ticket_{ticket_id}_{datetime.now().strftime('%Y%m%d_%H%M%S')}.png",
            ContentFile(image_content),
            save=True
        )
        
        # Clean up temp file
        try:
            os.remove(temp_path)
        except OSError:
            pass
        
        # Close browser
        context.close()
        browser.close()
        
        return True, f"Screenshot saved as DisputeScreenshot #{screenshot.id}"
        
    except Exception as e:
        # Ensure browser is closed on error
        if browser:
            try:
                browser.close()
            except Exception:
                pass
        raise e


def _update_dispute_status(dispute: Dispute) -> None:
    """
    Update dispute status through the progression:
    MATCHED -> GATHERING_DATA -> DOCUMENTS_READY
    
    Args:
        dispute: Dispute object to update
    """
    old_status = dispute.status
    
    # Progress through status states
    if dispute.status == 'MATCHED':
        dispute.status = 'GATHERING_DATA'
    elif dispute.status == 'GATHERING_DATA':
        dispute.status = 'DOCUMENTS_READY'
    # If already at DOCUMENTS_READY or beyond, don't change
    
    if dispute.status != old_status:
        dispute.save()
        logger.info(f"Dispute #{dispute.id} status updated: {old_status} -> {dispute.status}")
        
        # Log status change
        DisputeActivityLog.objects.create(
            dispute=dispute,
            action='STATUS_CHANGED',
            details=f"Status changed: {old_status} -> {dispute.status} (after screenshot capture)",
        )


def capture_screenshots_manual(dispute_id: int) -> Tuple[bool, str]:
    """
    Manual wrapper for capture_zendesk_screenshots.
    Can be called from Django views or management commands.
    
    This is a convenience function that calls capture_zendesk_screenshots
    with auto_retry=True for manual triggering.
    
    Args:
        dispute_id: The Django Dispute record ID
        
    Returns:
        Tuple of (success: bool, message: str)
    """
    return capture_zendesk_screenshots(dispute_id, auto_retry=True)


def capture_screenshots_batch(dispute_ids: list, auto_retry: bool = True) -> dict:
    """
    Capture screenshots for multiple disputes.
    
    Args:
        dispute_ids: List of Dispute IDs
        auto_retry: If True, retry on failure
        
    Returns:
        Dict with results: {'success': [...], 'failed': [...]}
    """
    results = {'success': [], 'failed': []}
    
    for dispute_id in dispute_ids:
        success, message = capture_zendesk_screenshots(dispute_id, auto_retry=auto_retry)
        
        if success:
            results['success'].append({'dispute_id': dispute_id, 'message': message})
        else:
            results['failed'].append({'dispute_id': dispute_id, 'message': message})
    
    return results
