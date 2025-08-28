"""RSS feed parser for NYT articles."""

import xml.etree.ElementTree as ET
from datetime import datetime
from typing import Dict, List, Optional
from urllib.parse import urljoin, urlparse

from dateutil.parser import parse as parse_date

from ..config import config
from ..fetching.http2_client import HTTP2Client
from ..monitoring import MetricsCollector, get_logger
from ..models import Article, ArticleStatus

logger = get_logger(__name__)


class RSSFetcher:
    """Fetch and parse NYT RSS feeds."""
    
    def __init__(self, http_client: HTTP2Client, metrics: MetricsCollector):
        self.http_client = http_client
        self.metrics = metrics
        
        # NYT RSS feeds by section
        self.rss_feeds = {
            "homepage": f"{config.rss_base_url}HomePage.xml",
            "world": f"{config.rss_base_url}World.xml",
            "us": f"{config.rss_base_url}US.xml",
            "business": f"{config.rss_base_url}Business.xml",
            "technology": f"{config.rss_base_url}Technology.xml",
            "science": f"{config.rss_base_url}Science.xml",
            "health": f"{config.rss_base_url}Health.xml",
            "sports": f"{config.rss_base_url}Sports.xml",
            "opinion": f"{config.rss_base_url}Opinion.xml",
            "arts": f"{config.rss_base_url}Arts.xml",
            "style": f"{config.rss_base_url}FashionandStyle.xml",
            "food": f"{config.rss_base_url}DiningandWine.xml",
            "travel": f"{config.rss_base_url}Travel.xml",
            "magazine": f"{config.rss_base_url}Magazine.xml",
            "realestate": f"{config.rss_base_url}RealEstate.xml",
        }
    
    async def fetch_all_feeds(self) -> Dict[str, List[Article]]:
        """Fetch articles from all RSS feeds."""
        results = {}
        
        for section, feed_url in self.rss_feeds.items():
            try:
                articles = await self.fetch_feed(feed_url, section)
                results[section] = articles
                
                logger.info(
                    "rss_feed_fetched",
                    section=section,
                    article_count=len(articles),
                    feed_url=feed_url
                )
                
            except Exception as e:
                logger.error(
                    "rss_feed_error",
                    section=section,
                    feed_url=feed_url,
                    error=str(e)
                )
                results[section] = []
        
        return results
    
    async def fetch_feed(self, feed_url: str, section: str) -> List[Article]:
        """Fetch and parse a single RSS feed."""
        # Fetch RSS content
        result = await self.http_client.fetch(feed_url)
        
        if not result.is_success:
            logger.error(
                "rss_fetch_failed",
                feed_url=feed_url,
                status_code=result.status_code,
                error=result.error
            )
            return []
        
        # Parse XML
        try:
            root = ET.fromstring(result.content.decode('utf-8'))
            articles = self._parse_rss_xml(root, section)
            
            logger.info(
                "rss_parsed",
                feed_url=feed_url,
                articles_found=len(articles)
            )
            
            return articles
            
        except ET.ParseError as e:
            logger.error(
                "rss_parse_error",
                feed_url=feed_url,
                error=str(e)
            )
            return []
    
    def _parse_rss_xml(self, root: ET.Element, section: str) -> List[Article]:
        """Parse RSS XML and extract articles."""
        articles = []
        
        # Handle both RSS 2.0 and Atom formats
        if root.tag == "rss":
            items = root.findall(".//item")
        elif root.tag.endswith("feed"):  # Atom format
            items = root.findall(".//{http://www.w3.org/2005/Atom}entry")
        else:
            logger.warning("rss_unknown_format", root_tag=root.tag)
            return articles
        
        for item in items:
            try:
                article = self._parse_rss_item(item, section)
                if article:
                    articles.append(article)
            except Exception as e:
                logger.warning(
                    "rss_item_parse_error",
                    error=str(e),
                    section=section
                )
                continue
        
        return articles
    
    def _parse_rss_item(self, item: ET.Element, section: str) -> Optional[Article]:
        """Parse individual RSS item."""
        # Extract basic fields
        title = self._get_text(item, ["title"])
        link = self._get_text(item, ["link", "guid"])
        description = self._get_text(item, ["description", "summary"])
        
        if not title or not link:
            return None
        
        # Clean and validate URL
        if not link.startswith("http"):
            link = urljoin(config.nyt_base_url, link)
        
        # Skip non-NYT URLs
        parsed = urlparse(link)
        if "nytimes.com" not in parsed.netloc:
            return None
        
        # Extract dates
        pub_date = self._parse_pub_date(item)
        
        # Extract author
        author = self._get_text(item, [
            "author", 
            "{http://purl.org/dc/elements/1.1/}creator",
            "dc:creator"
        ])
        
        # Extract categories/tags
        tags = self._extract_tags(item)
        
        return Article(
            source_url=link,
            title=title.strip(),
            body=description.strip() if description else "",
            section=section,
            author=author,
            published_date=pub_date,
            tags=tags,
            status=ArticleStatus.DISCOVERED,
            discovered_at=datetime.utcnow()
        )
    
    def _get_text(self, item: ET.Element, tag_names: List[str]) -> Optional[str]:
        """Get text from first matching XML tag."""
        for tag_name in tag_names:
            element = item.find(tag_name)
            if element is not None and element.text:
                return element.text.strip()
        return None
    
    def _parse_pub_date(self, item: ET.Element) -> Optional[datetime]:
        """Parse publication date from various formats."""
        date_fields = [
            "pubDate",
            "{http://www.w3.org/2005/Atom}published",
            "{http://www.w3.org/2005/Atom}updated",
            "{http://purl.org/dc/elements/1.1/}date",
            "dc:date"
        ]
        
        for field in date_fields:
            element = item.find(field)
            if element is not None and element.text:
                try:
                    # Parse and convert to UTC
                    dt = parse_date(element.text)
                    if dt.tzinfo is None:
                        # Assume UTC if no timezone
                        return dt.replace(tzinfo=None)
                    else:
                        # Convert to UTC and remove timezone info
                        return dt.utctimetuple()
                except Exception as e:
                    logger.debug(
                        "date_parse_error",
                        date_string=element.text,
                        error=str(e)
                    )
                    continue
        
        return None
    
    def _extract_tags(self, item: ET.Element) -> List[str]:
        """Extract tags/categories from RSS item."""
        tags = []
        
        # Look for category elements
        categories = item.findall("category")
        for cat in categories:
            if cat.text:
                tags.append(cat.text.strip())
        
        # Look for subject/keywords
        keywords = self._get_text(item, [
            "{http://purl.org/dc/elements/1.1/}subject",
            "dc:subject"
        ])
        
        if keywords:
            # Split on common separators
            for sep in [",", ";", "|"]:
                if sep in keywords:
                    tags.extend([tag.strip() for tag in keywords.split(sep)])
                    break
            else:
                tags.append(keywords.strip())
        
        # Remove duplicates and empty tags
        return list(set([tag for tag in tags if tag]))
    
    async def discover_fresh_articles(
        self, 
        sections: Optional[List[str]] = None,
        since: Optional[datetime] = None
    ) -> List[Article]:
        """
        Discover fresh articles from specified sections.
        
        Args:
            sections: List of sections to check, or None for all
            since: Only return articles newer than this date
            
        Returns:
            List of discovered articles
        """
        if sections is None:
            sections = list(self.rss_feeds.keys())
        
        all_articles = []
        
        for section in sections:
            if section not in self.rss_feeds:
                logger.warning("unknown_rss_section", section=section)
                continue
            
            try:
                articles = await self.fetch_feed(
                    self.rss_feeds[section],
                    section
                )
                
                # Filter by date if specified
                if since:
                    articles = [
                        article for article in articles
                        if article.published_date and article.published_date > since
                    ]
                
                all_articles.extend(articles)
                
            except Exception as e:
                logger.error(
                    "section_discovery_error",
                    section=section,
                    error=str(e)
                )
        
        logger.info(
            "discovery_complete",
            total_articles=len(all_articles),
            sections=len(sections),
            since=since.isoformat() if since else None
        )
        
        return all_articles
    
    def get_feed_stats(self) -> Dict:
        """Get RSS feed statistics."""
        return {
            "total_feeds": len(self.rss_feeds),
            "sections": list(self.rss_feeds.keys()),
            "base_url": config.rss_base_url
        }