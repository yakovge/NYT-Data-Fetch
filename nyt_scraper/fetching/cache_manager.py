"""Cache management with ETag and Last-Modified support."""

import hashlib
import json
import sqlite3
from datetime import datetime, timedelta
from typing import Dict, Optional

import aiofiles

from ..config import config
from ..monitoring.structured_logger import get_logger

logger = get_logger(__name__)


class CacheManager:
    """Manages HTTP cache with SQLite backend."""
    
    def __init__(self):
        self.db_path = config.sqlite_db_path.parent / "http_cache.db"
        self._init_database()
    
    def _init_database(self):
        """Initialize cache database."""
        with sqlite3.connect(self.db_path) as conn:
            conn.execute("""
                CREATE TABLE IF NOT EXISTS http_cache (
                    url_hash TEXT PRIMARY KEY,
                    url TEXT NOT NULL,
                    content BLOB,
                    headers TEXT,
                    etag TEXT,
                    last_modified TEXT,
                    cached_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    expires_at TIMESTAMP,
                    hit_count INTEGER DEFAULT 0
                )
            """)
            conn.execute("""
                CREATE INDEX IF NOT EXISTS idx_expires 
                ON http_cache(expires_at)
            """)
            conn.execute("""
                CREATE INDEX IF NOT EXISTS idx_url 
                ON http_cache(url)
            """)
    
    def _hash_url(self, url: str) -> str:
        """Create hash of URL for cache key."""
        return hashlib.sha256(url.encode()).hexdigest()
    
    async def get(self, url: str) -> Optional[Dict]:
        """Get cached content if not expired."""
        url_hash = self._hash_url(url)
        
        with sqlite3.connect(self.db_path) as conn:
            cursor = conn.execute("""
                SELECT content, headers, etag, last_modified
                FROM http_cache
                WHERE url_hash = ? AND 
                      (expires_at IS NULL OR expires_at > CURRENT_TIMESTAMP)
            """, (url_hash,))
            
            result = cursor.fetchone()
            
            if result:
                # Update hit count
                conn.execute("""
                    UPDATE http_cache 
                    SET hit_count = hit_count + 1 
                    WHERE url_hash = ?
                """, (url_hash,))
                
                content, headers_json, etag, last_modified = result
                headers = json.loads(headers_json) if headers_json else {}
                
                logger.debug("cache_hit", url=url, etag=etag)
                
                return {
                    "content": content,
                    "headers": headers,
                    "etag": etag,
                    "last_modified": last_modified
                }
        
        logger.debug("cache_miss", url=url)
        return None
    
    async def set(
        self, 
        url: str, 
        content: bytes, 
        headers: Dict[str, str]
    ):
        """Cache content with headers."""
        url_hash = self._hash_url(url)
        
        # Extract cache-related headers
        etag = headers.get("etag")
        last_modified = headers.get("last-modified")
        
        # Calculate expiration
        cache_control = headers.get("cache-control", "")
        expires_at = self._calculate_expiration(cache_control)
        
        with sqlite3.connect(self.db_path) as conn:
            conn.execute("""
                INSERT OR REPLACE INTO http_cache 
                (url_hash, url, content, headers, etag, last_modified, 
                 cached_at, expires_at, hit_count)
                VALUES (?, ?, ?, ?, ?, ?, CURRENT_TIMESTAMP, ?, 0)
            """, (
                url_hash,
                url,
                content,
                json.dumps(headers),
                etag,
                last_modified,
                expires_at
            ))
            
            logger.debug(
                "cache_set",
                url=url,
                etag=etag,
                expires_at=expires_at
            )
    
    def _calculate_expiration(self, cache_control: str) -> Optional[datetime]:
        """Calculate cache expiration from Cache-Control header."""
        if "no-cache" in cache_control or "no-store" in cache_control:
            return datetime.utcnow()  # Expire immediately
        
        # Look for max-age
        if "max-age" in cache_control:
            try:
                parts = cache_control.split(",")
                for part in parts:
                    if "max-age" in part:
                        max_age = int(part.split("=")[1].strip())
                        return datetime.utcnow() + timedelta(seconds=max_age)
            except (ValueError, IndexError):
                pass
        
        # Default TTL from config
        return datetime.utcnow() + timedelta(hours=config.cache_ttl_hours)
    
    async def get_cache_headers(self, url: str) -> Optional[Dict[str, str]]:
        """Get cache validation headers for conditional requests."""
        url_hash = self._hash_url(url)
        
        with sqlite3.connect(self.db_path) as conn:
            cursor = conn.execute("""
                SELECT etag, last_modified
                FROM http_cache
                WHERE url_hash = ?
            """, (url_hash,))
            
            result = cursor.fetchone()
            
            if result:
                etag, last_modified = result
                headers = {}
                if etag:
                    headers["etag"] = etag
                if last_modified:
                    headers["last_modified"] = last_modified
                return headers
        
        return None
    
    async def invalidate(self, url: str):
        """Invalidate cache entry for URL."""
        url_hash = self._hash_url(url)
        
        with sqlite3.connect(self.db_path) as conn:
            conn.execute("""
                DELETE FROM http_cache WHERE url_hash = ?
            """, (url_hash,))
            
            logger.debug("cache_invalidated", url=url)
    
    async def cleanup(self):
        """Remove expired cache entries."""
        with sqlite3.connect(self.db_path) as conn:
            cursor = conn.execute("""
                DELETE FROM http_cache
                WHERE expires_at < CURRENT_TIMESTAMP
            """)
            
            deleted = cursor.rowcount
            
            if deleted > 0:
                logger.info("cache_cleanup", deleted_entries=deleted)
                
                # Also vacuum to reclaim space
                conn.execute("VACUUM")
    
    def get_stats(self) -> Dict:
        """Get cache statistics."""
        with sqlite3.connect(self.db_path) as conn:
            # Total entries
            total = conn.execute(
                "SELECT COUNT(*) FROM http_cache"
            ).fetchone()[0]
            
            # Expired entries
            expired = conn.execute("""
                SELECT COUNT(*) FROM http_cache
                WHERE expires_at < CURRENT_TIMESTAMP
            """).fetchone()[0]
            
            # Cache hit rate (last 100 entries)
            hit_stats = conn.execute("""
                SELECT AVG(hit_count), MAX(hit_count), SUM(hit_count)
                FROM http_cache
            """).fetchone()
            
            # Database size
            db_size = self.db_path.stat().st_size if self.db_path.exists() else 0
            
            return {
                "total_entries": total,
                "expired_entries": expired,
                "avg_hits": hit_stats[0] or 0,
                "max_hits": hit_stats[1] or 0,
                "total_hits": hit_stats[2] or 0,
                "size_mb": db_size / (1024 * 1024)
            }
    
    async def close(self):
        """Clean up resources."""
        await self.cleanup()