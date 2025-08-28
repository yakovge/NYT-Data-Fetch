"""HTML parser using selectolax + readability (~50ms, fourth priority)."""

import re
import time
from datetime import datetime
from typing import Dict, List, Optional, Tuple

from dateutil.parser import parse as parse_date
from selectolax.parser import HTMLParser as SelectolaxParser

from ..monitoring.structured_logger import get_logger
from ..models import Article, ParseMethod, ParseResult

logger = get_logger(__name__)


class HTMLParser:
    """Extract article data using semantic HTML parsing with selectolax."""
    
    def __init__(self):
        # NYT-specific selectors (highest priority)
        self.nyt_selectors = {
            'title': [
                'h1[data-testid="headline"]',
                'h1.css-1rha1cf',  # NYT headline class
                'h1.headline',
                '.story-heading h1',
                '.entry-title'
            ],
            'body': [
                'section[name="articleBody"]',
                'div[data-testid="articleBody"]', 
                'div.css-53u6y8',  # NYT article body class
                '.story-body-text',
                '.entry-content',
                '.article-content'
            ],
            'author': [
                'span[data-testid="byline-author"]',
                '.css-1baulvz',  # NYT byline class
                '.byline-author',
                '.author-name',
                '[rel="author"]'
            ],
            'date': [
                'time[data-testid="timestamp"]',
                '.css-sv1te3',  # NYT timestamp class
                '.timestamp',
                'time[datetime]',
                '.published-date'
            ],
            'section': [
                '[data-testid="kicker"]',
                '.css-nzgijy',  # NYT kicker class
                '.kicker',
                '.section-name'
            ]
        }
        
        # Generic selectors (fallback)
        self.generic_selectors = {
            'title': [
                'h1',
                '.title',
                '.headline', 
                '.entry-title',
                '.post-title',
                '[class*="title"]',
                '[class*="headline"]'
            ],
            'body': [
                'article',
                '.content',
                '.entry-content',
                '.post-content',
                '.article-body',
                '.story-content',
                'main',
                '[class*="content"]',
                '[class*="body"]'
            ],
            'author': [
                '.author',
                '.byline',
                '.writer',
                '[rel="author"]',
                '[class*="author"]',
                '[class*="byline"]'
            ],
            'date': [
                'time[datetime]',
                '.date',
                '.published',
                '.timestamp',
                '[class*="date"]',
                '[class*="time"]'
            ]
        }
        
        # Content cleaning patterns
        self.cleaning_patterns = [
            (r'<script[^>]*>.*?</script>', ''),  # Remove scripts
            (r'<style[^>]*>.*?</style>', ''),   # Remove styles
            (r'<nav[^>]*>.*?</nav>', ''),       # Remove navigation
            (r'<header[^>]*>.*?</header>', ''), # Remove headers
            (r'<footer[^>]*>.*?</footer>', ''), # Remove footers
            (r'<aside[^>]*>.*?</aside>', ''),   # Remove sidebars
            (r'<div[^>]*class="[^"]*ad[^"]*"[^>]*>.*?</div>', ''),  # Remove ads
            (r'\s+', ' '),  # Normalize whitespace
        ]
        
        # Readability scoring weights
        self.scoring_weights = {
            'length': 0.3,
            'paragraph_count': 0.2,
            'link_density': -0.3,
            'class_weight': 0.2
        }
    
    def extract(self, html: str, url: str) -> ParseResult:
        """
        Extract article using HTML parsing with readability.
        
        Args:
            html: HTML content
            url: Source URL
            
        Returns:
            ParseResult with extracted article or error
        """
        start_time = time.time()
        
        try:
            # Clean HTML for better parsing
            cleaned_html = self._clean_html(html)
            parser = SelectolaxParser(cleaned_html)
            
            # Try NYT-specific selectors first
            article = self._extract_with_selectors(parser, self.nyt_selectors, url)
            
            # Fall back to generic selectors if needed
            if not article or not article.title:
                article = self._extract_with_selectors(parser, self.generic_selectors, url)
            
            # Try readability algorithm if still no good content
            if not article or len(article.body or '') < 500:
                readability_article = self._extract_with_readability(parser, url)
                if readability_article and len(readability_article.body or '') > len(article.body or '' if article else ''):
                    article = readability_article
            
            if article and article.title:
                duration = (time.time() - start_time) * 1000
                
                # Calculate confidence based on content quality
                confidence = self._calculate_confidence(article, parser)
                
                logger.debug(
                    "html_extraction_success",
                    url=url,
                    title_length=len(article.title),
                    body_length=len(article.body or ''),
                    duration_ms=duration,
                    confidence=confidence
                )
                
                return ParseResult(
                    success=True,
                    article=article,
                    parse_method=ParseMethod.HTML,
                    duration_ms=duration,
                    confidence=confidence
                )
            
            return ParseResult(
                success=False,
                error="Could not extract sufficient article content from HTML",
                duration_ms=(time.time() - start_time) * 1000,
                confidence=0.0
            )
            
        except Exception as e:
            return ParseResult(
                success=False,
                error=f"HTML extraction failed: {e}",
                duration_ms=(time.time() - start_time) * 1000,
                confidence=0.0
            )
    
    def _clean_html(self, html: str) -> str:
        """Clean HTML for better parsing."""
        cleaned = html
        
        for pattern, replacement in self.cleaning_patterns:
            cleaned = re.sub(pattern, replacement, cleaned, flags=re.IGNORECASE | re.DOTALL)
        
        return cleaned
    
    def _extract_with_selectors(
        self, 
        parser: SelectolaxParser, 
        selectors: Dict, 
        url: str
    ) -> Optional[Article]:
        """Extract article using CSS selectors."""
        # Extract title
        title = self._extract_text_with_selectors(parser, selectors.get('title', []))
        if not title:
            return None
        
        # Extract body
        body = self._extract_body_content(parser, selectors.get('body', []))
        
        # Extract author
        author = self._extract_text_with_selectors(parser, selectors.get('author', []))
        
        # Extract date
        published_date = self._extract_date_with_selectors(parser, selectors.get('date', []))
        
        # Extract section
        section = self._extract_text_with_selectors(parser, selectors.get('section', []))
        
        # Extract tags from various sources
        tags = self._extract_tags_from_html(parser)
        
        return Article(
            source_url=url,
            title=title.strip(),
            body=body.strip() if body else '',
            author=author,
            published_date=published_date,
            section=section,
            tags=tags,
            parse_method=ParseMethod.HTML,
            discovered_at=datetime.utcnow()
        )
    
    def _extract_text_with_selectors(
        self, 
        parser: SelectolaxParser, 
        selectors: List[str]
    ) -> Optional[str]:
        """Extract text using ordered list of selectors."""
        for selector in selectors:
            try:
                element = parser.css_first(selector)
                if element and element.text(strip=True):
                    return element.text(strip=True)
            except Exception as e:
                logger.debug("selector_error", selector=selector, error=str(e))
                continue
        
        return None
    
    def _extract_body_content(
        self, 
        parser: SelectolaxParser, 
        selectors: List[str]
    ) -> Optional[str]:
        """Extract body content with paragraph handling."""
        for selector in selectors:
            try:
                element = parser.css_first(selector)
                if element:
                    # Extract paragraphs
                    paragraphs = element.css('p')
                    if paragraphs:
                        # Join paragraphs with double newlines
                        content = '\n\n'.join([
                            p.text(strip=True) for p in paragraphs 
                            if p.text(strip=True)
                        ])
                        if len(content) > 200:  # Minimum content length
                            return content
                    
                    # Fall back to full text if no paragraphs
                    text = element.text(strip=True)
                    if len(text) > 200:
                        return text
                        
            except Exception as e:
                logger.debug("body_selector_error", selector=selector, error=str(e))
                continue
        
        return None
    
    def _extract_date_with_selectors(
        self, 
        parser: SelectolaxParser, 
        selectors: List[str]
    ) -> Optional[datetime]:
        """Extract and parse date using selectors."""
        for selector in selectors:
            try:
                element = parser.css_first(selector)
                if element:
                    # Try datetime attribute first
                    datetime_attr = element.attributes.get('datetime')
                    if datetime_attr:
                        try:
                            return parse_date(datetime_attr)
                        except Exception:
                            pass
                    
                    # Try element text
                    date_text = element.text(strip=True)
                    if date_text:
                        try:
                            return parse_date(date_text)
                        except Exception:
                            pass
                            
            except Exception as e:
                logger.debug("date_selector_error", selector=selector, error=str(e))
                continue
        
        return None
    
    def _extract_tags_from_html(self, parser: SelectolaxParser) -> List[str]:
        """Extract tags from various HTML elements."""
        tags = []
        
        # Try meta keywords
        keywords_meta = parser.css_first('meta[name="keywords"]')
        if keywords_meta:
            keywords = keywords_meta.attributes.get('content', '')
            if keywords:
                tags.extend([k.strip() for k in keywords.split(',') if k.strip()])
        
        # Try category/tag elements
        tag_selectors = [
            '.tags a',
            '.categories a',
            '[class*="tag"] a',
            '[class*="category"] a'
        ]
        
        for selector in tag_selectors:
            try:
                elements = parser.css(selector)
                for element in elements[:10]:  # Limit to 10 tags
                    tag = element.text(strip=True)
                    if tag:
                        tags.append(tag)
            except Exception:
                continue
        
        return list(set(tags))  # Remove duplicates
    
    def _extract_with_readability(
        self, 
        parser: SelectolaxParser, 
        url: str
    ) -> Optional[Article]:
        """Extract using readability algorithm."""
        try:
            # Find all potential content containers
            candidates = []
            
            # Get all divs, articles, sections
            for selector in ['div', 'article', 'section', 'main']:
                elements = parser.css(selector)
                for element in elements:
                    score = self._score_content_element(element)
                    if score > 0:
                        candidates.append((element, score))
            
            if not candidates:
                return None
            
            # Sort by score and take best candidate
            candidates.sort(key=lambda x: x[1], reverse=True)
            best_element = candidates[0][0]
            
            # Extract title from page
            title = self._extract_title_readability(parser)
            if not title:
                return None
            
            # Extract body from best element
            body = self._extract_clean_text(best_element)
            if len(body) < 200:
                return None
            
            return Article(
                source_url=url,
                title=title.strip(),
                body=body.strip(),
                parse_method=ParseMethod.HTML,
                discovered_at=datetime.utcnow()
            )
            
        except Exception as e:
            logger.debug("readability_error", error=str(e))
            return None
    
    def _score_content_element(self, element) -> float:
        """Score an element for content quality using readability heuristics."""
        try:
            score = 0.0
            
            # Get text content
            text = element.text(strip=True)
            if not text:
                return 0.0
            
            text_length = len(text)
            
            # Length scoring
            if text_length > 500:
                score += self.scoring_weights['length'] * (text_length / 1000)
            
            # Paragraph count
            paragraphs = element.css('p')
            paragraph_count = len([p for p in paragraphs if len(p.text(strip=True)) > 50])
            score += self.scoring_weights['paragraph_count'] * paragraph_count
            
            # Link density (negative scoring)
            links = element.css('a')
            link_text_length = sum(len(link.text(strip=True)) for link in links)
            link_density = link_text_length / max(text_length, 1)
            score += self.scoring_weights['link_density'] * link_density
            
            # Class/ID weight
            class_weight = self._get_class_weight(element)
            score += self.scoring_weights['class_weight'] * class_weight
            
            return max(score, 0.0)
            
        except Exception:
            return 0.0
    
    def _get_class_weight(self, element) -> float:
        """Get weight based on class and ID names."""
        weight = 0.0
        
        class_names = element.attributes.get('class', '').lower()
        id_name = element.attributes.get('id', '').lower()
        
        # Positive indicators
        positive_patterns = [
            'content', 'article', 'story', 'post', 'body', 'text', 'main'
        ]
        
        # Negative indicators
        negative_patterns = [
            'comment', 'sidebar', 'nav', 'ad', 'footer', 'header', 'menu'
        ]
        
        for pattern in positive_patterns:
            if pattern in class_names or pattern in id_name:
                weight += 1.0
        
        for pattern in negative_patterns:
            if pattern in class_names or pattern in id_name:
                weight -= 1.0
        
        return weight
    
    def _extract_title_readability(self, parser: SelectolaxParser) -> Optional[str]:
        """Extract title using readability approach."""
        # Try h1 elements first
        h1_elements = parser.css('h1')
        for h1 in h1_elements:
            text = h1.text(strip=True)
            if len(text) > 10 and len(text) < 200:  # Reasonable title length
                return text
        
        # Fall back to page title
        title_element = parser.css_first('title')
        if title_element:
            title = title_element.text(strip=True)
            # Clean up title (remove site name)
            title = re.sub(r'\s*[-|–]\s*.*$', '', title)  # Remove after dash
            if len(title) > 10:
                return title
        
        return None
    
    def _extract_clean_text(self, element) -> str:
        """Extract clean text from element."""
        # Remove unwanted elements
        unwanted_selectors = [
            'script', 'style', 'nav', 'header', 'footer', 'aside',
            '.ad', '.advertisement', '.social', '.share', '.comment'
        ]
        
        # Clone element to avoid modifying original
        html_str = str(element)
        temp_parser = SelectolaxParser(html_str)
        
        # Remove unwanted elements
        for selector in unwanted_selectors:
            try:
                elements = temp_parser.css(selector)
                for elem in elements:
                    elem.decompose()
            except Exception:
                continue
        
        # Extract paragraphs
        paragraphs = temp_parser.css('p')
        if paragraphs:
            text_parts = []
            for p in paragraphs:
                p_text = p.text(strip=True)
                if len(p_text) > 30:  # Skip very short paragraphs
                    text_parts.append(p_text)
            
            if text_parts:
                return '\n\n'.join(text_parts)
        
        # Fall back to all text
        return temp_parser.text(strip=True)
    
    def _calculate_confidence(self, article: Article, parser: SelectolaxParser) -> float:
        """Calculate confidence score based on content quality."""
        confidence = 0.0
        
        # Title quality
        if article.title and len(article.title) > 10:
            confidence += 0.2
        
        # Body quality
        if article.body:
            body_len = len(article.body)
            if body_len > 500:
                confidence += 0.3
            elif body_len > 200:
                confidence += 0.2
        
        # Author presence
        if article.author:
            confidence += 0.1
        
        # Date presence
        if article.published_date:
            confidence += 0.1
        
        # Section presence
        if article.section:
            confidence += 0.05
        
        # Tags presence
        if article.tags:
            confidence += 0.05
        
        # Check for article-like structure
        if parser.css('p'):
            confidence += 0.2
        
        return min(confidence, 0.75)  # Cap at 75% for HTML parsing
    
    def can_extract(self, html: str) -> bool:
        """Check if HTML can be parsed for content."""
        try:
            parser = SelectolaxParser(html)
            
            # Check for basic content indicators
            has_title = bool(parser.css('h1') or parser.css('title'))
            has_content = bool(parser.css('p') or parser.css('article') or parser.css('.content'))
            
            return has_title and has_content
            
        except Exception:
            return False
    
    def get_extraction_stats(self) -> Dict:
        """Get extraction statistics."""
        return {
            "method": "html",
            "priority": 4,
            "avg_duration_ms": 50,
            "confidence_range": "0.4-0.75",
            "nyt_selectors": len(self.nyt_selectors),
            "generic_selectors": len(self.generic_selectors),
            "uses_readability": True
        }