"""AI response validator with caching for content quality assurance."""

import hashlib
import json
import sqlite3
import threading
from datetime import datetime, timedelta
from pathlib import Path
from typing import Dict, List, Optional, Tuple, Any
import re

from ..config import config
from ..monitoring.structured_logger import get_logger
from ..models import Article

logger = get_logger(__name__)


class AIValidator:
    """Validates AI-extracted content with caching to avoid redundant validation."""
    
    def __init__(self, sqlite_manager=None):
        self.sqlite_manager = sqlite_manager
        self.validation_cache_path = config.data_dir / "ai_validation_cache.db"
        self._lock = threading.RLock()
        
        # Validation configuration
        self.cache_ttl_days = 7  # Cache validation results for a week
        self.min_content_length = 100
        self.max_content_length = 50000
        self.confidence_threshold = 0.6
        self.quality_score_threshold = 0.7
        
        # Content quality patterns
        self.spam_patterns = [
            r'(?i)\b(viagra|cialis|casino|poker|lottery|winner)\b',
            r'(?i)\b(click here|buy now|limited time|act now)\b',
            r'(?i)\b(make money|get rich|work from home)\b',
        ]
        
        self.navigation_patterns = [
            r'(?i)\b(skip to content|main navigation|breadcrumb)\b',
            r'(?i)\b(sign in|log in|register|subscribe|newsletter)\b',
            r'(?i)\b(facebook|twitter|instagram|linkedin|share)\b',
            r'(?i)\b(advertisement|sponsored|related articles)\b',
            r'(?i)\b(comments|leave a comment|show comments)\b',
        ]
        
        self._init_cache_db()
        logger.info("ai_validator_initialized", 
                   cache_path=str(self.validation_cache_path))
    
    def _init_cache_db(self):
        """Initialize validation cache database."""
        try:
            with sqlite3.connect(self.validation_cache_path) as conn:
                conn.execute("""
                    CREATE TABLE IF NOT EXISTS validation_cache (
                        content_hash TEXT PRIMARY KEY,
                        validation_result TEXT NOT NULL,
                        quality_score REAL NOT NULL,
                        validation_reason TEXT,
                        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                        last_accessed TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                        ai_model TEXT,
                        content_type TEXT DEFAULT 'article'
                    )
                """)
                
                # Index for cleanup operations
                conn.execute("""
                    CREATE INDEX IF NOT EXISTS idx_validation_cache_created_at
                    ON validation_cache(created_at)
                """)
                
                conn.execute("""
                    CREATE INDEX IF NOT EXISTS idx_validation_cache_accessed
                    ON validation_cache(last_accessed)
                """)
                
                logger.debug("ai_validation_cache_initialized")
                
        except Exception as e:
            logger.error("ai_validation_cache_init_failed", error=str(e))
    
    def validate_article_content(self, article: Article, ai_source: str = "unknown") -> Tuple[bool, float, str]:
        """
        Validate AI-extracted article content for quality and legitimacy.
        
        Args:
            article: Article object with AI-extracted content
            ai_source: Source of AI extraction ("micro_ai", "heavy_ai", etc.)
            
        Returns:
            Tuple of (is_valid, quality_score, reason)
        """
        if not article or not article.body:
            return False, 0.0, "no_content"
        
        try:
            # Generate content hash for caching
            content_hash = self._generate_content_hash(article)
            
            # Check cache first
            cached_result = self._get_cached_validation(content_hash)
            if cached_result:
                return cached_result
            
            # Perform validation
            validation_result = self._perform_validation(article)
            is_valid, quality_score, reason = validation_result
            
            # Cache the result
            self._cache_validation_result(content_hash, validation_result, ai_source)
            
            logger.debug("ai_content_validated",
                        url=article.canonical_url,
                        is_valid=is_valid,
                        quality_score=quality_score,
                        reason=reason,
                        ai_source=ai_source)
            
            return validation_result
            
        except Exception as e:
            logger.error("ai_validation_failed",
                        url=article.canonical_url if article else "unknown",
                        error=str(e))
            return False, 0.0, "validation_error"
    
    def validate_metadata_consistency(self, article: Article) -> Tuple[bool, Dict[str, Any]]:
        """
        Validate consistency between article metadata and content.
        
        Args:
            article: Article object to validate
            
        Returns:
            Tuple of (is_consistent, consistency_details)
        """
        try:
            consistency_issues = []
            confidence_scores = {}
            
            # Title-content consistency
            if article.title and article.body:
                title_consistency = self._check_title_content_consistency(article.title, article.body)
                confidence_scores['title_consistency'] = title_consistency
                if title_consistency < 0.5:
                    consistency_issues.append("title_content_mismatch")
            
            # Author validation
            if article.author:
                author_validity = self._validate_author_field(article.author)
                confidence_scores['author_validity'] = author_validity
                if author_validity < 0.7:
                    consistency_issues.append("suspicious_author")
            
            # Date consistency
            if article.published_date:
                date_validity = self._validate_publish_date(article.published_date)
                confidence_scores['date_validity'] = date_validity
                if date_validity < 0.8:
                    consistency_issues.append("invalid_date")
            
            # Content quality metrics
            content_quality = self._assess_content_quality(article.body or "")
            confidence_scores['content_quality'] = content_quality
            if content_quality < self.quality_score_threshold:
                consistency_issues.append("low_content_quality")
            
            # Overall consistency score
            avg_confidence = sum(confidence_scores.values()) / len(confidence_scores) if confidence_scores else 0
            is_consistent = len(consistency_issues) == 0 and avg_confidence >= self.confidence_threshold
            
            return is_consistent, {
                "issues": consistency_issues,
                "confidence_scores": confidence_scores,
                "overall_confidence": avg_confidence
            }
            
        except Exception as e:
            logger.error("metadata_consistency_check_failed", 
                        url=article.canonical_url if article else "unknown",
                        error=str(e))
            return False, {"error": str(e)}
    
    def _perform_validation(self, article: Article) -> Tuple[bool, float, str]:
        """Perform comprehensive article content validation."""
        content = article.body or ""
        
        # Basic length validation
        if len(content) < self.min_content_length:
            return False, 0.1, "content_too_short"
        
        if len(content) > self.max_content_length:
            return False, 0.2, "content_too_long"
        
        # Initialize quality score
        quality_score = 1.0
        issues = []
        
        # Check for spam content
        spam_score = self._calculate_spam_score(content)
        if spam_score > 0.3:
            quality_score -= 0.4
            issues.append(f"spam_content_{spam_score:.2f}")
        
        # Check for navigation artifacts
        nav_score = self._calculate_navigation_artifacts(content)
        if nav_score > 0.2:
            quality_score -= 0.3
            issues.append(f"navigation_artifacts_{nav_score:.2f}")
        
        # Check content structure
        structure_score = self._assess_content_structure(content)
        if structure_score < 0.5:
            quality_score -= 0.2
            issues.append(f"poor_structure_{structure_score:.2f}")
        
        # Check for repetitive content
        repetition_score = self._calculate_repetition_score(content)
        if repetition_score > 0.4:
            quality_score -= 0.3
            issues.append(f"repetitive_content_{repetition_score:.2f}")
        
        # Check language quality
        language_score = self._assess_language_quality(content)
        if language_score < 0.6:
            quality_score -= 0.2
            issues.append(f"poor_language_{language_score:.2f}")
        
        # Ensure quality score stays within bounds
        quality_score = max(0.0, min(1.0, quality_score))
        
        # Determine if content is valid
        is_valid = quality_score >= self.quality_score_threshold and len(issues) <= 2
        reason = "valid" if is_valid else ",".join(issues[:3])  # Limit reason length
        
        return is_valid, quality_score, reason
    
    def _calculate_spam_score(self, content: str) -> float:
        """Calculate spam likelihood score (0-1)."""
        if not content:
            return 0.0
        
        content_lower = content.lower()
        spam_matches = 0
        
        for pattern in self.spam_patterns:
            matches = len(re.findall(pattern, content_lower))
            spam_matches += matches
        
        # Normalize by content length
        spam_density = spam_matches / max(1, len(content.split()) / 100)
        return min(1.0, spam_density)
    
    def _calculate_navigation_artifacts(self, content: str) -> float:
        """Calculate navigation artifacts score (0-1)."""
        if not content:
            return 0.0
        
        content_lower = content.lower()
        nav_matches = 0
        
        for pattern in self.navigation_patterns:
            matches = len(re.findall(pattern, content_lower))
            nav_matches += matches
        
        # Normalize by content length
        nav_density = nav_matches / max(1, len(content.split()) / 50)
        return min(1.0, nav_density)
    
    def _assess_content_structure(self, content: str) -> float:
        """Assess content structure quality (0-1)."""
        if not content:
            return 0.0
        
        structure_score = 0.5  # Start with neutral score
        
        # Check for paragraphs
        paragraphs = [p.strip() for p in content.split('\n\n') if p.strip()]
        if len(paragraphs) >= 2:
            structure_score += 0.2
        
        # Check average sentence length
        sentences = re.split(r'[.!?]+', content)
        valid_sentences = [s.strip() for s in sentences if len(s.strip()) > 10]
        
        if valid_sentences:
            avg_sentence_length = sum(len(s.split()) for s in valid_sentences) / len(valid_sentences)
            if 10 <= avg_sentence_length <= 30:  # Reasonable sentence length
                structure_score += 0.2
            elif avg_sentence_length < 5 or avg_sentence_length > 50:
                structure_score -= 0.2
        
        # Check for proper capitalization
        if content and content[0].isupper():
            structure_score += 0.1
        
        return max(0.0, min(1.0, structure_score))
    
    def _calculate_repetition_score(self, content: str) -> float:
        """Calculate repetitive content score (0-1)."""
        if len(content) < 200:
            return 0.0
        
        words = content.lower().split()
        if len(words) < 50:
            return 0.0
        
        # Check for repeated phrases (3-5 words)
        phrase_counts = {}
        repetition_score = 0.0
        
        for phrase_len in [3, 4, 5]:
            for i in range(len(words) - phrase_len + 1):
                phrase = ' '.join(words[i:i + phrase_len])
                phrase_counts[phrase] = phrase_counts.get(phrase, 0) + 1
        
        # Calculate repetition based on most frequent phrases
        total_phrases = sum(phrase_counts.values())
        if total_phrases > 0:
            max_phrase_count = max(phrase_counts.values())
            repetition_score = max_phrase_count / total_phrases
        
        return min(1.0, repetition_score * 3)  # Amplify repetition signal
    
    def _assess_language_quality(self, content: str) -> float:
        """Assess language quality score (0-1)."""
        if not content:
            return 0.0
        
        language_score = 0.5  # Start neutral
        
        # Check for proper punctuation
        sentences = re.split(r'[.!?]+', content)
        punct_sentences = [s for s in sentences if s.strip()]
        if len(punct_sentences) > 0:
            punct_ratio = len(punct_sentences) / max(1, len(content.split('\n')))
            if punct_ratio > 0.8:
                language_score += 0.2
        
        # Check capitalization patterns
        words = content.split()
        if words:
            # Count properly capitalized words at sentence starts
            capitalized = sum(1 for word in words if word[0].isupper())
            cap_ratio = capitalized / len(words)
            if 0.05 <= cap_ratio <= 0.3:  # Reasonable capitalization
                language_score += 0.2
        
        # Check for complete words (not too many fragments)
        if words:
            complete_words = sum(1 for word in words if len(word) >= 3 and word.isalpha())
            complete_ratio = complete_words / len(words)
            if complete_ratio > 0.7:
                language_score += 0.1
        
        return max(0.0, min(1.0, language_score))
    
    def _check_title_content_consistency(self, title: str, content: str) -> float:
        """Check consistency between title and content."""
        if not title or not content:
            return 0.5
        
        title_words = set(word.lower().strip('.,!?":;()[]{}') for word in title.split() if len(word) > 3)
        content_words = set(word.lower().strip('.,!?":;()[]{}') for word in content[:500].split())
        
        if not title_words:
            return 0.5
        
        # Calculate overlap
        overlap = len(title_words.intersection(content_words))
        consistency_score = overlap / len(title_words)
        
        return min(1.0, consistency_score)
    
    def _validate_author_field(self, author: str) -> float:
        """Validate author field quality."""
        if not author:
            return 1.0  # No author is acceptable
        
        author = author.strip()
        
        # Check for suspicious patterns
        if re.match(r'^(by\s+|written\s+by\s+)', author.lower()):
            author = re.sub(r'^(by\s+|written\s+by\s+)', '', author, flags=re.IGNORECASE).strip()
        
        # Check if it looks like a real name
        words = author.split()
        if len(words) == 0:
            return 0.0
        
        # Should have 1-4 words for a typical name
        if not (1 <= len(words) <= 4):
            return 0.3
        
        # Check for proper capitalization
        if not all(word[0].isupper() if word else False for word in words):
            return 0.5
        
        # Check for suspicious content
        suspicious_patterns = [r'\d+', r'@', r'http', r'www\.', r'\.com']
        for pattern in suspicious_patterns:
            if re.search(pattern, author.lower()):
                return 0.2
        
        return 0.9
    
    def _assess_content_quality(self, content: str) -> float:
        """Assess content quality and return score (0.0-1.0)."""
        if not content:
            return 0.0
        
        score = 1.0
        content_lower = content.lower()
        
        # Check content length (minimum threshold)
        if len(content) < 100:
            return 0.1
        elif len(content) < 500:
            score -= 0.3
            
        # Check for spam indicators
        spam_words = [
            'click here', 'win lottery', 'make money fast', 'get rich quick',
            'casino', 'poker', 'viagra', 'buy now', 'limited time', 'act now'
        ]
        
        spam_count = sum(1 for word in spam_words if word in content_lower)
        if spam_count >= 3:
            return 0.2  # High spam likelihood
        elif spam_count >= 1:
            score -= 0.2 * spam_count
            
        # Check for repetitive content
        words = content.split()
        if len(words) > 50:
            unique_words = len(set(words))
            repetition_ratio = unique_words / len(words)
            if repetition_ratio < 0.3:
                score -= 0.4
            elif repetition_ratio < 0.5:
                score -= 0.2
                
        return max(0.0, min(1.0, score))
    
    def _validate_publish_date(self, publish_date) -> float:
        """Validate publish date field."""
        if not publish_date:
            return 1.0  # No date is acceptable
        
        try:
            # Handle both datetime objects and strings
            if isinstance(publish_date, str):
                from dateutil.parser import parse as parse_date
                parsed_date = parse_date(publish_date)
            else:
                parsed_date = publish_date
            
            # Check if date is reasonable (not too far in future/past)
            now = datetime.utcnow()
            years_diff = abs((now - parsed_date).days) / 365.25
            
            if years_diff > 30:  # More than 30 years old or in future
                return 0.3
            elif years_diff > 10:  # More than 10 years
                return 0.7
            else:
                return 0.95
                
        except (ValueError, TypeError):
            return 0.1  # Invalid date format
    
    def _generate_content_hash(self, article: Article) -> str:
        """Generate hash for content caching."""
        content_parts = [
            article.canonical_url or "",
            article.title or "",
            (article.body or "")[:1000],  # First 1000 chars for efficiency
            article.author or "",
        ]
        content_string = "|".join(content_parts)
        return hashlib.sha256(content_string.encode()).hexdigest()
    
    def _get_cached_validation(self, content_hash: str) -> Optional[Tuple[bool, float, str]]:
        """Get cached validation result if available and not expired."""
        try:
            with sqlite3.connect(self.validation_cache_path) as conn:
                cursor = conn.execute("""
                    SELECT validation_result, quality_score, validation_reason, created_at
                    FROM validation_cache
                    WHERE content_hash = ?
                    AND created_at > datetime('now', '-' || ? || ' days')
                """, (content_hash, self.cache_ttl_days))
                
                result = cursor.fetchone()
                if result:
                    # Update last accessed timestamp
                    conn.execute("""
                        UPDATE validation_cache 
                        SET last_accessed = CURRENT_TIMESTAMP 
                        WHERE content_hash = ?
                    """, (content_hash,))
                    
                    is_valid = result[0].lower() == 'true'
                    quality_score = float(result[1])
                    reason = result[2] or "cached"
                    
                    logger.debug("ai_validation_cache_hit", content_hash=content_hash[:12])
                    return is_valid, quality_score, reason
                
        except Exception as e:
            logger.debug("ai_validation_cache_read_failed", error=str(e))
        
        return None
    
    def _cache_validation_result(self, content_hash: str, validation_result: Tuple[bool, float, str], ai_source: str):
        """Cache validation result for future use."""
        try:
            is_valid, quality_score, reason = validation_result
            
            with sqlite3.connect(self.validation_cache_path) as conn:
                conn.execute("""
                    INSERT OR REPLACE INTO validation_cache
                    (content_hash, validation_result, quality_score, validation_reason, ai_model, created_at, last_accessed)
                    VALUES (?, ?, ?, ?, ?, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)
                """, (content_hash, str(is_valid), quality_score, reason, ai_source))
                
        except Exception as e:
            logger.debug("ai_validation_cache_write_failed", error=str(e))
    
    def cleanup_cache(self, days_old: int = None) -> int:
        """Clean up old validation cache entries."""
        days_old = days_old or (self.cache_ttl_days * 2)  # Default to 2x TTL
        
        try:
            with sqlite3.connect(self.validation_cache_path) as conn:
                cursor = conn.execute("""
                    DELETE FROM validation_cache
                    WHERE created_at < datetime('now', '-' || ? || ' days')
                    OR last_accessed < datetime('now', '-' || ? || ' days')
                """, (days_old, days_old))
                
                deleted_count = cursor.rowcount
                
                # Also vacuum to reclaim space
                if deleted_count > 0:
                    conn.execute("VACUUM")
                
                logger.info("ai_validation_cache_cleanup_completed", deleted_count=deleted_count)
                return deleted_count
                
        except Exception as e:
            logger.error("ai_validation_cache_cleanup_failed", error=str(e))
            return 0
    
    def get_cache_stats(self) -> Dict[str, Any]:
        """Get validation cache statistics."""
        try:
            with sqlite3.connect(self.validation_cache_path) as conn:
                cursor = conn.execute("""
                    SELECT 
                        COUNT(*) as total_entries,
                        COUNT(CASE WHEN validation_result = 'True' THEN 1 END) as valid_entries,
                        COUNT(CASE WHEN created_at > datetime('now', '-1 day') THEN 1 END) as recent_entries,
                        AVG(quality_score) as avg_quality_score,
                        MIN(created_at) as oldest_entry,
                        MAX(created_at) as newest_entry
                    FROM validation_cache
                """)
                
                stats = cursor.fetchone()
                
                return {
                    "total_entries": stats[0] or 0,
                    "valid_entries": stats[1] or 0,
                    "invalid_entries": (stats[0] or 0) - (stats[1] or 0),
                    "recent_entries": stats[2] or 0,
                    "avg_quality_score": round(stats[3] or 0, 3),
                    "oldest_entry": stats[4],
                    "newest_entry": stats[5],
                    "cache_ttl_days": self.cache_ttl_days
                }
                
        except Exception as e:
            logger.error("ai_validation_cache_stats_failed", error=str(e))
            return {"error": str(e)}