"""Configuration management for NYT Scraper."""

import os
from dataclasses import dataclass, field
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
    
    # Website Configuration
    website_config: str = os.getenv("WEBSITE_CONFIG", "nytimes")
    custom_config_path: Optional[str] = os.getenv("CUSTOM_CONFIG_PATH")
    
    # Per-Host Budgets
    per_host_max_requests_per_day: int = int(os.getenv("PER_HOST_MAX_REQUESTS_PER_DAY", "1000"))
    per_host_max_bytes_per_day: int = int(os.getenv("PER_HOST_MAX_BYTES_PER_DAY", "100000000"))
    challenge_cooldown_hours: int = int(os.getenv("CHALLENGE_COOLDOWN_HOURS", "24"))
    
    # FTS5 Toggle
    use_sqlite_fts5: bool = os.getenv("USE_SQLITE_FTS5", "false").lower() == "true"
    fts5_benchmark_threshold_qps: int = int(os.getenv("FTS5_BENCHMARK_THRESHOLD_QPS", "100"))
    
    # Service Level Objectives
    slo_deterministic_success_rate: float = float(os.getenv("SLO_DETERMINISTIC_SUCCESS_RATE", "0.95"))
    slo_ai_usage_rate: float = float(os.getenv("SLO_AI_USAGE_RATE", "0.01"))
    slo_freshness_lag_minutes: int = int(os.getenv("SLO_FRESHNESS_LAG_MINUTES", "60"))
    
    # Micro-AI (Gap Fill)
    ai_min_enabled: bool = os.getenv("AI_MIN_ENABLED", "true").lower() == "true"
    ai_min_model: str = os.getenv("AI_MIN_MODEL", "claude-3-haiku")
    ai_min_max_tokens: int = int(os.getenv("AI_MIN_MAX_TOKENS", "300"))
    ai_min_temperature: float = float(os.getenv("AI_MIN_TEMPERATURE", "0"))
    ai_min_snippet_bytes: int = int(os.getenv("AI_MIN_SNIPPET_BYTES", "3000"))
    ai_min_daily_limit: int = int(os.getenv("AI_MIN_DAILY_LIMIT", "30000"))
    ai_min_ratio: float = float(os.getenv("AI_MIN_RATIO", "0.10"))
    
    # Heavy AI (Full Analysis)
    ai_heavy_enabled: bool = os.getenv("AI_HEAVY_ENABLED", "true").lower() == "true"
    ai_heavy_model: str = os.getenv("AI_HEAVY_MODEL", "claude-3-haiku")
    ai_heavy_max_tokens: int = int(os.getenv("AI_HEAVY_MAX_TOKENS", "1200"))
    ai_heavy_temperature: float = float(os.getenv("AI_HEAVY_TEMPERATURE", "0"))
    ai_heavy_daily_limit: int = int(os.getenv("AI_HEAVY_DAILY_LIMIT", "6000"))
    ai_heavy_ratio: float = float(os.getenv("AI_HEAVY_RATIO", "0.005"))
    
    # AI Budget & Monitoring
    ai_total_daily_limit: int = int(os.getenv("AI_TOTAL_DAILY_LIMIT", "36000"))
    ai_alert_threshold: float = float(os.getenv("AI_ALERT_THRESHOLD", "0.8"))
    ai_budget_reset_hour: int = int(os.getenv("AI_BUDGET_RESET_HOUR", "0"))
    
    # Legacy AI config (for backward compatibility)
    ai_model: str = os.getenv("AI_MODEL", "claude-3-haiku")
    ai_daily_token_limit: int = int(os.getenv("AI_DAILY_TOKEN_LIMIT", "10000"))
    ai_fallback_ratio: float = float(os.getenv("AI_FALLBACK_RATIO", "0.01"))
    anthropic_api_key: Optional[str] = os.getenv("ANTHROPIC_API_KEY")
    ai_triggers_dir: Path = Path("./data/ai_triggers")
    
    # Challenge Detection
    challenge_max_retries: int = int(os.getenv("CHALLENGE_MAX_RETRIES", "1"))
    
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
    user_agents: list[str] = field(default_factory=lambda: [
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36", 
        "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36"
    ])
    
    # Directory paths (mockable for testing)
    data_dir: Path = field(default_factory=lambda: Path("./data"))
    
    def __post_init__(self) -> None:
        """Create required directories if they don't exist."""
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self.sqlite_db_path.parent.mkdir(parents=True, exist_ok=True)
        self.log_archive_dir.mkdir(parents=True, exist_ok=True)
        
    def is_operational(self) -> bool:
        """Check if scraper should operate based on kill switch."""
        if self.kill_switch:
            return False
        return True


config = Config()