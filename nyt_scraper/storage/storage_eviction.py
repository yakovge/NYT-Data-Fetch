"""Storage eviction policies for automatic database cleanup and optimization."""

import sqlite3
import threading
from datetime import datetime, timedelta
from pathlib import Path
from typing import Dict, List, Optional, Tuple

from ..config import config
from ..monitoring.structured_logger import get_logger
from ..models import ArticleStatus

logger = get_logger(__name__)


class StorageEviction:
    """Manages automatic storage eviction policies for database maintenance."""
    
    def __init__(self, sqlite_manager):
        self.sqlite_manager = sqlite_manager
        self.db_path = sqlite_manager.db_path
        self._lock = threading.RLock()
        
        # Eviction configuration
        self.max_db_size_bytes = config.max_db_size_gb * 1024 * 1024 * 1024  # Convert GB to bytes
        self.retention_days = config.retention_days
        self.eviction_batch_size = 1000  # Delete in batches for performance
        self.min_free_space_ratio = 0.1  # Keep 10% free space after eviction
        
        logger.info("storage_eviction_initialized", 
                   max_size_gb=config.max_db_size_gb,
                   retention_days=self.retention_days)
    
    def check_eviction_needed(self) -> Dict[str, any]:
        """
        Check if database eviction is needed based on size and age policies.
        
        Returns:
            Dictionary with eviction assessment details
        """
        try:
            # Check database file size
            db_size = self.db_path.stat().st_size if self.db_path.exists() else 0
            size_ratio = db_size / self.max_db_size_bytes if self.max_db_size_bytes > 0 else 0
            
            # Check article age distribution
            with sqlite3.connect(self.db_path) as conn:
                # Count articles by age
                cutoff_date = datetime.utcnow() - timedelta(days=self.retention_days)
                
                cursor = conn.execute("""
                    SELECT 
                        COUNT(*) as total_articles,
                        COUNT(CASE WHEN created_at < ? THEN 1 END) as old_articles,
                        MIN(created_at) as oldest_article,
                        MAX(created_at) as newest_article
                    FROM articles
                """, (cutoff_date.isoformat(),))
                
                stats = cursor.fetchone()
                
                assessment = {
                    "db_size_bytes": db_size,
                    "db_size_mb": db_size / (1024 * 1024),
                    "max_size_bytes": self.max_db_size_bytes,
                    "size_ratio": size_ratio,
                    "size_eviction_needed": size_ratio > 0.9,  # 90% threshold
                    "total_articles": stats[0] or 0,
                    "old_articles": stats[1] or 0,
                    "oldest_article": stats[2],
                    "newest_article": stats[3],
                    "age_eviction_needed": (stats[1] or 0) > 0,
                    "eviction_recommended": size_ratio > 0.9 or (stats[1] or 0) > 0
                }
                
                logger.debug("eviction_assessment", **assessment)
                return assessment
                
        except Exception as e:
            logger.error("eviction_check_failed", error=str(e))
            return {"error": str(e), "eviction_recommended": False}
    
    def evict_old_articles(self, dry_run: bool = False) -> Dict[str, int]:
        """
        Evict articles older than retention period.
        
        Args:
            dry_run: If True, only report what would be deleted
            
        Returns:
            Dictionary with eviction results
        """
        with self._lock:
            try:
                cutoff_date = datetime.utcnow() - timedelta(days=self.retention_days)
                
                total_deleted = 0
                
                with sqlite3.connect(self.db_path) as conn:
                    # First, count what would be deleted
                    cursor = conn.execute("""
                        SELECT COUNT(*) FROM articles 
                        WHERE created_at < ?
                    """, (cutoff_date.isoformat(),))
                    
                    articles_to_delete = cursor.fetchone()[0]
                    
                    if dry_run:
                        logger.info("eviction_dry_run", 
                                   articles_to_delete=articles_to_delete,
                                   cutoff_date=cutoff_date.isoformat())
                        return {"dry_run": True, "articles_to_delete": articles_to_delete}
                    
                    # Delete in batches for better performance
                    while True:
                        cursor = conn.execute("""
                            DELETE FROM articles 
                            WHERE id IN (
                                SELECT id FROM articles 
                                WHERE created_at < ? 
                                ORDER BY created_at ASC 
                                LIMIT ?
                            )
                        """, (cutoff_date.isoformat(), self.eviction_batch_size))
                        
                        batch_deleted = cursor.rowcount
                        total_deleted += batch_deleted
                        
                        if batch_deleted == 0:
                            break
                        
                        logger.debug("eviction_batch_completed", 
                                    batch_deleted=batch_deleted,
                                    total_deleted=total_deleted)
                
                # Vacuum database after large deletion (outside transaction)
                if total_deleted > 0:
                    with sqlite3.connect(self.db_path) as conn:
                        conn.execute("VACUUM")
                    logger.info("database_vacuumed_after_eviction")
                    
                    logger.info("old_articles_evicted", 
                               deleted_count=total_deleted,
                               cutoff_date=cutoff_date.isoformat())
                    
                    return {"deleted": total_deleted, "cutoff_date": cutoff_date.isoformat()}
                    
            except Exception as e:
                logger.error("old_article_eviction_failed", error=str(e))
                return {"error": str(e), "deleted": 0}
    
    def evict_by_size(self, target_size_ratio: float = 0.8, dry_run: bool = False) -> Dict[str, int]:
        """
        Evict oldest articles to reduce database size to target ratio.
        
        Args:
            target_size_ratio: Target size as ratio of max size (0.8 = 80%)
            dry_run: If True, only report what would be deleted
            
        Returns:
            Dictionary with eviction results
        """
        with self._lock:
            try:
                current_size = self.db_path.stat().st_size if self.db_path.exists() else 0
                target_size = int(self.max_db_size_bytes * target_size_ratio)
                
                if current_size <= target_size:
                    return {"message": "No size-based eviction needed", "deleted": 0}
                
                with sqlite3.connect(self.db_path) as conn:
                    # Calculate how many articles to delete based on average article size
                    cursor = conn.execute("SELECT COUNT(*) FROM articles")
                    total_articles = cursor.fetchone()[0]
                    
                    if total_articles == 0:
                        return {"message": "No articles to delete", "deleted": 0}
                    
                    avg_article_size = current_size / total_articles
                    articles_to_delete = max(1, int((current_size - target_size) / avg_article_size))
                    
                    # Add 10% buffer to ensure we get below target
                    articles_to_delete = int(articles_to_delete * 1.1)
                    
                    if dry_run:
                        logger.info("size_eviction_dry_run",
                                   current_size_mb=current_size / (1024 * 1024),
                                   target_size_mb=target_size / (1024 * 1024),
                                   articles_to_delete=articles_to_delete)
                        return {"dry_run": True, "articles_to_delete": articles_to_delete}
                    
                    # Delete oldest articles first
                    total_deleted = 0
                    remaining_to_delete = articles_to_delete
                    
                    while remaining_to_delete > 0:
                        batch_size = min(self.eviction_batch_size, remaining_to_delete)
                        
                        cursor = conn.execute("""
                            DELETE FROM articles 
                            WHERE id IN (
                                SELECT id FROM articles 
                                ORDER BY created_at ASC 
                                LIMIT ?
                            )
                        """, (batch_size,))
                        
                        batch_deleted = cursor.rowcount
                        total_deleted += batch_deleted
                        remaining_to_delete -= batch_deleted
                        
                        if batch_deleted == 0:
                            break
                    
                    # Vacuum after large deletion
                    if total_deleted > 0:
                        conn.execute("VACUUM")
                        
                        # Check final size
                        final_size = self.db_path.stat().st_size
                        logger.info("size_based_eviction_completed",
                                   deleted_count=total_deleted,
                                   initial_size_mb=current_size / (1024 * 1024),
                                   final_size_mb=final_size / (1024 * 1024),
                                   size_reduction_mb=(current_size - final_size) / (1024 * 1024))
                        
                        return {
                            "deleted": total_deleted,
                            "initial_size_bytes": current_size,
                            "final_size_bytes": final_size,
                            "size_reduction_bytes": current_size - final_size
                        }
                    
                    return {"message": "No articles were deleted", "deleted": 0}
                    
            except Exception as e:
                logger.error("size_based_eviction_failed", error=str(e))
                return {"error": str(e), "deleted": 0}
    
    def evict_failed_articles(self, days_old: int = 7, dry_run: bool = False) -> Dict[str, int]:
        """
        Evict articles with failed status older than specified days.
        
        Args:
            days_old: Delete failed articles older than this many days
            dry_run: If True, only report what would be deleted
            
        Returns:
            Dictionary with eviction results
        """
        with self._lock:
            try:
                cutoff_date = datetime.utcnow() - timedelta(days=days_old)
                
                with sqlite3.connect(self.db_path) as conn:
                    cursor = conn.execute("""
                        SELECT COUNT(*) FROM articles 
                        WHERE status = ? AND created_at < ?
                    """, (ArticleStatus.FAILED.value, cutoff_date.isoformat()))
                    
                    failed_to_delete = cursor.fetchone()[0]
                    
                    if dry_run:
                        return {"dry_run": True, "failed_articles_to_delete": failed_to_delete}
                    
                    if failed_to_delete > 0:
                        cursor = conn.execute("""
                            DELETE FROM articles 
                            WHERE status = ? AND created_at < ?
                        """, (ArticleStatus.FAILED.value, cutoff_date.isoformat()))
                        
                        deleted_count = cursor.rowcount
                        
                        logger.info("failed_articles_evicted",
                                   deleted_count=deleted_count,
                                   cutoff_date=cutoff_date.isoformat())
                        
                        return {"deleted": deleted_count, "type": "failed_articles"}
                    
                    return {"deleted": 0, "message": "No failed articles to delete"}
                    
            except Exception as e:
                logger.error("failed_article_eviction_failed", error=str(e))
                return {"error": str(e), "deleted": 0}
    
    def get_storage_stats(self) -> Dict[str, any]:
        """Get comprehensive storage statistics for monitoring."""
        try:
            # Database file stats
            db_size = self.db_path.stat().st_size if self.db_path.exists() else 0
            
            with sqlite3.connect(self.db_path) as conn:
                # Article counts by status and age
                cursor = conn.execute("""
                    SELECT 
                        status,
                        COUNT(*) as count,
                        AVG(CAST(julianday('now') - julianday(created_at) AS INTEGER)) as avg_age_days,
                        MIN(created_at) as oldest,
                        MAX(created_at) as newest
                    FROM articles 
                    GROUP BY status
                """)
                
                status_stats = {}
                for row in cursor.fetchall():
                    status_stats[row[0]] = {
                        "count": row[1],
                        "avg_age_days": row[2] or 0,
                        "oldest": row[3],
                        "newest": row[4]
                    }
                
                # Total counts
                cursor = conn.execute("""
                    SELECT 
                        COUNT(*) as total,
                        COUNT(CASE WHEN created_at < datetime('now', '-' || ? || ' days') THEN 1 END) as old_articles
                    FROM articles
                """, (self.retention_days,))
                
                totals = cursor.fetchone()
                
                # Size distribution
                cursor = conn.execute("""
                    SELECT 
                        AVG(LENGTH(content)) as avg_content_size,
                        MAX(LENGTH(content)) as max_content_size,
                        MIN(LENGTH(content)) as min_content_size
                    FROM articles 
                    WHERE content IS NOT NULL
                """)
                
                size_stats = cursor.fetchone()
                
                return {
                    "database": {
                        "size_bytes": db_size,
                        "size_mb": db_size / (1024 * 1024),
                        "max_size_gb": config.max_db_size_gb,
                        "size_ratio": db_size / self.max_db_size_bytes if self.max_db_size_bytes > 0 else 0,
                        "path": str(self.db_path)
                    },
                    "articles": {
                        "total": totals[0] or 0,
                        "old_articles": totals[1] or 0,
                        "retention_days": self.retention_days,
                        "by_status": status_stats
                    },
                    "content": {
                        "avg_size_bytes": size_stats[0] or 0,
                        "max_size_bytes": size_stats[1] or 0,
                        "min_size_bytes": size_stats[2] or 0,
                        "avg_size_kb": (size_stats[0] or 0) / 1024
                    }
                }
                
        except Exception as e:
            logger.error("storage_stats_failed", error=str(e))
            return {"error": str(e)}
    
    def run_full_maintenance(self, dry_run: bool = False) -> Dict[str, any]:
        """
        Run full database maintenance including eviction and optimization.
        
        Args:
            dry_run: If True, only report what would be done
            
        Returns:
            Dictionary with maintenance results
        """
        logger.info("storage_maintenance_started", dry_run=dry_run)
        
        results = {
            "dry_run": dry_run,
            "started_at": datetime.utcnow().isoformat(),
            "operations": {}
        }
        
        try:
            # Check if maintenance is needed
            assessment = self.check_eviction_needed()
            results["assessment"] = assessment
            
            if not assessment.get("eviction_recommended", False):
                results["message"] = "No maintenance needed"
                return results
            
            # 1. Evict old articles
            if assessment.get("age_eviction_needed", False):
                age_result = self.evict_old_articles(dry_run=dry_run)
                results["operations"]["age_eviction"] = age_result
            
            # 2. Evict by size if still needed
            if assessment.get("size_eviction_needed", False):
                size_result = self.evict_by_size(dry_run=dry_run)
                results["operations"]["size_eviction"] = size_result
            
            # 3. Clean up failed articles
            failed_result = self.evict_failed_articles(dry_run=dry_run)
            results["operations"]["failed_eviction"] = failed_result
            
            # 4. Final optimization (if not dry run)
            if not dry_run:
                self._optimize_database()
                results["operations"]["optimization"] = {"completed": True}
            
            results["completed_at"] = datetime.utcnow().isoformat()
            logger.info("storage_maintenance_completed", dry_run=dry_run)
            
        except Exception as e:
            logger.error("storage_maintenance_failed", error=str(e))
            results["error"] = str(e)
        
        return results
    
    def _optimize_database(self):
        """Run database optimization commands."""
        try:
            with sqlite3.connect(self.db_path) as conn:
                # Analyze tables for better query planning
                conn.execute("ANALYZE")
                
                # Update statistics
                conn.execute("PRAGMA optimize")
                
                logger.info("database_optimization_completed")
                
        except Exception as e:
            logger.error("database_optimization_failed", error=str(e))