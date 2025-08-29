"""Micro-AI controller for minimal content gap-filling and validation."""

import asyncio
import json
from datetime import datetime, timedelta
from typing import Dict, List, Optional, Tuple, Any
import hashlib

from ..config import config
from ..monitoring.structured_logger import get_logger
from ..models import Article, ArticleStatus
from ..ai import AIClient

logger = get_logger(__name__)


class MicroAIController:
    """Lightweight AI controller for gap-filling missing article metadata."""
    
    def __init__(self, anthropic_client=None, ai_client=None):
        # Support both new AI client and legacy anthropic client for backward compatibility
        if ai_client:
            self.ai_client = ai_client
        elif anthropic_client:
            # For backward compatibility, create a mock AI client that wraps the anthropic client
            self.ai_client = AIClient()
            # Mock the provider to make it appear available for testing
            self.ai_client.current_provider = anthropic_client
            self.ai_client.current_provider_name = "anthropic"
            # Store the original client for backward compatibility
            self._original_anthropic_client = anthropic_client
        else:
            # Create new AI client with default configuration
            self.ai_client = AIClient()
        
        self.enabled = config.ai_min_enabled
        self.model = config.ai_min_model
        self.max_tokens = config.ai_min_max_tokens
        self.temperature = config.ai_min_temperature
        self.snippet_bytes = config.ai_min_snippet_bytes
        self.daily_limit = config.ai_min_daily_limit
        self.usage_ratio = config.ai_min_ratio
        
        # Usage tracking
        self.usage_cache = {}
        self.last_reset = datetime.utcnow().replace(hour=config.ai_budget_reset_hour, minute=0, second=0)
        
        # Log current provider info
        provider_info = {
            "enabled": self.enabled,
            "model": self.model,
            "daily_limit": self.daily_limit
        }
        if self.ai_client and self.ai_client.current_provider_name:
            provider_info["provider"] = self.ai_client.current_provider_name
        
        logger.info("micro_ai_controller_initialized", **provider_info)
    
    @property
    def anthropic_client(self):
        """Backward compatibility property for tests and legacy code."""
        # Return the original anthropic client if available, otherwise the ai_client
        return getattr(self, '_original_anthropic_client', self.ai_client)
    
    def is_available(self, total_requests_today: int = 0) -> Tuple[bool, str]:
        """
        Check if micro-AI is available based on budget and usage limits.
        
        Args:
            total_requests_today: Total parsing requests made today
            
        Returns:
            Tuple of (available, reason)
        """
        if not self.enabled:
            return False, "micro_ai_disabled"
        
        if not self.ai_client or not self.ai_client.current_provider:
            return False, "no_ai_client"
        
        # Check daily token budget
        today_usage = self._get_today_usage()
        if today_usage >= self.daily_limit:
            return False, "daily_limit_exceeded"
        
        # Check usage ratio against total requests
        if total_requests_today > 0:
            current_ratio = today_usage / total_requests_today
            if current_ratio > self.usage_ratio:
                return False, "ratio_limit_exceeded"
        
        return True, "available"
    
    async def fill_missing_metadata(self, article: Article, html_content: str) -> Tuple[bool, Dict[str, Any]]:
        """
        Use micro-AI to fill missing article metadata from HTML content.
        
        Args:
            article: Article object with potentially missing metadata
            html_content: Raw HTML content for analysis
            
        Returns:
            Tuple of (success, filled_metadata)
        """
        if not self.is_available()[0]:
            return False, {}
        
        try:
            # Prepare content snippet for AI analysis
            content_snippet = self._prepare_content_snippet(html_content)
            if not content_snippet:
                logger.debug("micro_ai_no_content_snippet", url=article.canonical_url)
                return False, {}
            
            # Create AI prompt for metadata extraction
            prompt = self._create_metadata_prompt(article, content_snippet)
            
            # Make AI request
            response = await self._make_ai_request(prompt)
            if not response:
                return False, {}
            
            # Parse and validate response
            metadata = self._parse_metadata_response(response)
            if not metadata:
                return False, {}
            
            # Track usage
            self._track_usage(len(prompt) + len(response))
            
            logger.info("micro_ai_metadata_filled",
                       url=article.canonical_url,
                       fields_filled=list(metadata.keys()))
            
            return True, metadata
            
        except Exception as e:
            logger.error("micro_ai_fill_failed",
                        url=article.canonical_url,
                        error=str(e))
            return False, {}
    
    async def validate_extracted_content(self, article: Article) -> Tuple[bool, float]:
        """
        Use micro-AI to validate quality of extracted content.
        
        Args:
            article: Article with extracted content
            
        Returns:
            Tuple of (is_valid, confidence_score)
        """
        if not self.is_available()[0] or not article.body:
            return True, 1.0  # Default to valid if AI unavailable
        
        try:
            # Create validation prompt
            content_sample = article.body[:self.snippet_bytes]
            prompt = self._create_validation_prompt(article.title or "", content_sample)
            
            # Make AI request
            response = await self._make_ai_request(prompt)
            if not response:
                return True, 1.0
            
            # Parse validation response
            is_valid, confidence = self._parse_validation_response(response)
            
            # Track usage
            self._track_usage(len(prompt) + len(response))
            
            logger.debug("micro_ai_validation_completed",
                        url=article.canonical_url,
                        is_valid=is_valid,
                        confidence=confidence)
            
            return is_valid, confidence
            
        except Exception as e:
            logger.error("micro_ai_validation_failed",
                        url=article.canonical_url,
                        error=str(e))
            return True, 1.0  # Default to valid on error
    
    def _prepare_content_snippet(self, html_content: str) -> str:
        """Prepare HTML content snippet for AI analysis."""
        if not html_content:
            return ""
        
        # Take first N bytes, try to break at word boundary
        if len(html_content) <= self.snippet_bytes:
            return html_content
        
        snippet = html_content[:self.snippet_bytes]
        
        # Try to break at last complete tag or word
        last_tag = snippet.rfind('>')
        last_space = snippet.rfind(' ')
        
        if last_tag > len(snippet) * 0.8:  # Tag is near the end
            return snippet[:last_tag + 1]
        elif last_space > len(snippet) * 0.8:  # Space is near the end
            return snippet[:last_space]
        else:
            return snippet
    
    def _create_metadata_prompt(self, article: Article, content_snippet: str) -> str:
        """Create AI prompt for metadata extraction."""
        missing_fields = []
        if not article.title:
            missing_fields.append("title")
        if not article.author:
            missing_fields.append("author")
        if not article.section:
            missing_fields.append("section")
        if not getattr(article, 'summary', None):
            missing_fields.append("summary")
        
        return f"""Extract missing article metadata from this HTML snippet. Return ONLY a JSON object with the requested fields.

Missing fields to extract: {', '.join(missing_fields)}

HTML snippet:
{content_snippet}

Return JSON format:
{{
    "title": "Article title if found",
    "author": "Author name if found", 
    "section": "Section/category if found",
    "summary": "Brief summary if content allows"
}}

Only include fields that you can confidently extract. Return empty JSON {{}} if no clear metadata found."""
    
    def _create_validation_prompt(self, title: str, content_sample: str) -> str:
        """Create AI prompt for content validation."""
        return f"""Validate if this extracted article content appears to be legitimate news content.

Title: {title}

Content sample:
{content_sample}

Return ONLY a JSON object:
{{
    "is_valid": true/false,
    "confidence": 0.0-1.0,
    "reason": "brief explanation"
}}

Consider:
- Is this actual article content vs navigation/ads/boilerplate?
- Does content match the title?
- Is there substantive news information?"""
    
    async def _make_ai_request(self, prompt: str) -> Optional[str]:
        """Make request to AI provider via unified client."""
        if not self.ai_client or not self.ai_client.current_provider:
            return None
        
        try:
            # Check if we have a legacy anthropic client for backward compatibility
            if hasattr(self, '_original_anthropic_client'):
                # Use the original anthropic client interface for testing
                response = self._original_anthropic_client.messages.create(
                    model=self.model,
                    max_tokens=self.max_tokens,
                    temperature=self.temperature,
                    messages=[{"role": "user", "content": prompt}]
                )
                
                # Extract content from mock response
                if response.content and len(response.content) > 0:
                    return response.content[0].text
                return None
            else:
                # Use the unified AI client to make the request
                response = await self.ai_client.generate(
                    messages=[{"role": "user", "content": prompt}],
                    model=self.model,
                    max_tokens=self.max_tokens,
                    temperature=self.temperature
                )
                
                return response.content if response.content else None
            
        except Exception as e:
            logger.error("micro_ai_request_failed", 
                        provider=self.ai_client.current_provider_name if self.ai_client else "unknown",
                        error=str(e))
            return None
    
    def _parse_metadata_response(self, response: str) -> Dict[str, Any]:
        """Parse and validate AI metadata response."""
        try:
            # Try to extract JSON from response
            response = response.strip()
            
            # Handle potential markdown code blocks
            if response.startswith('```'):
                lines = response.split('\n')
                json_lines = []
                in_json = False
                for line in lines:
                    if line.startswith('```'):
                        if in_json:
                            break
                        in_json = True
                        continue
                    if in_json:
                        json_lines.append(line)
                response = '\n'.join(json_lines)
            
            metadata = json.loads(response)
            
            # Validate and clean metadata
            cleaned = {}
            if isinstance(metadata.get('title'), str) and len(metadata['title'].strip()) > 5:
                cleaned['title'] = metadata['title'].strip()
            if isinstance(metadata.get('author'), str) and len(metadata['author'].strip()) > 2:
                cleaned['author'] = metadata['author'].strip()
            if isinstance(metadata.get('section'), str) and len(metadata['section'].strip()) > 2:
                cleaned['section'] = metadata['section'].strip()
            if isinstance(metadata.get('summary'), str) and len(metadata['summary'].strip()) > 10:
                cleaned['summary'] = metadata['summary'].strip()
            
            return cleaned
            
        except (json.JSONDecodeError, KeyError, TypeError) as e:
            logger.debug("micro_ai_metadata_parse_failed", response=response[:200], error=str(e))
            return {}
    
    def _parse_validation_response(self, response: str) -> Tuple[bool, float]:
        """Parse and validate AI validation response."""
        try:
            response = response.strip()
            
            # Handle markdown code blocks
            if response.startswith('```'):
                lines = response.split('\n')
                json_lines = []
                in_json = False
                for line in lines:
                    if line.startswith('```'):
                        if in_json:
                            break
                        in_json = True
                        continue
                    if in_json:
                        json_lines.append(line)
                response = '\n'.join(json_lines)
            
            validation = json.loads(response)
            
            is_valid = bool(validation.get('is_valid', True))
            confidence = float(validation.get('confidence', 1.0))
            confidence = max(0.0, min(1.0, confidence))  # Clamp to 0-1 range
            
            return is_valid, confidence
            
        except (json.JSONDecodeError, KeyError, TypeError, ValueError) as e:
            logger.debug("micro_ai_validation_parse_failed", response=response[:200], error=str(e))
            return True, 1.0  # Default to valid
    
    def _get_today_usage(self) -> int:
        """Get today's AI usage in tokens."""
        now = datetime.utcnow()
        
        # Check if we need to reset daily usage
        if now.date() > self.last_reset.date():
            self.usage_cache = {}
            self.last_reset = now.replace(hour=config.ai_budget_reset_hour, minute=0, second=0)
        
        today_key = now.strftime('%Y-%m-%d')
        return self.usage_cache.get(today_key, 0)
    
    def _track_usage(self, tokens_used: int):
        """Track AI token usage for budget management."""
        today_key = datetime.utcnow().strftime('%Y-%m-%d')
        self.usage_cache[today_key] = self.usage_cache.get(today_key, 0) + tokens_used
        
        logger.debug("micro_ai_usage_tracked",
                    tokens_used=tokens_used,
                    daily_total=self.usage_cache[today_key],
                    daily_limit=self.daily_limit)
    
    def get_usage_stats(self) -> Dict[str, Any]:
        """Get comprehensive usage statistics."""
        today_usage = self._get_today_usage()
        
        return {
            "enabled": self.enabled,
            "model": self.model,
            "daily_limit": self.daily_limit,
            "today_usage": today_usage,
            "usage_ratio_limit": self.usage_ratio,
            "remaining_budget": max(0, self.daily_limit - today_usage),
            "budget_exhausted": today_usage >= self.daily_limit
        }