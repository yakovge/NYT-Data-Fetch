"""Governance and compliance management."""

import asyncio
from datetime import datetime, timedelta
from typing import Dict, Optional

from ..config import config
from ..monitoring.structured_logger import get_logger
from .paywall_detector import PaywallDetector
from .robots_manager import RobotsManager

logger = get_logger(__name__)


class ComplianceManager:
    """Central compliance and governance management."""
    
    def __init__(self):
        self.robots_manager = RobotsManager()
        self.paywall_detector = PaywallDetector()
        self.start_time = datetime.utcnow()
        self.stats: Dict = {
            "total_requests": 0,
            "robots_blocked": 0,
            "paywalls_detected": 0,
            "compliance_violations": 0
        }
        
    async def check_compliance(self, url: str, html: Optional[str] = None) -> Dict:
        """
        Run all compliance checks for a URL.
        
        Returns:
            Dict with compliance results and any issues found
        """
        result = {
            "url": url,
            "compliant": True,
            "can_fetch": True,
            "is_paywalled": False,
            "issues": [],
            "timestamp": datetime.utcnow().isoformat()
        }
        
        # Check kill switch first
        if config.kill_switch:
            result["compliant"] = False
            result["can_fetch"] = False
            result["issues"].append("Kill switch activated")
            logger.critical("kill_switch_active", url=url)
            return result
        
        # Check robots.txt
        try:
            can_fetch = await self.robots_manager.can_fetch(url)
            if not can_fetch:
                result["compliant"] = False
                result["can_fetch"] = False
                result["issues"].append("Blocked by robots.txt")
                self.stats["robots_blocked"] += 1
                logger.warning("robots_blocked", url=url)
        except Exception as e:
            logger.error("robots_check_failed", url=url, error=str(e))
            # On error, be conservative and allow
            result["issues"].append(f"Robots check failed: {e}")
        
        # Check for paywall if HTML provided
        if html and result["can_fetch"]:
            is_paywalled = self.paywall_detector.detect(html, url)
            if is_paywalled:
                result["is_paywalled"] = True
                result["issues"].append("Paywall detected")
                self.stats["paywalls_detected"] += 1
                logger.warning("paywall_found", url=url)
        
        # Check if URL was previously detected as paywalled
        if self.paywall_detector.is_url_paywalled(url):
            result["is_paywalled"] = True
            result["issues"].append("Previously detected paywall")
        
        # Increment request counter
        self.stats["total_requests"] += 1
        
        # Log compliance check
        if not result["compliant"] or result["is_paywalled"]:
            self.stats["compliance_violations"] += 1
            logger.warning(
                "compliance_violation",
                url=url,
                issues=result["issues"]
            )
        
        return result
    
    def check_dry_run(self) -> bool:
        """Check if system is in dry-run mode."""
        return config.dry_run
    
    def activate_kill_switch(self, reason: str):
        """Activate the kill switch to stop all operations."""
        config.kill_switch = True
        logger.critical(
            "kill_switch_activated",
            reason=reason,
            timestamp=datetime.utcnow().isoformat()
        )
    
    def deactivate_kill_switch(self):
        """Deactivate the kill switch."""
        config.kill_switch = False
        logger.info("kill_switch_deactivated")
    
    async def get_crawl_delay(self, url: str) -> float:
        """
        Get appropriate crawl delay for URL.
        
        Returns delay in seconds, respecting both robots.txt 
        and configured delays.
        """
        # Check robots.txt for crawl-delay directive
        robots_delay = self.robots_manager.get_crawl_delay(url)
        
        # Use the maximum of robots delay and configured minimum
        if robots_delay:
            delay = max(robots_delay, config.request_delay_min)
        else:
            # Use configured delay range
            import random
            delay = random.uniform(
                config.request_delay_min,
                config.request_delay_max
            )
        
        logger.debug(
            "crawl_delay",
            url=url,
            delay_seconds=delay,
            from_robots=robots_delay is not None
        )
        
        return delay
    
    def get_compliance_stats(self) -> Dict:
        """Get compliance statistics."""
        uptime = datetime.utcnow() - self.start_time
        
        return {
            "uptime_hours": uptime.total_seconds() / 3600,
            "total_requests": self.stats["total_requests"],
            "robots_blocked": self.stats["robots_blocked"],
            "robots_block_rate": (
                self.stats["robots_blocked"] / max(1, self.stats["total_requests"])
            ),
            "paywalls_detected": self.stats["paywalls_detected"],
            "paywall_rate": (
                self.stats["paywalls_detected"] / max(1, self.stats["total_requests"])
            ),
            "compliance_violations": self.stats["compliance_violations"],
            "violation_rate": (
                self.stats["compliance_violations"] / max(1, self.stats["total_requests"])
            ),
            "kill_switch_active": config.kill_switch,
            "dry_run_mode": config.dry_run
        }
    
    async def cleanup(self):
        """Clean up expired robots.txt entries."""
        self.robots_manager.cleanup_expired()
        logger.info("compliance_cleanup_completed")
    
    def should_respect_crawl_delay(self) -> bool:
        """Check if we should respect crawl delays."""
        # Always respect delays unless in emergency mode
        return not config.kill_switch
    
    async def wait_if_needed(self, url: str):
        """Wait for appropriate crawl delay if needed."""
        if self.should_respect_crawl_delay():
            delay = await self.get_crawl_delay(url)
            if delay > 0:
                await asyncio.sleep(delay)
    
    def log_compliance_event(self, event_type: str, url: str, details: Dict):
        """Log compliance-related events for audit trail."""
        logger.info(
            "compliance_event",
            event_type=event_type,
            url=url,
            details=details,
            timestamp=datetime.utcnow().isoformat()
        )