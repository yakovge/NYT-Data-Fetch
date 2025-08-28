"""Fetching layer for NYT Scraper."""

from .cache_manager import CacheManager
from .circuit_breaker import CircuitBreaker
from .http2_client import HTTP2Client
from .per_host_budgets import PerHostBudgets

try:
    from .charset_handler import CharsetHandler
except ImportError:
    CharsetHandler = None

__all__ = ["CacheManager", "CircuitBreaker", "HTTP2Client", "PerHostBudgets", "CharsetHandler"]