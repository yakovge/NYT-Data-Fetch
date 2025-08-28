"""Configuration management for NYT Scraper."""

import os
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

from dotenv import load_dotenv

load_dotenv()


@dataclass
class Config:
    """Central configuration for the NYT scraper."""
    
    # Rate limiting
    request_delay_min: float = float(os.getenv("REQUEST_DELAY_MIN", "1"))
    request_delay_max: float = float(os.getenv("REQUEST_DELAY_MAX", "5"))
    max_concurrent_requests: int = int(os.getenv("MAX_CONCURRENT_REQUESTS", "3"))
    
    # Caching
    cache_ttl_hours: int = int(os.getenv("CACHE_TTL_HOURS", "24"))
    sqlite_db_path: Path = Path(os.getenv("SQLITE_DB_PATH", "./data/nyt_cache.db"))
    robots_cache_db: Path = Path("./data/robots_cache.db")
    ai_usage_db: Path = Path("./data/ai_usage.db")
    
    # AI Fallback
    ai_model: str = os.getenv("AI_MODEL", "claude-3-haiku")
    ai_daily_token_limit: int = int(os.getenv("AI_DAILY_TOKEN_LIMIT", "10000"))
    ai_fallback_ratio: float = float(os.getenv("AI_FALLBACK_RATIO", "0.01"))
    anthropic_api_key: Optional[str] = os.getenv("ANTHROPIC_API_KEY")
    ai_triggers_dir: Path = Path("./data/ai_triggers")
    
    # Monitoring
    slack_webhook: Optional[str] = os.getenv("SLACK_WEBHOOK")
    prometheus_port: int = int(os.getenv("PROMETHEUS_PORT", "9090"))
    log_file: Path = Path("./logs/scraper.jsonl")
    log_archive_dir: Path = Path("./logs/archive")
    
    # Compliance
    kill_switch: bool = os.getenv("KILL_SWITCH", "false").lower() == "true"
    dry_run: bool = os.getenv("DRY_RUN", "false").lower() == "true"
    enable_headless: bool = os.getenv("ENABLE_HEADLESS", "false").lower() == "true"
    
    # Storage
    max_db_size_gb: float = float(os.getenv("MAX_DB_SIZE_GB", "1"))
    retention_days: int = int(os.getenv("RETENTION_DAYS", "30"))
    
    # URLs
    nyt_base_url: str = "https://www.nytimes.com"
    rss_base_url: str = "https://rss.nytimes.com/services/xml/rss/nyt/"
    wayback_api: str = "https://archive.org/wayback/available"
    
    # User agents rotation
    user_agents: list[str] = [
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36",
        "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36"
    ]
    
    def __post_init__(self) -> None:
        """Create required directories if they don't exist."""
        self.sqlite_db_path.parent.mkdir(parents=True, exist_ok=True)
        self.log_file.parent.mkdir(parents=True, exist_ok=True)
        self.log_archive_dir.mkdir(parents=True, exist_ok=True)
        self.ai_triggers_dir.mkdir(parents=True, exist_ok=True)
        
    def is_operational(self) -> bool:
        """Check if scraper should operate based on kill switch."""
        if self.kill_switch:
            return False
        return True


config = Config()