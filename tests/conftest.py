"""Test configuration and fixtures for NYT Scraper test suite."""

import asyncio
import gc
import pytest
import tempfile
import time
from pathlib import Path
from unittest.mock import Mock, AsyncMock
import sqlite3
from datetime import datetime, timedelta

# VCR.py for HTTP mocking
import vcr

# Import components for testing
from nyt_scraper.config import config
from nyt_scraper.models import Article, ArticleStatus
from nyt_scraper.storage import SQLiteManager
from nyt_scraper.monitoring.structured_logger import get_logger



@pytest.fixture
def temp_db():
    """Create a temporary SQLite database for testing."""
    temp_file = tempfile.NamedTemporaryFile(delete=False, suffix='.db')
    temp_file.close()
    
    db_path = Path(temp_file.name)
    yield db_path
    
    # Cleanup - handle Windows file locking
    gc.collect()  # Force garbage collection
    time.sleep(0.1)  # Small delay for Windows
    
    try:
        if db_path.exists():
            db_path.unlink()
    except PermissionError:
        # On Windows, sometimes the file is still locked
        pass  # Will be cleaned up by OS later


@pytest.fixture
def sqlite_manager(temp_db):
    """Create a SQLiteManager instance with temporary database."""
    # Override config for testing
    original_db_path = config.sqlite_db_path
    config.sqlite_db_path = temp_db
    
    manager = SQLiteManager()
    
    yield manager
    
    # Cleanup
    manager.close()
    config.sqlite_db_path = original_db_path
    
    # Force cleanup on Windows
    gc.collect()
    time.sleep(0.1)


@pytest.fixture
def mock_article():
    """Create a mock article for testing."""
    return Article(
        source_url="https://www.nytimes.com/2024/01/15/world/test-article.html",
        canonical_url="https://www.nytimes.com/2024/01/15/world/test-article.html",
        title="Test Article: Breaking News Story",
        author="Test Author",
        body="This is a test article about breaking news story with substantial content for testing purposes. The article provides detailed coverage of important world events and their implications. Local authorities have confirmed multiple reports of significant developments in the region. Economic analysts suggest these events will have far-reaching consequences for international markets. Political leaders from various nations have issued statements regarding the situation. Emergency services are coordinating response efforts to address immediate concerns. Media outlets worldwide are monitoring the ongoing developments closely. Experts predict this story will continue to evolve over the coming days and weeks.",
        section="World",
        tags=["test", "breaking-news", "world"],
        published_date=datetime(2024, 1, 15, 10, 0, 0),
        status=ArticleStatus.STORED,
        parser_path="json_ld",
        parse_confidence=0.95,
        word_count=250,
        char_count=1500,
        did_use_micro_ai=False,
        did_escalate_heavy=False,
        challenge_detected=False,
        paywall_detected=False
    )


@pytest.fixture
def dedup_manager(sqlite_manager):
    """Create a DedupManager instance for testing."""
    from nyt_scraper.storage.dedup_manager import DedupManager
    manager = DedupManager()
    manager.sqlite_manager = sqlite_manager
    return manager


@pytest.fixture
def storage_eviction(sqlite_manager):
    """Create a StorageEviction instance for testing."""
    from nyt_scraper.storage.storage_eviction import StorageEviction
    eviction = StorageEviction(sqlite_manager)
    return eviction


@pytest.fixture
def fts5_indexer(sqlite_manager):
    """Create an FTS5Indexer instance for testing."""
    pytest.importorskip("sqlite3")  # Skip if sqlite3 not available
    from nyt_scraper.storage.fts5_indexer import FTS5Indexer
    try:
        indexer = FTS5Indexer(sqlite_manager)
        return indexer
    except RuntimeError as e:
        if "FTS5 not available" in str(e):
            pytest.skip("FTS5 not available in this SQLite build")
        raise


@pytest.fixture
def sample_html():
    """Sample HTML content for parser testing."""
    return '''
    <!DOCTYPE html>
    <html>
    <head>
        <meta charset="utf-8">
        <title>Test Article: Breaking News Story</title>
        <meta property="og:title" content="Test Article: Breaking News Story">
        <meta property="og:description" content="This is the main content of the article with multiple paragraphs. Second paragraph continues the story with more details. Third paragraph provides additional context and information.">
        <meta property="article:author" content="Test Author">
        <meta property="article:section" content="World">
        <meta name="description" content="This is the main content of the article with multiple paragraphs. Second paragraph continues the story with more details.">
        <script type="application/ld+json">
        {
            "@context": "http://schema.org",
            "@type": "NewsArticle",
            "headline": "Test Article: Breaking News Story",
            "author": {"@type": "Person", "name": "Test Author"},
            "articleSection": "World",
            "articleBody": "This is the main content of the article with multiple paragraphs. Second paragraph continues the story with more details. Third paragraph provides additional context and information. Fourth paragraph expands on the implications of these events. Fifth paragraph discusses the broader context and potential outcomes. This comprehensive coverage ensures readers get the full picture.",
            "datePublished": "2024-01-15T10:00:00Z"
        }
        </script>
    </head>
    <body>
        <article>
            <h1>Test Article: Breaking News Story</h1>
            <div class="byline">By Test Author</div>
            <time datetime="2024-01-15T10:00:00Z">January 15, 2024</time>
            <div class="article-body">
                <p>This is the main content of the article with multiple paragraphs.</p>
                <p>Second paragraph continues the story with more details.</p>
                <p>Third paragraph provides additional context and information.</p>
            </div>
        </article>
    </body>
    </html>
    '''


@pytest.fixture
def sample_html_with_challenges():
    """Sample HTML with challenge detection markers."""
    return '''
    <!DOCTYPE html>
    <html>
    <head>
        <title>Checking your browser</title>
    </head>
    <body>
        <div id="cf-challenge-stage">
            <h1>Please wait while we verify you are a human</h1>
            <p>This process is automatic. Your browser will redirect to your requested content shortly.</p>
        </div>
        <script src="https://challenges.cloudflare.com/turnstile/v0/api.js"></script>
    </body>
    </html>
    '''


@pytest.fixture
def sample_html_with_paywall():
    """Sample HTML with paywall detection markers."""
    return '''
    <!DOCTYPE html>
    <html>
    <body>
        <article>
            <h1>Premium Article</h1>
            <div class="article-body">
                <p>This is the beginning of a premium article...</p>
                <div data-testid="paywall-prompt" class="paywall-container">
                    <h2>Subscribe to continue reading</h2>
                    <p>This article is for subscribers only.</p>
                </div>
            </div>
        </article>
    </body>
    </html>
    '''


@pytest.fixture
def mock_http_response():
    """Mock HTTP response for testing."""
    class MockResponse:
        def __init__(self, content="", status_code=200, headers=None, url=""):
            self.content = content.encode('utf-8') if isinstance(content, str) else content
            self.text = content if isinstance(content, str) else content.decode('utf-8')
            self.status_code = status_code
            self.headers = headers or {'content-type': 'text/html; charset=utf-8'}
            self.url = url
    
    return MockResponse


@pytest.fixture
def mock_anthropic_client():
    """Mock Anthropic client for AI testing."""
    mock_client = Mock()
    
    # Mock successful response
    mock_message = Mock()
    mock_message.content = [Mock(text='{"title": "Test Article: Breaking News Story", "author": "Test Author", "section": "World", "content": "This is the main content of the article with multiple paragraphs. Second paragraph continues the story with more details. Third paragraph provides additional context and information."}')]
    
    mock_client.messages.create.return_value = mock_message
    
    return mock_client


@pytest.fixture
def vcr_cassette():
    """VCR cassette for recording/replaying HTTP interactions."""
    my_vcr = vcr.VCR(
        cassette_library_dir='tests/cassettes',
        record_mode='once',
        match_on=['uri', 'method'],
        filter_headers=['authorization', 'user-agent'],
        decode_compressed_response=True
    )
    return my_vcr


@pytest.fixture
def parsing_test_cases():
    """Test cases for parsing validation."""
    return {
        'minimal_valid': {
            'title': 'Test Article: Breaking News Story',
            'content': 'A' * 200,  # Minimum content length
            'expected_confidence': 0.6
        },
        'full_article': {
            'title': 'Comprehensive Test Article',
            'author': 'Test Author',
            'content': 'A' * 2000,  # Substantial content
            'section': 'Technology',
            'summary': 'A test article summary',
            'expected_confidence': 0.9
        },
        'insufficient_content': {
            'title': 'Short Article',
            'content': 'Too short',  # Below minimum
            'expected_confidence': 0.2
        }
    }


@pytest.fixture
def ai_response_test_cases():
    """Test cases for AI response validation."""
    return {
        'valid_micro_ai': {
            'response': '{"title": "Valid Title", "author": "Test Author"}',
            'expected_valid': True
        },
        'valid_heavy_ai': {
            'response': '{"title": "Full Article", "content": "' + 'A' * 300 + '", "confidence": 0.85}',
            'expected_valid': True  
        },
        'invalid_json': {
            'response': 'Not valid JSON response',
            'expected_valid': False
        },
        'malformed_response': {
            'response': '{"title": "", "content": null}',
            'expected_valid': False
        }
    }


@pytest.fixture
def challenge_detection_test_cases():
    """Test cases for challenge detection."""
    return {
        'cloudflare_challenge': {
            'content': '<div id="cf-challenge">Please wait</div>',
            'expected': True
        },
        'turnstile_challenge': {
            'content': '<script src="challenges.cloudflare.com/turnstile"></script>',
            'expected': True
        },
        'normal_content': {
            'content': '<article><h1>Normal Article</h1></article>',
            'expected': False
        }
    }


@pytest.fixture
def paywall_detection_test_cases():
    """Test cases for paywall detection."""
    return {
        'nyt_paywall': {
            'content': '<div data-testid="paywall-prompt">Subscribe to continue</div>',
            'expected': True
        },
        'metered_content': {
            'content': '<div class="meteredContent">Limited access</div>',
            'expected': True
        },
        'free_content': {
            'content': '<article><p>Free content available to all</p></article>',
            'expected': False
        }
    }


@pytest.fixture
def slo_test_scenarios():
    """Test scenarios for SLO monitoring."""
    return {
        'deterministic_success_high': {
            'successful_requests': 950,
            'total_requests': 1000,
            'expected_slo_met': True
        },
        'deterministic_success_low': {
            'successful_requests': 850,
            'total_requests': 1000, 
            'expected_slo_met': False
        },
        'ai_usage_within_limit': {
            'ai_requests': 5,
            'total_requests': 1000,
            'expected_slo_met': True
        },
        'ai_usage_over_limit': {
            'ai_requests': 25,
            'total_requests': 1000,
            'expected_slo_met': False
        }
    }


@pytest.fixture
def dedup_test_articles():
    """Articles for deduplication testing."""
    base_content = "This is the original article content with unique information about breaking news events. It contains substantial text to ensure SimHash calculations work properly with meaningful content length for accurate deduplication analysis."
    
    return {
        'original': Article(
            source_url="https://example.com/original",
            canonical_url="https://example.com/original",
            title="Original Article",
            body=base_content,
            status=ArticleStatus.STORED
        ),
        'exact_duplicate': Article(
            source_url="https://example.com/duplicate",
            canonical_url="https://example.com/duplicate",
            title="Original Article", 
            body=base_content,
            status=ArticleStatus.STORED
        ),
        'near_duplicate': Article(
            source_url="https://example.com/similar",
            canonical_url="https://example.com/similar",
            title="Original Article",
            body=base_content + " With slight modifications added here.",
            status=ArticleStatus.STORED
        ),
        'different': Article(
            source_url="https://example.com/different",
            canonical_url="https://example.com/different",
            title="Completely Different Article",
            body="This article has completely different content about other topics.",
            status=ArticleStatus.STORED
        )
    }


# Utility functions for tests
def assert_article_valid(article: Article):
    """Assert that an article meets basic validity requirements."""
    assert article.title is not None
    assert len(article.title) >= 5
    assert article.body is not None
    assert len(article.body) >= 100
    assert article.canonical_url is not None
    assert article.status == ArticleStatus.STORED


def create_test_database(db_path: Path):
    """Create a test database with sample data."""
    conn = sqlite3.connect(db_path)
    
    # Create tables (simplified version)
    conn.execute("""
        CREATE TABLE articles (
            id INTEGER PRIMARY KEY,
            canonical_url TEXT UNIQUE,
            title TEXT,
            content TEXT,
            status TEXT,
            created_at TIMESTAMP
        )
    """)
    
    # Insert sample data
    conn.execute("""
        INSERT INTO articles (canonical_url, title, content, status, created_at)
        VALUES (?, ?, ?, ?, ?)
    """, (
        "https://example.com/test-1",
        "Test Article 1", 
        "Content for test article 1",
        "completed",
        datetime.utcnow().isoformat()
    ))
    
    conn.commit()
    conn.close()


# Performance testing helpers
@pytest.fixture
def performance_benchmarks():
    """Performance benchmarks for testing."""
    return {
        'deterministic_parsing': {
            'json_ld': 0.010,    # 10ms
            'meta_tags': 0.020,  # 20ms
            'framework': 0.030,  # 30ms
            'html': 0.050       # 50ms
        },
        'ai_parsing': {
            'micro_ai': 0.300,   # 300ms
            'heavy_ai': 1.000    # 1000ms
        }
    }


# Logging setup for tests
@pytest.fixture(autouse=True)
def setup_test_logging():
    """Setup structured logging for tests."""
    logger = get_logger('test')
    yield logger


@pytest.fixture
def temp_directories():
    """Create temporary directories for testing."""
    import tempfile
    import shutil
    import time
    import gc
    from pathlib import Path
    
    temp_dir = Path(tempfile.mkdtemp())
    
    # Create required subdirectories
    data_dir = temp_dir / "data"
    logs_dir = temp_dir / "logs"
    data_dir.mkdir()
    logs_dir.mkdir()
    
    yield {
        "base": temp_dir,
        "data": data_dir,
        "logs": logs_dir
    }
    
    # Cleanup with Windows file locking retry logic
    # Force garbage collection to release file handles
    gc.collect()
    time.sleep(0.2)  # Give Windows time to release locks
    
    # Retry cleanup up to 3 times
    for attempt in range(3):
        try:
            shutil.rmtree(temp_dir)
            break
        except PermissionError:
            if attempt < 2:  # Try 2 more times
                time.sleep(0.5)
                gc.collect()
                continue
            # Final attempt failed - just ignore on Windows
            pass


@pytest.fixture
async def mock_scraper(temp_directories, mock_anthropic_client):
    """Create fully mocked scraper for integration testing."""
    from unittest.mock import patch, AsyncMock
    from nyt_scraper.scraper import NYTScraper
    
    # Mock config paths
    with patch.object(config, 'data_dir', temp_directories["data"]), \
         patch.object(config, 'sqlite_db_path', temp_directories["data"] / "test.db"), \
         patch.object(config, 'log_file', temp_directories["logs"] / "test.jsonl"):
        
        # Mock external HTTP client
        with patch('nyt_scraper.scraper.HTTP2Client') as mock_http_class, \
             patch('nyt_scraper.scraper.RSSFetcher') as mock_rss_class, \
             patch('nyt_scraper.scraper.SitemapParser') as mock_sitemap_class, \
             patch('nyt_scraper.scraper.SearchFallback') as mock_search_class:
            
            scraper = NYTScraper(mock_anthropic_client)
            
            # Setup mock return values
            mock_http_client = mock_http_class.return_value
            mock_http_client.fetch = AsyncMock()
            mock_http_client.close = AsyncMock()  # Mock the close method as async
            
            mock_rss_class.return_value.get_latest_articles = AsyncMock(return_value=[])
            mock_sitemap_class.return_value.get_latest_articles = AsyncMock(return_value=[])
            mock_search_class.return_value.search_recent_articles = AsyncMock(return_value=[])
            
            # Initialize stats if not present
            if not hasattr(scraper, 'stats'):
                scraper.stats = {
                    'total_processed': 0,
                    'deterministic_success': 0,
                    'ai_escalations': 0,
                    'cache_hits': 0,
                    'duplicates_found': 0,
                    'challenges_detected': 0,
                    'paywalls_detected': 0,
                    'errors': 0
                }
            
            yield scraper


@pytest.fixture
def temp_slo_db():
    """Create temporary SLO database."""
    import tempfile
    temp_file = tempfile.NamedTemporaryFile(delete=False, suffix='.db')
    temp_file.close()
    db_path = Path(temp_file.name)
    yield db_path
    
    # Windows-compatible cleanup with retry logic
    if db_path.exists():
        import time
        import gc
        
        # Force garbage collection to release file handles
        gc.collect()
        time.sleep(0.1)
        
        # Retry deletion up to 3 times
        for attempt in range(3):
            try:
                db_path.unlink()
                break
            except PermissionError:
                if attempt < 2:  # Try 2 more times
                    time.sleep(0.3)
                    gc.collect()
                    continue
                # Final attempt failed - ignore on Windows
                pass


@pytest.fixture
def slo_tracker(temp_slo_db):
    """Create SLO tracker with temporary database."""
    from unittest.mock import patch
    from nyt_scraper.monitoring.slo_tracker import SLOTracker
    
    with patch('nyt_scraper.monitoring.slo_tracker.config') as mock_config:
        mock_config.data_dir = temp_slo_db.parent
        mock_config.slo_deterministic_success_rate = 0.95
        mock_config.slo_ai_usage_rate = 0.01
        mock_config.slo_freshness_lag_minutes = 60
        
        tracker = SLOTracker()
        tracker.slo_db_path = temp_slo_db
        tracker._init_slo_db()
        
        return tracker