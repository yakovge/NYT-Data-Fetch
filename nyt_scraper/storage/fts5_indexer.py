"""SQLite FTS5-based full-text search indexer as zero-dependency alternative to Whoosh."""

import sqlite3
import threading
import time
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Optional, Tuple, Set

from ..config import config
from ..monitoring.structured_logger import get_logger
from ..models import Article

logger = get_logger(__name__)


class FTS5Indexer:
    """SQLite FTS5-based full-text search indexer for articles."""
    
    def __init__(self, sqlite_manager):
        self.sqlite_manager = sqlite_manager
        self.db_path = sqlite_manager.db_path
        self._lock = threading.RLock()
        
        # FTS5 configuration
        self.fts_table_name = "articles_fts"
        self.index_fields = ["title", "content", "author", "section"]  # Fields to index
        self.rebuild_threshold = 1000  # Rebuild index after this many changes
        self.changes_since_rebuild = 0
        
        # Performance tracking
        self.last_index_time = None
        self.index_size_bytes = 0
        
        self._init_fts_index()
        logger.info("fts5_indexer_initialized", table=self.fts_table_name)
    
    def _init_fts_index(self):
        """Initialize FTS5 index table and triggers."""
        with self._lock:
            try:
                with sqlite3.connect(self.db_path) as conn:
                    # Check if FTS5 is available
                    try:
                        conn.execute("SELECT fts5_version()")
                        logger.info("fts5_available")
                    except sqlite3.OperationalError:
                        logger.error("fts5_not_available", 
                                   message="SQLite was compiled without FTS5 support")
                        raise RuntimeError("FTS5 not available in this SQLite build")
                    
                    # Create FTS5 virtual table
                    conn.execute(f"""
                        CREATE VIRTUAL TABLE IF NOT EXISTS {self.fts_table_name} USING fts5(
                            title,
                            content,
                            author,
                            section,
                            tags,
                            url UNINDEXED,
                            created_at UNINDEXED,
                            content_type UNINDEXED,
                            tokenize = 'porter unicode61 remove_diacritics 1'
                        )
                    """)
                    
                    # Create triggers to keep FTS5 in sync with main table
                    self._create_sync_triggers(conn)
                    
                    # Check if we need initial population
                    cursor = conn.execute(f"SELECT COUNT(*) FROM {self.fts_table_name}")
                    fts_count = cursor.fetchone()[0]
                    
                    cursor = conn.execute("SELECT COUNT(*) FROM articles")
                    articles_count = cursor.fetchone()[0]
                    
                    if fts_count == 0 and articles_count > 0:
                        logger.info("populating_fts_index", articles_count=articles_count)
                        self._populate_initial_index(conn)
                    
                    self._update_index_stats()
                    
            except Exception as e:
                logger.error("fts5_init_failed", error=str(e))
                raise
    
    def _create_sync_triggers(self, conn: sqlite3.Connection):
        """Create triggers to keep FTS5 index in sync with main articles table."""
        
        # Insert trigger
        conn.execute(f"""
            CREATE TRIGGER IF NOT EXISTS articles_ai AFTER INSERT ON articles BEGIN
                INSERT INTO {self.fts_table_name}(
                    title, content, author, section, tags, url, created_at, content_type
                ) VALUES (
                    NEW.title, NEW.content, NEW.author, NEW.section, NEW.tags,
                    NEW.canonical_url, NEW.created_at, 'article'
                );
            END
        """)
        
        # Update trigger
        conn.execute(f"""
            CREATE TRIGGER IF NOT EXISTS articles_au AFTER UPDATE ON articles BEGIN
                UPDATE {self.fts_table_name} SET
                    title = NEW.title,
                    content = NEW.content,
                    author = NEW.author,
                    section = NEW.section,
                    tags = NEW.tags,
                    url = NEW.canonical_url,
                    created_at = NEW.created_at
                WHERE url = OLD.canonical_url;
            END
        """)
        
        # Delete trigger
        conn.execute(f"""
            CREATE TRIGGER IF NOT EXISTS articles_ad AFTER DELETE ON articles BEGIN
                DELETE FROM {self.fts_table_name} WHERE url = OLD.canonical_url;
            END
        """)
        
        logger.debug("fts5_sync_triggers_created")
    
    def _populate_initial_index(self, conn: sqlite3.Connection):
        """Populate FTS5 index with existing articles."""
        start_time = time.time()
        
        cursor = conn.execute("""
            SELECT canonical_url, title, content, author, section, tags, created_at
            FROM articles
            WHERE canonical_url IS NOT NULL
        """)
        
        articles = cursor.fetchall()
        
        # Insert in batches for better performance
        batch_size = 1000
        for i in range(0, len(articles), batch_size):
            batch = articles[i:i + batch_size]
            
            conn.executemany(f"""
                INSERT INTO {self.fts_table_name}(
                    title, content, author, section, tags, url, created_at, content_type
                ) VALUES (?, ?, ?, ?, ?, ?, ?, 'article')
            """, [(row[1], row[2], row[3], row[4], row[5], row[0], row[6]) for row in batch])
            
            logger.debug("fts5_batch_indexed", batch_start=i, batch_size=len(batch))
        
        duration = time.time() - start_time
        logger.info("fts5_initial_population_completed", 
                   articles_indexed=len(articles),
                   duration_seconds=duration)
    
    def search(self, query: str, limit: int = 50, offset: int = 0) -> List[Dict]:
        """
        Search articles using FTS5 full-text search.
        
        Args:
            query: Search query string
            limit: Maximum number of results
            offset: Offset for pagination
            
        Returns:
            List of matching articles with relevance scores
        """
        try:
            with sqlite3.connect(self.db_path) as conn:
                conn.row_factory = sqlite3.Row
                
                # Sanitize query for FTS5
                sanitized_query = self._sanitize_fts_query(query)
                
                if not sanitized_query:
                    return []
                
                # Search with ranking
                cursor = conn.execute(f"""
                    SELECT 
                        a.*,
                        bm25({self.fts_table_name}) as relevance_score,
                        highlight({self.fts_table_name}, 0, '<mark>', '</mark>') as title_highlight,
                        snippet({self.fts_table_name}, 1, '<mark>', '</mark>', '...', 32) as content_snippet
                    FROM {self.fts_table_name}
                    JOIN articles a ON a.canonical_url = {self.fts_table_name}.url
                    WHERE {self.fts_table_name} MATCH ?
                    ORDER BY bm25({self.fts_table_name})
                    LIMIT ? OFFSET ?
                """, (sanitized_query, limit, offset))
                
                results = []
                for row in cursor.fetchall():
                    result = dict(row)
                    result['search_score'] = abs(result['relevance_score'])  # BM25 returns negative scores
                    results.append(result)
                
                logger.debug("fts5_search_completed",
                           query=query,
                           results_count=len(results),
                           limit=limit,
                           offset=offset)
                
                return results
                
        except Exception as e:
            logger.error("fts5_search_failed", query=query, error=str(e))
            return []
    
    def search_by_section(self, section: str, query: str = None, limit: int = 20) -> List[Dict]:
        """
        Search articles within a specific section.
        
        Args:
            section: Section to search within
            query: Optional additional search terms
            limit: Maximum number of results
            
        Returns:
            List of matching articles
        """
        try:
            with sqlite3.connect(self.db_path) as conn:
                conn.row_factory = sqlite3.Row
                
                if query:
                    sanitized_query = self._sanitize_fts_query(query)
                    search_query = f"section:{section} AND {sanitized_query}"
                else:
                    search_query = f"section:{section}"
                
                cursor = conn.execute(f"""
                    SELECT 
                        a.*,
                        bm25({self.fts_table_name}) as relevance_score
                    FROM {self.fts_table_name}
                    JOIN articles a ON a.canonical_url = {self.fts_table_name}.url
                    WHERE {self.fts_table_name} MATCH ?
                    ORDER BY bm25({self.fts_table_name})
                    LIMIT ?
                """, (search_query, limit))
                
                results = [dict(row) for row in cursor.fetchall()]
                
                logger.debug("fts5_section_search_completed",
                           section=section,
                           query=query,
                           results_count=len(results))
                
                return results
                
        except Exception as e:
            logger.error("fts5_section_search_failed", 
                        section=section,
                        query=query, 
                        error=str(e))
            return []
    
    def get_search_suggestions(self, partial_query: str, limit: int = 10) -> List[str]:
        """
        Get search suggestions based on partial query.
        
        Args:
            partial_query: Partial search term
            limit: Maximum number of suggestions
            
        Returns:
            List of suggested search terms
        """
        try:
            if len(partial_query) < 2:
                return []
            
            with sqlite3.connect(self.db_path) as conn:
                # Search for terms that start with the partial query
                cursor = conn.execute(f"""
                    SELECT DISTINCT 
                        title,
                        bm25({self.fts_table_name}) as score
                    FROM {self.fts_table_name}
                    WHERE {self.fts_table_name} MATCH ?
                    ORDER BY score
                    LIMIT ?
                """, (f"{partial_query}*", limit))
                
                suggestions = [row[0] for row in cursor.fetchall() if row[0]]
                
                # Extract meaningful words from titles
                words = set()
                for title in suggestions:
                    title_words = [w.lower().strip() for w in title.split() 
                                 if len(w) >= 3 and w.lower().startswith(partial_query.lower())]
                    words.update(title_words)
                
                return sorted(list(words))[:limit]
                
        except Exception as e:
            logger.error("fts5_suggestions_failed", 
                        partial_query=partial_query, 
                        error=str(e))
            return []
    
    def get_trending_terms(self, days: int = 7, limit: int = 20) -> List[Tuple[str, int]]:
        """
        Get trending search terms based on recent article content.
        
        Args:
            days: Look at articles from last N days
            limit: Maximum number of terms to return
            
        Returns:
            List of (term, frequency) tuples
        """
        try:
            with sqlite3.connect(self.db_path) as conn:
                # Get most common words from recent articles
                cursor = conn.execute(f"""
                    SELECT 
                        title || ' ' || content as full_text
                    FROM {self.fts_table_name}
                    WHERE created_at >= datetime('now', '-{days} days')
                    AND content_type = 'article'
                """)
                
                # Simple word frequency analysis
                word_freq = {}
                stop_words = {'the', 'and', 'or', 'but', 'in', 'on', 'at', 'to', 'for', 'of', 'with', 'by'}
                
                for row in cursor.fetchall():
                    if row[0]:
                        words = row[0].lower().split()
                        for word in words:
                            word = word.strip('.,!?":;()[]{}')
                            if len(word) >= 3 and word not in stop_words:
                                word_freq[word] = word_freq.get(word, 0) + 1
                
                # Sort by frequency and return top terms
                trending = sorted(word_freq.items(), key=lambda x: x[1], reverse=True)[:limit]
                
                logger.debug("trending_terms_calculated", 
                           days=days,
                           terms_count=len(trending))
                
                return trending
                
        except Exception as e:
            logger.error("trending_terms_failed", error=str(e))
            return []
    
    def _sanitize_fts_query(self, query: str) -> str:
        """
        Sanitize query string for safe FTS5 usage.
        
        Args:
            query: Raw query string
            
        Returns:
            Sanitized query string safe for FTS5
        """
        if not query or not query.strip():
            return ""
        
        # Remove special FTS5 characters that could cause syntax errors
        # Keep only alphanumeric, spaces, and basic operators
        import re
        
        # Remove dangerous characters
        sanitized = re.sub(r'[^\w\s\-\*\"\']', ' ', query)
        
        # Handle quoted phrases
        if '"' in sanitized:
            # Ensure quotes are balanced
            quote_count = sanitized.count('"')
            if quote_count % 2 == 1:
                sanitized += '"'
        
        # Split into words and rejoin
        words = sanitized.split()
        if not words:
            return ""
        
        # Limit query length to prevent performance issues
        if len(words) > 10:
            words = words[:10]
        
        return ' '.join(words)
    
    def _update_index_stats(self):
        """Update internal statistics about the FTS5 index."""
        try:
            with sqlite3.connect(self.db_path) as conn:
                # Get index size
                cursor = conn.execute(f"""
                    SELECT SUM(LENGTH(title) + LENGTH(content) + LENGTH(author) + LENGTH(section))
                    FROM {self.fts_table_name}
                """)
                
                self.index_size_bytes = cursor.fetchone()[0] or 0
                self.last_index_time = datetime.utcnow()
                
        except Exception as e:
            logger.error("fts5_stats_update_failed", error=str(e))
    
    def rebuild_index(self) -> bool:
        """
        Rebuild the FTS5 index from scratch.
        
        Returns:
            True if rebuild was successful
        """
        with self._lock:
            try:
                logger.info("fts5_rebuild_started")
                start_time = time.time()
                
                with sqlite3.connect(self.db_path) as conn:
                    # Drop existing FTS table
                    conn.execute(f"DROP TABLE IF EXISTS {self.fts_table_name}")
                    
                    # Recreate FTS table
                    conn.execute(f"""
                        CREATE VIRTUAL TABLE {self.fts_table_name} USING fts5(
                            title,
                            content,
                            author,
                            section,
                            tags,
                            url UNINDEXED,
                            created_at UNINDEXED,
                            content_type UNINDEXED,
                            tokenize = 'porter unicode61 remove_diacritics 1'
                        )
                    """)
                    
                    # Repopulate index
                    self._populate_initial_index(conn)
                    
                    # Recreate triggers
                    self._create_sync_triggers(conn)
                    
                    # Optimize index
                    conn.execute(f"INSERT INTO {self.fts_table_name}({self.fts_table_name}) VALUES('optimize')")
                
                duration = time.time() - start_time
                self.changes_since_rebuild = 0
                self._update_index_stats()
                
                logger.info("fts5_rebuild_completed", duration_seconds=duration)
                return True
                
            except Exception as e:
                logger.error("fts5_rebuild_failed", error=str(e))
                return False
    
    def optimize_index(self) -> bool:
        """
        Optimize the FTS5 index for better performance.
        
        Returns:
            True if optimization was successful
        """
        try:
            with sqlite3.connect(self.db_path) as conn:
                # Run FTS5 optimize command
                conn.execute(f"INSERT INTO {self.fts_table_name}({self.fts_table_name}) VALUES('optimize')")
                
            logger.info("fts5_index_optimized")
            return True
            
        except Exception as e:
            logger.error("fts5_optimize_failed", error=str(e))
            return False
    
    def get_index_stats(self) -> Dict[str, any]:
        """
        Get comprehensive FTS5 index statistics.
        
        Returns:
            Dictionary with index statistics
        """
        try:
            with sqlite3.connect(self.db_path) as conn:
                # Get document count
                cursor = conn.execute(f"SELECT COUNT(*) FROM {self.fts_table_name}")
                doc_count = cursor.fetchone()[0]
                
                # Get index integrity info
                cursor = conn.execute(f"INSERT INTO {self.fts_table_name}({self.fts_table_name}) VALUES('integrity-check')")
                
                return {
                    "table_name": self.fts_table_name,
                    "document_count": doc_count,
                    "index_size_bytes": self.index_size_bytes,
                    "index_size_mb": self.index_size_bytes / (1024 * 1024) if self.index_size_bytes else 0,
                    "last_updated": self.last_index_time.isoformat() if self.last_index_time else None,
                    "changes_since_rebuild": self.changes_since_rebuild,
                    "rebuild_threshold": self.rebuild_threshold,
                    "indexed_fields": self.index_fields
                }
                
        except Exception as e:
            logger.error("fts5_stats_failed", error=str(e))
            return {"error": str(e)}
    
    def close(self):
        """Close the FTS5 indexer and clean up resources."""
        logger.info("fts5_indexer_closed")