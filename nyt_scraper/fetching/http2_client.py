"""HTTP/2 client with Brotli compression and optimizations."""

import random
import time
from typing import Dict, List, Optional, Tuple
from urllib.parse import urlparse

import chardet
import httpx
from tenacity import (
    retry,
    retry_if_exception_type,
    stop_after_attempt,
    wait_exponential
)

from ..config import config
from ..models import FetchResult
from ..monitoring import MetricsCollector, get_logger
from .cache_manager import CacheManager
from .charset_handler import CharsetHandler
from .circuit_breaker import CircuitBreaker

logger = get_logger(__name__)


class HTTP2Client:
    """HTTP/2 client with advanced features."""
    
    def __init__(self, metrics: MetricsCollector):
        self.metrics = metrics
        self.cache_manager = CacheManager()
        self.charset_handler = CharsetHandler()
        self.circuit_breaker = CircuitBreaker(metrics)
        
        # Configure HTTP client
        self.client_config = {
            "http2": True,
            "timeout": httpx.Timeout(
                connect=10.0,
                read=30.0,
                write=10.0,
                pool=5.0
            ),
            "limits": httpx.Limits(
                max_connections=100,
                max_keepalive_connections=20,
                keepalive_expiry=30
            ),
            "follow_redirects": True,
            "max_redirects": 5
        }
    
    def _get_headers(self) -> Dict[str, str]:
        """Get request headers with random User-Agent."""
        return {
            "User-Agent": random.choice(config.user_agents),
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            "Accept-Language": "en-US,en;q=0.9",
            "Accept-Encoding": "br, gzip, deflate",
            "DNT": "1",
            "Connection": "keep-alive",
            "Upgrade-Insecure-Requests": "1"
        }
    
    @retry(
        retry=retry_if_exception_type(httpx.TimeoutException),
        stop=stop_after_attempt(3),
        wait=wait_exponential(multiplier=1, min=2, max=10)
    )
    async def fetch(
        self,
        url: str,
        use_cache: bool = True,
        headers_only: bool = False
    ) -> FetchResult:
        """
        Fetch URL with HTTP/2 and optimizations.
        
        Args:
            url: URL to fetch
            use_cache: Whether to use cache
            headers_only: Only fetch headers (for dry-run mode)
            
        Returns:
            FetchResult with content and metadata
        """
        start_time = time.time()
        parsed_url = urlparse(url)
        host = parsed_url.netloc
        
        # Check circuit breaker
        if not self.circuit_breaker.can_request(host):
            logger.warning("circuit_breaker_open", host=host, url=url)
            return FetchResult(
                url=url,
                status_code=429,
                error="Circuit breaker open",
                duration_ms=(time.time() - start_time) * 1000
            )
        
        # Check cache
        if use_cache:
            cached = await self.cache_manager.get(url)
            if cached:
                self.metrics.record_cache_hit(True)
                return FetchResult(
                    url=url,
                    status_code=200,
                    content=cached["content"],
                    headers=cached["headers"],
                    from_cache=True,
                    duration_ms=(time.time() - start_time) * 1000
                )
            else:
                self.metrics.record_cache_hit(False)
        
        # Prepare request
        headers = self._get_headers()
        
        # Add cache headers if available
        cached_headers = await self.cache_manager.get_cache_headers(url)
        if cached_headers:
            if "etag" in cached_headers:
                headers["If-None-Match"] = cached_headers["etag"]
            if "last_modified" in cached_headers:
                headers["If-Modified-Since"] = cached_headers["last_modified"]
        
        try:
            async with httpx.AsyncClient(**self.client_config) as client:
                with self.metrics.track_active_request():
                    # Perform request
                    method = "HEAD" if headers_only else "GET"
                    response = await client.request(
                        method,
                        url,
                        headers=headers
                    )
                    
                    # Track redirects
                    redirect_chain = []
                    if hasattr(response, "history"):
                        redirect_chain = [str(r.url) for r in response.history]
                    
                    # Handle different status codes
                    result = await self._handle_response(
                        response,
                        url,
                        redirect_chain,
                        start_time
                    )
                    
                    # Update circuit breaker
                    self.circuit_breaker.record_response(
                        host,
                        response.status_code
                    )
                    
                    # Cache if successful
                    if result.is_success and use_cache:
                        await self.cache_manager.set(
                            url,
                            result.content,
                            result.headers
                        )
                    
                    return result
                    
        except httpx.TimeoutException as e:
            logger.error("fetch_timeout", url=url, error=str(e))
            self.circuit_breaker.record_failure(host)
            return FetchResult(
                url=url,
                status_code=0,
                error=f"Timeout: {e}",
                duration_ms=(time.time() - start_time) * 1000
            )
            
        except Exception as e:
            logger.error("fetch_error", url=url, error=str(e))
            self.circuit_breaker.record_failure(host)
            return FetchResult(
                url=url,
                status_code=0,
                error=str(e),
                duration_ms=(time.time() - start_time) * 1000
            )
    
    async def _handle_response(
        self,
        response: httpx.Response,
        url: str,
        redirect_chain: List[str],
        start_time: float
    ) -> FetchResult:
        """Handle HTTP response based on status code."""
        status = response.status_code
        headers = dict(response.headers)
        
        # 304 Not Modified - use cached version
        if status == 304:
            cached = await self.cache_manager.get(url)
            if cached:
                return FetchResult(
                    url=url,
                    status_code=304,
                    content=cached["content"],
                    headers=headers,
                    redirect_chain=redirect_chain,
                    from_cache=True,
                    duration_ms=(time.time() - start_time) * 1000
                )
        
        # 410 Gone - permanent failure
        if status == 410:
            logger.warning("url_gone", url=url)
            return FetchResult(
                url=url,
                status_code=410,
                error="URL permanently gone",
                redirect_chain=redirect_chain,
                duration_ms=(time.time() - start_time) * 1000
            )
        
        # 451 Unavailable for Legal Reasons (paywall)
        if status == 451:
            logger.warning("paywall_status", url=url)
            return FetchResult(
                url=url,
                status_code=451,
                error="Paywall detected",
                redirect_chain=redirect_chain,
                duration_ms=(time.time() - start_time) * 1000
            )
        
        # 429 Too Many Requests or 403 Forbidden
        if status in (429, 403):
            retry_after = response.headers.get("Retry-After")
            logger.warning(
                "rate_limited",
                url=url,
                status=status,
                retry_after=retry_after
            )
            
            self.metrics.record_rate_limit(
                urlparse(url).netloc,
                status
            )
            
            return FetchResult(
                url=url,
                status_code=status,
                error=f"Rate limited: {status}",
                headers=headers,
                redirect_chain=redirect_chain,
                duration_ms=(time.time() - start_time) * 1000
            )
        
        # Success (2xx)
        if 200 <= status < 300:
            # Check content size
            content_length = response.headers.get("Content-Length")
            if content_length and int(content_length) > 2 * 1024 * 1024:
                logger.warning(
                    "content_too_large",
                    url=url,
                    size_mb=int(content_length) / 1024 / 1024
                )
                return FetchResult(
                    url=url,
                    status_code=status,
                    error="Content too large",
                    headers=headers,
                    redirect_chain=redirect_chain,
                    duration_ms=(time.time() - start_time) * 1000
                )
            
            # Get content
            content = await response.aread()
            
            # Validate and normalize encoding
            content = self.charset_handler.normalize_encoding(
                content,
                headers.get("Content-Type")
            )
            
            return FetchResult(
                url=url,
                status_code=status,
                content=content,
                headers=headers,
                redirect_chain=redirect_chain,
                duration_ms=(time.time() - start_time) * 1000
            )
        
        # Other status codes
        return FetchResult(
            url=url,
            status_code=status,
            error=f"HTTP {status}: {response.reason_phrase}",
            headers=headers,
            redirect_chain=redirect_chain,
            duration_ms=(time.time() - start_time) * 1000
        )
    
    async def batch_fetch(
        self,
        urls: List[str],
        max_concurrent: Optional[int] = None
    ) -> List[FetchResult]:
        """
        Fetch multiple URLs with concurrency control.
        
        Args:
            urls: List of URLs to fetch
            max_concurrent: Maximum concurrent requests
            
        Returns:
            List of FetchResult objects
        """
        import asyncio
        
        max_concurrent = max_concurrent or config.max_concurrent_requests
        semaphore = asyncio.Semaphore(max_concurrent)
        
        async def fetch_with_semaphore(url: str) -> FetchResult:
            async with semaphore:
                return await self.fetch(url)
        
        tasks = [fetch_with_semaphore(url) for url in urls]
        return await asyncio.gather(*tasks)
    
    async def close(self):
        """Clean up resources."""
        await self.cache_manager.close()