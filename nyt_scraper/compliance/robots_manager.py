"""Robots.txt parser and manager with persistent caching."""

import sqlite3
import time
from datetime import datetime, timedelta
from typing import Dict, Optional, Tuple
from urllib.parse import urlparse
from urllib.robotparser import RobotFileParser

import httpx
from tenacity import retry, stop_after_attempt, wait_exponential

from ..config import config
from ..monitoring.structured_logger import get_logger

logger = get_logger(__name__)


class RobotsManager:
    """Manages robots.txt parsing with persistent SQLite cache."""
    
    def __init__(self):
        self.db_path = config.robots_cache_db
        self.default_ttl = 86400  # 24 hours default
        self._init_database()
        
    def _init_database(self):
        """Initialize the robots cache database."""
        with sqlite3.connect(self.db_path) as conn:
            conn.execute("""
                CREATE TABLE IF NOT EXISTS robots_cache (
                    host TEXT PRIMARY KEY,
                    content TEXT NOT NULL,
                    etag TEXT,
                    max_age INTEGER DEFAULT 86400,
                    fetched_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    expires_at TIMESTAMP,
                    can_fetch_cache TEXT
                )
            """)
            conn.execute("""
                CREATE INDEX IF NOT EXISTS idx_expires 
                ON robots_cache(expires_at)
            """)
    
    @retry(
        stop=stop_after_attempt(3),
        wait=wait_exponential(multiplier=1, min=2, max=10)
    )
    async def fetch_robots(self, url: str) -> Optional[str]:
        """Fetch robots.txt for a given URL."""
        parsed = urlparse(url)
        robots_url = f"{parsed.scheme}://{parsed.netloc}/robots.txt"
        
        try:
            async with httpx.AsyncClient() as client:
                response = await client.get(
                    robots_url,
                    headers={"User-Agent": config.user_agents[0]},
                    timeout=10.0,
                    follow_redirects=True
                )
                
                if response.status_code == 200:
                    # Extract max-age from Cache-Control header
                    cache_control = response.headers.get("cache-control", "")
                    max_age = self._extract_max_age(cache_control)
                    
                    return response.text, response.headers.get("etag"), max_age
                    
        except Exception as e:
            logger.warning(
                "robots_fetch_failed",
                url=robots_url,
                error=str(e)
            )
        
        return None, None, self.default_ttl
    
    def _extract_max_age(self, cache_control: str) -> int:
        """Extract max-age from Cache-Control header."""
        if not cache_control:
            return self.default_ttl
            
        parts = cache_control.lower().split(",")
        for part in parts:
            if "max-age" in part:
                try:
                    return int(part.split("=")[1].strip())
                except (IndexError, ValueError):
                    pass
        
        return self.default_ttl
    
    async def can_fetch(self, url: str, user_agent: str = "*") -> bool:
        """Check if URL can be fetched according to robots.txt."""
        if config.kill_switch:
            return False
            
        parsed = urlparse(url)
        host = parsed.netloc
        
        # Check cache first
        cached = self._get_cached_robots(host)
        
        if cached:
            robots_content, can_fetch_cache = cached
            
            # Check if we have a cached result for this specific path
            if can_fetch_cache:
                cache_dict = eval(can_fetch_cache)  # Safe since we control the data
                path_key = f"{user_agent}:{parsed.path}"
                if path_key in cache_dict:
                    return cache_dict[path_key]
            
            # Parse and check
            parser = RobotFileParser()
            parser.parse(robots_content.splitlines())
            can_fetch = parser.can_fetch(user_agent, url)
            
            # Update cache
            self._update_can_fetch_cache(host, user_agent, parsed.path, can_fetch)
            return can_fetch
        
        # Fetch robots.txt
        content, etag, max_age = await self.fetch_robots(url)
        
        if content is None:
            # If robots.txt is unreachable, allow crawling (fallback behavior)
            logger.info(
                "robots_unreachable_allowing",
                host=host,
                url=url
            )
            return True
        
        # Cache the robots.txt
        self._cache_robots(host, content, etag, max_age)
        
        # Parse and check
        parser = RobotFileParser()
        parser.parse(content.splitlines())
        can_fetch = parser.can_fetch(user_agent, url)
        
        # Cache the result
        self._update_can_fetch_cache(host, user_agent, parsed.path, can_fetch)
        
        return can_fetch
    
    def _get_cached_robots(self, host: str) -> Optional[Tuple[str, str]]:
        """Get cached robots.txt content if not expired."""
        with sqlite3.connect(self.db_path) as conn:
            cursor = conn.execute("""
                SELECT content, can_fetch_cache
                FROM robots_cache
                WHERE host = ? AND expires_at > CURRENT_TIMESTAMP
            """, (host,))
            
            result = cursor.fetchone()
            return result if result else None
    
    def _cache_robots(self, host: str, content: str, etag: Optional[str], max_age: int):
        """Cache robots.txt content with TTL."""
        expires_at = datetime.utcnow() + timedelta(seconds=max_age)
        
        with sqlite3.connect(self.db_path) as conn:
            conn.execute("""
                INSERT OR REPLACE INTO robots_cache 
                (host, content, etag, max_age, fetched_at, expires_at)
                VALUES (?, ?, ?, ?, CURRENT_TIMESTAMP, ?)
            """, (host, content, etag, max_age, expires_at))
            
            logger.info(
                "robots_cached",
                host=host,
                max_age=max_age,
                expires_at=expires_at.isoformat()
            )
    
    def _update_can_fetch_cache(self, host: str, user_agent: str, path: str, can_fetch: bool):
        """Update the can_fetch cache for quick lookups."""
        with sqlite3.connect(self.db_path) as conn:
            # Get existing cache
            cursor = conn.execute(
                "SELECT can_fetch_cache FROM robots_cache WHERE host = ?",
                (host,)
            )
            result = cursor.fetchone()
            
            if result and result[0]:
                cache_dict = eval(result[0])
            else:
                cache_dict = {}
            
            # Update cache
            cache_dict[f"{user_agent}:{path}"] = can_fetch
            
            # Limit cache size to prevent unbounded growth
            if len(cache_dict) > 1000:
                # Keep only the 500 most recent entries
                items = list(cache_dict.items())
                cache_dict = dict(items[-500:])
            
            conn.execute(
                "UPDATE robots_cache SET can_fetch_cache = ? WHERE host = ?",
                (str(cache_dict), host)
            )
    
    def cleanup_expired(self):
        """Remove expired robots.txt entries."""
        with sqlite3.connect(self.db_path) as conn:
            cursor = conn.execute("""
                DELETE FROM robots_cache
                WHERE expires_at < CURRENT_TIMESTAMP
            """)
            
            deleted = cursor.rowcount
            if deleted > 0:
                logger.info("robots_cleanup", deleted_entries=deleted)
    
    def get_crawl_delay(self, url: str, user_agent: str = "*") -> Optional[float]:
        """Get crawl delay from robots.txt if specified."""
        parsed = urlparse(url)
        host = parsed.netloc
        
        cached = self._get_cached_robots(host)
        if not cached:
            return None
        
        robots_content, _ = cached
        parser = RobotFileParser()
        parser.parse(robots_content.splitlines())
        
        # RobotFileParser doesn't expose crawl-delay directly
        # We need to parse it manually
        for line in robots_content.splitlines():
            line = line.strip().lower()
            if line.startswith("crawl-delay:"):
                try:
                    return float(line.split(":")[1].strip())
                except (IndexError, ValueError):
                    pass
        
        return None