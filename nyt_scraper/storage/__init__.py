"""Storage layer for NYT Scraper with deduplication and eviction."""

from .dedup_manager import DedupManager
from .sqlite_manager import SQLiteManager

try:
    from .storage_eviction import StorageEviction
except ImportError:
    StorageEviction = None

try:
    from .whoosh_indexer import WhooshIndexer
except ImportError:
    WhooshIndexer = None

try:
    from .fts5_indexer import FTS5Indexer
except ImportError:
    FTS5Indexer = None

__all__ = ["SQLiteManager", "DedupManager", "StorageEviction", "WhooshIndexer", "FTS5Indexer"]