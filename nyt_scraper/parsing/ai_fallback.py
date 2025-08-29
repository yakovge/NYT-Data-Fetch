"""AI fallback with configurable AI providers and hard budget caps (~1000ms, last resort)."""

import json
import sqlite3
import time
from datetime import datetime, date
from pathlib import Path
from typing import Dict, Optional

import jsonschema
from selectolax.parser import HTMLParser

from ..config import config
from ..monitoring import MetricsCollector, get_logger
from ..models import Article, ParseMethod, ParseResult
from ..ai import AIClient

logger = get_logger(__name__)


class AIFallback:
    """AI-powered article extraction with strict budget enforcement."""
    
    def __init__(self, metrics: MetricsCollector, ai_client: Optional[AIClient] = None):
        self.metrics = metrics
        self.ai_client = ai_client or AIClient()
        self.budget_db_path = config.ai_usage_db
        self._init_budget_database()
        
        # For backward compatibility, keep client reference
        self.client = self.ai_client
        
        # AI configuration from implementation plan
        self.ai_config = {
            "model": "claude-3-haiku-20240307",
            "temperature": 0,
            "max_tokens": 500,
            "html_max_chars": 5000,
            "output_format": "json_only"
        }
        
        # Hard budget limits
        self.budget_limits = {
            "daily_tokens": config.ai_daily_token_limit,
            "calls_per_minute": 10,
            "total_calls_limit": 1000,
            "alert_threshold": 0.8  # 80% usage triggers alert
        }
        
        # JSON schema for validation
        self.response_schema = {
            "type": "object",
            "properties": {
                "title": {"type": "string"},
                "author": {"type": ["string", "null"]},
                "published_date": {"type": ["string", "null"]},
                "body": {"type": "string"}
            },
            "required": ["title", "body"],
            "additionalProperties": False
        }
        
        # Fallback response for over-budget or errors
        self.fallback_response = {
            "title": "",
            "body": "",
            "author": None,
            "published_date": None
        }
    
    def _init_budget_database(self):
        """Initialize AI usage tracking database."""
        self.budget_db_path.parent.mkdir(parents=True, exist_ok=True)
        
        with sqlite3.connect(self.budget_db_path) as conn:
            conn.execute("""
                CREATE TABLE IF NOT EXISTS ai_usage (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    date DATE NOT NULL,
                    tokens_used INTEGER DEFAULT 0,
                    calls_made INTEGER DEFAULT 0,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    UNIQUE(date)
                )
            """)
            
            conn.execute("""
                CREATE TABLE IF NOT EXISTS ai_calls (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    timestamp TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    url TEXT,
                    tokens_used INTEGER,
                    success BOOLEAN,
                    error TEXT
                )
            """)
            
            # Create today's record if it doesn't exist
            today = date.today()
            conn.execute("""
                INSERT OR IGNORE INTO ai_usage (date, tokens_used, calls_made)
                VALUES (?, 0, 0)
            """, (today,))
    
    
    def extract(self, html: str, url: str) -> ParseResult:
        """
        Extract article using AI with budget enforcement.
        
        Args:
            html: HTML content
            url: Source URL
            
        Returns:
            ParseResult with extracted article or budget error
        """
        start_time = time.time()
        
        # Check if AI client is available
        if not self.ai_client or not self.ai_client.current_provider:
            return ParseResult(
                success=False,
                error="AI client not available",
                duration_ms=(time.time() - start_time) * 1000,
                confidence=0.0
            )
        
        # Check budget before making call
        budget_check = self._check_budget_limits()
        if not budget_check["can_proceed"]:
            logger.warning(
                "ai_budget_exceeded",
                reason=budget_check["reason"],
                usage=budget_check["usage"]
            )
            
            return ParseResult(
                success=False,
                error=f"AI budget exceeded: {budget_check['reason']}",
                duration_ms=(time.time() - start_time) * 1000,
                confidence=0.0
            )
        
        try:
            # Prepare HTML for AI processing
            processed_html = self._prepare_html_for_ai(html)
            
            # Make AI request (now async)
            import asyncio
            try:
                loop = asyncio.get_event_loop()
                if loop.is_running():
                    # If we're in an async context, create a task
                    import concurrent.futures
                    with concurrent.futures.ThreadPoolExecutor() as executor:
                        future = executor.submit(asyncio.run, self._make_ai_request(processed_html, url))
                        ai_response = future.result()
                else:
                    ai_response = loop.run_until_complete(self._make_ai_request(processed_html, url))
            except RuntimeError:
                # No event loop, create new one
                ai_response = asyncio.run(self._make_ai_request(processed_html, url))
            
            if ai_response["success"]:
                # Parse and validate response
                article = self._parse_ai_response(ai_response["data"], url)
                
                if article:
                    # Record successful usage
                    self._record_ai_usage(
                        url=url,
                        tokens_used=ai_response.get("tokens_used", 0),
                        success=True
                    )
                    
                    duration = (time.time() - start_time) * 1000
                    
                    logger.info(
                        "ai_extraction_success",
                        url=url,
                        title_length=len(article.title),
                        body_length=len(article.body),
                        tokens_used=ai_response.get("tokens_used", 0),
                        duration_ms=duration
                    )
                    
                    # Update metrics
                    self.metrics.record_ai_usage(
                        ai_response.get("tokens_used", 0),
                        self.budget_limits["daily_tokens"]
                    )
                    
                    # Sample AI trigger for analysis (10% rate)
                    if self._should_sample_trigger():
                        self._store_ai_trigger_sample(html, url)
                    
                    return ParseResult(
                        success=True,
                        article=article,
                        parse_method=ParseMethod.AI_FALLBACK,
                        duration_ms=duration,
                        confidence=0.95  # High confidence for AI
                    )
            
            # Record failed usage
            self._record_ai_usage(
                url=url,
                tokens_used=0,
                success=False,
                error=ai_response.get("error", "Unknown AI error")
            )
            
            return ParseResult(
                success=False,
                error=f"AI extraction failed: {ai_response.get('error', 'Unknown error')}",
                duration_ms=(time.time() - start_time) * 1000,
                confidence=0.0
            )
            
        except Exception as e:
            # Record error
            self._record_ai_usage(
                url=url,
                tokens_used=0,
                success=False,
                error=str(e)
            )
            
            logger.error("ai_fallback_error", url=url, error=str(e))
            
            return ParseResult(
                success=False,
                error=f"AI fallback error: {e}",
                duration_ms=(time.time() - start_time) * 1000,
                confidence=0.0
            )
    
    def _check_budget_limits(self) -> Dict:
        """Check all budget limits and return status."""
        today = date.today()
        
        with sqlite3.connect(self.budget_db_path) as conn:
            # Check daily limits
            daily_usage = conn.execute("""
                SELECT tokens_used, calls_made FROM ai_usage WHERE date = ?
            """, (today,)).fetchone()
            
            if not daily_usage:
                return {"can_proceed": True, "usage": {"daily_tokens": 0, "daily_calls": 0}}
            
            daily_tokens, daily_calls = daily_usage
            
            # Check daily token limit
            if daily_tokens >= self.budget_limits["daily_tokens"]:
                return {
                    "can_proceed": False,
                    "reason": f"Daily token limit exceeded ({daily_tokens}/{self.budget_limits['daily_tokens']})",
                    "usage": {"daily_tokens": daily_tokens, "daily_calls": daily_calls}
                }
            
            # Check rate limit (calls per minute)
            minute_ago = datetime.now().replace(second=0, microsecond=0)
            recent_calls = conn.execute("""
                SELECT COUNT(*) FROM ai_calls 
                WHERE timestamp >= ? AND success = 1
            """, (minute_ago,)).fetchone()[0]
            
            if recent_calls >= self.budget_limits["calls_per_minute"]:
                return {
                    "can_proceed": False,
                    "reason": f"Rate limit exceeded ({recent_calls}/{self.budget_limits['calls_per_minute']} calls/min)",
                    "usage": {"daily_tokens": daily_tokens, "daily_calls": daily_calls, "recent_calls": recent_calls}
                }
            
            # Check total calls limit
            total_calls = conn.execute("""
                SELECT COUNT(*) FROM ai_calls WHERE success = 1
            """).fetchone()[0]
            
            if total_calls >= self.budget_limits["total_calls_limit"]:
                return {
                    "can_proceed": False,
                    "reason": f"Total calls limit exceeded ({total_calls}/{self.budget_limits['total_calls_limit']})",
                    "usage": {"daily_tokens": daily_tokens, "daily_calls": daily_calls, "total_calls": total_calls}
                }
            
            # Check alert threshold
            usage_percent = daily_tokens / self.budget_limits["daily_tokens"]
            if usage_percent >= self.budget_limits["alert_threshold"]:
                logger.warning(
                    "ai_budget_alert",
                    usage_percent=usage_percent * 100,
                    daily_tokens=daily_tokens,
                    limit=self.budget_limits["daily_tokens"]
                )
        
        return {
            "can_proceed": True,
            "usage": {
                "daily_tokens": daily_tokens,
                "daily_calls": daily_calls,
                "usage_percent": usage_percent
            }
        }
    
    def _prepare_html_for_ai(self, html: str) -> str:
        """Prepare and truncate HTML for AI processing."""
        # Parse HTML and extract relevant content
        parser = HTMLParser(html)
        
        # Remove scripts, styles, and other noise
        for element in parser.css('script, style, nav, footer, header, aside'):
            try:
                element.decompose()
            except Exception:
                continue
        
        # Get main content areas
        content_areas = []
        
        # Try to find main content
        main_selectors = [
            'article', 'main', '.content', '.article-content', 
            '.story-content', '[role="main"]'
        ]
        
        for selector in main_selectors:
            elements = parser.css(selector)
            for element in elements:
                text = element.text(strip=True)
                if len(text) > 200:  # Substantial content
                    content_areas.append(str(element))
        
        # If no main content found, use body
        if not content_areas:
            body = parser.css_first('body')
            if body:
                content_areas.append(str(body))
        
        # Combine and truncate
        combined_html = '\n'.join(content_areas)
        
        # Truncate to max length
        if len(combined_html) > self.ai_config["html_max_chars"]:
            combined_html = combined_html[:self.ai_config["html_max_chars"]]
            # Try to end at a reasonable boundary
            last_tag = combined_html.rfind('>')
            if last_tag > len(combined_html) * 0.8:  # If we're close to end
                combined_html = combined_html[:last_tag + 1]
        
        return combined_html
    
    async def _make_ai_request(self, html: str, url: str) -> Dict:
        """Make request to AI provider via unified client."""
        system_prompt = "Extract article data as JSON. Output ONLY valid JSON, no explanations."
        
        user_prompt = f"""Extract from this HTML snippet:
{html}

Return JSON with keys: title, author, published_date, body

Requirements:
- title: Main headline (required)
- author: Writer/byline if available (or null)
- published_date: Publication date in ISO format if found (or null)  
- body: Main article content, multiple paragraphs (required)

Output only valid JSON, no markdown, no explanations."""
        
        try:
            response = await self.ai_client.generate(
                messages=[{"role": "user", "content": user_prompt}],
                model=self.ai_config["model"],
                max_tokens=self.ai_config["max_tokens"],
                temperature=self.ai_config["temperature"],
                system_prompt=system_prompt
            )
            
            # Extract response content
            if response.content:
                content = response.content.strip()
                
                # Parse JSON response
                try:
                    json_data = json.loads(content)
                    
                    # Validate against schema
                    jsonschema.validate(json_data, self.response_schema)
                    
                    return {
                        "success": True,
                        "data": json_data,
                        "tokens_used": response.usage.get('input_tokens', 0) + response.usage.get('output_tokens', 0)
                    }
                    
                except json.JSONDecodeError as e:
                    logger.warning("ai_json_parse_error", content=content[:200], error=str(e))
                    return {
                        "success": False,
                        "error": f"Invalid JSON response: {e}",
                        "fallback": self.fallback_response
                    }
                    
                except jsonschema.ValidationError as e:
                    logger.warning("ai_schema_validation_error", error=str(e))
                    # Try to salvage what we can
                    try:
                        salvaged = {
                            "title": json_data.get("title", ""),
                            "body": json_data.get("body", ""),
                            "author": json_data.get("author"),
                            "published_date": json_data.get("published_date")
                        }
                        if salvaged["title"] and salvaged["body"]:
                            return {"success": True, "data": salvaged, "tokens_used": response.usage.get('input_tokens', 0) + response.usage.get('output_tokens', 0)}
                    except Exception:
                        pass
                    
                    return {
                        "success": False,
                        "error": f"Schema validation failed: {e}",
                        "fallback": self.fallback_response
                    }
            
            return {
                "success": False,
                "error": "Empty response from AI"
            }
            
        except Exception as e:
            # Handle rate limits and API errors generically
            error_msg = str(e).lower()
            if "rate limit" in error_msg or "quota" in error_msg:
                return {
                    "success": False,
                    "error": f"Rate limited: {e}"
                }
            elif "api" in error_msg or "authentication" in error_msg:
                return {
                    "success": False,
                    "error": f"API error: {e}"
                }
            else:
                return {
                    "success": False,
                    "error": f"Unexpected error: {e}"
                }
    
    def _parse_ai_response(self, data: Dict, url: str) -> Optional[Article]:
        """Parse AI response into Article object."""
        try:
            title = data.get("title", "").strip()
            body = data.get("body", "").strip()
            
            if not title or not body:
                return None
            
            # Parse date if provided
            published_date = None
            date_str = data.get("published_date")
            if date_str:
                try:
                    from dateutil.parser import parse as parse_date
                    published_date = parse_date(date_str)
                except Exception as e:
                    logger.debug("ai_date_parse_error", date_str=date_str, error=str(e))
            
            return Article(
                source_url=url,
                title=title,
                body=body,
                author=data.get("author"),
                published_date=published_date,
                parse_method=ParseMethod.AI_FALLBACK,
                discovered_at=datetime.utcnow()
            )
            
        except Exception as e:
            logger.error("ai_response_parse_error", data=data, error=str(e))
            return None
    
    def _record_ai_usage(
        self, 
        url: str, 
        tokens_used: int, 
        success: bool, 
        error: str = None
    ):
        """Record AI usage for budget tracking."""
        today = date.today()
        
        with sqlite3.connect(self.budget_db_path) as conn:
            # Update daily usage
            conn.execute("""
                UPDATE ai_usage 
                SET tokens_used = tokens_used + ?,
                    calls_made = calls_made + 1
                WHERE date = ?
            """, (tokens_used, today))
            
            # Record individual call
            conn.execute("""
                INSERT INTO ai_calls (url, tokens_used, success, error)
                VALUES (?, ?, ?, ?)
            """, (url, tokens_used, success, error))
    
    def _should_sample_trigger(self) -> bool:
        """Check if we should sample this AI trigger (10% rate)."""
        import random
        return random.random() < 0.1
    
    def _store_ai_trigger_sample(self, html: str, url: str):
        """Store sample of HTML that triggered AI for analysis."""
        try:
            timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
            filename = f"ai_trigger_{timestamp}.html"
            filepath = config.ai_triggers_dir / filename
            
            # Store compressed
            import gzip
            with gzip.open(f"{filepath}.gz", 'wt', encoding='utf-8') as f:
                f.write(f"<!-- URL: {url} -->\n")
                f.write(f"<!-- Timestamp: {datetime.now().isoformat()} -->\n")
                f.write(html)
            
            logger.debug("ai_trigger_sampled", url=url, filename=filename)
            
        except Exception as e:
            logger.debug("ai_trigger_sample_error", error=str(e))
    
    def get_usage_stats(self) -> Dict:
        """Get AI usage statistics."""
        today = date.today()
        
        with sqlite3.connect(self.budget_db_path) as conn:
            # Daily stats
            daily_stats = conn.execute("""
                SELECT tokens_used, calls_made FROM ai_usage WHERE date = ?
            """, (today,)).fetchone()
            
            if not daily_stats:
                daily_stats = (0, 0)
            
            # Total stats
            total_calls = conn.execute("""
                SELECT COUNT(*), SUM(tokens_used) FROM ai_calls WHERE success = 1
            """).fetchone()
            
            # Success rate
            success_rate = conn.execute("""
                SELECT 
                    COUNT(CASE WHEN success = 1 THEN 1 END) * 100.0 / COUNT(*) as success_rate
                FROM ai_calls
                WHERE timestamp >= date('now', '-7 days')
            """).fetchone()[0] or 0
            
            return {
                "daily_tokens_used": daily_stats[0],
                "daily_tokens_limit": self.budget_limits["daily_tokens"],
                "daily_usage_percent": (daily_stats[0] / self.budget_limits["daily_tokens"]) * 100,
                "daily_calls": daily_stats[1],
                "total_calls": total_calls[0] or 0,
                "total_tokens": total_calls[1] or 0,
                "success_rate_7d": success_rate,
                "budget_status": "active" if self.ai_client and self.ai_client.current_provider else "disabled"
            }
    
    def can_extract(self, html: str) -> bool:
        """Check if AI extraction is available and within budget."""
        if not self.ai_client or not self.ai_client.current_provider:
            return False
        
        budget_check = self._check_budget_limits()
        return budget_check["can_proceed"]
    
    def get_extraction_stats(self) -> Dict:
        """Get extraction statistics."""
        return {
            "method": "ai_fallback",
            "priority": 6,  # Last resort
            "avg_duration_ms": 1000,
            "confidence": 0.95,
            "model": self.ai_config["model"],
            "max_tokens": self.ai_config["max_tokens"],
            "html_max_chars": self.ai_config["html_max_chars"],
            "budget_limits": self.budget_limits
        }