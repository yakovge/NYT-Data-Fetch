"""Per-host budget tracking for domain-level request and byte limits with challenge cooldowns."""

import sqlite3
import threading
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Dict, List, Optional, Tuple
from urllib.parse import urlparse

from ..config import config
from ..monitoring.structured_logger import get_logger

logger = get_logger(__name__)


class PerHostBudgets:
    """Manages per-host request and byte budgets with challenge detection cooldowns."""
    
    def __init__(self):
        self.db_path = Path(config.data_dir) / "host_budgets.db"
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        
        # Budget configuration from implementation plan
        self.max_requests_per_day = config.per_host_max_requests_per_day
        self.max_bytes_per_day = config.per_host_max_bytes_per_day
        self.challenge_cooldown_hours = config.challenge_cooldown_hours
        
        self._init_database()
        logger.info("per_host_budgets_initialized",
                   max_requests=self.max_requests_per_day,
                   max_bytes_mb=self.max_bytes_per_day / (1024 * 1024),
                   cooldown_hours=self.challenge_cooldown_hours)
    
    def _init_database(self):
        """Initialize SQLite database for host budget tracking."""
        with self._lock:
            try:
                with sqlite3.connect(self.db_path) as conn:
                    # Enable WAL mode for better concurrency
                    conn.execute("PRAGMA journal_mode=WAL")
                    conn.execute("PRAGMA synchronous=NORMAL")
                    
                    # Create host_budgets table
                    conn.execute("""
                        CREATE TABLE IF NOT EXISTS host_budgets (
                            host TEXT PRIMARY KEY,
                            requests_today INTEGER DEFAULT 0,
                            bytes_today INTEGER DEFAULT 0,
                            last_request_at DATETIME,
                            budget_reset_at DATETIME,
                            challenge_detected_at DATETIME,
                            cooldown_until DATETIME,
                            total_requests INTEGER DEFAULT 0,
                            total_bytes INTEGER DEFAULT 0,
                            first_seen DATETIME DEFAULT CURRENT_TIMESTAMP,
                            updated_at DATETIME DEFAULT CURRENT_TIMESTAMP
                        )
                    """)
                    
                    # Create indexes
                    conn.execute("CREATE INDEX IF NOT EXISTS idx_cooldown ON host_budgets(cooldown_until)")
                    conn.execute("CREATE INDEX IF NOT EXISTS idx_last_request ON host_budgets(last_request_at)")
                    
                    # Create auto-update trigger
                    conn.execute("""
                        CREATE TRIGGER IF NOT EXISTS update_host_budgets_timestamp 
                        AFTER UPDATE ON host_budgets
                        BEGIN
                            UPDATE host_budgets SET updated_at = CURRENT_TIMESTAMP 
                            WHERE host = NEW.host;
                        END
                    """)
                    
                    logger.info("host_budgets_database_initialized", db_path=str(self.db_path))
                    
            except Exception as e:
                logger.error("host_budgets_init_error", error=str(e))
                raise
    
    def _extract_host(self, url: str) -> str:
        """Extract host from URL."""
        try:
            parsed = urlparse(url)
            return parsed.netloc.lower()
        except Exception:
            return url  # Fallback to original if parsing fails
    
    def can_make_request(self, url: str) -> Tuple[bool, str]:
        """
        Check if a request can be made to the given URL based on budget and cooldowns.
        
        Args:
            url: URL to check
            
        Returns:
            Tuple of (can_make_request, reason)
        """
        host = self._extract_host(url)
        
        with self._lock:
            try:
                with sqlite3.connect(self.db_path) as conn:
                    cursor = conn.execute("""
                        SELECT 
                            requests_today,
                            bytes_today,
                            cooldown_until,
                            budget_reset_at
                        FROM host_budgets 
                        WHERE host = ?
                    """, (host,))
                    
                    row = cursor.fetchone()
                    
                    if not row:
                        # First request to this host
                        return True, "first_request"
                    
                    requests_today, bytes_today, cooldown_until, budget_reset_at = row
                    now = datetime.utcnow()
                    
                    # Check if we're in cooldown period
                    if cooldown_until:
                        cooldown_end = datetime.fromisoformat(cooldown_until)
                        if now < cooldown_end:
                            remaining_minutes = (cooldown_end - now).total_seconds() / 60
                            return False, f"challenge_cooldown_active_for_{remaining_minutes:.1f}_minutes"
                    
                    # Check if budget needs reset (new day)
                    if budget_reset_at:
                        reset_time = datetime.fromisoformat(budget_reset_at)
                        if now >= reset_time + timedelta(days=1):
                            # Reset budget for new day
                            self._reset_daily_budget(conn, host)
                            return True, "budget_reset_new_day"
                    
                    # Check request budget
                    if requests_today >= self.max_requests_per_day:
                        return False, f"daily_request_limit_exceeded_{requests_today}/{self.max_requests_per_day}"
                    
                    # Check byte budget  
                    if bytes_today >= self.max_bytes_per_day:
                        return False, f"daily_byte_limit_exceeded_{bytes_today/(1024*1024):.1f}MB/{self.max_bytes_per_day/(1024*1024):.1f}MB"
                    
                    return True, "within_budget"
                    
            except Exception as e:
                logger.error("budget_check_failed", host=host, error=str(e))
                return True, "check_failed_allowing_request"  # Fail open for reliability
    
    def record_request(self, url: str, response_size_bytes: int = 0, success: bool = True):
        """
        Record a request against the host budget.
        
        Args:
            url: URL that was requested
            response_size_bytes: Size of response in bytes
            success: Whether the request was successful
        """
        host = self._extract_host(url)
        
        with self._lock:
            try:
                with sqlite3.connect(self.db_path) as conn:
                    now = datetime.utcnow()
                    
                    # Upsert host budget record
                    conn.execute("""
                        INSERT INTO host_budgets (
                            host, requests_today, bytes_today, last_request_at, budget_reset_at,
                            total_requests, total_bytes
                        ) VALUES (?, 1, ?, ?, ?, 1, ?)
                        ON CONFLICT(host) DO UPDATE SET
                            requests_today = requests_today + 1,
                            bytes_today = bytes_today + ?,
                            last_request_at = ?,
                            total_requests = total_requests + 1,
                            total_bytes = total_bytes + ?
                    """, (host, response_size_bytes, now.isoformat(), now.isoformat(),
                         response_size_bytes, response_size_bytes, now.isoformat(), response_size_bytes))
                    
                    logger.debug("request_recorded",
                               host=host,
                               response_size_bytes=response_size_bytes,
                               success=success)
                    
            except Exception as e:
                logger.error("request_recording_failed", host=host, error=str(e))
    
    def record_challenge_detected(self, url: str, challenge_type: str = "unknown"):
        """
        Record that a bot challenge was detected for this host.
        
        Args:
            url: URL where challenge was detected
            challenge_type: Type of challenge (e.g., "cloudflare", "turnstile")
        """
        host = self._extract_host(url)
        
        with self._lock:
            try:
                with sqlite3.connect(self.db_path) as conn:
                    now = datetime.utcnow()
                    cooldown_until = now + timedelta(hours=self.challenge_cooldown_hours)
                    
                    # Update challenge detection and set cooldown
                    conn.execute("""
                        INSERT INTO host_budgets (
                            host, challenge_detected_at, cooldown_until
                        ) VALUES (?, ?, ?)
                        ON CONFLICT(host) DO UPDATE SET
                            challenge_detected_at = ?,
                            cooldown_until = ?
                    """, (host, now.isoformat(), cooldown_until.isoformat(),
                         now.isoformat(), cooldown_until.isoformat()))
                    
                    logger.warning("challenge_detected_cooldown_activated",
                                 host=host,
                                 challenge_type=challenge_type,
                                 cooldown_hours=self.challenge_cooldown_hours,
                                 cooldown_until=cooldown_until.isoformat())
                    
            except Exception as e:
                logger.error("challenge_recording_failed", host=host, error=str(e))
    
    def clear_cooldown(self, url: str) -> bool:
        """
        Manually clear cooldown for a host (for testing or manual intervention).
        
        Args:
            url: URL/host to clear cooldown for
            
        Returns:
            True if cooldown was cleared successfully
        """
        host = self._extract_host(url)
        
        with self._lock:
            try:
                with sqlite3.connect(self.db_path) as conn:
                    cursor = conn.execute("""
                        UPDATE host_budgets 
                        SET cooldown_until = NULL, challenge_detected_at = NULL
                        WHERE host = ?
                    """, (host,))
                    
                    if cursor.rowcount > 0:
                        logger.info("cooldown_cleared", host=host)
                        return True
                    
                    return False
                    
            except Exception as e:
                logger.error("cooldown_clear_failed", host=host, error=str(e))
                return False
    
    def _reset_daily_budget(self, conn: sqlite3.Connection, host: str):
        """Reset daily budget counters for a host."""
        now = datetime.utcnow()
        
        conn.execute("""
            UPDATE host_budgets 
            SET 
                requests_today = 0,
                bytes_today = 0,
                budget_reset_at = ?
            WHERE host = ?
        """, (now.isoformat(), host))
        
        logger.debug("daily_budget_reset", host=host)
    
    def get_host_status(self, url: str) -> Dict[str, any]:
        """
        Get current budget status for a host.
        
        Args:
            url: URL to check status for
            
        Returns:
            Dictionary with host budget status
        """
        host = self._extract_host(url)
        
        try:
            with sqlite3.connect(self.db_path) as conn:
                cursor = conn.execute("""
                    SELECT 
                        requests_today,
                        bytes_today,
                        last_request_at,
                        challenge_detected_at,
                        cooldown_until,
                        total_requests,
                        total_bytes,
                        first_seen
                    FROM host_budgets 
                    WHERE host = ?
                """, (host,))
                
                row = cursor.fetchone()
                
                if not row:
                    return {
                        "host": host,
                        "status": "new_host",
                        "requests_today": 0,
                        "bytes_today": 0,
                        "request_budget_remaining": self.max_requests_per_day,
                        "byte_budget_remaining": self.max_bytes_per_day
                    }
                
                requests_today, bytes_today, last_request, challenge_detected, cooldown_until, total_requests, total_bytes, first_seen = row
                
                now = datetime.utcnow()
                in_cooldown = False
                cooldown_remaining_minutes = 0
                
                if cooldown_until:
                    cooldown_end = datetime.fromisoformat(cooldown_until)
                    if now < cooldown_end:
                        in_cooldown = True
                        cooldown_remaining_minutes = (cooldown_end - now).total_seconds() / 60
                
                return {
                    "host": host,
                    "status": "active",
                    "requests_today": requests_today or 0,
                    "bytes_today": bytes_today or 0,
                    "bytes_today_mb": (bytes_today or 0) / (1024 * 1024),
                    "request_budget_remaining": max(0, self.max_requests_per_day - (requests_today or 0)),
                    "byte_budget_remaining": max(0, self.max_bytes_per_day - (bytes_today or 0)),
                    "request_budget_used_percent": ((requests_today or 0) / self.max_requests_per_day) * 100,
                    "byte_budget_used_percent": ((bytes_today or 0) / self.max_bytes_per_day) * 100,
                    "last_request_at": last_request,
                    "challenge_detected_at": challenge_detected,
                    "in_cooldown": in_cooldown,
                    "cooldown_remaining_minutes": cooldown_remaining_minutes,
                    "total_requests": total_requests or 0,
                    "total_bytes": total_bytes or 0,
                    "total_bytes_mb": (total_bytes or 0) / (1024 * 1024),
                    "first_seen": first_seen
                }
                
        except Exception as e:
            logger.error("host_status_failed", host=host, error=str(e))
            return {"host": host, "error": str(e)}
    
    def get_all_hosts_status(self) -> List[Dict[str, any]]:
        """Get budget status for all tracked hosts."""
        try:
            with sqlite3.connect(self.db_path) as conn:
                cursor = conn.execute("""
                    SELECT 
                        host,
                        requests_today,
                        bytes_today,
                        last_request_at,
                        challenge_detected_at,
                        cooldown_until,
                        total_requests,
                        total_bytes
                    FROM host_budgets
                    ORDER BY total_requests DESC
                """)
                
                hosts = []
                now = datetime.utcnow()
                
                for row in cursor.fetchall():
                    host, requests_today, bytes_today, last_request, challenge_detected, cooldown_until, total_requests, total_bytes = row
                    
                    in_cooldown = False
                    if cooldown_until:
                        cooldown_end = datetime.fromisoformat(cooldown_until)
                        in_cooldown = now < cooldown_end
                    
                    hosts.append({
                        "host": host,
                        "requests_today": requests_today or 0,
                        "bytes_today_mb": (bytes_today or 0) / (1024 * 1024),
                        "request_budget_used_percent": ((requests_today or 0) / self.max_requests_per_day) * 100,
                        "byte_budget_used_percent": ((bytes_today or 0) / self.max_bytes_per_day) * 100,
                        "last_request_at": last_request,
                        "in_cooldown": in_cooldown,
                        "total_requests": total_requests or 0,
                        "total_bytes_mb": (total_bytes or 0) / (1024 * 1024)
                    })
                
                return hosts
                
        except Exception as e:
            logger.error("all_hosts_status_failed", error=str(e))
            return []
    
    def cleanup_old_records(self, days_old: int = 30) -> int:
        """
        Clean up old host budget records.
        
        Args:
            days_old: Remove records older than this many days
            
        Returns:
            Number of records cleaned up
        """
        with self._lock:
            try:
                with sqlite3.connect(self.db_path) as conn:
                    cutoff_date = datetime.utcnow() - timedelta(days=days_old)
                    
                    cursor = conn.execute("""
                        DELETE FROM host_budgets
                        WHERE last_request_at < ? AND cooldown_until IS NULL
                    """, (cutoff_date.isoformat(),))
                    
                    deleted_count = cursor.rowcount
                    
                    if deleted_count > 0:
                        logger.info("host_budget_records_cleaned",
                                   deleted_count=deleted_count,
                                   cutoff_date=cutoff_date.isoformat())
                    
                    return deleted_count
                    
            except Exception as e:
                logger.error("host_budget_cleanup_failed", error=str(e))
                return 0
    
    def get_budget_stats(self) -> Dict[str, any]:
        """Get comprehensive budget statistics."""
        try:
            with sqlite3.connect(self.db_path) as conn:
                # Overall stats
                cursor = conn.execute("""
                    SELECT 
                        COUNT(*) as total_hosts,
                        SUM(requests_today) as total_requests_today,
                        SUM(bytes_today) as total_bytes_today,
                        SUM(total_requests) as total_requests_all_time,
                        SUM(total_bytes) as total_bytes_all_time,
                        COUNT(CASE WHEN cooldown_until > datetime('now') THEN 1 END) as hosts_in_cooldown,
                        COUNT(CASE WHEN requests_today >= ? THEN 1 END) as hosts_at_request_limit,
                        COUNT(CASE WHEN bytes_today >= ? THEN 1 END) as hosts_at_byte_limit
                    FROM host_budgets
                """, (self.max_requests_per_day, self.max_bytes_per_day))
                
                stats = cursor.fetchone()
                
                # Top hosts by usage
                cursor = conn.execute("""
                    SELECT host, requests_today, bytes_today
                    FROM host_budgets
                    WHERE requests_today > 0
                    ORDER BY requests_today DESC
                    LIMIT 10
                """)
                
                top_hosts = [{"host": row[0], "requests": row[1], "bytes_mb": (row[2] or 0) / (1024 * 1024)} 
                           for row in cursor.fetchall()]
                
                return {
                    "total_hosts": stats[0] or 0,
                    "total_requests_today": stats[1] or 0,
                    "total_bytes_today_mb": (stats[2] or 0) / (1024 * 1024),
                    "total_requests_all_time": stats[3] or 0,
                    "total_bytes_all_time_mb": (stats[4] or 0) / (1024 * 1024),
                    "hosts_in_cooldown": stats[5] or 0,
                    "hosts_at_request_limit": stats[6] or 0,
                    "hosts_at_byte_limit": stats[7] or 0,
                    "top_hosts_today": top_hosts,
                    "budget_limits": {
                        "max_requests_per_day": self.max_requests_per_day,
                        "max_bytes_per_day_mb": self.max_bytes_per_day / (1024 * 1024),
                        "challenge_cooldown_hours": self.challenge_cooldown_hours
                    }
                }
                
        except Exception as e:
            logger.error("budget_stats_failed", error=str(e))
            return {"error": str(e)}
    
    def close(self):
        """Close the budget tracker and clean up resources."""
        logger.info("per_host_budgets_closed")