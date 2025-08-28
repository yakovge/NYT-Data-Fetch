"""SimHash-based deduplication manager for near-duplicate article detection."""

import re
import hashlib
from typing import List, Set, Optional, Tuple
from collections import Counter

from simhash import Simhash
from selectolax.parser import HTMLParser

from ..config import config
from ..monitoring.structured_logger import get_logger
from ..models import Article

logger = get_logger(__name__)


class DedupManager:
    """Manages article deduplication using SimHash for near-duplicate detection."""
    
    def __init__(self):
        self.similarity_threshold = 3  # Hamming distance threshold for near-duplicates
        self.min_content_length = 100  # Minimum content length to consider
        self.stopwords = self._load_stopwords()
        
    def _load_stopwords(self) -> Set[str]:
        """Load common English stopwords for better deduplication."""
        return {
            'the', 'and', 'or', 'but', 'in', 'on', 'at', 'to', 'for', 'of', 'with',
            'by', 'from', 'up', 'about', 'into', 'through', 'during', 'before',
            'after', 'above', 'below', 'between', 'among', 'is', 'are', 'was',
            'were', 'be', 'been', 'being', 'have', 'has', 'had', 'do', 'does',
            'did', 'will', 'would', 'could', 'should', 'may', 'might', 'must',
            'can', 'shall', 'a', 'an', 'this', 'that', 'these', 'those'
        }
    
    def calculate_simhash(self, article: Article) -> int:
        """
        Calculate SimHash for an article based on its content.
        
        Args:
            article: Article to calculate SimHash for
            
        Returns:
            SimHash value as integer
        """
        try:
            # Extract meaningful text content
            content = self._extract_content_features(article)
            
            if len(content) < self.min_content_length:
                logger.warning(
                    "content_too_short_for_simhash",
                    url=article.source_url,
                    content_length=len(content)
                )
                return 0
            
            # Calculate SimHash
            simhash = Simhash(content)
            
            logger.debug(
                "simhash_calculated",
                url=article.source_url,
                simhash=simhash.value,
                content_length=len(content)
            )
            
            return simhash.value
            
        except Exception as e:
            logger.error(
                "simhash_calculation_failed",
                url=article.source_url,
                error=str(e)
            )
            return 0
    
    def _extract_content_features(self, article: Article) -> str:
        """
        Extract meaningful features from article for SimHash calculation.
        
        Args:
            article: Article to extract features from
            
        Returns:
            Normalized content string for SimHash
        """
        # Combine title and body with title weighted higher
        content_parts = []
        
        if article.title:
            # Title gets 3x weight
            normalized_title = self._normalize_text(article.title)
            content_parts.extend([normalized_title] * 3)
        
        if article.body:
            # Clean HTML from body if present
            clean_body = self._clean_html(article.body)
            normalized_body = self._normalize_text(clean_body)
            content_parts.append(normalized_body)
        
        if article.author:
            # Author gets 2x weight for byline similarity
            normalized_author = self._normalize_text(article.author)
            content_parts.extend([normalized_author] * 2)
        
        return ' '.join(content_parts)
    
    def _clean_html(self, html_content: str) -> str:
        """Remove HTML tags and extract clean text."""
        try:
            # Parse HTML and extract text
            parser = HTMLParser(html_content)
            text = parser.text()
            return text or html_content
        except Exception:
            # Fallback to regex-based cleaning
            return re.sub(r'<[^>]+>', ' ', html_content)
    
    def _normalize_text(self, text: str) -> str:
        """
        Normalize text for better deduplication matching.
        
        Args:
            text: Raw text to normalize
            
        Returns:
            Normalized text string
        """
        if not text:
            return ""
        
        # Convert to lowercase
        text = text.lower()
        
        # Remove special characters but keep word boundaries
        text = re.sub(r'[^\w\s]', ' ', text)
        
        # Split into words and remove stopwords
        words = text.split()
        filtered_words = [word for word in words if word not in self.stopwords and len(word) > 2]
        
        return ' '.join(filtered_words)
    
    def is_duplicate(self, article1: Article, article2: Article) -> bool:
        """
        Check if two articles are near-duplicates using SimHash.
        
        Args:
            article1: First article to compare
            article2: Second article to compare
            
        Returns:
            True if articles are considered near-duplicates
        """
        try:
            # Calculate SimHashes if not already present
            simhash1 = article1.simhash or self.calculate_simhash(article1)
            simhash2 = article2.simhash or self.calculate_simhash(article2)
            
            if simhash1 == 0 or simhash2 == 0:
                return False
            
            # Calculate Hamming distance
            distance = bin(simhash1 ^ simhash2).count('1')
            is_duplicate = distance <= self.similarity_threshold
            
            logger.debug(
                "duplicate_check",
                url1=article1.source_url,
                url2=article2.source_url,
                distance=distance,
                is_duplicate=is_duplicate
            )
            
            return is_duplicate
            
        except Exception as e:
            logger.error(
                "duplicate_check_failed",
                url1=article1.source_url,
                url2=article2.source_url,
                error=str(e)
            )
            return False
    
    def find_similar_articles(self, target_article: Article, candidate_articles: List[Article]) -> List[Tuple[Article, int]]:
        """
        Find articles similar to target article.
        
        Args:
            target_article: Article to find similarities for
            candidate_articles: List of articles to compare against
            
        Returns:
            List of (article, hamming_distance) tuples, sorted by similarity
        """
        similar_articles = []
        
        target_simhash = target_article.simhash or self.calculate_simhash(target_article)
        if target_simhash == 0:
            return similar_articles
        
        for candidate in candidate_articles:
            if candidate.source_url == target_article.source_url:
                continue
                
            candidate_simhash = candidate.simhash or self.calculate_simhash(candidate)
            if candidate_simhash == 0:
                continue
            
            distance = bin(target_simhash ^ candidate_simhash).count('1')
            if distance <= self.similarity_threshold * 2:  # Broader threshold for similarity search
                similar_articles.append((candidate, distance))
        
        # Sort by similarity (lower distance = more similar)
        similar_articles.sort(key=lambda x: x[1])
        
        logger.debug(
            "similar_articles_found",
            target_url=target_article.source_url,
            similar_count=len(similar_articles)
        )
        
        return similar_articles
    
    def detect_content_duplication_ratio(self, article: Article, existing_articles: List[Article]) -> float:
        """
        Calculate what percentage of existing articles are duplicates of this article.
        
        Args:
            article: Article to check for duplication
            existing_articles: List of existing articles to compare against
            
        Returns:
            Ratio of duplicate articles (0.0 to 1.0)
        """
        if not existing_articles:
            return 0.0
        
        duplicate_count = 0
        
        for existing_article in existing_articles:
            if self.is_duplicate(article, existing_article):
                duplicate_count += 1
        
        ratio = duplicate_count / len(existing_articles)
        
        logger.debug(
            "duplication_ratio_calculated",
            url=article.source_url,
            duplicate_count=duplicate_count,
            total_articles=len(existing_articles),
            ratio=ratio
        )
        
        return ratio
    
    def calculate_content_uniqueness_score(self, article: Article) -> float:
        """
        Calculate a uniqueness score for the article content.
        
        Args:
            article: Article to calculate uniqueness for
            
        Returns:
            Uniqueness score (0.0 to 1.0, higher = more unique)
        """
        try:
            content = self._extract_content_features(article)
            
            if len(content) < self.min_content_length:
                return 0.0
            
            # Calculate word frequency distribution
            words = content.split()
            word_freq = Counter(words)
            
            # Calculate entropy-based uniqueness
            total_words = len(words)
            entropy = 0.0
            
            for count in word_freq.values():
                probability = count / total_words
                if probability > 0:
                    entropy -= probability * (probability).bit_length()
            
            # Normalize entropy to 0-1 scale
            max_entropy = (1.0 / total_words).bit_length() * total_words if total_words > 0 else 1.0
            uniqueness_score = min(entropy / max_entropy, 1.0) if max_entropy > 0 else 0.0
            
            logger.debug(
                "uniqueness_score_calculated",
                url=article.source_url,
                score=uniqueness_score,
                word_count=total_words,
                unique_words=len(word_freq)
            )
            
            return uniqueness_score
            
        except Exception as e:
            logger.error(
                "uniqueness_calculation_failed",
                url=article.source_url,
                error=str(e)
            )
            return 0.0