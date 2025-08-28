"""JSON-LD structured data extractor (~10ms, highest priority)."""

import json
import time
from datetime import datetime
from typing import Dict, List, Optional

from dateutil.parser import parse as parse_date
from selectolax.parser import HTMLParser

from ..monitoring.structured_logger import get_logger
from ..models import Article, ParseMethod, ParseResult

logger = get_logger(__name__)


class JSONLDExtractor:
    """Extract article data from JSON-LD structured data."""
    
    def __init__(self):
        # Schema.org types we recognize as articles
        self.article_types = {
            "Article",
            "NewsArticle", 
            "BlogPosting",
            "Report",
            "ScholarlyArticle"
        }
        
        # JSON-LD contexts we support
        self.supported_contexts = {
            "https://schema.org",
            "http://schema.org",
            "schema.org"
        }
    
    def extract(self, html: str, url: str) -> ParseResult:
        """
        Extract article from JSON-LD structured data.
        
        Args:
            html: HTML content
            url: Source URL
            
        Returns:
            ParseResult with extracted article or error
        """
        start_time = time.time()
        
        try:
            parser = HTMLParser(html)
            json_ld_scripts = parser.css('script[type="application/ld+json"]')
            
            if not json_ld_scripts:
                return ParseResult(
                    success=False,
                    error="No JSON-LD scripts found",
                    duration_ms=(time.time() - start_time) * 1000,
                    confidence=0.0
                )
            
            # Try each JSON-LD script
            for script in json_ld_scripts:
                script_text = script.text()
                if not script_text:
                    continue
                
                try:
                    data = json.loads(script_text)
                    article = self._extract_from_json_ld(data, url)
                    
                    if article and article.title and article.body:
                        duration = (time.time() - start_time) * 1000
                        
                        logger.debug(
                            "json_ld_extraction_success",
                            url=url,
                            title_length=len(article.title),
                            body_length=len(article.body),
                            duration_ms=duration
                        )
                        
                        return ParseResult(
                            success=True,
                            article=article,
                            parse_method=ParseMethod.JSON_LD,
                            duration_ms=duration,
                            confidence=0.9  # High confidence for structured data
                        )
                        
                except json.JSONDecodeError as e:
                    logger.debug(
                        "json_ld_parse_error",
                        url=url,
                        error=str(e)
                    )
                    continue
                except Exception as e:
                    logger.debug(
                        "json_ld_extraction_error", 
                        url=url,
                        error=str(e)
                    )
                    continue
            
            return ParseResult(
                success=False,
                error="No valid JSON-LD article data found",
                duration_ms=(time.time() - start_time) * 1000,
                confidence=0.0
            )
            
        except Exception as e:
            return ParseResult(
                success=False,
                error=f"JSON-LD extraction failed: {e}",
                duration_ms=(time.time() - start_time) * 1000,
                confidence=0.0
            )
    
    def _extract_from_json_ld(self, data: Dict, url: str) -> Optional[Article]:
        """Extract article from JSON-LD data structure."""
        # Handle @graph arrays
        if isinstance(data, dict) and "@graph" in data:
            items = data["@graph"]
        elif isinstance(data, list):
            items = data
        else:
            items = [data]
        
        # Find article-like objects
        for item in items:
            if not isinstance(item, dict):
                continue
            
            # Check if this is an article type
            item_type = item.get("@type", "")
            if isinstance(item_type, list):
                item_types = set(item_type)
            else:
                item_types = {item_type}
            
            if not item_types.intersection(self.article_types):
                continue
            
            # Validate context if present
            context = item.get("@context", "")
            if context and not any(ctx in str(context) for ctx in self.supported_contexts):
                continue
            
            # Extract article fields
            article = self._parse_article_fields(item, url)
            if article:
                return article
        
        return None
    
    def _parse_article_fields(self, item: Dict, url: str) -> Optional[Article]:
        """Parse individual JSON-LD article item."""
        # Extract title
        title = self._get_text_field(item, ["headline", "name", "title"])
        if not title:
            return None
        
        # Extract body content
        body = self._get_text_field(item, [
            "articleBody", 
            "text", 
            "description",
            "abstract"
        ])
        
        if not body:
            return None
        
        # Extract author
        author = self._extract_author(item)
        
        # Extract dates
        published_date = self._extract_date(item, [
            "datePublished", 
            "dateCreated",
            "publishedDate"
        ])
        
        updated_date = self._extract_date(item, [
            "dateModified",
            "dateUpdated",
            "modifiedDate"
        ])
        
        # Extract section/category
        section = self._extract_section(item)
        
        # Extract tags/keywords
        tags = self._extract_tags(item)
        
        return Article(
            source_url=url,
            title=title.strip(),
            body=body.strip(),
            author=author,
            published_date=published_date,
            updated_date=updated_date,
            section=section,
            tags=tags,
            parse_method=ParseMethod.JSON_LD,
            discovered_at=datetime.utcnow()
        )
    
    def _get_text_field(self, item: Dict, field_names: List[str]) -> Optional[str]:
        """Get text from first available field."""
        for field in field_names:
            value = item.get(field)
            if value:
                if isinstance(value, dict):
                    # Handle @value or text properties
                    text = value.get("@value") or value.get("text") or value.get("value")
                    if text:
                        return str(text)
                elif isinstance(value, list):
                    # Take first non-empty item
                    for v in value:
                        if isinstance(v, dict):
                            text = v.get("@value") or v.get("text") or str(v)
                            if text:
                                return text
                        elif v:
                            return str(v)
                else:
                    return str(value)
        
        return None
    
    def _extract_author(self, item: Dict) -> Optional[str]:
        """Extract author information."""
        author = item.get("author")
        if not author:
            return None
        
        if isinstance(author, dict):
            return author.get("name") or author.get("@name") or author.get("text")
        elif isinstance(author, list):
            authors = []
            for auth in author:
                if isinstance(auth, dict):
                    name = auth.get("name") or auth.get("@name") or auth.get("text")
                    if name:
                        authors.append(name)
                else:
                    authors.append(str(auth))
            return ", ".join(authors) if authors else None
        else:
            return str(author)
    
    def _extract_date(self, item: Dict, field_names: List[str]) -> Optional[datetime]:
        """Extract and parse date field."""
        for field in field_names:
            value = item.get(field)
            if value:
                try:
                    if isinstance(value, dict):
                        date_str = value.get("@value") or value.get("value")
                    else:
                        date_str = str(value)
                    
                    if date_str:
                        return parse_date(date_str)
                        
                except Exception as e:
                    logger.debug(
                        "date_parse_error",
                        field=field,
                        value=value,
                        error=str(e)
                    )
                    continue
        
        return None
    
    def _extract_section(self, item: Dict) -> Optional[str]:
        """Extract article section/category."""
        # Try various section fields
        section_fields = [
            "articleSection",
            "section", 
            "category",
            "genre"
        ]
        
        for field in section_fields:
            value = item.get(field)
            if value:
                if isinstance(value, list):
                    return str(value[0]) if value else None
                else:
                    return str(value)
        
        # Try publisher/provider
        publisher = item.get("publisher", {})
        if isinstance(publisher, dict):
            pub_name = publisher.get("name", "")
            if "section" in pub_name.lower():
                return pub_name
        
        return None
    
    def _extract_tags(self, item: Dict) -> List[str]:
        """Extract tags/keywords."""
        tags = []
        
        # Try keywords field
        keywords = item.get("keywords")
        if keywords:
            if isinstance(keywords, list):
                for keyword in keywords:
                    if isinstance(keyword, dict):
                        tag = keyword.get("name") or keyword.get("text") or str(keyword)
                        tags.append(tag)
                    else:
                        tags.append(str(keyword))
            elif isinstance(keywords, str):
                # Split comma-separated keywords
                tags.extend([k.strip() for k in keywords.split(",") if k.strip()])
        
        # Try about/mentions
        about = item.get("about", item.get("mentions", []))
        if about:
            if not isinstance(about, list):
                about = [about]
            
            for entity in about:
                if isinstance(entity, dict):
                    name = entity.get("name") or entity.get("text")
                    if name:
                        tags.append(str(name))
        
        return list(set(tags))  # Remove duplicates
    
    def can_extract(self, html: str) -> bool:
        """Check if HTML contains JSON-LD structured data."""
        try:
            parser = HTMLParser(html)
            json_ld_scripts = parser.css('script[type="application/ld+json"]')
            return len(json_ld_scripts) > 0
        except Exception:
            return False
    
    def get_extraction_stats(self) -> Dict:
        """Get extraction statistics."""
        return {
            "method": "json_ld",
            "priority": 1,
            "avg_duration_ms": 10,
            "confidence": 0.9,
            "supported_types": list(self.article_types),
            "supported_contexts": list(self.supported_contexts)
        }