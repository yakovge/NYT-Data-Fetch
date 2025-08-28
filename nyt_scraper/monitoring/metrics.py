"""Prometheus metrics collection."""

import time
from contextlib import contextmanager
from typing import Dict, Optional

from prometheus_client import Counter, Gauge, Histogram, Summary, start_http_server

from ..config import config
from .structured_logger import get_logger

logger = get_logger(__name__)

# Define metrics
scraper_requests_total = Counter(
    'scraper_requests_total',
    'Total number of scraper requests',
    ['status', 'parser']
)

articles_processed_total = Counter(
    'articles_processed_total',
    'Total articles processed',
    ['parser', 'status']
)

cache_hit_rate = Gauge(
    'cache_hit_rate',
    'Cache hit rate percentage'
)

ai_budget_used_percent = Gauge(
    'ai_budget_used_percent',
    'Percentage of AI budget used'
)

fetch_duration_seconds = Histogram(
    'fetch_duration_seconds',
    'HTTP fetch duration in seconds',
    buckets=[0.1, 0.25, 0.5, 1.0, 2.5, 5.0, 10.0]
)

parse_duration_seconds = Histogram(
    'parse_duration_seconds',
    'Parse duration in seconds by method',
    ['method'],
    buckets=[0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1.0]
)

circuit_breaker_trips = Counter(
    'circuit_breaker_trips_total',
    'Total circuit breaker trips',
    ['host', 'reason']
)

rate_limit_events = Counter(
    'rate_limit_events_total',
    'Total rate limiting events',
    ['host', 'status_code']
)

storage_size_bytes = Gauge(
    'storage_size_bytes',
    'Database size in bytes'
)

articles_in_storage = Gauge(
    'articles_in_storage',
    'Number of articles in storage'
)

active_requests = Gauge(
    'active_requests',
    'Number of active requests'
)


class MetricsCollector:
    """Collects and exposes metrics for monitoring."""
    
    def __init__(self):
        self.start_time = time.time()
        self.stats = {
            "total_requests": 0,
            "successful_requests": 0,
            "failed_requests": 0,
            "cache_hits": 0,
            "cache_misses": 0,
            "ai_calls": 0,
            "ai_tokens_used": 0,
            "parse_success_by_method": {},
            "parse_failures_by_method": {},
        }
        self._start_server()
    
    def _start_server(self):
        """Start Prometheus metrics server."""
        try:
            start_http_server(config.prometheus_port)
            logger.info(
                "metrics_server_started",
                port=config.prometheus_port
            )
        except Exception as e:
            logger.error(
                "metrics_server_failed",
                error=str(e)
            )
    
    def record_request(self, status: str, parser: Optional[str] = None):
        """Record a scraper request."""
        self.stats["total_requests"] += 1
        
        if status == "success":
            self.stats["successful_requests"] += 1
        else:
            self.stats["failed_requests"] += 1
        
        scraper_requests_total.labels(
            status=status,
            parser=parser or "none"
        ).inc()
    
    def record_article(self, parser: str, status: str):
        """Record article processing."""
        articles_processed_total.labels(
            parser=parser,
            status=status
        ).inc()
        
        if status == "success":
            self.stats["parse_success_by_method"][parser] = \
                self.stats["parse_success_by_method"].get(parser, 0) + 1
        else:
            self.stats["parse_failures_by_method"][parser] = \
                self.stats["parse_failures_by_method"].get(parser, 0) + 1
    
    def record_cache_hit(self, hit: bool):
        """Record cache hit/miss."""
        if hit:
            self.stats["cache_hits"] += 1
        else:
            self.stats["cache_misses"] += 1
        
        # Update cache hit rate gauge
        total = self.stats["cache_hits"] + self.stats["cache_misses"]
        if total > 0:
            rate = (self.stats["cache_hits"] / total) * 100
            cache_hit_rate.set(rate)
    
    def record_ai_usage(self, tokens: int, daily_limit: int):
        """Record AI API usage."""
        self.stats["ai_calls"] += 1
        self.stats["ai_tokens_used"] += tokens
        
        # Update AI budget gauge
        percent_used = (self.stats["ai_tokens_used"] / daily_limit) * 100
        ai_budget_used_percent.set(percent_used)
    
    @contextmanager
    def measure_fetch_time(self):
        """Context manager to measure fetch duration."""
        start = time.time()
        try:
            yield
        finally:
            duration = time.time() - start
            fetch_duration_seconds.observe(duration)
    
    @contextmanager
    def measure_parse_time(self, method: str):
        """Context manager to measure parse duration."""
        start = time.time()
        try:
            yield
        finally:
            duration = time.time() - start
            parse_duration_seconds.labels(method=method).observe(duration)
    
    def record_circuit_breaker(self, host: str, reason: str):
        """Record circuit breaker trip."""
        circuit_breaker_trips.labels(
            host=host,
            reason=reason
        ).inc()
    
    def record_rate_limit(self, host: str, status_code: int):
        """Record rate limiting event."""
        rate_limit_events.labels(
            host=host,
            status_code=str(status_code)
        ).inc()
    
    def update_storage_metrics(self, size_bytes: int, article_count: int):
        """Update storage metrics."""
        storage_size_bytes.set(size_bytes)
        articles_in_storage.set(article_count)
    
    @contextmanager
    def track_active_request(self):
        """Track active requests."""
        active_requests.inc()
        try:
            yield
        finally:
            active_requests.dec()
    
    def get_summary(self) -> Dict:
        """Get metrics summary."""
        uptime_hours = (time.time() - self.start_time) / 3600
        
        total = self.stats["total_requests"]
        cache_total = self.stats["cache_hits"] + self.stats["cache_misses"]
        
        return {
            "uptime_hours": round(uptime_hours, 2),
            "total_requests": total,
            "success_rate": (
                self.stats["successful_requests"] / max(1, total)
            ) * 100,
            "cache_hit_rate": (
                self.stats["cache_hits"] / max(1, cache_total)
            ) * 100,
            "ai_calls": self.stats["ai_calls"],
            "ai_tokens_used": self.stats["ai_tokens_used"],
            "ai_usage_rate": (
                self.stats["ai_calls"] / max(1, total)
            ) * 100,
            "parse_methods": self.stats["parse_success_by_method"],
            "active_requests": active_requests._value.get(),
        }
    
    def calculate_parse_success_rate(self) -> float:
        """Calculate overall parse success rate without AI."""
        non_ai_success = sum(
            count for method, count in self.stats["parse_success_by_method"].items()
            if method != "ai_fallback"
        )
        
        non_ai_total = non_ai_success + sum(
            count for method, count in self.stats["parse_failures_by_method"].items()
            if method != "ai_fallback"
        )
        
        if non_ai_total == 0:
            return 0.0
        
        return (non_ai_success / non_ai_total) * 100