"""Basic tests to ensure core functionality works."""

import pytest
from nyt_scraper.models import Article, ArticleStatus, ParseMethod
from nyt_scraper.config import Config


class TestBasicModels:
    """Test basic model functionality."""
    
    def test_article_creation(self):
        """Test creating an Article instance."""
        article = Article(
            source_url="https://test.com/article",
            title="Test Article",
            body="Test content"
        )
        assert article.source_url == "https://test.com/article"
        assert article.title == "Test Article"
        assert article.body == "Test content"
        assert article.status == ArticleStatus.DISCOVERED
    
    def test_article_with_metadata(self):
        """Test Article with metadata fields."""
        article = Article(
            source_url="https://test.com/article",
            title="Test Article",
            body="Test content",
            author="Test Author",
            section="Test Section",
            tags=["test", "example"]
        )
        assert article.author == "Test Author"
        assert article.section == "Test Section"
        assert len(article.tags) == 2
    
    def test_article_to_dict(self):
        """Test converting Article to dictionary."""
        article = Article(
            source_url="https://test.com/article",
            title="Test Article",
            body="Test content"
        )
        data = article.to_dict()
        assert data["source_url"] == "https://test.com/article"
        assert data["title"] == "Test Article"
        assert data["body"] == "Test content"
    
    def test_article_status_enum(self):
        """Test ArticleStatus enum values."""
        assert ArticleStatus.DISCOVERED.value == "discovered"
        assert ArticleStatus.FETCHING.value == "fetching"
        assert ArticleStatus.PARSING.value == "parsing"
        assert ArticleStatus.STORED.value == "stored"
        assert ArticleStatus.FAILED.value == "failed"
        assert ArticleStatus.PAYWALL.value == "paywall"
        assert ArticleStatus.RATE_LIMITED.value == "rate_limited"
    
    def test_parse_method_enum(self):
        """Test ParseMethod enum values."""
        assert ParseMethod.JSON_LD.value == "json_ld"
        assert ParseMethod.META_TAGS.value == "meta_tags"
        assert ParseMethod.HTML.value == "html"
        assert ParseMethod.AI_FALLBACK.value == "ai_fallback"


class TestBasicConfig:
    """Test basic configuration."""
    
    def test_config_creation(self):
        """Test creating a Config instance."""
        config = Config()
        assert config.request_delay_min >= 0
        assert config.request_delay_max >= config.request_delay_min
        assert config.max_concurrent_requests > 0
    
    def test_config_paths(self):
        """Test configuration paths."""
        config = Config()
        assert config.sqlite_db_path is not None
        assert config.data_dir is not None
    
    def test_config_ai_settings(self):
        """Test AI configuration settings."""
        config = Config()
        assert config.ai_daily_token_limit >= 0
        assert 0 <= config.ai_fallback_ratio <= 1
    

class TestBasicArithmetic:
    """Simple tests to ensure pytest is working correctly."""
    
    def test_addition(self):
        """Test basic addition."""
        assert 2 + 2 == 4
    
    def test_multiplication(self):
        """Test basic multiplication."""
        assert 3 * 4 == 12
    
    def test_division(self):
        """Test basic division."""
        assert 10 / 2 == 5
    
    def test_boolean_logic(self):
        """Test boolean logic."""
        assert True and True
        assert not (True and False)
        assert True or False
        assert not False
    
    def test_string_operations(self):
        """Test string operations."""
        assert "hello" + " world" == "hello world"
        assert "test".upper() == "TEST"
        assert "TEST".lower() == "test"
        assert "hello world".split() == ["hello", "world"]
    
    def test_list_operations(self):
        """Test list operations."""
        test_list = [1, 2, 3]
        assert len(test_list) == 3
        assert test_list[0] == 1
        assert test_list[-1] == 3
        assert sum(test_list) == 6
    
    def test_dict_operations(self):
        """Test dictionary operations."""
        test_dict = {"key": "value", "number": 42}
        assert test_dict["key"] == "value"
        assert test_dict.get("number") == 42
        assert len(test_dict) == 2
        assert "key" in test_dict