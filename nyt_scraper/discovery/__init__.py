"""Discovery layer for finding NYT articles."""

from .local_search import LocalSearch
from .rss_fetcher import RSSFetcher
from .search_fallback import SearchFallback
from .sitemap_parser import SitemapParser

__all__ = ["RSSFetcher", "SitemapParser", "SearchFallback", "LocalSearch"]