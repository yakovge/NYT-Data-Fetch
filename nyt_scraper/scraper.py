"""Main NYT scraper orchestrator with three-tier parsing cascade and enhanced monitoring."""

import asyncio
import signal
import sys
import time
from datetime import datetime, timedelta
from pathlib import Path
from typing import Dict, List, Optional, Tuple, Any
import threading

# Import all enhanced components
from .config import config
from .models import Article, ArticleStatus, FetchResult, ParseResult
from .monitoring.structured_logger import get_logger
from .monitoring import MetricsCollector, AlertManager, SLOTracker

# Compliance and governance
from .compliance import ComplianceManager, RobotsManager, PaywallDetector

# Discovery layer
from .discovery import RSSFetcher, SitemapParser, SearchFallback, LocalSearch

# Enhanced fetching layer
from .fetching import HTTP2Client, CacheManager, CircuitBreaker, PerHostBudgets
from .fetching import CharsetHandler

# Three-tier parsing cascade
from .parsing import (
    JSONLDExtractor, MetaTagParser, FrameworkParser, NYTHTMLParser,
    MicroAIController, HeavyAIFallback, AIValidator
)

# Enhanced storage layer
from .storage import SQLiteManager, DedupManager, StorageEviction
from .storage import WhooshIndexer, FTS5Indexer

logger = get_logger(__name__)


class NYTScraper:
    """
    Main orchestrator for NYT article scraping with three-tier parsing cascade.
    
    Features:
    - Three-tier parsing: Deterministic → Micro-AI → Heavy AI
    - Enhanced telemetry and SLO tracking
    - Per-host budget management
    - Advanced deduplication and storage eviction
    - Comprehensive monitoring and alerting
    """
    
    def __init__(self, anthropic_client=None):
        self.anthropic_client = anthropic_client
        self._shutdown_event = asyncio.Event()
        self._running = False
        
        # Initialize monitoring first
        logger.info("nyt_scraper_initializing")
        self.metrics = MetricsCollector()
        self.alerts = AlertManager()
        self.slo_tracker = SLOTracker()
        
        # Compliance and governance
        self.compliance_manager = ComplianceManager()
        self.robots_manager = RobotsManager()
        self.paywall_detector = PaywallDetector()
        
        # Enhanced storage layer
        self.sqlite_manager = SQLiteManager()
        self.dedup_manager = DedupManager()
        self.storage_eviction = StorageEviction(self.sqlite_manager)
        
        # Initialize search indexer (FTS5 or Whoosh based on config)
        if config.use_sqlite_fts5 and FTS5Indexer is not None:
            try:
                self.search_indexer = FTS5Indexer(self.sqlite_manager)
            except RuntimeError:
                # FTS5 not available, fall back to None
                self.search_indexer = None
        elif WhooshIndexer is not None:
            self.search_indexer = WhooshIndexer()
        else:
            # No search indexer available
            self.search_indexer = None
        
        # Enhanced fetching layer
        self.http_client = HTTP2Client(self.metrics)
        self.cache_manager = CacheManager()
        self.circuit_breaker = CircuitBreaker(self.metrics)
        self.per_host_budgets = PerHostBudgets()
        self.charset_handler = CharsetHandler()
        
        # Discovery layer
        self.rss_fetcher = RSSFetcher(self.http_client, self.metrics)
        self.sitemap_parser = SitemapParser(self.http_client, self.metrics)
        self.search_fallback = SearchFallback(self.http_client, self.metrics)
        self.local_search = LocalSearch()
        
        # Three-tier parsing cascade
        self.json_ld_extractor = JSONLDExtractor()
        self.meta_tag_parser = MetaTagParser()
        self.framework_parser = FrameworkParser()
        self.html_parser = NYTHTMLParser()
        
        # AI components
        self.micro_ai_controller = MicroAIController(anthropic_client)
        self.heavy_ai_fallback = HeavyAIFallback(anthropic_client)
        self.ai_validator = AIValidator(self.sqlite_manager)
        
        # Statistics tracking
        self.stats = {
            'total_processed': 0,
            'deterministic_success': 0,
            'micro_ai_used': 0,
            'heavy_ai_used': 0,
            'cache_hits': 0,
            'duplicates_found': 0,
            'challenges_detected': 0,
            'paywalls_detected': 0
        }
        
        # Setup graceful shutdown
        self._setup_signal_handlers()
        
        logger.info("nyt_scraper_initialized",
                   use_fts5=config.use_sqlite_fts5,
                   ai_enabled=config.ai_min_enabled or config.ai_heavy_enabled)
    
    def _setup_signal_handlers(self):
        """Setup graceful shutdown signal handlers."""
        def signal_handler(signum, frame):
            logger.info("shutdown_signal_received", signal=signum)
            asyncio.create_task(self.shutdown())
        
        signal.signal(signal.SIGINT, signal_handler)
        signal.signal(signal.SIGTERM, signal_handler)
    
    async def start(self):
        """Start the scraper main loop."""
        if not self.compliance_manager.is_operational():
            logger.warning("scraper_not_operational", reason="kill_switch_active")
            return
        
        self._running = True
        logger.info("nyt_scraper_started")
        
        try:
            # Start background tasks
            background_tasks = [
                asyncio.create_task(self._maintenance_loop()),
                asyncio.create_task(self._metrics_reporting_loop()),
                asyncio.create_task(self._discovery_loop()),
            ]
            
            # Wait for shutdown signal
            await self._shutdown_event.wait()
            
            # Cancel background tasks
            for task in background_tasks:
                task.cancel()
            
            await asyncio.gather(*background_tasks, return_exceptions=True)
            
        except Exception as e:
            logger.error("scraper_main_loop_error", error=str(e))
            await self.alerts.send_alert("critical", f"Scraper main loop error: {str(e)}")
        finally:
            await self._cleanup()
    
    async def _discovery_loop(self):
        """Main discovery and processing loop."""
        while self._running and not self._shutdown_event.is_set():
            try:
                # Discover new articles
                discovered_urls = await self._discover_articles()
                
                if not discovered_urls:
                    logger.debug("no_articles_discovered", wait_time=300)
                    await asyncio.sleep(300)  # Wait 5 minutes before retry
                    continue
                
                # Process discovered articles
                await self._process_articles(discovered_urls)
                
                # Wait between discovery cycles
                await asyncio.sleep(60)  # 1 minute between cycles
                
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error("discovery_loop_error", error=str(e))
                await asyncio.sleep(60)
    
    async def _discover_articles(self) -> List[str]:
        """Discover articles from all sources."""
        discovered_urls = []
        
        try:
            # RSS feeds (primary source)
            rss_urls = await self.rss_fetcher.get_latest_articles()
            discovered_urls.extend(rss_urls)
            logger.debug("rss_discovery_completed", count=len(rss_urls))
            
            # Sitemaps (secondary source)
            sitemap_urls = await self.sitemap_parser.get_latest_articles()
            discovered_urls.extend(sitemap_urls)
            logger.debug("sitemap_discovery_completed", count=len(sitemap_urls))
            
            # Search fallback (if under volume cap)
            search_volume_ratio = len(discovered_urls) * 0.05  # 5% cap
            if search_volume_ratio < 50:  # Max 50 search results
                search_urls = await self.search_fallback.search_recent_articles()
                discovered_urls.extend(search_urls[:int(search_volume_ratio)])
                logger.debug("search_fallback_completed", count=len(search_urls))
            
            # Remove duplicates and filter already processed
            discovered_urls = list(set(discovered_urls))
            discovered_urls = await self._filter_already_processed(discovered_urls)
            
            logger.info("article_discovery_completed", 
                       total_discovered=len(discovered_urls),
                       rss_count=len(rss_urls),
                       sitemap_count=len(sitemap_urls))
            
            return discovered_urls
            
        except Exception as e:
            logger.error("article_discovery_error", error=str(e))
            return []
    
    async def _process_articles(self, urls: List[str]):
        """Process articles with three-tier parsing cascade."""
        semaphore = asyncio.Semaphore(config.max_concurrent_requests)
        
        async def process_single_article(url: str):
            async with semaphore:
                await self._process_single_article(url)
        
        # Process articles concurrently
        tasks = [process_single_article(url) for url in urls]
        await asyncio.gather(*tasks, return_exceptions=True)
        
        logger.info("batch_processing_completed", 
                   urls_processed=len(urls),
                   total_processed=self.stats['total_processed'])
    
    async def _process_single_article(self, url: str) -> Optional[Article]:
        """Process a single article with comprehensive error handling and telemetry."""
        start_time = time.time()
        article = None
        
        try:
            self.stats['total_processed'] += 1
            
            # Check compliance and budgets
            if not await self._check_processing_allowed(url):
                return None
            
            # Fetch article content
            fetch_result = await self._fetch_article(url)
            if not fetch_result or not fetch_result.content:
                return None
            
            # Three-tier parsing cascade
            article = await self._parse_article_cascade(url, fetch_result)
            if not article:
                return None
            
            # Deduplication check
            if await self._is_duplicate(article):
                self.stats['duplicates_found'] += 1
                logger.debug("duplicate_article_skipped", url=url)
                return None
            
            # Store article
            await self._store_article(article)
            
            # Update SLO metrics
            processing_time = time.time() - start_time
            self.slo_tracker.record_response_time(processing_time, "article_processing")
            
            # Record parsing success
            did_use_ai = article.did_use_micro_ai or article.did_escalate_heavy
            self.slo_tracker.record_deterministic_parsing_result(
                success=not did_use_ai,
                total_attempts=1
            )
            
            logger.info("article_processed_successfully",
                       url=url,
                       parser_path=article.parser_path,
                       confidence=article.parse_confidence,
                       processing_time=processing_time,
                       used_micro_ai=article.did_use_micro_ai,
                       escalated_heavy=article.did_escalate_heavy)
            
            return article
            
        except Exception as e:
            logger.error("article_processing_failed", 
                        url=url, 
                        error=str(e),
                        processing_time=time.time() - start_time)
            
            # Record error for SLO tracking
            self.slo_tracker.record_error_rate(1, 1)
            return None
    
    async def _check_processing_allowed(self, url: str) -> bool:
        """Check if processing is allowed for this URL."""
        # Check kill switch
        if not self.compliance_manager.is_operational():
            return False
        
        # Check robots.txt
        if not await self.robots_manager.can_fetch(url):
            logger.debug("robots_txt_blocked", url=url)
            return False
        
        # Check per-host budgets
        can_request, reason = self.per_host_budgets.can_make_request(url)
        if not can_request:
            logger.debug("per_host_budget_exceeded", url=url, reason=reason)
            return False
        
        # Check circuit breaker
        if self.circuit_breaker.is_open(url):
            logger.debug("circuit_breaker_open", url=url)
            return False
        
        return True
    
    async def _fetch_article(self, url: str) -> Optional[FetchResult]:
        """Fetch article content with caching and error handling."""
        try:
            # Check cache first
            cached_result = await self.cache_manager.get(url)
            if cached_result:
                self.stats['cache_hits'] += 1
                return cached_result
            
            # Track request in per-host budget
            self.per_host_budgets.record_request(url)
            
            # Fetch with HTTP/2 client
            response = await self.http_client.fetch(url)
            if not response or response.status_code >= 400:
                return None
            
            # Detect and handle charset  
            content_bytes = self.charset_handler.normalize_encoding(
                response.content, 
                response.headers.get('content-type', '')
            )
            
            # Decode bytes to string for processing
            try:
                content = content_bytes.decode('utf-8')
            except UnicodeDecodeError:
                # Fallback to smart decode if UTF-8 fails
                content = self.charset_handler.smart_decode(content_bytes)
            
            # Check for challenges/paywalls
            challenge_detected = self._detect_challenges(content, response)
            paywall_detected = self.paywall_detector.detect(content, url)
            
            if challenge_detected:
                self.stats['challenges_detected'] += 1
                self.per_host_budgets.record_challenge(url)
                logger.warning("challenge_detected", url=url, type="bot_protection")
                return None
            
            if paywall_detected:
                self.stats['paywalls_detected'] += 1
                logger.debug("paywall_detected", url=url)
                # Continue processing - we might extract some metadata
            
            # Create fetch result
            fetch_result = FetchResult(
                url=url,
                status_code=response.status_code,
                content=content_bytes,  # Store original bytes
                headers=dict(response.headers),
            )
            # Add additional attributes
            fetch_result.canonical_url = response.url
            fetch_result.challenge_detected = challenge_detected
            fetch_result.paywall_detected = paywall_detected
            
            # Cache the result
            if fetch_result.content:
                await self.cache_manager.set(url, fetch_result.content, fetch_result.headers)
            
            return fetch_result
            
        except Exception as e:
            logger.error("article_fetch_failed", url=url, error=str(e))
            self.circuit_breaker.record_failure(url)
            return None
    
    async def _parse_article_cascade(self, url: str, fetch_result: FetchResult) -> Optional[Article]:
        """Three-tier parsing cascade: Deterministic → Micro-AI → Heavy AI."""
        # Convert bytes to string for parsing
        if isinstance(fetch_result.content, bytes):
            content = fetch_result.content.decode('utf-8', errors='ignore')
        else:
            content = fetch_result.content
        article = Article(
            source_url=url,
            canonical_url=fetch_result.canonical_url,
            title="",  # Will be populated by parsing
            body="",   # Will be populated by parsing
            status=ArticleStatus.PARSING,
            challenge_detected=fetch_result.challenge_detected,
            paywall_detected=fetch_result.paywall_detected,
            etag=fetch_result.headers.get('etag'),
            last_modified=fetch_result.headers.get('last-modified')
        )
        
        # TIER 1: Deterministic parsing cascade
        deterministic_result = await self._try_deterministic_parsing(content, article)
        
        if deterministic_result and self._is_parsing_sufficient(deterministic_result):
            self.stats['deterministic_success'] += 1
            return deterministic_result
        
        # TIER 2: Micro-AI gap filling (if available and needed)
        if self.micro_ai_controller.is_available()[0]:
            micro_ai_result = await self._try_micro_ai_gap_fill(content, deterministic_result or article)
            
            if micro_ai_result and self._is_parsing_sufficient(micro_ai_result):
                self.stats['micro_ai_used'] += 1
                return micro_ai_result
        
        # TIER 3: Heavy AI fallback (strict budget control)
        escalation_needed, reason = self.heavy_ai_fallback.should_escalate(
            deterministic_result or article, 
            deterministic_result.parse_confidence if deterministic_result else 0.0,
            self.stats['total_processed']
        )
        
        if escalation_needed:
            heavy_ai_result = await self._try_heavy_ai_fallback(content, url)
            
            if heavy_ai_result:
                self.stats['heavy_ai_used'] += 1
                return heavy_ai_result
        
        # Return best available result or mark as failed
        best_result = deterministic_result or article
        best_result.status = ArticleStatus.FAILED if not self._has_minimal_content(best_result) else ArticleStatus.STORED
        
        logger.warning("parsing_cascade_exhausted",
                      url=url,
                      deterministic_confidence=deterministic_result.parse_confidence if deterministic_result else 0.0,
                      micro_ai_available=self.micro_ai_controller.is_available()[0],
                      heavy_ai_reason=reason)
        
        return best_result
    
    async def _try_deterministic_parsing(self, content: str, article: Article) -> Optional[Article]:
        """Try all deterministic parsers in sequence."""
        parsers = [
            ("json_ld", self.json_ld_extractor),
            ("meta_tags", self.meta_tag_parser), 
            ("framework", self.framework_parser),
            ("html", self.html_parser)
        ]
        
        for parser_name, parser in parsers:
            try:
                if parser_name == "json_ld":
                    parse_result = parser.extract(content, article.source_url)
                else:
                    # Other parsers might have different method names - need to check
                    parse_result = await parser.parse(content)
                if parse_result and parse_result.confidence > 0.5:
                    
                    # Update article with parsed data
                    if parse_result.article:
                        article.title = parse_result.article.title or article.title
                        article.author = parse_result.article.author or article.author
                        article.body = parse_result.article.body or article.body
                        article.summary = getattr(parse_result.article, 'summary', None) or getattr(article, 'summary', None)
                        article.section = parse_result.article.section or article.section
                        article.tags = parse_result.article.tags or article.tags
                        article.published_date = parse_result.article.published_date or article.published_date
                    
                    # Update telemetry
                    article.parser_path = parser_name
                    article.parse_confidence = parse_result.confidence
                    article.word_count = len((article.body or "").split())
                    article.char_count = len(article.body or "")
                    
                    logger.debug("deterministic_parsing_success",
                               parser=parser_name,
                               confidence=parse_result.confidence,
                               has_title=bool(article.title),
                               has_content=bool(article.body))
                    
                    return article
                    
            except Exception as e:
                logger.debug("deterministic_parser_failed", 
                           parser=parser_name, 
                           error=str(e))
                continue
        
        return None
    
    async def _try_micro_ai_gap_fill(self, content: str, article: Article) -> Optional[Article]:
        """Use micro-AI to fill missing metadata fields."""
        try:
            success, metadata = await self.micro_ai_controller.fill_missing_metadata(article, content)
            
            if success and metadata:
                # Fill missing fields
                article.title = metadata.get('title') or article.title
                article.author = metadata.get('author') or article.author
                article.section = metadata.get('section') or article.section  
                article.summary = metadata.get('summary') or article.summary
                
                # Update telemetry
                article.did_use_micro_ai = True
                article.parser_path = f"{article.parser_path or 'none'}+micro_ai"
                
                # Validate content quality
                is_valid, confidence = await self.micro_ai_controller.validate_extracted_content(article)
                article.parse_confidence = min(article.parse_confidence or 0.0, confidence)
                
                logger.info("micro_ai_gap_fill_success",
                           url=article.canonical_url,
                           fields_filled=list(metadata.keys()),
                           final_confidence=article.parse_confidence)
                
                return article
            
        except Exception as e:
            logger.error("micro_ai_gap_fill_failed",
                        url=article.canonical_url,
                        error=str(e))
        
        return None
    
    async def _try_heavy_ai_fallback(self, content: str, url: str) -> Optional[Article]:
        """Use heavy AI for comprehensive content extraction."""
        try:
            success, extracted_data = await self.heavy_ai_fallback.extract_full_article(content, url)
            
            if success and extracted_data:
                # Create new article with AI-extracted data
                article = Article(
                    source_url=url,
                    canonical_url=url,
                    title=extracted_data.get('title'),
                    author=extracted_data.get('author'),
                    body=extracted_data.get('content'),
                    summary=extracted_data.get('summary'),
                    section=extracted_data.get('section'),
                    tags=extracted_data.get('tags'),
                    word_count=extracted_data.get('word_count'),
                    parser_path="heavy_ai",
                    parse_confidence=extracted_data.get('confidence', 0.8),
                    did_escalate_heavy=True,
                    status=ArticleStatus.STORED
                )
                
                # Validate AI-extracted content
                is_valid, quality_score, reason = self.ai_validator.validate_article_content(
                    article, "heavy_ai"
                )
                
                if not is_valid:
                    logger.warning("heavy_ai_content_invalid",
                                 url=url,
                                 quality_score=quality_score,
                                 reason=reason)
                    return None
                
                logger.info("heavy_ai_extraction_success",
                           url=url,
                           confidence=article.parse_confidence,
                           quality_score=quality_score)
                
                return article
            
        except Exception as e:
            logger.error("heavy_ai_extraction_failed", url=url, error=str(e))
        
        return None
    
    def _is_parsing_sufficient(self, article: Article) -> bool:
        """Check if parsing results meet minimum quality requirements."""
        if not article:
            return False
        
        # Must have title and some content
        if not article.title or len(article.title) < 10:
            return False
        
        if not article.body or len(article.body) < 200:
            return False
        
        # Confidence must be reasonable
        if article.parse_confidence < 0.6:
            return False
        
        return True
    
    def _has_minimal_content(self, article: Article) -> bool:
        """Check if article has minimal extractable content."""
        return bool(article and article.title and len(article.title) > 5)
    
    def _detect_challenges(self, content: str, response) -> bool:
        """Detect bot challenges (Cloudflare, Turnstile, etc.)."""
        challenge_indicators = [
            'cf-browser-verification',
            'cf-challenge',
            'Turnstile',
            'Please wait while we verify',
            'Checking your browser',
            'DDoS protection by Cloudflare'
        ]
        
        content_lower = content.lower()
        return any(indicator.lower() in content_lower for indicator in challenge_indicators)
    
    async def _is_duplicate(self, article: Article) -> bool:
        """Check if article is a duplicate using SimHash."""
        try:
            return await self.dedup_manager.is_duplicate(article)
        except Exception as e:
            logger.error("duplicate_check_failed", 
                        url=article.canonical_url, 
                        error=str(e))
            return False
    
    async def _store_article(self, article: Article):
        """Store article with full telemetry."""
        try:
            # Set final status
            article.status = ArticleStatus.STORED
            article.created_at = datetime.utcnow()
            
            # Generate content checksums
            if article.body:
                import hashlib
                article.normalized_checksum = hashlib.sha256(
                    article.body.encode('utf-8')
                ).hexdigest()
            
            # Store in SQLite
            self.sqlite_manager.store_article(article)
            
            # Update search index
            if config.use_sqlite_fts5:
                # FTS5 automatically syncs via triggers
                pass
            else:
                # Check if search indexer exists and has index_article method
                if self.search_indexer and hasattr(self.search_indexer, 'index_article'):
                    self.search_indexer.index_article(article)
            
            logger.debug("article_stored", 
                        url=article.canonical_url,
                        title=article.title[:50] if article.title else "")
            
        except Exception as e:
            logger.error("article_storage_failed", 
                        url=article.canonical_url, 
                        error=str(e))
    
    async def _filter_already_processed(self, urls: List[str]) -> List[str]:
        """Filter out URLs that have already been processed recently."""
        try:
            # Check against database
            new_urls = []
            for url in urls:
                exists = await self.sqlite_manager.article_exists(url)
                if not exists:
                    new_urls.append(url)
            
            return new_urls
            
        except Exception as e:
            logger.error("url_filtering_failed", error=str(e))
            return urls  # Return all URLs on error
    
    async def _maintenance_loop(self):
        """Background maintenance tasks."""
        while self._running and not self._shutdown_event.is_set():
            try:
                # Daily maintenance
                if datetime.utcnow().hour == 2:  # Run at 2 AM UTC
                    await self._run_daily_maintenance()
                
                # Hourly SLO checks
                if datetime.utcnow().minute == 0:
                    await self._check_slo_status()
                
                await asyncio.sleep(3600)  # Check every hour
                
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error("maintenance_loop_error", error=str(e))
                await asyncio.sleep(3600)
    
    async def _run_daily_maintenance(self):
        """Run daily maintenance tasks."""
        logger.info("daily_maintenance_started")
        
        try:
            # Storage eviction
            eviction_results = self.storage_eviction.run_full_maintenance()
            logger.info("storage_eviction_completed", **eviction_results)
            
            # AI validation cache cleanup
            cleaned_entries = self.ai_validator.cleanup_cache()
            logger.info("ai_validation_cache_cleaned", entries_cleaned=cleaned_entries)
            
            # SLO data cleanup  
            slo_cleanup = self.slo_tracker.cleanup_old_data()
            logger.info("slo_data_cleaned", **slo_cleanup)
            
            # Search index optimization
            if self.search_indexer and hasattr(self.search_indexer, 'optimize_index'):
                self.search_indexer.optimize_index()
            
            logger.info("daily_maintenance_completed")
            
        except Exception as e:
            logger.error("daily_maintenance_failed", error=str(e))
            await self.alerts.send_alert("warning", f"Daily maintenance failed: {str(e)}")
    
    async def _check_slo_status(self):
        """Check SLO compliance and send alerts if needed."""
        try:
            slo_status = self.slo_tracker.get_slo_status(hours=1)
            error_budget_status = self.slo_tracker.get_error_budget_status()
            
            # Check for SLO violations
            for slo_name, status in slo_status.items():
                if isinstance(status, dict) and not status.get('is_meeting_slo', True):
                    await self.alerts.send_alert(
                        "warning",
                        f"SLO violation: {slo_name} at {status.get('success_rate', 0):.2%}"
                    )
            
            # Check error budget consumption
            for slo_name, budget in error_budget_status.items():
                if isinstance(budget, dict) and budget.get('is_critical', False):
                    await self.alerts.send_alert(
                        "critical", 
                        f"Error budget critical: {slo_name} at {budget.get('budget_consumed_percent', 0):.1f}%"
                    )
            
        except Exception as e:
            logger.error("slo_status_check_failed", error=str(e))
    
    async def _metrics_reporting_loop(self):
        """Background metrics reporting."""
        while self._running and not self._shutdown_event.is_set():
            try:
                # Report current statistics
                self.metrics.gauge('scraper.stats.total_processed', self.stats['total_processed'])
                self.metrics.gauge('scraper.stats.deterministic_success', self.stats['deterministic_success'])
                self.metrics.gauge('scraper.stats.micro_ai_used', self.stats['micro_ai_used'])
                self.metrics.gauge('scraper.stats.heavy_ai_used', self.stats['heavy_ai_used'])
                self.metrics.gauge('scraper.stats.cache_hits', self.stats['cache_hits'])
                self.metrics.gauge('scraper.stats.duplicates_found', self.stats['duplicates_found'])
                
                # AI usage rates
                if self.stats['total_processed'] > 0:
                    micro_ai_rate = self.stats['micro_ai_used'] / self.stats['total_processed']
                    heavy_ai_rate = self.stats['heavy_ai_used'] / self.stats['total_processed']
                    deterministic_rate = self.stats['deterministic_success'] / self.stats['total_processed']
                    
                    self.metrics.gauge('scraper.ai.micro_usage_rate', micro_ai_rate)
                    self.metrics.gauge('scraper.ai.heavy_usage_rate', heavy_ai_rate)
                    self.metrics.gauge('scraper.parsing.deterministic_success_rate', deterministic_rate)
                
                # AI budget status
                micro_ai_stats = self.micro_ai_controller.get_usage_stats()
                heavy_ai_stats = self.heavy_ai_fallback.get_usage_stats()
                
                self.metrics.gauge('scraper.ai.micro_budget_used', micro_ai_stats['today_usage'])
                self.metrics.gauge('scraper.ai.heavy_budget_used', heavy_ai_stats['today_usage'])
                
                await asyncio.sleep(60)  # Report every minute
                
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error("metrics_reporting_error", error=str(e))
                await asyncio.sleep(60)
    
    async def shutdown(self):
        """Graceful shutdown of all components."""
        logger.info("scraper_shutdown_initiated")
        self._running = False
        self._shutdown_event.set()
        
        try:
            # Close HTTP client
            await self.http_client.close()
            
            # Close database connections
            self.sqlite_manager.close()
            
            # Close search indexer
            if hasattr(self.search_indexer, 'close'):
                self.search_indexer.close()
            
            logger.info("scraper_shutdown_completed")
            
        except Exception as e:
            logger.error("scraper_shutdown_error", error=str(e))
    
    async def _cleanup(self):
        """Final cleanup tasks."""
        try:
            # Final metrics report
            logger.info("scraper_final_stats", **self.stats)
            
            # Close all resources
            await self.shutdown()
            
        except Exception as e:
            logger.error("scraper_cleanup_error", error=str(e))


async def main():
    """Main entry point."""
    import os
    from anthropic import Anthropic
    
    # Initialize Anthropic client if API key is provided
    anthropic_client = None
    if config.anthropic_api_key:
        anthropic_client = Anthropic(api_key=config.anthropic_api_key)
    
    # Create and start scraper
    scraper = NYTScraper(anthropic_client)
    
    try:
        await scraper.start()
    except KeyboardInterrupt:
        logger.info("keyboard_interrupt_received")
    except Exception as e:
        logger.error("scraper_main_error", error=str(e))
        sys.exit(1)
    finally:
        await scraper.shutdown()


if __name__ == "__main__":
    asyncio.run(main())