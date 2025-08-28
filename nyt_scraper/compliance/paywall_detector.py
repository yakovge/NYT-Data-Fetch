"""Paywall detection using DOM markers."""

from typing import List, Optional

from selectolax.parser import HTMLParser

from ..monitoring.structured_logger import get_logger

logger = get_logger(__name__)


class PaywallDetector:
    """Detect paywalls using DOM markers, not URLs."""
    
    # DOM selectors that indicate a paywall
    PAYWALL_SELECTORS = [
        'div[data-testid="paywall-prompt"]',
        'div.meteredContent',
        '[aria-label*="subscriber"]',
        'div.paywall-container',
        'div.subscription-required',
        'div.article-paywall',
        '[data-paywall="true"]',
        'div.premium-content',
        'div.locked-content',
        '.paywall-overlay',
        '#paywall-banner',
        '.subscription-wall',
        '[class*="paywall"]',
        '[id*="paywall"]',
        'div.meter-wall',
        'div.registration-wall',
        '.content-gate',
        '[data-content-type="premium"]'
    ]
    
    # Text markers that indicate paywall
    PAYWALL_TEXT_MARKERS = [
        "subscriber exclusive",
        "subscription required",
        "subscribe to read",
        "already a subscriber",
        "unlock this article",
        "premium content",
        "members only",
        "subscriber only",
        "create a free account to continue",
        "you've reached your limit",
        "subscribe for unlimited access"
    ]
    
    def __init__(self):
        self.detected_paywalls: List[str] = []
    
    def detect(self, html: str, url: str) -> bool:
        """
        Detect if content is behind a paywall.
        
        Args:
            html: HTML content to check
            url: URL being checked (for logging)
            
        Returns:
            True if paywall detected, False otherwise
        """
        if not html:
            return False
        
        try:
            parser = HTMLParser(html)
            
            # Check DOM selectors
            for selector in self.PAYWALL_SELECTORS:
                if parser.css_first(selector):
                    logger.warning(
                        "paywall_detected",
                        url=url,
                        selector=selector,
                        detection_method="dom_selector"
                    )
                    self.detected_paywalls.append(url)
                    return True
            
            # Check text markers
            text_content = parser.text().lower() if parser.body else ""
            for marker in self.PAYWALL_TEXT_MARKERS:
                if marker in text_content:
                    logger.warning(
                        "paywall_detected",
                        url=url,
                        marker=marker,
                        detection_method="text_marker"
                    )
                    self.detected_paywalls.append(url)
                    return True
            
            # Check for truncated content patterns
            if self._detect_truncated_content(parser):
                logger.warning(
                    "paywall_detected",
                    url=url,
                    detection_method="truncated_content"
                )
                self.detected_paywalls.append(url)
                return True
                
        except Exception as e:
            logger.error(
                "paywall_detection_error",
                url=url,
                error=str(e)
            )
        
        return False
    
    def _detect_truncated_content(self, parser: HTMLParser) -> bool:
        """
        Detect if article content appears truncated.
        
        Common patterns:
        - Article starts but cuts off mid-sentence
        - Fade-out gradient at bottom of content
        - "Continue reading" without full content
        """
        # Check for fade-out gradients
        fade_selectors = [
            '[class*="fade-out"]',
            '[class*="gradient-overlay"]',
            '[style*="linear-gradient"]'
        ]
        
        for selector in fade_selectors:
            element = parser.css_first(selector)
            if element:
                # Check if it's overlaying article content
                parent = element.parent
                if parent and any(cls in str(parent.attrs.get('class', '')) 
                                 for cls in ['article', 'content', 'story']):
                    return True
        
        # Check for truncation indicators
        article_body = parser.css_first('div.article-body, div.story-content, article')
        if article_body:
            text = article_body.text()
            
            # Check if text ends mid-sentence (no proper punctuation)
            if text and not text.rstrip().endswith(('.', '!', '?', '"', "'")):
                # But make sure it's not just a short preview
                if len(text) > 200:  # Reasonable article should be longer
                    return True
        
        return False
    
    def check_response_status(self, status_code: int) -> bool:
        """
        Check if HTTP status indicates paywall.
        
        Args:
            status_code: HTTP response status code
            
        Returns:
            True if status indicates paywall (451), False otherwise
        """
        # 451: Unavailable For Legal Reasons (often used for paywalls)
        if status_code == 451:
            logger.warning(
                "paywall_detected",
                detection_method="http_status",
                status_code=status_code
            )
            return True
        
        return False
    
    def is_url_paywalled(self, url: str) -> bool:
        """
        Check if URL has been previously detected as paywalled.
        
        Args:
            url: URL to check
            
        Returns:
            True if URL was previously detected as paywalled
        """
        return url in self.detected_paywalls
    
    def get_stats(self) -> dict:
        """Get paywall detection statistics."""
        return {
            "total_paywalls_detected": len(self.detected_paywalls),
            "paywalled_urls": self.detected_paywalls[-10:]  # Last 10 for monitoring
        }