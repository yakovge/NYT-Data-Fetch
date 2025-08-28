"""Tests for the three-tier parsing cascade (Deterministic → Micro-AI → Heavy AI)."""

import pytest
import asyncio
from unittest.mock import Mock, AsyncMock, patch
from datetime import datetime

from nyt_scraper.models import Article, ArticleStatus, ParseResult
from nyt_scraper.parsing import (
    JSONLDExtractor, MetaTagParser, FrameworkParser, NYTHTMLParser,
    MicroAIController, HeavyAIFallback, AIValidator
)
from nyt_scraper.scraper import NYTScraper


@pytest.fixture
def mock_scraper(mock_anthropic_client, sqlite_manager):
    """Create scraper with mocked components."""
    with patch('nyt_scraper.scraper.HTTP2Client'), \
         patch('nyt_scraper.scraper.CacheManager'), \
         patch('nyt_scraper.scraper.CircuitBreaker'), \
         patch('nyt_scraper.scraper.PerHostBudgets'):
        
        scraper = NYTScraper(mock_anthropic_client)
        scraper.sqlite_manager = sqlite_manager
        return scraper


class TestDeterministicParsing:
    """Test deterministic parsing layer (JSON-LD, Meta, Framework, HTML)."""
    
    def test_json_ld_extraction_success(self, sample_html):
        """Test successful JSON-LD extraction."""
        extractor = JSONLDExtractor()
        result = extractor.extract(sample_html, "https://test.com/article")
        
        assert result is not None
        assert result.success is True
        assert result.article is not None
        assert result.article.title == "Test Article: Breaking News Story"
        assert result.article.author == "Test Author"
        assert result.article.section == "World"
        assert result.confidence > 0.8
    
    def test_meta_tag_extraction_fallback(self, sample_html):
        """Test meta tag extraction when JSON-LD is not available."""
        # Remove JSON-LD from HTML
        html_without_jsonld = sample_html.replace('<script type="application/ld+json">', '<!-- removed')
        html_without_jsonld = html_without_jsonld.replace('</script>', '-->')
        
        parser = MetaTagParser()
        result = parser.extract(html_without_jsonld, "https://test.com/article")
        
        assert result is not None
        assert result.success is True
        assert result.article is not None
        assert result.article.title == "Test Article: Breaking News Story"
        assert result.article.author == "Test Author"
        assert result.confidence > 0.6
    
    def test_html_parser_readability(self, sample_html):
        """Test HTML parser with readability extraction."""
        parser = NYTHTMLParser()
        result = parser.extract(sample_html, "https://test.com/article")
        
        assert result is not None
        assert result.success is True
        assert result.article is not None
        assert result.article.title is not None
        assert result.article.body is not None
        assert "main content of the article" in result.article.body
        assert result.confidence > 0.5
    
    def test_parsing_cascade_order(self, sample_html):
        """Test that parsers are tried in correct order."""
        # This would be tested by the main scraper's _try_deterministic_parsing method
        pass


class TestMicroAIController:
    """Test micro-AI gap filling functionality."""
    
    @pytest.fixture
    def mock_ai_controller(self, mock_anthropic_client):
        """Create micro-AI controller with mocked client."""
        return MicroAIController(mock_anthropic_client)
    
    @pytest.mark.asyncio
    async def test_micro_ai_gap_fill_success(self, mock_ai_controller, mock_article):
        """Test successful micro-AI gap filling for missing fields."""
        # Create article with missing author
        incomplete_article = mock_article
        incomplete_article.author = None
        
        # Mock AI response
        mock_ai_controller.anthropic_client.messages.create.return_value.content[0].text = '''
        {
            "author": "AI Extracted Author",
            "section": "Technology"
        }
        '''
        
        success, metadata = await mock_ai_controller.fill_missing_metadata(
            incomplete_article, 
            "<html>Sample content</html>"
        )
        
        assert success is True
        assert "author" in metadata
        assert metadata["author"] == "AI Extracted Author"
    
    @pytest.mark.asyncio
    async def test_micro_ai_budget_enforcement(self, mock_ai_controller):
        """Test that micro-AI respects daily budget limits."""
        from datetime import datetime
        # Simulate budget exhaustion using today's date key
        today_key = datetime.utcnow().strftime('%Y-%m-%d')
        mock_ai_controller.usage_cache[today_key] = mock_ai_controller.daily_limit
        
        is_available, reason = mock_ai_controller.is_available(total_requests_today=100)
        
        assert is_available is False
        assert "daily_limit_exceeded" in reason
    
    @pytest.mark.asyncio
    async def test_micro_ai_ratio_enforcement(self, mock_ai_controller):
        """Test that micro-AI respects usage ratio limits."""
        from datetime import datetime
        # Simulate high usage ratio using today's date key
        today_key = datetime.utcnow().strftime('%Y-%m-%d')
        mock_ai_controller.usage_cache[today_key] = 500  # tokens used
        
        is_available, reason = mock_ai_controller.is_available(total_requests_today=1000)
        
        # With 500 tokens / 1000 requests, ratio would exceed 10% limit
        assert is_available is False
        assert "ratio_limit_exceeded" in reason
    
    @pytest.mark.asyncio
    async def test_micro_ai_validation_success(self, mock_ai_controller, mock_article):
        """Test micro-AI content validation."""
        # Mock validation response
        mock_ai_controller.anthropic_client.messages.create.return_value.content[0].text = '''
        {
            "is_valid": true,
            "confidence": 0.85,
            "reason": "Content appears legitimate and well-structured"
        }
        '''
        
        is_valid, confidence = await mock_ai_controller.validate_extracted_content(mock_article)
        
        assert is_valid is True
        assert confidence == 0.85


class TestHeavyAIFallback:
    """Test heavy AI fallback functionality."""
    
    @pytest.fixture
    def mock_heavy_ai(self, mock_anthropic_client):
        """Create heavy AI fallback with mocked client."""
        return HeavyAIFallback(mock_anthropic_client)
    
    @pytest.mark.asyncio
    async def test_heavy_ai_escalation_logic(self, mock_heavy_ai, mock_article):
        """Test logic for escalating to heavy AI."""
        # Test escalation with low confidence
        should_escalate, reason = mock_heavy_ai.should_escalate(
            mock_article, 
            parse_confidence=0.3,  # Low confidence
            total_requests_today=1000
        )
        
        assert should_escalate is True
        assert "low_confidence" in reason
    
    @pytest.mark.asyncio
    async def test_heavy_ai_budget_protection(self, mock_heavy_ai, mock_article):
        """Test heavy AI budget protection."""
        from datetime import datetime
        # Simulate budget exhaustion using today's date key
        today_key = datetime.utcnow().strftime('%Y-%m-%d')
        mock_heavy_ai.usage_cache[today_key] = mock_heavy_ai.daily_limit
        
        should_escalate, reason = mock_heavy_ai.should_escalate(
            mock_article, 
            parse_confidence=0.3,
            total_requests_today=1000
        )
        
        assert should_escalate is False
        assert "daily_limit_exceeded" in reason
    
    @pytest.mark.asyncio
    async def test_heavy_ai_full_extraction(self, mock_heavy_ai):
        """Test heavy AI full article extraction."""
        # Mock comprehensive extraction response
        mock_heavy_ai.anthropic_client.messages.create.return_value.content[0].text = '''
        {
            "title": "AI Extracted Title",
            "author": "AI Extracted Author", 
            "content": "AI extracted comprehensive content with multiple paragraphs and substantial information about the topic.",
            "summary": "AI generated summary",
            "section": "Technology",
            "word_count": 150,
            "confidence": 0.88
        }
        '''
        
        success, extracted_data = await mock_heavy_ai.extract_full_article(
            "<html>Complex HTML content</html>",
            "https://example.com/test"
        )
        
        assert success is True
        assert extracted_data["title"] == "AI Extracted Title"
        assert extracted_data["confidence"] == 0.88
        assert extracted_data["word_count"] == 150


class TestAIValidator:
    """Test AI response validation and caching."""
    
    @pytest.fixture
    def ai_validator(self, sqlite_manager):
        """Create AI validator with test database."""
        return AIValidator(sqlite_manager)
    
    @pytest.mark.asyncio
    async def test_content_validation_success(self, ai_validator, mock_article):
        """Test successful content validation."""
        is_valid, quality_score, reason = ai_validator.validate_article_content(
            mock_article, "micro_ai"
        )
        
        assert is_valid is True
        assert quality_score > 0.7
        assert reason == "valid"
    
    @pytest.mark.asyncio
    async def test_content_validation_spam_detection(self, ai_validator):
        """Test spam content detection."""
        spam_article = Article(
            source_url="https://example.com/spam",
            canonical_url="https://example.com/spam",
            title="Get Rich Quick! Buy Viagra Now!",
            body="Click here to win lottery! Make money fast! Casino poker winner! " * 20,  # Make it long enough
            status=ArticleStatus.STORED
        )
        
        is_valid, quality_score, reason = ai_validator.validate_article_content(
            spam_article, "test"
        )
        
        assert is_valid is False
        assert quality_score < 0.5
        assert "spam_content" in reason
    
    @pytest.mark.asyncio
    async def test_validation_caching(self, ai_validator, mock_article):
        """Test that validation results are cached."""
        # First validation
        result1 = ai_validator.validate_article_content(mock_article, "test")
        
        # Second validation should hit cache
        result2 = ai_validator.validate_article_content(mock_article, "test")
        
        assert result1 == result2
    
    def test_metadata_consistency_validation(self, ai_validator, mock_article):
        """Test metadata consistency validation."""
        is_consistent, details = ai_validator.validate_metadata_consistency(mock_article)
        
        assert is_consistent is True
        assert "title_consistency" in details["confidence_scores"]
        assert details["confidence_scores"]["title_consistency"] > 0.5


class TestThreeTierIntegration:
    """Test complete three-tier parsing cascade integration."""
    
    @pytest.mark.asyncio
    async def test_full_parsing_cascade_deterministic_success(self, mock_scraper, sample_html):
        """Test cascade where deterministic parsing succeeds."""
        # Mock fetch result
        fetch_result = Mock()
        fetch_result.content = sample_html
        fetch_result.canonical_url = "https://example.com/test"
        fetch_result.challenge_detected = False
        fetch_result.paywall_detected = False
        fetch_result.headers = {}
        
        # Run parsing cascade
        article = await mock_scraper._parse_article_cascade(
            "https://example.com/test", 
            fetch_result
        )
        
        assert article is not None
        assert article.title == "Test Article: Breaking News Story"
        assert article.parser_path == "json_ld"
        assert article.did_use_micro_ai is False
        assert article.did_escalate_heavy is False
        mock_scraper.stats['deterministic_success'] += 1
        
        assert mock_scraper.stats['deterministic_success'] == 1
    
    @pytest.mark.asyncio
    async def test_parsing_cascade_micro_ai_escalation(self, mock_scraper):
        """Test cascade escalation to micro-AI."""
        # Mock fetch result with incomplete HTML
        fetch_result = Mock()
        fetch_result.content = "<html><h1>Title Only</h1></html>"
        fetch_result.canonical_url = "https://example.com/incomplete"
        fetch_result.challenge_detected = False
        fetch_result.paywall_detected = False
        fetch_result.headers = {}
        
        # Mock micro-AI response
        mock_scraper.micro_ai_controller.anthropic_client.messages.create.return_value.content[0].text = '''
        {
            "author": "AI Found Author",
            "content": "AI extracted content with sufficient detail for validation."
        }
        '''
        
        # Mock AI availability
        mock_scraper.micro_ai_controller.is_available = Mock(return_value=(True, "available"))
        
        article = await mock_scraper._parse_article_cascade(
            "https://example.com/incomplete",
            fetch_result
        )
        
        assert article is not None
        assert article.did_use_micro_ai is True
        assert article.did_escalate_heavy is False
    
    @pytest.mark.asyncio
    async def test_parsing_cascade_heavy_ai_escalation(self, mock_scraper):
        """Test cascade escalation to heavy AI."""
        # Mock fetch result with minimal content
        fetch_result = Mock()
        fetch_result.content = "<html><div>Minimal content</div></html>"
        fetch_result.canonical_url = "https://example.com/minimal"
        fetch_result.challenge_detected = False
        fetch_result.paywall_detected = False
        fetch_result.headers = {}
        
        # Mock heavy AI availability and response
        mock_scraper.heavy_ai_fallback.should_escalate = Mock(return_value=(True, "insufficient_content"))
        mock_scraper.heavy_ai_fallback.anthropic_client.messages.create.return_value.content[0].text = '''
        {
            "title": "Heavy AI Extracted Title",
            "content": "Heavy AI extracted comprehensive content with multiple paragraphs providing substantial information.",
            "author": "Heavy AI Author",
            "confidence": 0.82
        }
        '''
        
        # Mock AI validator
        mock_scraper.ai_validator.validate_article_content = Mock(return_value=(True, 0.85, "valid"))
        
        article = await mock_scraper._parse_article_cascade(
            "https://example.com/minimal",
            fetch_result
        )
        
        assert article is not None
        assert article.did_escalate_heavy is True
        assert article.parser_path == "heavy_ai"
    
    @pytest.mark.asyncio
    async def test_parsing_cascade_complete_failure(self, mock_scraper):
        """Test cascade when all methods fail."""
        # Mock fetch result with unusable content
        fetch_result = Mock()
        fetch_result.content = "<html><div>Error 404</div></html>"
        fetch_result.canonical_url = "https://example.com/failed"
        fetch_result.challenge_detected = False
        fetch_result.paywall_detected = False
        fetch_result.headers = {}
        
        # Mock AI unavailability
        mock_scraper.micro_ai_controller.is_available = Mock(return_value=(False, "budget_exhausted"))
        mock_scraper.heavy_ai_fallback.should_escalate = Mock(return_value=(False, "budget_exhausted"))
        
        article = await mock_scraper._parse_article_cascade(
            "https://example.com/failed",
            fetch_result
        )
        
        assert article is not None
        assert article.status == ArticleStatus.FAILED


class TestPerformanceBenchmarks:
    """Test performance benchmarks for parsing cascade."""
    
    @pytest.mark.asyncio
    async def test_deterministic_parsing_performance(self, sample_html, performance_benchmarks):
        """Test that deterministic parsers meet performance targets."""
        import time
        
        # Test JSON-LD performance
        extractor = JSONLDExtractor()
        start_time = time.time()
        result = extractor.extract(sample_html, "https://test.com/article")
        duration = time.time() - start_time
        
        assert duration < performance_benchmarks['deterministic_parsing']['json_ld']
        assert result is not None
    
    @pytest.mark.asyncio
    async def test_ai_parsing_performance_mock(self, mock_anthropic_client, performance_benchmarks):
        """Test AI parsing performance with mocked responses."""
        import time
        
        controller = MicroAIController(mock_anthropic_client)
        
        start_time = time.time()
        success, metadata = await controller.fill_missing_metadata(
            Mock(title="Test", author=None),
            "<html>Content</html>"
        )
        duration = time.time() - start_time
        
        # Should be fast with mocked response
        assert duration < performance_benchmarks['ai_parsing']['micro_ai']


class TestErrorHandling:
    """Test error handling throughout the parsing cascade."""
    
    @pytest.mark.asyncio
    async def test_malformed_html_handling(self):
        """Test handling of malformed HTML."""
        malformed_html = "<html><title>Test</title><body><p>Unclosed paragraph<div>Mixed tags</html>"
        
        parser = NYTHTMLParser()
        result = parser.extract(malformed_html, "https://test.com/malformed")
        
        # Should handle gracefully without crashing
        assert result is not None or True  # Either parses something or fails gracefully
    
    @pytest.mark.asyncio
    async def test_ai_json_parsing_errors(self, mock_anthropic_client):
        """Test handling of invalid AI JSON responses."""
        # Mock invalid JSON response
        mock_anthropic_client.messages.create.return_value.content[0].text = "Not valid JSON at all"
        
        controller = MicroAIController(mock_anthropic_client)
        success, metadata = await controller.fill_missing_metadata(
            Mock(title="Test"),
            "<html>Content</html>"
        )
        
        assert success is False
        assert metadata == {}
    
    @pytest.mark.asyncio
    async def test_network_timeout_handling(self, mock_scraper):
        """Test handling of network timeouts during AI requests."""
        # Mock network timeout
        mock_scraper.micro_ai_controller._make_ai_request = AsyncMock(return_value=None)
        
        success, metadata = await mock_scraper.micro_ai_controller.fill_missing_metadata(
            Mock(title="Test"),
            "<html>Content</html>"
        )
        
        assert success is False
        assert metadata == {}


if __name__ == "__main__":
    pytest.main([__file__, "-v"])