"""Integration tests for the complete NYT Scraper system."""

import pytest
import asyncio
import tempfile
from datetime import datetime, timedelta
from pathlib import Path
from unittest.mock import Mock, AsyncMock, patch, MagicMock

from nyt_scraper.scraper import NYTScraper
from nyt_scraper.models import Article, ArticleStatus
from nyt_scraper.config import config


class TestFullScraperIntegration:
    """Test complete scraper functionality end-to-end."""
    
    @pytest.fixture
    def temp_directories(self):
        """Create temporary directories for testing."""
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
        import shutil
        import time
        import gc
        
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
    async def mock_scraper(self, temp_directories, mock_anthropic_client):
        """Create fully mocked scraper for integration testing."""
        
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
                
                yield scraper
                
                await scraper.shutdown()
    
    @pytest.mark.asyncio
    async def test_article_discovery_flow(self, mock_scraper):
        """Test the complete article discovery flow."""
        # Mock discovery sources
        mock_urls = [
            "https://www.nytimes.com/2024/01/15/world/test-article-1.html",
            "https://www.nytimes.com/2024/01/15/tech/test-article-2.html",
            "https://www.nytimes.com/2024/01/15/politics/test-article-3.html"
        ]
        
        mock_scraper.rss_fetcher.get_latest_articles.return_value = mock_urls[:2]
        mock_scraper.sitemap_parser.get_latest_articles.return_value = [mock_urls[2]]
        mock_scraper.search_fallback.search_recent_articles.return_value = []
        
        # Test discovery
        discovered_urls = await mock_scraper._discover_articles()
        
        assert len(discovered_urls) == 3
        assert set(discovered_urls) == set(mock_urls)
        
        # Verify discovery sources were called
        mock_scraper.rss_fetcher.get_latest_articles.assert_called_once()
        mock_scraper.sitemap_parser.get_latest_articles.assert_called_once()
    
    @pytest.mark.asyncio
    async def test_complete_article_processing_pipeline(self, mock_scraper, sample_html):
        """Test complete article processing from fetch to storage."""
        test_url = "https://www.nytimes.com/2024/01/15/test/pipeline-test.html"
        
        # Mock HTTP fetch - use AsyncMock for async method
        mock_response = Mock()
        mock_response.status_code = 200
        mock_response.content = sample_html.encode('utf-8')
        mock_response.text = sample_html
        mock_response.url = test_url
        mock_response.headers = {'content-type': 'text/html; charset=utf-8'}
        
        mock_scraper.http_client.fetch = AsyncMock(return_value=mock_response)
        
        # Mock charset handler  
        mock_scraper.charset_handler.normalize_encoding = Mock(return_value=sample_html.encode('utf-8'))
        
        # Mock compliance checks
        mock_scraper.robots_manager.can_fetch = AsyncMock(return_value=True)
        mock_scraper.per_host_budgets.can_make_request = Mock(return_value=(True, "allowed"))
        mock_scraper.circuit_breaker.is_open = Mock(return_value=False)
        mock_scraper.compliance_manager.is_operational = Mock(return_value=True)
        
        # Mock cache miss
        mock_scraper.cache_manager.get = AsyncMock(return_value=None)
        mock_scraper.cache_manager.set = AsyncMock()
        
        # Mock challenge and paywall detection
        mock_scraper._detect_challenges = Mock(return_value=False)
        mock_scraper.paywall_detector.detect = Mock(return_value=False)
        
        # Mock deduplication  
        mock_scraper.dedup_manager.is_duplicate = AsyncMock(return_value=False)
        
        # Process single article
        result = await mock_scraper._process_single_article(test_url)
        
        # Verify result
        assert result is not None
        assert isinstance(result, Article)
        assert result.title == "Test Article: Breaking News Story"
        assert result.author == "Test Author"
        assert result.section == "World"
        assert result.status == ArticleStatus.STORED
        assert result.parser_path == "json_ld"  # Should succeed with deterministic parsing
        assert result.did_use_micro_ai is False
        assert result.did_escalate_heavy is False
        
        # Verify telemetry
        assert result.word_count > 0
        assert result.parse_confidence > 0.8
    
    @pytest.mark.asyncio
    async def test_ai_escalation_pipeline(self, mock_scraper):
        """Test AI escalation pipeline when deterministic parsing fails."""
        test_url = "https://www.nytimes.com/2024/01/15/test/ai-escalation.html"
        
        # Mock minimal HTML that will trigger AI escalation
        minimal_html = "<html><head><title>Minimal</title></head><body><p>Not enough content</p></body></html>"
        
        # Mock HTTP fetch
        mock_response = Mock()
        mock_response.status_code = 200
        mock_response.content = minimal_html.encode('utf-8')
        mock_response.text = minimal_html
        mock_response.url = test_url
        mock_response.headers = {'content-type': 'text/html; charset=utf-8'}
        
        mock_scraper.http_client.fetch.return_value = mock_response
        
        # Mock compliance (all good)
        mock_scraper._check_processing_allowed = AsyncMock(return_value=True)
        mock_scraper.cache_manager.get = AsyncMock(return_value=None)
        mock_scraper.dedup_manager.is_duplicate = AsyncMock(return_value=False)
        
        # Mock AI responses
        mock_scraper.micro_ai_controller.is_available = Mock(return_value=(True, "available"))
        mock_scraper.micro_ai_controller.fill_missing_metadata = AsyncMock(return_value=(
            True, 
            {
                "author": "AI Extracted Author",
                "content": "AI extracted content with sufficient detail for processing."
            }
        ))
        mock_scraper.micro_ai_controller.validate_extracted_content = AsyncMock(return_value=(True, 0.75))
        
        # Process article
        result = await mock_scraper._process_single_article(test_url)
        
        # Verify AI escalation occurred
        assert result is not None
        assert result.did_use_micro_ai is True
        assert result.author == "AI Extracted Author"
        assert "micro_ai" in result.parser_path
        
        # Verify AI methods were called
        mock_scraper.micro_ai_controller.fill_missing_metadata.assert_called_once()
    
    @pytest.mark.asyncio
    async def test_challenge_detection_and_handling(self, mock_scraper, sample_html_with_challenges):
        """Test challenge detection and proper handling."""
        test_url = "https://www.nytimes.com/2024/01/15/test/challenge-test.html"
        
        # Mock HTTP response with challenge
        mock_response = Mock()
        mock_response.status_code = 200
        mock_response.content = sample_html_with_challenges.encode('utf-8')
        mock_response.text = sample_html_with_challenges
        mock_response.url = test_url
        mock_response.headers = {'content-type': 'text/html; charset=utf-8'}
        
        mock_scraper.http_client.fetch.return_value = mock_response
        mock_scraper._check_processing_allowed = AsyncMock(return_value=True)
        mock_scraper.cache_manager.get = AsyncMock(return_value=None)
        
        # Process article
        result = await mock_scraper._process_single_article(test_url)
        
        # Should return None due to challenge detection
        assert result is None
        
        # Verify challenge was recorded
        mock_scraper.per_host_budgets.record_challenge.assert_called_once_with(test_url)
        assert mock_scraper.stats['challenges_detected'] == 1
    
    @pytest.mark.asyncio
    async def test_paywall_detection_and_processing(self, mock_scraper, sample_html_with_paywall):
        """Test paywall detection while still processing available content."""
        test_url = "https://www.nytimes.com/2024/01/15/test/paywall-test.html"
        
        # Mock HTTP response with paywall
        mock_response = Mock()
        mock_response.status_code = 200
        mock_response.content = sample_html_with_paywall.encode('utf-8')
        mock_response.text = sample_html_with_paywall
        mock_response.url = test_url
        mock_response.headers = {'content-type': 'text/html; charset=utf-8'}
        
        mock_scraper.http_client.fetch.return_value = mock_response
        mock_scraper._check_processing_allowed = AsyncMock(return_value=True)
        mock_scraper.cache_manager.get = AsyncMock(return_value=None)
        mock_scraper.dedup_manager.is_duplicate = AsyncMock(return_value=False)
        
        # Process article
        result = await mock_scraper._process_single_article(test_url)
        
        # Should still process available content
        assert result is not None
        assert result.paywall_detected is True
        assert result.title == "Premium Article"
        assert mock_scraper.stats['paywalls_detected'] == 1
    
    @pytest.mark.asyncio
    async def test_deduplication_flow(self, mock_scraper, sample_html):
        """Test deduplication prevents duplicate storage."""
        test_url = "https://www.nytimes.com/2024/01/15/test/duplicate-test.html"
        
        # Mock successful fetch
        mock_response = Mock()
        mock_response.status_code = 200
        mock_response.content = sample_html.encode('utf-8')
        mock_response.text = sample_html
        mock_response.url = test_url
        mock_response.headers = {'content-type': 'text/html; charset=utf-8'}
        
        mock_scraper.http_client.fetch.return_value = mock_response
        mock_scraper._check_processing_allowed = AsyncMock(return_value=True)
        mock_scraper.cache_manager.get = AsyncMock(return_value=None)
        
        # Mock duplicate detection
        mock_scraper.dedup_manager.is_duplicate = AsyncMock(return_value=True)
        
        # Process article
        result = await mock_scraper._process_single_article(test_url)
        
        # Should return None due to duplicate detection
        assert result is None
        assert mock_scraper.stats['duplicates_found'] == 1
    
    @pytest.mark.asyncio
    async def test_cache_hit_flow(self, mock_scraper, sample_html):
        """Test cache hit bypasses HTTP fetch."""
        test_url = "https://www.nytimes.com/2024/01/15/test/cache-test.html"
        
        # Mock cache hit
        from nyt_scraper.models import FetchResult
        cached_result = FetchResult(
            url=test_url,
            canonical_url=test_url,
            status_code=200,
            content=sample_html,
            headers={'content-type': 'text/html; charset=utf-8'},
            encoding='utf-8',
            challenge_detected=False,
            paywall_detected=False
        )
        
        mock_scraper._check_processing_allowed = AsyncMock(return_value=True)
        mock_scraper.cache_manager.get_cached_response = AsyncMock(return_value=cached_result)
        mock_scraper.dedup_manager.is_duplicate = AsyncMock(return_value=False)
        
        # Process article
        result = await mock_scraper._process_single_article(test_url)
        
        # Should succeed without HTTP fetch
        assert result is not None
        assert result.title == "Test Article: Breaking News Story"
        assert mock_scraper.stats['cache_hits'] == 1
        
        # HTTP fetch should not have been called
        mock_scraper.http_client.fetch.assert_not_called()
    
    @pytest.mark.asyncio
    async def test_compliance_blocking(self, mock_scraper):
        """Test that compliance checks block processing when needed."""
        test_url = "https://www.nytimes.com/2024/01/15/test/blocked-test.html"
        
        # Mock compliance failure
        mock_scraper.compliance_manager.is_operational = Mock(return_value=False)
        
        # Process article
        result = await mock_scraper._process_single_article(test_url)
        
        # Should return None due to compliance block
        assert result is None
        
        # HTTP client should not have been called
        mock_scraper.http_client.fetch.assert_not_called()


class TestScraperStatistics:
    """Test scraper statistics and telemetry."""
    
    @pytest.mark.asyncio
    async def test_statistics_tracking(self, mock_scraper, sample_html):
        """Test that statistics are properly tracked."""
        initial_stats = mock_scraper.stats.copy()
        
        # Mock successful processing
        mock_response = Mock()
        mock_response.status_code = 200
        mock_response.content = sample_html.encode('utf-8')
        mock_response.text = sample_html
        mock_response.url = "https://test.com/stats"
        mock_response.headers = {'content-type': 'text/html; charset=utf-8'}
        
        mock_scraper.http_client.fetch.return_value = mock_response
        mock_scraper._check_processing_allowed = AsyncMock(return_value=True)
        mock_scraper.cache_manager.get = AsyncMock(return_value=None)
        mock_scraper.dedup_manager.is_duplicate = AsyncMock(return_value=False)
        
        # Process article
        result = await mock_scraper._process_single_article("https://test.com/stats")
        
        # Verify statistics updated
        assert mock_scraper.stats['total_processed'] == initial_stats['total_processed'] + 1
        assert mock_scraper.stats['deterministic_success'] == initial_stats['deterministic_success'] + 1
        assert result is not None


class TestScraperMaintenanceAndCleanup:
    """Test maintenance and cleanup operations."""
    
    @pytest.mark.asyncio
    async def test_daily_maintenance_operations(self, mock_scraper):
        """Test daily maintenance operations."""
        # Mock maintenance components
        mock_scraper.storage_eviction.run_full_maintenance = Mock(return_value={
            "operations": {"age_eviction": {"deleted": 10}}
        })
        mock_scraper.ai_validator.cleanup_cache = Mock(return_value=5)
        mock_scraper.slo_tracker.cleanup_old_data = Mock(return_value={"measurements_deleted": 100})
        mock_scraper.search_indexer.optimize_index = Mock()
        
        # Run maintenance
        await mock_scraper._run_daily_maintenance()
        
        # Verify all maintenance operations were called
        mock_scraper.storage_eviction.run_full_maintenance.assert_called_once()
        mock_scraper.ai_validator.cleanup_cache.assert_called_once()
        mock_scraper.slo_tracker.cleanup_old_data.assert_called_once()
        mock_scraper.search_indexer.optimize_index.assert_called_once()
    
    @pytest.mark.asyncio
    async def test_graceful_shutdown(self, mock_scraper):
        """Test graceful shutdown process."""
        # Mock component close methods
        mock_scraper.http_client.close = AsyncMock()
        mock_scraper.sqlite_manager.close = Mock()
        mock_scraper.search_indexer.close = Mock()
        
        # Shutdown
        await mock_scraper.shutdown()
        
        # Verify all components were closed
        mock_scraper.http_client.close.assert_called_once()
        mock_scraper.sqlite_manager.close.assert_called_once()
        mock_scraper.search_indexer.close.assert_called_once()


class TestScraperPerformance:
    """Test scraper performance characteristics."""
    
    @pytest.mark.asyncio
    async def test_concurrent_processing_performance(self, mock_scraper, sample_html):
        """Test concurrent article processing performance."""
        import time
        
        # Create multiple test URLs
        test_urls = [f"https://example.com/perf-test-{i}" for i in range(20)]
        
        # Mock responses for all URLs
        mock_response = Mock()
        mock_response.status_code = 200
        mock_response.content = sample_html.encode('utf-8')
        mock_response.text = sample_html
        mock_response.headers = {'content-type': 'text/html; charset=utf-8'}
        
        async def mock_fetch(url):
            mock_response.url = url
            await asyncio.sleep(0.1)  # Simulate network delay
            return mock_response
        
        mock_scraper.http_client.fetch.side_effect = mock_fetch
        mock_scraper._check_processing_allowed = AsyncMock(return_value=True)
        mock_scraper.cache_manager.get = AsyncMock(return_value=None)
        mock_scraper.dedup_manager.is_duplicate = AsyncMock(return_value=False)
        
        # Process articles concurrently
        start_time = time.time()
        await mock_scraper._process_articles(test_urls)
        duration = time.time() - start_time
        
        # Should complete much faster than sequential processing
        # With concurrency limit of 3, should take ~7 seconds vs 20 seconds sequential
        assert duration < 10.0
        assert mock_scraper.stats['total_processed'] == 20
    
    @pytest.mark.asyncio
    async def test_memory_usage_under_load(self, mock_scraper, sample_html):
        """Test memory usage under sustained load."""
        import gc
        import psutil
        import os
        
        # Get initial memory usage
        process = psutil.Process(os.getpid())
        initial_memory = process.memory_info().rss
        
        # Mock processing for many articles
        mock_response = Mock()
        mock_response.status_code = 200
        mock_response.content = sample_html.encode('utf-8')
        mock_response.text = sample_html
        mock_response.headers = {'content-type': 'text/html; charset=utf-8'}
        
        mock_scraper.http_client.fetch.return_value = mock_response
        mock_scraper._check_processing_allowed = AsyncMock(return_value=True)
        mock_scraper.cache_manager.get = AsyncMock(return_value=None)
        mock_scraper.dedup_manager.is_duplicate = AsyncMock(return_value=False)
        
        # Process many articles
        for i in range(100):
            mock_response.url = f"https://example.com/memory-test-{i}"
            await mock_scraper._process_single_article(f"https://example.com/memory-test-{i}")
            
            if i % 20 == 0:  # Force garbage collection periodically
                gc.collect()
        
        # Check final memory usage
        final_memory = process.memory_info().rss
        memory_increase = final_memory - initial_memory
        
        # Memory increase should be reasonable (less than 100MB for 100 articles)
        assert memory_increase < 100 * 1024 * 1024  # 100MB limit


class TestErrorHandlingAndRecovery:
    """Test error handling and recovery mechanisms."""
    
    @pytest.mark.asyncio
    async def test_http_error_recovery(self, mock_scraper):
        """Test recovery from HTTP errors."""
        test_url = "https://example.com/http-error-test"
        
        # Mock HTTP error
        mock_scraper.http_client.fetch.side_effect = Exception("Network timeout")
        mock_scraper._check_processing_allowed = AsyncMock(return_value=True)
        
        # Process article
        result = await mock_scraper._process_single_article(test_url)
        
        # Should handle error gracefully
        assert result is None
        
        # Circuit breaker should record failure
        mock_scraper.circuit_breaker.record_failure.assert_called_once_with(test_url)
    
    @pytest.mark.asyncio
    async def test_database_error_recovery(self, mock_scraper, sample_html):
        """Test recovery from database errors."""
        # Mock successful fetch but database error
        mock_response = Mock()
        mock_response.status_code = 200
        mock_response.content = sample_html.encode('utf-8')
        mock_response.text = sample_html
        mock_response.url = "https://example.com/db-error-test"
        mock_response.headers = {'content-type': 'text/html; charset=utf-8'}
        
        mock_scraper.http_client.fetch.return_value = mock_response
        mock_scraper._check_processing_allowed = AsyncMock(return_value=True)
        mock_scraper.cache_manager.get = AsyncMock(return_value=None)
        mock_scraper.dedup_manager.is_duplicate = AsyncMock(return_value=False)
        
        # Mock storage error
        mock_scraper.sqlite_manager.store_article = AsyncMock(side_effect=Exception("DB connection failed"))
        
        # Process article
        result = await mock_scraper._process_single_article("https://example.com/db-error-test")
        
        # Should handle error and still return article (but not store it)
        assert result is not None
        assert result.title == "Test Article: Breaking News Story"


if __name__ == "__main__":
    pytest.main([__file__, "-v"])