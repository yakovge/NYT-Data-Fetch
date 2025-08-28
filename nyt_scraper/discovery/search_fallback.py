"""Search engine fallback for discovering NYT articles."""

import re
from datetime import datetime, timedelta
from typing import Dict, List, Optional
from urllib.parse import quote_plus, urljoin, urlparse

from selectolax.parser import HTMLParser

from ..config import config
from ..fetching.http2_client import HTTP2Client
from ..monitoring import MetricsCollector, get_logger
from ..models import Article, ArticleStatus

logger = get_logger(__name__)


class SearchFallback:
    """Search engine fallback for NYT article discovery (<5% volume)."""
    
    def __init__(self, http_client: HTTP2Client, metrics: MetricsCollector):
        self.http_client = http_client
        self.metrics = metrics
        
        # Search engines in priority order
        self.search_engines = [
            {
                "name": "duckduckgo",
                "url": "https://html.duckduckgo.com/html/",
                "params": {"q": "{query}"},
                "selector": ".result__a",
                "title_selector": ".result__title",
                "captcha_indicators": ["captcha", "robot", "automated"]
            },
            {
                "name": "startpage",
                "url": "https://www.startpage.com/sp/search",
                "params": {"query": "{query}", "cat": "web"},
                "selector": ".w-gl__result-title",
                "title_selector": ".w-gl__result-title",
                "captcha_indicators": ["captcha", "verification", "human"]
            },
            {
                "name": "bing",
                "url": "https://www.bing.com/search",
                "params": {"q": "{query}"},
                "selector": ".b_algo h2 a",
                "title_selector": "h2",
                "captcha_indicators": ["captcha", "verification", "blocked"]
            }
        ]
        
        # Track usage to stay under 5% volume cap
        self.usage_stats = {
            "total_requests": 0,
            "search_requests": 0,
            "captcha_detections": 0,
            "circuit_breaker_active": {}
        }
        
        # Circuit breakers for search engines
        self.circuit_breakers = {}
        for engine in self.search_engines:
            self.circuit_breakers[engine["name"]] = {
                "active": False,
                "until": None,
                "failure_count": 0
            }
    
    async def search_articles(
        self,
        query: str,
        days_back: int = 7,
        max_results: int = 50
    ) -> List[Article]:
        """
        Search for NYT articles using search engines.
        
        Args:
            query: Search query
            days_back: Search articles from last N days
            max_results: Maximum results to return
            
        Returns:
            List of discovered articles
        """
        # Check volume cap (should be <5% of total requests)
        if not self._check_volume_cap():
            logger.warning(
                "search_volume_cap_exceeded",
                search_ratio=self._get_search_ratio()
            )
            return []
        
        # Build search query with site restriction and date
        nyt_query = f'site:nytimes.com {query}'
        if days_back > 0:
            # Add date restriction (format varies by engine)
            date_filter = self._build_date_filter(days_back)
            if date_filter:
                nyt_query += f' {date_filter}'
        
        articles = []
        
        # Try search engines in priority order
        for engine in self.search_engines:
            # Check circuit breaker
            if self._is_circuit_breaker_active(engine["name"]):
                continue
            
            try:
                engine_results = await self._search_engine(
                    engine, 
                    nyt_query, 
                    max_results
                )
                articles.extend(engine_results)
                
                # Reset circuit breaker on success
                self._reset_circuit_breaker(engine["name"])
                
                logger.info(
                    "search_engine_success",
                    engine=engine["name"],
                    results_found=len(engine_results),
                    query=query
                )
                
                # If we got enough results, stop trying other engines
                if len(articles) >= max_results:
                    articles = articles[:max_results]
                    break
                    
            except Exception as e:
                logger.error(
                    "search_engine_error",
                    engine=engine["name"],
                    query=query,
                    error=str(e)
                )
                self._trip_circuit_breaker(engine["name"], str(e))
                continue
        
        self.usage_stats["search_requests"] += 1
        
        logger.info(
            "search_fallback_complete",
            query=query,
            total_articles=len(articles),
            engines_tried=len([e for e in self.search_engines 
                             if not self._is_circuit_breaker_active(e["name"])])
        )
        
        return articles
    
    async def _search_engine(
        self, 
        engine: Dict, 
        query: str, 
        max_results: int
    ) -> List[Article]:
        """Search using a specific search engine."""
        # Build search URL
        params = {k: v.format(query=quote_plus(query)) 
                 for k, v in engine["params"].items()}
        
        # Simple URL building (more robust than complex parameter handling)
        if engine["name"] == "duckduckgo":
            search_url = f'{engine["url"]}?q={quote_plus(query)}'
        elif engine["name"] == "startpage":
            search_url = f'{engine["url"]}?query={quote_plus(query)}&cat=web'
        else:  # bing
            search_url = f'{engine["url"]}?q={quote_plus(query)}'
        
        # Fetch search results
        result = await self.http_client.fetch(search_url)
        
        if not result.is_success:
            raise Exception(f"HTTP {result.status_code}: {result.error}")
        
        # Parse HTML
        parser = HTMLParser(result.content.decode('utf-8', errors='ignore'))
        
        # Check for CAPTCHA
        if self._detect_captcha(parser, engine["captcha_indicators"]):
            self.usage_stats["captcha_detections"] += 1
            raise Exception("CAPTCHA detected - triggering 24h circuit breaker")
        
        # Extract article URLs
        articles = []
        links = parser.css(engine["selector"])
        
        for link in links[:max_results]:
            try:
                article = self._extract_article_from_link(link, engine)
                if article:
                    articles.append(article)
            except Exception as e:
                logger.debug(
                    "link_extraction_error",
                    engine=engine["name"],
                    error=str(e)
                )
                continue
        
        return articles
    
    def _extract_article_from_link(
        self, 
        link_element, 
        engine: Dict
    ) -> Optional[Article]:
        """Extract article from search result link."""
        # Get URL
        href = link_element.attributes.get('href')
        if not href:
            return None
        
        # Handle relative URLs
        if href.startswith('/'):
            if engine["name"] == "duckduckgo":
                # DuckDuckGo uses redirect URLs
                if '/l/?uddg=' in href:
                    # Extract actual URL from redirect
                    import urllib.parse
                    try:
                        actual_url = urllib.parse.unquote(
                            href.split('/l/?uddg=')[1].split('&')[0]
                        )
                        href = actual_url
                    except Exception:
                        return None
            else:
                href = urljoin(engine["url"], href)
        
        # Validate NYT URL
        parsed = urlparse(href)
        if "nytimes.com" not in parsed.netloc:
            return None
        
        # Skip non-article URLs
        if not self._is_article_url(href):
            return None
        
        # Extract title
        title_elem = link_element.css_first(engine["title_selector"])
        if title_elem:
            title = title_elem.text(strip=True)
        else:
            title = link_element.text(strip=True)
        
        if not title:
            return None
        
        return Article(
            source_url=href,
            title=title,
            body='',  # Will be populated during parsing
            section=self._extract_section_from_url(href),
            status=ArticleStatus.DISCOVERED,
            discovered_at=datetime.utcnow()
        )
    
    def _detect_captcha(self, parser: HTMLParser, indicators: List[str]) -> bool:
        """Detect CAPTCHA or bot detection on search results page."""
        page_text = parser.text().lower()
        
        for indicator in indicators:
            if indicator in page_text:
                return True
        
        # Check for specific CAPTCHA elements
        captcha_selectors = [
            '[id*="captcha"]',
            '[class*="captcha"]',
            'iframe[src*="recaptcha"]',
            '[class*="challenge"]'
        ]
        
        for selector in captcha_selectors:
            if parser.css_first(selector):
                return True
        
        return False
    
    def _build_date_filter(self, days_back: int) -> Optional[str]:
        """Build date filter for search query."""
        # Most search engines don't support reliable date filtering
        # This is a best-effort attempt
        return None  # Disabled for now
    
    def _is_article_url(self, url: str) -> bool:
        """Check if URL appears to be an article."""
        parsed = urlparse(url)
        path = parsed.path.lower()
        
        # Skip homepage, section pages, etc.
        skip_patterns = [
            '/section/',
            '/topic/',
            '/column/',
            '/newsletters/',
            '/subscription',
            '/login',
            '/register'
        ]
        
        if any(pattern in path for pattern in skip_patterns):
            return False
        
        # Check for article patterns
        return (
            len(path.split('/')) > 3 or  # Likely article if deep path
            '/article/' in path or
            '/story/' in path or
            re.search(r'/\d{4}/\d{2}/\d{2}/', path)  # Date pattern
        )
    
    def _extract_section_from_url(self, url: str) -> Optional[str]:
        """Extract section from URL."""
        parsed = urlparse(url)
        path_parts = [p for p in parsed.path.split('/') if p]
        
        if not path_parts:
            return None
        
        # Skip date parts and get section
        for part in path_parts:
            if not re.match(r'^\d{4}$', part) and not re.match(r'^\d{2}$', part):
                return part
        
        return None
    
    def _check_volume_cap(self) -> bool:
        """Check if search volume is under 5% cap."""
        if self.usage_stats["total_requests"] == 0:
            return True
        
        ratio = self.usage_stats["search_requests"] / self.usage_stats["total_requests"]
        return ratio < 0.05
    
    def _get_search_ratio(self) -> float:
        """Get current search volume ratio."""
        if self.usage_stats["total_requests"] == 0:
            return 0.0
        
        return self.usage_stats["search_requests"] / self.usage_stats["total_requests"]
    
    def _is_circuit_breaker_active(self, engine_name: str) -> bool:
        """Check if circuit breaker is active for engine."""
        breaker = self.circuit_breakers.get(engine_name, {})
        
        if not breaker.get("active", False):
            return False
        
        until = breaker.get("until")
        if until and datetime.utcnow() > until:
            # Circuit breaker expired, reset it
            self._reset_circuit_breaker(engine_name)
            return False
        
        return True
    
    def _trip_circuit_breaker(self, engine_name: str, reason: str):
        """Trip circuit breaker for search engine."""
        breaker = self.circuit_breakers[engine_name]
        breaker["failure_count"] += 1
        breaker["active"] = True
        
        # CAPTCHA detection triggers 24-hour breaker
        if "captcha" in reason.lower():
            breaker["until"] = datetime.utcnow() + timedelta(hours=24)
        else:
            # Regular failures: exponential backoff up to 1 hour
            minutes = min(60, 5 * (2 ** breaker["failure_count"]))
            breaker["until"] = datetime.utcnow() + timedelta(minutes=minutes)
        
        logger.warning(
            "search_circuit_breaker_tripped",
            engine=engine_name,
            reason=reason,
            until=breaker["until"].isoformat()
        )
    
    def _reset_circuit_breaker(self, engine_name: str):
        """Reset circuit breaker for search engine."""
        self.circuit_breakers[engine_name] = {
            "active": False,
            "until": None,
            "failure_count": 0
        }
    
    def increment_total_requests(self):
        """Increment total request counter for volume tracking."""
        self.usage_stats["total_requests"] += 1
    
    def get_search_stats(self) -> Dict:
        """Get search fallback statistics."""
        active_breakers = {
            name: breaker for name, breaker in self.circuit_breakers.items()
            if breaker.get("active", False)
        }
        
        return {
            "usage_ratio": self._get_search_ratio(),
            "total_searches": self.usage_stats["search_requests"],
            "captcha_detections": self.usage_stats["captcha_detections"],
            "active_circuit_breakers": len(active_breakers),
            "available_engines": len([
                e for e in self.search_engines 
                if not self._is_circuit_breaker_active(e["name"])
            ]),
            "engines": [e["name"] for e in self.search_engines]
        }