"""Framework parser for __NEXT_DATA__ and similar JS frameworks (~30ms, third priority)."""

import json
import re
import time
from datetime import datetime
from typing import Dict, List, Optional

from dateutil.parser import parse as parse_date
from selectolax.parser import HTMLParser

from ..monitoring.structured_logger import get_logger
from ..models import Article, ParseMethod, ParseResult

logger = get_logger(__name__)


class FrameworkParser:
    """Extract article data from JavaScript framework data."""
    
    def __init__(self):
        # Framework data patterns we look for
        self.framework_patterns = [
            {
                'name': 'nextjs',
                'selector': '#__NEXT_DATA__',
                'attribute': 'text',
                'parser': self._parse_nextjs_data
            },
            {
                'name': 'nuxt',
                'selector': '#__NUXT__',
                'attribute': 'text', 
                'parser': self._parse_nuxt_data
            },
            {
                'name': 'react_apollo',
                'selector': 'script[data-apollo-state]',
                'attribute': 'data-apollo-state',
                'parser': self._parse_apollo_data
            },
            {
                'name': 'gatsby',
                'selector': '#___gatsby',
                'attribute': 'text',
                'parser': self._parse_gatsby_data
            }
        ]
        
        # Common article indicators in framework data
        self.article_indicators = {
            'headline', 'title', 'name',
            'body', 'content', 'text', 'articleBody',
            'author', 'byline', 'creator',
            'publishedDate', 'datePublished', 'createdAt',
            'section', 'category', 'kicker',
            'tags', 'keywords', 'topics'
        }
        
        # NYT-specific data paths
        self.nyt_paths = [
            'props.pageProps.initialState',
            'props.pageProps.article', 
            'props.initialProps.article',
            'props.pageProps.data.article',
            'query.article',
            'runtimeConfig.article'
        ]
    
    def extract(self, html: str, url: str) -> ParseResult:
        """
        Extract article from JavaScript framework data.
        
        Args:
            html: HTML content
            url: Source URL
            
        Returns:
            ParseResult with extracted article or error
        """
        start_time = time.time()
        
        try:
            parser = HTMLParser(html)
            
            # Try each framework pattern
            for pattern in self.framework_patterns:
                try:
                    article = self._try_framework_pattern(parser, pattern, url)
                    if article and article.title:
                        duration = (time.time() - start_time) * 1000
                        
                        logger.debug(
                            "framework_extraction_success",
                            url=url,
                            framework=pattern['name'],
                            title_length=len(article.title),
                            body_length=len(article.body or ''),
                            duration_ms=duration
                        )
                        
                        return ParseResult(
                            success=True,
                            article=article,
                            parse_method=ParseMethod.FRAMEWORK,
                            duration_ms=duration,
                            confidence=0.85  # High confidence for framework data
                        )
                        
                except Exception as e:
                    logger.debug(
                        "framework_pattern_error",
                        framework=pattern['name'],
                        url=url,
                        error=str(e)
                    )
                    continue
            
            return ParseResult(
                success=False,
                error="No framework data found or parsed successfully",
                duration_ms=(time.time() - start_time) * 1000,
                confidence=0.0
            )
            
        except Exception as e:
            return ParseResult(
                success=False,
                error=f"Framework extraction failed: {e}",
                duration_ms=(time.time() - start_time) * 1000,
                confidence=0.0
            )
    
    def _try_framework_pattern(
        self, 
        parser: HTMLParser, 
        pattern: Dict, 
        url: str
    ) -> Optional[Article]:
        """Try to extract using a specific framework pattern."""
        elements = parser.css(pattern['selector'])
        
        for element in elements:
            if pattern['attribute'] == 'text':
                data_text = element.text()
            else:
                data_text = element.attributes.get(pattern['attribute'])
            
            if not data_text:
                continue
            
            try:
                # Parse JSON data
                data = json.loads(data_text)
                
                # Use framework-specific parser
                article = pattern['parser'](data, url)
                if article:
                    return article
                    
            except json.JSONDecodeError as e:
                logger.debug(
                    "framework_json_error",
                    framework=pattern['name'],
                    error=str(e)
                )
                continue
            except Exception as e:
                logger.debug(
                    "framework_parse_error",
                    framework=pattern['name'],
                    error=str(e)
                )
                continue
        
        return None
    
    def _parse_nextjs_data(self, data: Dict, url: str) -> Optional[Article]:
        """Parse Next.js __NEXT_DATA__ structure."""
        # Try NYT-specific paths first
        for path in self.nyt_paths:
            article_data = self._get_nested_value(data, path)
            if article_data:
                article = self._extract_article_from_data(article_data, url)
                if article:
                    return article
        
        # Try generic Next.js structure
        props = data.get('props', {})
        
        # Check pageProps
        page_props = props.get('pageProps', {})
        if page_props:
            article = self._extract_article_from_data(page_props, url)
            if article:
                return article
        
        # Check query params
        query = data.get('query', {})
        if query:
            article = self._extract_article_from_data(query, url)
            if article:
                return article
        
        return None
    
    def _parse_nuxt_data(self, data: Dict, url: str) -> Optional[Article]:
        """Parse Nuxt.js __NUXT__ structure."""
        # Nuxt stores data in different structures
        if isinstance(data, list) and data:
            # Try first item if it's an array
            return self._extract_article_from_data(data[0], url)
        elif isinstance(data, dict):
            return self._extract_article_from_data(data, url)
        
        return None
    
    def _parse_apollo_data(self, data: Dict, url: str) -> Optional[Article]:
        """Parse Apollo GraphQL state data."""
        # Apollo stores normalized data with IDs as keys
        for key, value in data.items():
            if isinstance(value, dict) and self._looks_like_article(value):
                article = self._extract_article_from_data(value, url)
                if article:
                    return article
        
        return None
    
    def _parse_gatsby_data(self, data: Dict, url: str) -> Optional[Article]:
        """Parse Gatsby framework data."""
        # Gatsby has various data structures
        return self._extract_article_from_data(data, url)
    
    def _extract_article_from_data(self, data: Dict, url: str) -> Optional[Article]:
        """Extract article fields from framework data structure."""
        if not isinstance(data, dict):
            return None
        
        # Search recursively through the data structure
        article_fields = {}
        self._search_article_fields(data, article_fields)
        
        if not article_fields:
            return None
        
        # Extract title
        title = (
            article_fields.get('headline') or
            article_fields.get('title') or
            article_fields.get('name', '')
        ).strip()
        
        if not title:
            return None
        
        # Extract body
        body = (
            article_fields.get('body') or
            article_fields.get('content') or
            article_fields.get('text') or
            article_fields.get('articleBody') or
            article_fields.get('abstract', '')
        ).strip()
        
        # Extract author
        author = self._extract_author_from_fields(article_fields)
        
        # Extract dates
        published_date = self._extract_date_from_fields(
            article_fields,
            ['publishedDate', 'datePublished', 'createdAt', 'published']
        )
        
        updated_date = self._extract_date_from_fields(
            article_fields,
            ['modifiedDate', 'dateModified', 'updatedAt', 'updated']
        )
        
        # Extract section
        section = (
            article_fields.get('section') or
            article_fields.get('category') or
            article_fields.get('kicker')
        )
        
        if section:
            section = str(section).strip()
        
        # Extract tags
        tags = self._extract_tags_from_fields(article_fields)
        
        return Article(
            source_url=url,
            title=title,
            body=body,
            author=author,
            published_date=published_date,
            updated_date=updated_date,
            section=section,
            tags=tags,
            parse_method=ParseMethod.FRAMEWORK,
            discovered_at=datetime.utcnow()
        )
    
    def _search_article_fields(self, data: Dict, fields: Dict, max_depth: int = 5):
        """Recursively search for article fields in nested data structure."""
        if max_depth <= 0:
            return
        
        for key, value in data.items():
            key_lower = str(key).lower()
            
            # Check if this key matches an article indicator
            if key_lower in self.article_indicators:
                if isinstance(value, (str, int, float)) and value:
                    fields[key_lower] = value
                elif isinstance(value, list) and value:
                    # Handle arrays (e.g., authors, tags)
                    if key_lower in ['tags', 'keywords', 'topics', 'authors']:
                        fields[key_lower] = value
                    else:
                        # For other arrays, take first non-empty item
                        for item in value:
                            if item:
                                fields[key_lower] = item
                                break
            
            # Recurse into nested objects
            elif isinstance(value, dict):
                self._search_article_fields(value, fields, max_depth - 1)
            
            # Check arrays of objects
            elif isinstance(value, list):
                for item in value[:3]:  # Limit to first 3 items
                    if isinstance(item, dict):
                        self._search_article_fields(item, fields, max_depth - 1)
    
    def _extract_author_from_fields(self, fields: Dict) -> Optional[str]:
        """Extract author from article fields."""
        author_fields = ['author', 'byline', 'creator', 'authors']
        
        for field in author_fields:
            author_data = fields.get(field)
            if not author_data:
                continue
            
            if isinstance(author_data, str):
                return author_data.strip()
            elif isinstance(author_data, list):
                authors = []
                for author in author_data:
                    if isinstance(author, dict):
                        name = author.get('name') or author.get('displayName') or str(author)
                        authors.append(name)
                    else:
                        authors.append(str(author))
                
                if authors:
                    return ', '.join(authors)
            elif isinstance(author_data, dict):
                return (
                    author_data.get('name') or
                    author_data.get('displayName') or
                    str(author_data)
                )
        
        return None
    
    def _extract_date_from_fields(
        self, 
        fields: Dict, 
        date_fields: List[str]
    ) -> Optional[datetime]:
        """Extract and parse date from article fields."""
        for field in date_fields:
            date_value = fields.get(field)
            if not date_value:
                continue
            
            try:
                if isinstance(date_value, str):
                    return parse_date(date_value)
                elif isinstance(date_value, (int, float)):
                    # Assume Unix timestamp
                    return datetime.fromtimestamp(date_value)
            except Exception as e:
                logger.debug(
                    "framework_date_parse_error",
                    field=field,
                    value=date_value,
                    error=str(e)
                )
                continue
        
        return None
    
    def _extract_tags_from_fields(self, fields: Dict) -> List[str]:
        """Extract tags from article fields."""
        tags = []
        tag_fields = ['tags', 'keywords', 'topics']
        
        for field in tag_fields:
            tag_data = fields.get(field)
            if not tag_data:
                continue
            
            if isinstance(tag_data, list):
                for tag in tag_data:
                    if isinstance(tag, dict):
                        tag_name = tag.get('name') or tag.get('label') or str(tag)
                        tags.append(tag_name)
                    else:
                        tags.append(str(tag))
            elif isinstance(tag_data, str):
                # Split comma-separated tags
                tags.extend([t.strip() for t in tag_data.split(',') if t.strip()])
        
        return list(set([tag.strip() for tag in tags if tag.strip()]))
    
    def _get_nested_value(self, data: Dict, path: str):
        """Get value from nested dictionary using dot notation."""
        keys = path.split('.')
        current = data
        
        for key in keys:
            if isinstance(current, dict) and key in current:
                current = current[key]
            else:
                return None
        
        return current
    
    def _looks_like_article(self, data: Dict) -> bool:
        """Check if data structure looks like an article."""
        if not isinstance(data, dict):
            return False
        
        # Check for article indicators
        keys = set(str(k).lower() for k in data.keys())
        article_key_count = len(keys.intersection(self.article_indicators))
        
        # Need at least 2 article-like keys
        return article_key_count >= 2
    
    def can_extract(self, html: str) -> bool:
        """Check if HTML contains framework data."""
        try:
            parser = HTMLParser(html)
            
            # Check for any framework patterns
            for pattern in self.framework_patterns:
                elements = parser.css(pattern['selector'])
                if elements:
                    return True
            
            return False
            
        except Exception:
            return False
    
    def get_extraction_stats(self) -> Dict:
        """Get extraction statistics."""
        return {
            "method": "framework",
            "priority": 3,
            "avg_duration_ms": 30,
            "confidence": 0.85,
            "supported_frameworks": [p['name'] for p in self.framework_patterns],
            "nyt_paths": self.nyt_paths
        }