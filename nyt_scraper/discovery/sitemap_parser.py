"""News sitemap parser with lastmod support."""

import gzip
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta
from typing import Dict, List, Optional
from urllib.parse import urljoin, urlparse

from dateutil.parser import parse as parse_date

from ..config import config
from ..fetching.http2_client import HTTP2Client
from ..monitoring import MetricsCollector, get_logger
from ..models import Article, ArticleStatus

logger = get_logger(__name__)


class SitemapParser:
    """Parse NYT news sitemaps with lastmod support."""
    
    def __init__(self, http_client: HTTP2Client, metrics: MetricsCollector):
        self.http_client = http_client
        self.metrics = metrics
        
        # NYT sitemap URLs
        self.sitemap_urls = [
            "https://www.nytimes.com/sitemaps/new/news.xml.gz",
            "https://www.nytimes.com/sitemaps/www.nytimes.com/sitemap.xml",
            "https://www.nytimes.com/sitemaps/www.nytimes.com/news-sitemap.xml",
        ]
        
        # Namespace mappings for XML parsing
        self.namespaces = {
            'news': 'http://www.google.com/schemas/sitemap-news/0.9',
            'sitemap': 'http://www.sitemaps.org/schemas/sitemap/0.9'
        }
    
    async def discover_articles(
        self,
        since: Optional[datetime] = None,
        limit: Optional[int] = None
    ) -> List[Article]:
        """
        Discover articles from news sitemaps.
        
        Args:
            since: Only return articles newer than this date
            limit: Maximum number of articles to return
            
        Returns:
            List of discovered articles
        """
        all_articles = []
        
        for sitemap_url in self.sitemap_urls:
            try:
                articles = await self._parse_sitemap(sitemap_url, since)
                all_articles.extend(articles)
                
                logger.info(
                    "sitemap_parsed",
                    sitemap_url=sitemap_url,
                    articles_found=len(articles)
                )
                
                # Apply limit if specified
                if limit and len(all_articles) >= limit:
                    all_articles = all_articles[:limit]
                    break
                    
            except Exception as e:
                logger.error(
                    "sitemap_parse_error",
                    sitemap_url=sitemap_url,
                    error=str(e)
                )
                continue
        
        # Sort by publication date (newest first)
        all_articles.sort(
            key=lambda x: x.published_date or datetime.min,
            reverse=True
        )
        
        logger.info(
            "sitemap_discovery_complete",
            total_articles=len(all_articles),
            sitemaps_checked=len(self.sitemap_urls)
        )
        
        return all_articles
    
    async def _parse_sitemap(
        self, 
        sitemap_url: str, 
        since: Optional[datetime]
    ) -> List[Article]:
        """Parse a single sitemap."""
        # Fetch sitemap content
        result = await self.http_client.fetch(sitemap_url)
        
        if not result.is_success:
            logger.error(
                "sitemap_fetch_failed",
                sitemap_url=sitemap_url,
                status_code=result.status_code,
                error=result.error
            )
            return []
        
        # Handle compressed sitemaps
        content = result.content
        if sitemap_url.endswith('.gz'):
            try:
                content = gzip.decompress(content)
            except Exception as e:
                logger.error(
                    "sitemap_decompress_error",
                    sitemap_url=sitemap_url,
                    error=str(e)
                )
                return []
        
        # Parse XML
        try:
            root = ET.fromstring(content.decode('utf-8'))
            
            # Check if this is a sitemap index
            if self._is_sitemap_index(root):
                return await self._parse_sitemap_index(root, since)
            else:
                return self._parse_sitemap_entries(root, since)
                
        except ET.ParseError as e:
            logger.error(
                "sitemap_xml_parse_error",
                sitemap_url=sitemap_url,
                error=str(e)
            )
            return []
    
    def _is_sitemap_index(self, root: ET.Element) -> bool:
        """Check if XML is a sitemap index."""
        return (
            root.tag.endswith("sitemapindex") or 
            len(root.findall("sitemap", self.namespaces)) > 0
        )
    
    async def _parse_sitemap_index(
        self, 
        root: ET.Element, 
        since: Optional[datetime]
    ) -> List[Article]:
        """Parse sitemap index and fetch child sitemaps."""
        articles = []
        
        # Find all sitemap entries
        sitemaps = root.findall("sitemap", self.namespaces)
        if not sitemaps:
            sitemaps = root.findall(".//{http://www.sitemaps.org/schemas/sitemap/0.9}sitemap")
        
        for sitemap in sitemaps[:10]:  # Limit to 10 child sitemaps
            loc_elem = sitemap.find("loc", self.namespaces)
            if loc_elem is None:
                loc_elem = sitemap.find(".//{http://www.sitemaps.org/schemas/sitemap/0.9}loc")
            
            if loc_elem is not None and loc_elem.text:
                try:
                    child_articles = await self._parse_sitemap(loc_elem.text, since)
                    articles.extend(child_articles)
                except Exception as e:
                    logger.warning(
                        "child_sitemap_error",
                        sitemap_url=loc_elem.text,
                        error=str(e)
                    )
        
        return articles
    
    def _parse_sitemap_entries(
        self, 
        root: ET.Element, 
        since: Optional[datetime]
    ) -> List[Article]:
        """Parse sitemap URL entries."""
        articles = []
        
        # Find all URL entries
        urls = root.findall("url", self.namespaces)
        if not urls:
            urls = root.findall(".//{http://www.sitemaps.org/schemas/sitemap/0.9}url")
        
        for url in urls:
            try:
                article = self._parse_sitemap_entry(url, since)
                if article:
                    articles.append(article)
            except Exception as e:
                logger.debug(
                    "sitemap_entry_parse_error",
                    error=str(e)
                )
                continue
        
        return articles
    
    def _parse_sitemap_entry(
        self, 
        url_elem: ET.Element, 
        since: Optional[datetime]
    ) -> Optional[Article]:
        """Parse individual sitemap entry."""
        # Get URL
        loc_elem = url_elem.find("loc", self.namespaces)
        if loc_elem is None:
            loc_elem = url_elem.find(".//{http://www.sitemaps.org/schemas/sitemap/0.9}loc")
        
        if loc_elem is None or not loc_elem.text:
            return None
        
        url = loc_elem.text.strip()
        
        # Validate URL
        parsed = urlparse(url)
        if "nytimes.com" not in parsed.netloc:
            return None
        
        # Skip non-article URLs
        if not self._is_article_url(url):
            return None
        
        # Get lastmod date
        lastmod = self._get_lastmod(url_elem)
        
        # Filter by date if specified
        if since and lastmod and lastmod <= since:
            return None
        
        # Extract news-specific data
        news_data = self._extract_news_data(url_elem)
        
        return Article(
            source_url=url,
            title=news_data.get('title', ''),
            body='',  # Will be populated during parsing
            section=self._extract_section(url),
            published_date=news_data.get('publication_date') or lastmod,
            tags=news_data.get('keywords', []),
            status=ArticleStatus.DISCOVERED,
            discovered_at=datetime.utcnow()
        )
    
    def _get_lastmod(self, url_elem: ET.Element) -> Optional[datetime]:
        """Extract lastmod date from URL element."""
        lastmod_elem = url_elem.find("lastmod", self.namespaces)
        if lastmod_elem is None:
            lastmod_elem = url_elem.find(".//{http://www.sitemaps.org/schemas/sitemap/0.9}lastmod")
        
        if lastmod_elem is not None and lastmod_elem.text:
            try:
                return parse_date(lastmod_elem.text)
            except Exception as e:
                logger.debug(
                    "lastmod_parse_error",
                    lastmod=lastmod_elem.text,
                    error=str(e)
                )
        
        return None
    
    def _extract_news_data(self, url_elem: ET.Element) -> Dict:
        """Extract news-specific metadata from sitemap entry."""
        news_data = {
            'title': '',
            'publication_date': None,
            'keywords': []
        }
        
        # Look for news:news element
        news_elem = url_elem.find("news:news", self.namespaces)
        if news_elem is None:
            return news_data
        
        # Extract title
        title_elem = news_elem.find("news:title", self.namespaces)
        if title_elem is not None and title_elem.text:
            news_data['title'] = title_elem.text.strip()
        
        # Extract publication date
        pub_elem = news_elem.find("news:publication_date", self.namespaces)
        if pub_elem is not None and pub_elem.text:
            try:
                news_data['publication_date'] = parse_date(pub_elem.text)
            except Exception:
                pass
        
        # Extract keywords
        keywords_elem = news_elem.find("news:keywords", self.namespaces)
        if keywords_elem is not None and keywords_elem.text:
            keywords = [k.strip() for k in keywords_elem.text.split(',')]
            news_data['keywords'] = [k for k in keywords if k]
        
        return news_data
    
    def _is_article_url(self, url: str) -> bool:
        """Check if URL appears to be an article."""
        parsed = urlparse(url)
        path = parsed.path.lower()
        
        # Common article patterns
        article_patterns = [
            '/article/',
            '/story/',
            '/news/',
            '/opinion/',
            '/business/',
            '/technology/',
            '/science/',
            '/health/',
            '/sports/',
            '/arts/',
            '/style/',
            '/travel/',
            '/realestate/',
            '/food/',
            '/magazine/'
        ]
        
        # Check for date patterns (YYYY/MM/DD)
        import re
        date_pattern = r'/\d{4}/\d{2}/\d{2}/'
        if re.search(date_pattern, path):
            return True
        
        # Check for article patterns
        return any(pattern in path for pattern in article_patterns)
    
    def _extract_section(self, url: str) -> Optional[str]:
        """Extract section from URL path."""
        parsed = urlparse(url)
        path_parts = [p for p in parsed.path.split('/') if p]
        
        if not path_parts:
            return None
        
        # Skip date parts
        import re
        section_parts = []
        for part in path_parts:
            if not re.match(r'^\d{4}$', part) and not re.match(r'^\d{2}$', part):
                section_parts.append(part)
        
        return section_parts[0] if section_parts else None
    
    async def get_recent_articles(self, hours: int = 24) -> List[Article]:
        """Get articles from the last N hours."""
        since = datetime.utcnow() - timedelta(hours=hours)
        return await self.discover_articles(since=since)
    
    def get_sitemap_stats(self) -> Dict:
        """Get sitemap statistics."""
        return {
            "total_sitemaps": len(self.sitemap_urls),
            "sitemap_urls": self.sitemap_urls,
            "namespaces": self.namespaces
        }