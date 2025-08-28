"""SQLite storage manager with WAL mode and auto-triggers."""

import hashlib
import json
import sqlite3
import threading
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Optional

from ..config import config
from ..monitoring.structured_logger import get_logger
from ..models import Article, ArticleStatus

logger = get_logger(__name__)


class SQLiteManager:
    """Manages SQLite storage with WAL mode for concurrent access."""
    
    def __init__(self):
        self.db_path = config.sqlite_db_path
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._init_database()
    
    def _init_database(self):
        """Initialize SQLite database with WAL mode and schema."""
        with self._lock:
            try:
                with sqlite3.connect(self.db_path) as conn:
                    # Enable WAL mode for better concurrency
                    conn.execute("PRAGMA journal_mode=WAL")
                    conn.execute("PRAGMA synchronous=NORMAL")
                    conn.execute("PRAGMA cache_size=10000")
                    conn.execute("PRAGMA temp_store=MEMORY")
                    conn.execute("PRAGMA mmap_size=268435456")  # 256MB
                    
                    # Create articles table (from implementation plan)
                    conn.execute("""
                        CREATE TABLE IF NOT EXISTS articles (
                            id INTEGER PRIMARY KEY AUTOINCREMENT,
                            source_url TEXT NOT NULL,
                            canonical_url TEXT UNIQUE NOT NULL,
                            redirect_chain TEXT,
                            title TEXT,
                            author TEXT,
                            published_date DATETIME,
                            updated_date DATETIME,
                            content TEXT,
                            section TEXT,
                            tags TEXT,
                            body_hash TEXT,
                            simhash INTEGER,
                            etag TEXT,
                            last_modified TEXT,
                            parse_method TEXT,
                            parse_duration_ms REAL,
                            fetch_duration_ms REAL,
                            status TEXT DEFAULT 'stored',
                            error_message TEXT,
                            last_seen DATETIME DEFAULT CURRENT_TIMESTAMP,
                            created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
                            updated_at DATETIME DEFAULT CURRENT_TIMESTAMP
                        )
                    """)
                    
                    # Create indexes for performance
                    indexes = [
                        "CREATE UNIQUE INDEX IF NOT EXISTS idx_canonical ON articles(canonical_url)",
                        "CREATE INDEX IF NOT EXISTS idx_body_hash ON articles(body_hash)",
                        "CREATE INDEX IF NOT EXISTS idx_simhash ON articles(simhash)",
                        "CREATE INDEX IF NOT EXISTS idx_source_url ON articles(source_url)",
                        "CREATE INDEX IF NOT EXISTS idx_published_date ON articles(published_date)",
                        "CREATE INDEX IF NOT EXISTS idx_section ON articles(section)",
                        "CREATE INDEX IF NOT EXISTS idx_status ON articles(status)",
                        "CREATE INDEX IF NOT EXISTS idx_created_at ON articles(created_at)",
                        "CREATE INDEX IF NOT EXISTS idx_last_seen ON articles(last_seen)"
                    ]
                    
                    for index_sql in indexes:
                        conn.execute(index_sql)
                    
                    # Create auto-update trigger
                    conn.execute("""
                        CREATE TRIGGER IF NOT EXISTS update_timestamp 
                        AFTER UPDATE ON articles
                        BEGIN
                            UPDATE articles SET updated_at = CURRENT_TIMESTAMP 
                            WHERE id = NEW.id;
                        END
                    """)
                    
                    logger.info("sqlite_database_initialized", db_path=str(self.db_path))
                    
            except Exception as e:
                logger.error("sqlite_init_error", error=str(e))
                raise
    
    def store_article(self, article: Article) -> bool:
        """
        Store or update article in database.
        
        Args:
            article: Article to store
            
        Returns:
            True if stored successfully, False otherwise
        """
        with self._lock:
            try:
                # Calculate content hash and canonical URL
                canonical_url = self._canonicalize_url(article.source_url)
                body_hash = self._calculate_body_hash(article.body or '')
                
                # Convert article to database format
                article_data = self._article_to_db_dict(article, canonical_url, body_hash)
                
                with sqlite3.connect(self.db_path) as conn:
                    # Try to insert, update on conflict
                    conn.execute("""
                        INSERT INTO articles (
                            source_url, canonical_url, redirect_chain, title, author,
                            published_date, updated_date, content, section, tags,
                            body_hash, simhash, etag, last_modified, parse_method,
                            parse_duration_ms, fetch_duration_ms, status, error_message
                        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                        ON CONFLICT(canonical_url) DO UPDATE SET
                            source_url = excluded.source_url,
                            title = excluded.title,
                            author = excluded.author,
                            updated_date = excluded.updated_date,
                            content = excluded.content,
                            section = excluded.section,
                            tags = excluded.tags,
                            body_hash = excluded.body_hash,
                            simhash = excluded.simhash,
                            etag = excluded.etag,
                            last_modified = excluded.last_modified,
                            parse_method = excluded.parse_method,
                            parse_duration_ms = excluded.parse_duration_ms,
                            fetch_duration_ms = excluded.fetch_duration_ms,
                            status = excluded.status,
                            error_message = excluded.error_message,
                            last_seen = CURRENT_TIMESTAMP
                    """, article_data)
                    
                    logger.debug(
                        "article_stored",
                        canonical_url=canonical_url,
                        title_length=len(article.title),
                        body_length=len(article.body or '')
                    )
                    
                    return True
                    
            except Exception as e:
                logger.error(
                    "article_store_error",
                    url=article.source_url,
                    error=str(e)
                )
                return False
    
    def get_article_by_url(self, url: str) -> Optional[Article]:
        """Get article by source or canonical URL."""
        canonical_url = self._canonicalize_url(url)
        
        with sqlite3.connect(self.db_path) as conn:
            conn.row_factory = sqlite3.Row
            
            # Try canonical URL first, then source URL
            for url_column in ['canonical_url', 'source_url']:
                cursor = conn.execute(f"""
                    SELECT * FROM articles 
                    WHERE {url_column} = ?
                    ORDER BY created_at DESC
                    LIMIT 1
                """, (canonical_url,))
                
                row = cursor.fetchone()
                if row:
                    return self._db_row_to_article(row)
        
        return None
    
    def get_articles_by_hash(self, body_hash: str) -> List[Article]:
        """Get articles with matching body hash."""
        with sqlite3.connect(self.db_path) as conn:
            conn.row_factory = sqlite3.Row
            
            cursor = conn.execute("""
                SELECT * FROM articles 
                WHERE body_hash = ?
                ORDER BY created_at DESC
            """, (body_hash,))
            
            return [self._db_row_to_article(row) for row in cursor.fetchall()]
    
    def get_recent_articles(self, limit: int = 100) -> List[Article]:
        """Get most recently stored articles."""
        with sqlite3.connect(self.db_path) as conn:
            conn.row_factory = sqlite3.Row
            
            cursor = conn.execute("""
                SELECT * FROM articles 
                ORDER BY created_at DESC
                LIMIT ?
            """, (limit,))
            
            return [self._db_row_to_article(row) for row in cursor.fetchall()]
    
    def get_articles_by_section(self, section: str, limit: int = 50) -> List[Article]:
        """Get articles from specific section."""
        with sqlite3.connect(self.db_path) as conn:
            conn.row_factory = sqlite3.Row
            
            cursor = conn.execute("""
                SELECT * FROM articles 
                WHERE section = ?
                ORDER BY published_date DESC, created_at DESC
                LIMIT ?
            """, (section, limit))
            
            return [self._db_row_to_article(row) for row in cursor.fetchall()]
    
    def search_articles(
        self, 
        query: str = None,
        section: str = None,
        author: str = None,
        limit: int = 50
    ) -> List[Article]:
        """Search articles with filters."""
        with sqlite3.connect(self.db_path) as conn:
            conn.row_factory = sqlite3.Row
            
            conditions = []
            params = []
            
            if query:
                conditions.append("(title LIKE ? OR content LIKE ?)")
                query_param = f"%{query}%"
                params.extend([query_param, query_param])
            
            if section:
                conditions.append("section = ?")
                params.append(section)
            
            if author:
                conditions.append("author LIKE ?")
                params.append(f"%{author}%")
            
            where_clause = " AND ".join(conditions) if conditions else "1=1"
            
            cursor = conn.execute(f"""
                SELECT * FROM articles 
                WHERE {where_clause}
                ORDER BY published_date DESC, created_at DESC
                LIMIT ?
            """, params + [limit])
            
            return [self._db_row_to_article(row) for row in cursor.fetchall()]
    
    def get_storage_stats(self) -> Dict:
        """Get database statistics."""
        with sqlite3.connect(self.db_path) as conn:
            # Article counts by status
            status_counts = {}
            cursor = conn.execute("""
                SELECT status, COUNT(*) as count 
                FROM articles 
                GROUP BY status
            """)
            for row in cursor.fetchall():
                status_counts[row[0]] = row[1]
            
            # Total articles
            total_articles = conn.execute("SELECT COUNT(*) FROM articles").fetchone()[0]
            
            # Date range
            date_range = conn.execute("""
                SELECT MIN(published_date), MAX(published_date)
                FROM articles 
                WHERE published_date IS NOT NULL
            """).fetchone()
            
            # Database size
            db_size = self.db_path.stat().st_size if self.db_path.exists() else 0
            
            # Section distribution
            sections = {}
            cursor = conn.execute("""
                SELECT section, COUNT(*) as count 
                FROM articles 
                WHERE section IS NOT NULL
                GROUP BY section
                ORDER BY count DESC
                LIMIT 10
            """)
            for row in cursor.fetchall():
                sections[row[0]] = row[1]
            
            return {
                "total_articles": total_articles,
                "status_counts": status_counts,
                "date_range": {
                    "earliest": date_range[0],
                    "latest": date_range[1]
                },
                "db_size_mb": db_size / (1024 * 1024),
                "db_path": str(self.db_path),
                "top_sections": sections
            }
    
    def cleanup_old_articles(self, days: int) -> int:
        """Remove articles older than specified days."""
        with self._lock:
            with sqlite3.connect(self.db_path) as conn:
                cursor = conn.execute("""
                    DELETE FROM articles
                    WHERE created_at < datetime('now', '-{} days')
                """.format(days))
                
                deleted_count = cursor.rowcount
                
                if deleted_count > 0:
                    logger.info("old_articles_cleaned", deleted_count=deleted_count, days=days)
                
                return deleted_count
    
    def vacuum_database(self):
        """Optimize database by running VACUUM."""
        with self._lock:
            try:
                with sqlite3.connect(self.db_path) as conn:
                    conn.execute("VACUUM")
                    logger.info("database_vacuumed")
            except Exception as e:
                logger.error("vacuum_error", error=str(e))
    
    def backup_database(self, backup_path: Path) -> bool:
        """Create backup of database."""
        try:
            backup_path.parent.mkdir(parents=True, exist_ok=True)
            
            with sqlite3.connect(self.db_path) as source:
                with sqlite3.connect(backup_path) as backup:
                    source.backup(backup)
            
            logger.info("database_backed_up", backup_path=str(backup_path))
            return True
            
        except Exception as e:
            logger.error("backup_error", error=str(e))
            return False
    
    def _canonicalize_url(self, url: str) -> str:
        """Canonicalize URL for deduplication."""
        from urllib.parse import urlparse, urlunparse
        
        try:
            parsed = urlparse(url)
            
            # Normalize components
            scheme = parsed.scheme.lower()
            netloc = parsed.netloc.lower()
            path = parsed.path.rstrip('/')
            
            # Remove common tracking parameters
            query_parts = []
            if parsed.query:
                from urllib.parse import parse_qsl
                for key, value in parse_qsl(parsed.query):
                    if not key.lower().startswith(('utm_', 'fb_', 'gclid')):
                        query_parts.append(f"{key}={value}")
            
            query = '&'.join(query_parts)
            
            return urlunparse((scheme, netloc, path, parsed.params, query, ''))
            
        except Exception:
            return url  # Return original if parsing fails
    
    def _calculate_body_hash(self, body: str) -> str:
        """Calculate SHA256 hash of article body."""
        return hashlib.sha256(body.encode('utf-8')).hexdigest()
    
    def _article_to_db_dict(self, article: Article, canonical_url: str, body_hash: str) -> tuple:
        """Convert Article to database tuple."""
        return (
            article.source_url,
            canonical_url,
            json.dumps(article.redirect_chain) if article.redirect_chain else None,
            article.title,
            article.author,
            article.published_date.isoformat() if article.published_date else None,
            article.updated_date.isoformat() if article.updated_date else None,
            article.body,
            article.section,
            ','.join(article.tags) if article.tags else None,
            body_hash,
            article.simhash,
            article.etag,
            article.last_modified,
            article.parse_method.value if article.parse_method else None,
            article.parse_duration_ms,
            article.fetch_duration_ms,
            article.status.value,
            article.error_message
        )
    
    def _db_row_to_article(self, row: sqlite3.Row) -> Article:
        """Convert database row to Article object."""
        return Article(
            source_url=row['source_url'],
            canonical_url=row['canonical_url'],
            title=row['title'] or '',
            body=row['content'] or '',
            author=row['author'],
            published_date=datetime.fromisoformat(row['published_date']) if row['published_date'] else None,
            updated_date=datetime.fromisoformat(row['updated_date']) if row['updated_date'] else None,
            section=row['section'],
            tags=row['tags'].split(',') if row['tags'] else [],
            redirect_chain=json.loads(row['redirect_chain']) if row['redirect_chain'] else [],
            etag=row['etag'],
            last_modified=row['last_modified'],
            body_hash=row['body_hash'],
            simhash=row['simhash'],
            parse_method=row['parse_method'],
            parse_duration_ms=row['parse_duration_ms'],
            fetch_duration_ms=row['fetch_duration_ms'],
            status=ArticleStatus(row['status']) if row['status'] else ArticleStatus.STORED,
            error_message=row['error_message'],
            discovered_at=datetime.fromisoformat(row['created_at']),
            stored_at=datetime.fromisoformat(row['created_at'])
        )
    
    def close(self):
        """Close database connection and perform cleanup."""
        logger.info("sqlite_manager_closed")