# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview

This is a NYT Scraper project designed to extract New York Times articles without an API key using a robust, multi-layered approach with minimal AI usage.

## Development Commands

### Installation
```bash
pip install -r requirements.txt
```

### Testing
```bash
# Run all tests
python -m pytest tests/

# Run specific test categories
python -m pytest tests/test_discovery.py
python -m pytest tests/test_parsing.py
python -m pytest tests/test_fetching.py

# Run with coverage
python -m pytest --cov=nyt_scraper tests/
```

## Architecture

### Core Components

1. **Discovery Layer** - Finds articles via:
   - RSS feeds (`https://rss.nytimes.com/services/xml/rss/nyt/`)
   - Section sitemaps with `<lastmod>` tags
   - Search engine fallbacks (`site:nytimes.com` queries)
   - Local SQLite + Whoosh index for offline queries

2. **Fetching Layer** - Resilient HTTP client with:
   - Connection pooling + retries via `httpx`
   - Delta crawling using `If-Modified-Since`/`ETag`
   - Rotating User-Agents
   - Free proxy rotation on 429/403
   - Wayback Machine fallback

3. **Parsing Layer** - Cascaded extraction strategy:
   - JSON-LD scripts
   - OpenGraph/Twitter meta tags
   - Framework blobs (`__NEXT_DATA__`)
   - Semantic HTML parsing
   - XPath/CSS rules
   - AI fallback (capped at 1% usage)

### Data Storage

- **SQLite** for article caching with indexes on date, section, and tags
- **Whoosh** for full-text search indexing

### Key Dependencies

- `httpx` - HTTP client with connection pooling
- `lxml` - Fast XML/HTML parsing
- `beautifulsoup4` - HTML parsing fallback
- `whoosh` - Local search indexing
- `sqlite3` - Local caching
- `python-dateutil` - Date parsing

## Configuration

Environment variables (via `.env` file):
- `REQUEST_DELAY_MIN` / `REQUEST_DELAY_MAX` - Rate limiting (1-5s)
- `MAX_CONCURRENT_REQUESTS` - Concurrency limit (default: 3)
- `CACHE_TTL_HOURS` - Cache duration (default: 24)
- `SQLITE_DB_PATH` - Database location
- `AI_FALLBACK_RATIO` - AI usage cap (default: 0.01)

## Implementation Notes

- Main module expected at `nyt_scraper/`
- Tests expected in `tests/` directory
- Success rate target: 95% without AI
- AI usage must remain <1% of total requests
- Respect rate limiting with 1-5s delays between requests

## Testing Strategy

### High-Level Testing Approach
- **Integration tests** for end-to-end scraping workflows
- **Mock external services** (RSS feeds, HTTP responses) to avoid hitting real NYT servers
- **Test parsing fallbacks** by simulating different HTML structures
- **Performance benchmarks** for rate limiting and concurrency

### Code Quality & Duplication Prevention
- **DRY principle**: Extract common parsing logic into reusable functions
- **Shared test utilities**: Create `tests/conftest.py` with common fixtures and mocks
- **Base classes**: Use inheritance for similar parsing strategies (e.g., `BaseParser`, `BaseFetcher`)
- **Configuration testing**: Test different environment variable combinations
- **Avoid duplicate selectors**: Centralize CSS/XPath rules in configuration files

### Test Coverage Requirements
- **Discovery layer**: Test RSS parsing, sitemap handling, search fallbacks
- **Fetching layer**: Test retry logic, User-Agent rotation, proxy handling
- **Parsing layer**: Test each extraction method and fallback chain
- **Storage layer**: Test SQLite operations and Whoosh indexing
- **Error handling**: Test rate limiting, network failures, parsing failures