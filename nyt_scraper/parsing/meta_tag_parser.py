"""Meta tag parser for OpenGraph/Twitter cards (~20ms, second priority)."""

import re
import time
from datetime import datetime
from typing import Dict, List, Optional

from dateutil.parser import parse as parse_date
from selectolax.parser import HTMLParser

from ..monitoring.structured_logger import get_logger
from ..models import Article, ParseMethod, ParseResult

logger = get_logger(__name__)


class MetaTagParser:
    """Extract article data from OpenGraph and Twitter Card meta tags."""
    
    def __init__(self):
        # OpenGraph properties we extract
        self.og_properties = {
            'title': ['og:title'],
            'description': ['og:description'],
            'url': ['og:url'],
            'type': ['og:type'],
            'site_name': ['og:site_name'],
            'published_time': ['og:published_time', 'article:published_time'],
            'modified_time': ['og:modified_time', 'article:modified_time'],
            'author': ['og:author', 'article:author'],
            'section': ['og:section', 'article:section'],
            'tag': ['og:tag', 'article:tag'],
            'image': ['og:image']
        }
        
        # Twitter Card properties
        self.twitter_properties = {
            'title': ['twitter:title'],
            'description': ['twitter:description'],
            'site': ['twitter:site'],
            'creator': ['twitter:creator'],
            'card': ['twitter:card'],
            'image': ['twitter:image']
        }
        
        # Standard HTML meta tags
        self.html_meta = {
            'title': ['title'],
            'description': ['description', 'summary'],
            'keywords': ['keywords'],
            'author': ['author', 'creator'],
            'published': ['date', 'publish-date', 'published-date'],
            'modified': ['last-modified', 'modified-date']
        }
        
        # Article type indicators
        self.article_types = {
            'article', 'news', 'blog', 'story', 'report'
        }
    
    def extract(self, html: str, url: str) -> ParseResult:
        """
        Extract article from meta tags.
        
        Args:
            html: HTML content
            url: Source URL
            
        Returns:
            ParseResult with extracted article or error
        """
        start_time = time.time()
        
        try:
            parser = HTMLParser(html)
            
            # Extract all meta tag data
            og_data = self._extract_opengraph(parser)
            twitter_data = self._extract_twitter_cards(parser)
            html_meta_data = self._extract_html_meta(parser)
            
            # Try to build article from combined data
            article = self._build_article_from_meta(
                og_data, twitter_data, html_meta_data, url
            )
            
            if article and article.title:
                duration = (time.time() - start_time) * 1000
                
                # Calculate confidence based on data quality
                confidence = self._calculate_confidence(
                    og_data, twitter_data, html_meta_data
                )
                
                logger.debug(
                    "meta_tag_extraction_success",
                    url=url,
                    title_length=len(article.title),
                    body_length=len(article.body or ''),
                    duration_ms=duration,
                    confidence=confidence
                )
                
                return ParseResult(
                    success=True,
                    article=article,
                    parse_method=ParseMethod.META_TAGS,
                    duration_ms=duration,
                    confidence=confidence
                )
            
            return ParseResult(
                success=False,
                error="Insufficient meta tag data for article",
                duration_ms=(time.time() - start_time) * 1000,
                confidence=0.0
            )
            
        except Exception as e:
            return ParseResult(
                success=False,
                error=f"Meta tag extraction failed: {e}",
                duration_ms=(time.time() - start_time) * 1000,
                confidence=0.0
            )
    
    def _extract_opengraph(self, parser: HTMLParser) -> Dict:
        """Extract OpenGraph meta properties."""
        og_data = {}
        
        # Get all meta tags with property attribute
        meta_tags = parser.css('meta[property]')
        
        for tag in meta_tags:
            prop = tag.attributes.get('property', '').lower()
            content = tag.attributes.get('content', '')
            
            if not prop.startswith('og:') and not prop.startswith('article:'):
                continue
            
            if not content:
                continue
            
            # Store the data
            og_data[prop] = content
            
            # Handle multiple values (like tags)
            if prop in ['og:tag', 'article:tag']:
                if 'tags' not in og_data:
                    og_data['tags'] = []
                og_data['tags'].append(content)
        
        return og_data
    
    def _extract_twitter_cards(self, parser: HTMLParser) -> Dict:
        """Extract Twitter Card meta properties."""
        twitter_data = {}
        
        # Get all meta tags with name attribute starting with twitter
        meta_tags = parser.css('meta[name^="twitter:"]')
        
        for tag in meta_tags:
            name = tag.attributes.get('name', '').lower()
            content = tag.attributes.get('content', '')
            
            if not content:
                continue
            
            twitter_data[name] = content
        
        return twitter_data
    
    def _extract_html_meta(self, parser: HTMLParser) -> Dict:
        """Extract standard HTML meta tags."""
        html_data = {}
        
        # Get title from <title> tag
        title_tag = parser.css_first('title')
        if title_tag and title_tag.text():
            html_data['title'] = title_tag.text().strip()
        
        # Get meta tags with name attribute
        meta_tags = parser.css('meta[name]')
        
        for tag in meta_tags:
            name = tag.attributes.get('name', '').lower()
            content = tag.attributes.get('content', '')
            
            if not content:
                continue
            
            html_data[name] = content
        
        # Also check meta tags with http-equiv
        http_equiv_tags = parser.css('meta[http-equiv]')
        for tag in http_equiv_tags:
            equiv = tag.attributes.get('http-equiv', '').lower()
            content = tag.attributes.get('content', '')
            
            if equiv in ['date', 'last-modified'] and content:
                html_data[equiv] = content
        
        return html_data
    
    def _build_article_from_meta(
        self, 
        og_data: Dict, 
        twitter_data: Dict, 
        html_data: Dict, 
        url: str
    ) -> Optional[Article]:
        """Build article from combined meta tag data."""
        # Extract title (priority: OG > Twitter > HTML)
        title = (
            og_data.get('og:title') or 
            twitter_data.get('twitter:title') or
            html_data.get('title', '')
        ).strip()
        
        if not title:
            return None
        
        # Extract description/body (priority: OG > Twitter > HTML)
        body = (
            og_data.get('og:description') or
            twitter_data.get('twitter:description') or
            html_data.get('description', '') or
            html_data.get('summary', '')
        ).strip()
        
        # Extract author
        author = self._extract_author_from_meta(og_data, twitter_data, html_data)
        
        # Extract dates
        published_date = self._extract_published_date(og_data, html_data)
        updated_date = self._extract_updated_date(og_data, html_data)
        
        # Extract section
        section = self._extract_section_from_meta(og_data, url)
        
        # Extract tags
        tags = self._extract_tags_from_meta(og_data, html_data)
        
        # Validate this looks like an article
        if not self._validate_article_content(og_data, twitter_data):
            return None
        
        return Article(
            source_url=url,
            title=title,
            body=body,
            author=author,
            published_date=published_date,
            updated_date=updated_date,
            section=section,
            tags=tags,
            parse_method=ParseMethod.META_TAGS,
            discovered_at=datetime.utcnow()
        )
    
    def _extract_author_from_meta(
        self, 
        og_data: Dict, 
        twitter_data: Dict, 
        html_data: Dict
    ) -> Optional[str]:
        """Extract author from meta data."""
        author = (
            og_data.get('og:author') or
            og_data.get('article:author') or
            html_data.get('author') or
            html_data.get('creator')
        )
        
        if author:
            return author.strip()
        
        # Try Twitter creator (remove @ symbol)
        twitter_creator = twitter_data.get('twitter:creator', '')
        if twitter_creator:
            return twitter_creator.lstrip('@').strip()
        
        return None
    
    def _extract_published_date(self, og_data: Dict, html_data: Dict) -> Optional[datetime]:
        """Extract publication date."""
        date_candidates = [
            og_data.get('og:published_time'),
            og_data.get('article:published_time'),
            html_data.get('date'),
            html_data.get('publish-date'),
            html_data.get('published-date')
        ]
        
        for date_str in date_candidates:
            if date_str:
                try:
                    return parse_date(date_str)
                except Exception as e:
                    logger.debug(
                        "date_parse_error",
                        date_string=date_str,
                        error=str(e)
                    )
                    continue
        
        return None
    
    def _extract_updated_date(self, og_data: Dict, html_data: Dict) -> Optional[datetime]:
        """Extract modification date."""
        date_candidates = [
            og_data.get('og:modified_time'),
            og_data.get('article:modified_time'),
            html_data.get('last-modified'),
            html_data.get('modified-date')
        ]
        
        for date_str in date_candidates:
            if date_str:
                try:
                    return parse_date(date_str)
                except Exception as e:
                    logger.debug(
                        "modified_date_parse_error",
                        date_string=date_str,
                        error=str(e)
                    )
                    continue
        
        return None
    
    def _extract_section_from_meta(self, og_data: Dict, url: str) -> Optional[str]:
        """Extract section from meta data and URL."""
        # Try OpenGraph section first
        section = og_data.get('og:section') or og_data.get('article:section')
        
        if section:
            return section.strip()
        
        # Try to extract from URL path
        from urllib.parse import urlparse
        parsed = urlparse(url)
        path_parts = [p for p in parsed.path.split('/') if p]
        
        if path_parts:
            # Skip date parts and return first non-date part
            for part in path_parts:
                if not re.match(r'^\d{4}$', part) and not re.match(r'^\d{2}$', part):
                    return part
        
        return None
    
    def _extract_tags_from_meta(self, og_data: Dict, html_data: Dict) -> List[str]:
        """Extract tags from meta data."""
        tags = []
        
        # Get OpenGraph/Article tags
        if 'tags' in og_data:
            tags.extend(og_data['tags'])
        
        # Get from single tag property
        single_tags = og_data.get('og:tag') or og_data.get('article:tag')
        if single_tags:
            if isinstance(single_tags, list):
                tags.extend(single_tags)
            else:
                tags.append(single_tags)
        
        # Get from HTML keywords
        keywords = html_data.get('keywords', '')
        if keywords:
            # Split on common separators
            for separator in [',', ';', '|']:
                if separator in keywords:
                    keyword_tags = [k.strip() for k in keywords.split(separator)]
                    tags.extend([k for k in keyword_tags if k])
                    break
            else:
                tags.append(keywords.strip())
        
        return list(set([tag.strip() for tag in tags if tag.strip()]))
    
    def _validate_article_content(self, og_data: Dict, twitter_data: Dict) -> bool:
        """Validate that meta data indicates this is an article."""
        # Check OpenGraph type
        og_type = og_data.get('og:type', '').lower()
        if og_type and any(article_type in og_type for article_type in self.article_types):
            return True
        
        # Check Twitter card type
        twitter_card = twitter_data.get('twitter:card', '').lower()
        if 'summary' in twitter_card:  # summary cards often used for articles
            return True
        
        # Check site name for news indicators
        site_name = og_data.get('og:site_name', '').lower()
        if any(indicator in site_name for indicator in ['times', 'news', 'journal', 'post']):
            return True
        
        # If we have reasonable content length, assume it's valid
        description = og_data.get('og:description', '')
        if len(description) > 100:  # Reasonable description length
            return True
        
        return False
    
    def _calculate_confidence(
        self, 
        og_data: Dict, 
        twitter_data: Dict, 
        html_data: Dict
    ) -> float:
        """Calculate confidence score based on available meta data."""
        confidence = 0.0
        
        # OpenGraph data adds significant confidence
        if og_data.get('og:title'):
            confidence += 0.3
        if og_data.get('og:description'):
            confidence += 0.2
        if og_data.get('og:type'):
            confidence += 0.1
        if og_data.get('og:published_time') or og_data.get('article:published_time'):
            confidence += 0.1
        if og_data.get('og:author') or og_data.get('article:author'):
            confidence += 0.1
        
        # Twitter cards add moderate confidence
        if twitter_data.get('twitter:title'):
            confidence += 0.1
        if twitter_data.get('twitter:description'):
            confidence += 0.1
        
        # HTML meta adds basic confidence
        if html_data.get('title'):
            confidence += 0.05
        if html_data.get('description'):
            confidence += 0.05
        
        return min(confidence, 0.8)  # Cap at 80% for meta tags
    
    def can_extract(self, html: str) -> bool:
        """Check if HTML contains sufficient meta tags."""
        try:
            parser = HTMLParser(html)
            
            # Check for essential meta tags
            og_title = parser.css_first('meta[property="og:title"]')
            og_desc = parser.css_first('meta[property="og:description"]')
            twitter_title = parser.css_first('meta[name="twitter:title"]')
            html_title = parser.css_first('title')
            
            return bool(og_title or og_desc or twitter_title or html_title)
            
        except Exception:
            return False
    
    def get_extraction_stats(self) -> Dict:
        """Get extraction statistics."""
        return {
            "method": "meta_tags",
            "priority": 2,
            "avg_duration_ms": 20,
            "confidence_range": "0.5-0.8",
            "supported_formats": [
                "OpenGraph", "Twitter Cards", "HTML Meta"
            ],
            "key_properties": list(self.og_properties.keys())
        }