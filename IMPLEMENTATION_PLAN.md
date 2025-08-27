# NYT Scraper - Production Implementation Plan

## Table of Contents
1. [Project Overview](#project-overview)
2. [Architecture](#architecture)
3. [Project Structure](#project-structure)
4. [Core Components](#core-components)
5. [Implementation Details](#implementation-details)
6. [Testing Strategy](#testing-strategy)
7. [Deployment & Operations](#deployment--operations)
8. [Governance & Compliance](#governance--compliance)
9. [Timeline](#timeline)

## Project Overview

A **resilient, cost-efficient** New York Times article scraper designed to operate **without an API key**. The system uses a cascading extraction strategy with minimal AI usage (<1% of requests) and includes enterprise-grade monitoring, compliance, and error handling.

### Key Goals
- ✅ Discover fresh NYT articles (RSS, sitemaps, search fallback)
- ✅ Extract structured data (title, author, date, body, tags)
- ✅ Run cheaply (<$5/month on VPS)
- ✅ Use AI minimally (hard budget caps)
- ✅ Survive layout changes via fallback chains
- ✅ Respect robots.txt and detect paywalls
- ✅ Access only public content (no authentication/cookies)

### Performance Targets
- **Discovery**: 1000+ articles/hour
- **Parsing Success**: 95% without AI
- **AI Usage**: <1% of total requests
- **Cost**: <$0.01 per article
- **Cache Hit Rate**: >60%
- **Storage**: ~1KB per article (compressed)

## Architecture

### Three-Layer Architecture

```
┌─────────────────────────────────────────────────────────────┐
│                      DISCOVERY LAYER                         │
│  RSS Feeds → Sitemaps → Search Engines → Local Cache        │
└─────────────────────────────────────────────────────────────┘
                              ↓
┌─────────────────────────────────────────────────────────────┐
│                      FETCHING LAYER                          │
│  HTTP/2 Client → Cache Manager → Circuit Breaker → Wayback  │
└─────────────────────────────────────────────────────────────┘
                              ↓
┌─────────────────────────────────────────────────────────────┐
│                      PARSING CASCADE                         │
│  JSON-LD → Meta Tags → Framework → HTML → XPath → AI        │
└─────────────────────────────────────────────────────────────┘
                              ↓
┌─────────────────────────────────────────────────────────────┐
│                    STORAGE & DEDUP                           │
│  SQLite (WAL) → SimHash Dedup → Whoosh Index → Eviction     │
└─────────────────────────────────────────────────────────────┘
```

## Project Structure

```
NYT-Data-Fetch/
├── nyt_scraper/
│   ├── __init__.py
│   ├── config.py                    # Environment vars, kill-switch, dry-run
│   ├── compliance/
│   │   ├── robots_manager.py        # Persistent robots.txt cache with TTL
│   │   ├── paywall_detector.py      # DOM-based paywall detection
│   │   └── governance.py            # Usage policies, kill switch
│   ├── discovery/
│   │   ├── rss_fetcher.py          # NYT RSS feed parser
│   │   ├── sitemap_parser.py       # News sitemap with lastmod support
│   │   ├── search_fallback.py      # DuckDuckGo/Bing scraping (<5% volume)
│   │   └── local_search.py         # Whoosh-based offline search
│   ├── fetching/
│   │   ├── http2_client.py         # HTTP/2 with Brotli, redirect tracking
│   │   ├── charset_handler.py      # Encoding detection & normalization
│   │   ├── cache_manager.py        # ETag/Last-Modified caching
│   │   ├── circuit_breaker.py      # 429/403 protection
│   │   ├── concurrency_limiter.py  # Per-host rate limiting
│   │   ├── headless_browser.py     # Optional Playwright fallback
│   │   └── wayback_fallback.py     # Internet Archive for dead URLs
│   ├── parsing/
│   │   ├── json_ld_extractor.py    # Structured data extraction
│   │   ├── meta_tag_parser.py      # OpenGraph/Twitter cards
│   │   ├── amp_extractor.py        # AMP-specific parsing
│   │   ├── framework_parser.py     # __NEXT_DATA__ extraction
│   │   ├── html_parser.py          # Selectolax + readability
│   │   ├── xpath_extractor.py      # Custom XPath rules
│   │   ├── content_validator.py    # Min content requirements
│   │   ├── date_normalizer.py      # UTC ISO-8601 normalization
│   │   ├── canonicalizer.py        # URL canonicalization
│   │   └── ai_fallback.py          # Claude Haiku with hard caps
│   ├── storage/
│   │   ├── sqlite_manager.py       # WAL mode, auto-triggers
│   │   ├── storage_eviction.py     # 30-day TTL, 1GB cap
│   │   ├── dedup_manager.py        # SimHash near-duplicate detection
│   │   └── whoosh_indexer.py       # Full-text search
│   ├── monitoring/
│   │   ├── metrics.py               # Prometheus-compatible metrics
│   │   ├── alerts.py                # Slack/email notifications
│   │   ├── structured_logger.py    # JSONL structured logs
│   │   └── log_rotation.py         # 7-day retention, compression
│   ├── utils/
│   │   ├── shutdown_handler.py     # Graceful async shutdown
│   │   └── boilerplate_rules.py    # Configurable content cleanup
│   ├── models.py                    # Article dataclass
│   └── scraper.py                   # Main orchestrator
├── config/
│   ├── boilerplate_patterns.yaml   # External regex patterns
│   └── ai_prompt.json               # Deterministic AI config
├── tests/
│   ├── conftest.py                 # VCR.py setup, fixtures
│   ├── test_compliance.py          # Robots/paywall tests
│   ├── test_stress.py              # 429/403 backoff tests
│   ├── test_drift.py               # Selector mutation tests
│   ├── test_charset_handler.py     # Encoding edge cases
│   └── test_boilerplate_rules.py   # Pattern over-stripping tests
├── logs/
│   ├── scraper.jsonl               # Current log file
│   └── archive/                    # Compressed old logs
├── data/
│   ├── robots_cache.db             # Persistent robots.txt cache
│   ├── ai_usage.db                 # AI budget tracking
│   ├── nyt_cache.db                # Main article storage
│   └── ai_triggers/                # HTML that triggered AI
├── requirements.txt                # Pinned dependencies
├── requirements.lock               # pip-compile output
├── .env.example                    # Configuration template
├── .github/workflows/ci.yml        # CI/CD pipeline
├── pyproject.toml                  # Black/Flake8/MyPy config
└── setup.py                        # Package setup
```

## Core Components

### 1. Compliance Layer

#### Robots Manager
```python
class RobotsManager:
    # Features:
    - Persistent SQLite cache with TTL
    - Fallback to "allow-all" if unreachable
    - Per-host max-age tracking
    - Selective refresh based on http_fetched_at
```

#### Paywall Detector
```python
class PaywallDetector:
    # DOM markers (not URL-based):
    - 'div[data-testid="paywall-prompt"]'
    - 'div.meteredContent'
    - '[aria-label*="subscriber"]'
```

### 2. Discovery Layer

#### RSS & Sitemaps
- Parse all NYT RSS feeds by section
- Support paginated/dated sitemaps
- Handle `<news:news>` entries
- Respect `<lastmod>` granularity

#### Search Engine Fallback
- **Priority**: DuckDuckGo > Startpage > Bing (tried in sequence)
- **Retry Logic**: Max 2 retries per engine before moving to next
- **Volume Cap**: <5% of total discovery volume
- **CAPTCHA Detection**: Auto-detect and trigger 24-hour circuit breaker
- **Implementation**: HTML scraping only (no paid APIs)

### 3. Fetching Layer

#### HTTP/2 Client Features
- Brotli/Gzip compression
- `Accept-Language: en-US` enforcement
- 301/302 redirect tracking
- Content-Type/charset validation
- MIME sniffing fallback
- 410/451 special handling
- 2MB HTML size cap

#### Circuit Breaker
- Per-host concurrency limits
- Exponential backoff on 429/403
- Cool-off period logging
- Graceful degradation

### 4. Parsing Cascade

```python
# Extraction order (fastest to slowest):
1. JSON-LD extractor       # ~10ms, structured data
2. Meta tag parser         # ~20ms, OpenGraph/Twitter
3. AMP extractor          # ~30ms, cleaner HTML
4. Framework parser       # ~30ms, __NEXT_DATA__
5. HTML parser            # ~50ms, selectolax + readability
6. XPath extractor        # ~50ms, custom rules
7. AI fallback            # ~1000ms, <1% usage

# Optional (on high failure rate):
8. Headless browser       # ~3000ms, Playwright rendering
```

### 5. AI Fallback

#### Deterministic Configuration
```json
{
  "model": "claude-3-haiku",
  "temperature": 0,
  "max_tokens": 500,
  "html_max_chars": 5000,
  "output_format": "json_only",
  "system_prompt": "Extract article data as JSON. Output ONLY valid JSON, no explanations.",
  "user_prompt_template": "Extract from this HTML snippet:\n{html_truncated}\n\nReturn JSON with keys: title, author, published_date, body"
}
```

#### Example AI Prompt
```
System: Extract article data as JSON. Output ONLY valid JSON, no explanations.

User: Extract from this HTML snippet:
<article><h1>Climate Change Accelerating</h1><p>Scientists report...</p></article>

Return JSON with keys: title, author, published_date, body

Expected Response:
{"title": "Climate Change Accelerating", "author": null, "published_date": null, "body": "Scientists report..."}
```

#### Hard Budget Caps
- **Daily tokens**: 10,000 max
- **Calls/minute**: 10 max
- **Total calls**: 1,000 max
- **Alert threshold**: 80% usage
- **Fallback response**: `{"title": "", "body": ""}`

### 6. Storage & Deduplication

#### SQLite Schema
```sql
CREATE TABLE articles (
    id INTEGER PRIMARY KEY,
    source_url TEXT NOT NULL,           -- Original URL
    canonical_url TEXT UNIQUE NOT NULL, -- For dedup
    redirect_chain TEXT,                -- JSON array
    title TEXT,
    author TEXT,
    published_date DATETIME,            -- UTC ISO-8601
    updated_date DATETIME,              -- Distinct from published
    content TEXT,
    body_hash TEXT,                     -- SHA256
    simhash INTEGER,                    -- 64-bit for near-dedup
    etag TEXT,
    last_modified TEXT,
    last_seen DATETIME DEFAULT CURRENT_TIMESTAMP,
    created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
    updated_at DATETIME DEFAULT CURRENT_TIMESTAMP
);

-- Indexes
CREATE UNIQUE INDEX idx_canonical ON articles(canonical_url);
CREATE INDEX idx_body_hash ON articles(body_hash);
CREATE INDEX idx_simhash ON articles(simhash);
CREATE INDEX idx_source_url ON articles(source_url);

-- Auto-update trigger
CREATE TRIGGER update_timestamp 
AFTER UPDATE ON articles
BEGIN
    UPDATE articles SET updated_at = CURRENT_TIMESTAMP 
    WHERE id = NEW.id;
END;
```

#### Eviction Policy
- **TTL**: 30 days
- **Size cap**: 1GB database
- **Strategy**: Evict 10% oldest at a time
- **Schedule**: Daily vacuum/optimize
- **Whoosh**: Periodic index optimization

#### Backup & Recovery
- **Daily Backup**: SQLite dump with compression
- **WAL Shipping**: Continuous transaction log backup
- **Retention**: 7 daily + 4 weekly + 12 monthly backups
- **Disaster Recovery**: 
  1. Stop scraper
  2. Restore from latest backup
  3. Replay WAL files
  4. Verify data integrity
  5. Resume operations

#### Index Maintenance
- **Whoosh Optimization**: Weekly index cleanup
- **SQLite VACUUM**: Daily after eviction
- **Performance Monitoring**: Query time tracking
- **Storage Health**: Weekly storage integrity checks

### 7. Monitoring & Alerts

#### Prometheus Metrics
```python
# Exported metrics:
- scraper_requests_total{status}
- articles_processed_total{parser}
- cache_hit_rate
- ai_budget_used_percent
- fetch_duration_seconds (histogram)
```

#### Alert Thresholds
- Parse failure rate >5%
- AI budget usage >80%
- Repeated 429/403 (>10 events)
- Circuit breaker trips

#### Structured Logging
- **Format**: JSONL to `logs/scraper.jsonl`
- **Retention**: 7-day active retention with automatic rotation
- **Archiving**: Compress logs >1GB to `logs/archive/` folder
- **Cleanup**: Auto-delete archived logs older than 7 days
- **Fields**: timestamp, request_id, hostname, type, data

#### AI Fallback Sampling
- **Sample Rate**: 10% of AI triggers stored for analysis
- **Storage**: Compressed HTML in `data/ai_triggers/`
- **Cleanup**: 7-day retention for sampled triggers
- **Analysis**: Weekly review of AI usage patterns

#### Dashboard Examples (Grafana)
```yaml
# Key Panels:
- AI Usage Rate (target: <1%)
- Parse Success Rate (target: >95%)
- Circuit Breaker Status
- Cache Hit Rate
- Storage Usage & Eviction
- Rate Limiting Events
- Error Rate by Parser
- Request Duration Histograms
- Daily Article Volume
- Failed Parsing Breakdown
```

## Implementation Details

### Content Validation

```python
# Requirements:
- Headline required
- Body required with either:
  - ≥2000 characters
  - ≥5 paragraphs
  - ≥300 words
- Exception for articles marked as "brief"

# Boilerplate removal:
- "Sign up for newsletter"
- "Related Articles"
- "Advertisement"
- Configurable via YAML
```

### Date Normalization

```python
# Centralized parsing:
- Use dateutil for flexible parsing
- Normalize to UTC ISO-8601
- Log both raw and parsed values
- Handle published vs updated distinctly
```

### Charset Handling

```python
# Priority order:
1. <meta charset> tag
2. Content-Type header
3. chardet detection (>70% confidence)
4. UTF-8 fallback

# Always transcode to UTF-8
```

### Headless Browser Optimization

```python
# Resource blocking for speed:
- Block images
- Block stylesheets
- Block fonts
- Block media

# Only load HTML/JS
# Timeout: 10 seconds max
```

## Testing Strategy

### Unit Tests
- Each parser module
- Encoding edge cases (Latin1 as UTF-8)
- Boilerplate pattern validation
- Date parsing variations

### Integration Tests
- Full parsing cascade
- Fallback chains
- Cache hit/miss scenarios
- Deduplication logic

### Compliance Tests
```python
# Explicit tests for:
- robots.txt respect
- Paywall detection
- 429/403 backoff behavior
- Circuit breaker states
```

### Drift Tests
```python
# Mutation scenarios:
- Class name changes (article-body → story-content-v2)
- Attribute changes (data-testid → data-test-id)
- JSON-LD position moves
- Nested structure changes
```

### Stress Tests
- High concurrency
- Rate limit handling
- Storage eviction
- Memory usage under load

### CI/CD Requirements
- **Coverage**: ≥80% enforced with fail-under threshold
- **Coverage Reports**: Codecov integration + HTML reports in CI artifacts  
- **Formatting**: Black (line-length=100)
- **Linting**: Flake8 with max-line-length=100
- **Type checking**: MyPy strict mode
- **Security**: Ruff static analysis + Bandit security scanning
- **Test data**: VCR.py cassettes for HTTP mocking
- **Snapshots**: JSON output validation against golden files
- **Dependency Security**: pip-audit + Dependabot integration
- **Container Security**: Trivy for Docker images (if containerized)

### Security Testing
- **Bandit**: Python security vulnerability scanning
- **Dependency Scanning**: pip-audit + Dependabot
- **Container Security**: Trivy for Docker images
- **Secret Detection**: Pre-commit hooks
- **SAST**: Static Application Security Testing

### Fuzz Testing
- **HTML Edge Cases**: Malformed HTML, encoding issues
- **JSON Injection**: Corrupted AI responses
- **SQL Injection**: Malicious input validation
- **Memory Testing**: Large file handling
- **Encoding Edge Cases**: Latin1 as UTF-8, mixed encodings

## Deployment & Operations

### Environment Configuration

```bash
# .env.example

# Rate Limiting
REQUEST_DELAY_MIN=1
REQUEST_DELAY_MAX=5
MAX_CONCURRENT_REQUESTS=3

# Caching
CACHE_TTL_HOURS=24
SQLITE_DB_PATH=./data/nyt_cache.db

# AI Fallback
AI_MODEL=claude-3-haiku
AI_DAILY_TOKEN_LIMIT=10000
AI_FALLBACK_RATIO=0.01

# Monitoring
SLACK_WEBHOOK=https://hooks.slack.com/...
PROMETHEUS_PORT=9090

# Compliance
KILL_SWITCH=false
DRY_RUN=false
ENABLE_HEADLESS=false

# Storage
MAX_DB_SIZE_GB=1
RETENTION_DAYS=30
```

### Graceful Shutdown

```python
# Handles SIGINT/SIGTERM:
1. Stop accepting new requests
2. Await pending async tasks
3. Flush request queues
4. Save in-flight metrics
5. Close database connections
6. Archive current logs
```

### Operational Modes

#### Dry-Run Mode
- Fetch headers only
- No content parsing
- No storage writes
- Safe for testing

#### Kill Switch
- Instantly disable all fetching
- Maintain read-only access
- Emergency compliance tool

### Daily Operations

#### Automated Tasks
- Storage eviction (10% oldest if >1GB)
- Log rotation and compression
- Whoosh index optimization
- Metrics report generation
- Alert threshold checking

#### Manual Review
- AI trigger samples
- CAPTCHA detection logs
- Robots.txt fallback events
- Parse failure patterns

#### Scaling Strategy
- **Horizontal Scaling**: Multiple scraper instances
- **Load Distribution**: Round-robin feed assignment
- **Database Scaling**: Read replicas for search
- **Cache Scaling**: Redis cluster for high volume

#### Traffic Doubling Response
- **Concurrency Increase**: Auto-scale to 6 concurrent requests
- **Rate Limiting**: Maintain 1-5s delays per host
- **Storage Scaling**: Auto-eviction to maintain 1GB cap
- **Monitoring**: Alert on 80% resource usage
- **Performance**: Maintain <50ms median parse time

## Governance & Compliance

### Data Privacy
- **GDPR/CCPA Compliance**: No PII collection, public content only
- **Data Retention**: 30-day TTL with automatic deletion
- **Right to Deletion**: Full data export and deletion capabilities
- **Audit Trail**: All operations logged with request IDs
- **Data Minimization**: Store only necessary article metadata
- **Consent Management**: No user tracking or cookies

### Terms of Service Monitoring
- **Monthly Review**: Manual compliance verification
- **Change Detection**: Monitor NYT ToS updates
- **Kill Switch**: Immediate shutdown on compliance issues
- **Legal Review**: Quarterly legal compliance assessment
- **Policy Updates**: Automated alerts on ToS changes
- **Compliance Dashboard**: Real-time compliance status

### Regulatory Compliance
- **Robots.txt**: Strict adherence with fallback policies
- **Rate Limiting**: Respectful crawling with exponential backoff
- **Content Access**: Public content only, no authentication bypass
- **Copyright Respect**: No content redistribution, metadata only
- **International Law**: Compliance with local data protection laws

## Timeline

### Week 1: Foundation (Days 1-5)
- **Day 1-2**: Project setup, core infrastructure, models
- **Day 3**: Compliance layer (robots, paywall, governance)
- **Day 4**: HTTP/2 client with all optimizations
- **Day 5**: Monitoring (Prometheus, alerts, structured logging)

### Week 2: Implementation (Days 6-10)
- **Day 6**: Discovery layer (RSS, sitemaps, search)
- **Day 7**: Parsing cascade (all extractors)
- **Day 8**: AI fallback with budget controls
- **Day 9**: Storage with deduplication
- **Day 10**: Headless browser, circuit breakers

### Week 3: Testing & Deployment (Days 11-14)
- **Day 11**: Unit and integration tests
- **Day 12**: Compliance and drift tests
- **Day 13**: CI/CD pipeline setup
- **Day 14**: Production deployment

### Post-Launch
- Week 4: Performance tuning
- Week 5: Alert threshold adjustment
- Week 6: Parsing rule updates based on AI triggers

## Success Metrics

### Technical KPIs
- ✅ 95% parse success without AI
- ✅ <1% AI usage rate
- ✅ <50ms median parse time
- ✅ >60% cache hit rate
- ✅ <$5/month operational cost
- ✅ <100ms 95th percentile response time

### Operational KPIs
- ✅ 99.9% uptime
- ✅ <5 minute MTTR
- ✅ Zero robots.txt violations
- ✅ Zero paywall breaches
- ✅ 100% GDPR compliance
- ✅ <1 hour disaster recovery time

### Compliance KPIs
- ✅ Zero data privacy violations
- ✅ 100% ToS compliance
- ✅ Complete audit trail availability
- ✅ Zero copyright infringements
- ✅ Full regulatory compliance

## Risk Mitigation

### Technical Risks
- **Layout changes**: Multi-layer parsing cascade
- **Rate limiting**: Circuit breakers, backoff
- **Content changes**: AI fallback, drift tests
- **Storage growth**: Automatic eviction

### Compliance Risks
- **Robots.txt**: Cached parser with fallback
- **Paywalls**: DOM-based detection
- **Legal issues**: Kill switch, dry-run mode
- **Usage abuse**: Governance policies
- **Data privacy**: GDPR/CCPA compliance monitoring
- **ToS violations**: Automated compliance checking

### Operational Risks
- **High costs**: Hard budget caps
- **Data loss**: WAL mode, backups
- **Service degradation**: Graceful fallbacks
- **Alert fatigue**: Smart thresholds

## Conclusion

This implementation provides a **production-ready, cost-efficient, and legally compliant** NYT scraping system with enterprise-grade reliability and observability. The cascading architecture ensures maximum resilience while minimizing costs and respecting website policies.

### Key Advantages
- **Resilient**: Multiple fallback layers
- **Efficient**: <1% AI usage, smart caching
- **Compliant**: Robots.txt, paywall detection, GDPR/CCPA
- **Observable**: Comprehensive monitoring with Grafana dashboards
- **Maintainable**: Clean code, 80%+ test coverage
- **Scalable**: Async operations, circuit breakers, horizontal scaling
- **Secure**: Security scanning, fuzz testing, dependency monitoring
- **Recoverable**: Automated backups, disaster recovery procedures

Ready for immediate implementation with clear success metrics and risk mitigation strategies.