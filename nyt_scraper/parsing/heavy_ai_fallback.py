"""Heavy AI fallback for comprehensive content extraction when deterministic parsing fails."""

import asyncio
import json
from datetime import datetime
from typing import Dict, List, Optional, Tuple, Any
import hashlib
import re

from ..config import config
from ..monitoring.structured_logger import get_logger
from ..models import Article, ArticleStatus
from ..ai import AIClient

logger = get_logger(__name__)


class HeavyAIFallback:
    """Heavy AI fallback for comprehensive article content extraction."""
    
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
        
        self.enabled = config.ai_heavy_enabled
        self.model = config.ai_heavy_model
        self.max_tokens = config.ai_heavy_max_tokens
        self.temperature = config.ai_heavy_temperature
        self.daily_limit = config.ai_heavy_daily_limit
        self.usage_ratio = config.ai_heavy_ratio
        
        # Usage tracking
        self.usage_cache = {}
        self.last_reset = datetime.utcnow().replace(hour=config.ai_budget_reset_hour, minute=0, second=0)
        
        # Content quality thresholds
        self.min_content_length = 200
        self.min_confidence_score = 0.7
        
        # Log current provider info
        provider_info = {
            "enabled": self.enabled,
            "model": self.model,
            "daily_limit": self.daily_limit
        }
        if self.ai_client and self.ai_client.current_provider_name:
            provider_info["provider"] = self.ai_client.current_provider_name
        
        logger.info("heavy_ai_fallback_initialized", **provider_info)
    
    @property
    def anthropic_client(self):
        """Backward compatibility property for tests and legacy code."""
        # Return the original anthropic client if available, otherwise the ai_client
        return getattr(self, '_original_anthropic_client', self.ai_client)
    
    def should_escalate(self, article: Article, parse_confidence: float, total_requests_today: int = 0) -> Tuple[bool, str]:
        """
        Determine if article should escalate to heavy AI analysis.
        
        Args:
            article: Article object with current parsing results
            parse_confidence: Confidence score from previous parsing attempts
            total_requests_today: Total parsing requests made today
            
        Returns:
            Tuple of (should_escalate, reason)
        """
        if not self.enabled:
            return False, "heavy_ai_disabled"
        
        if not self.ai_client or not self.ai_client.current_provider:
            return False, "no_ai_client"
        
        # Check daily budget
        today_usage = self._get_today_usage()
        if today_usage >= self.daily_limit:
            return False, "daily_limit_exceeded"
        
        # Check usage ratio
        if total_requests_today > 0:
            current_ratio = today_usage / total_requests_today
            if current_ratio > self.usage_ratio:
                return False, "ratio_limit_exceeded"
        
        # Content quality checks
        content_issues = self._assess_content_quality(article)
        confidence_threshold = self.min_confidence_score
        
        # Escalate if confidence is low or content has significant issues
        if parse_confidence < confidence_threshold:
            return True, f"low_confidence_{parse_confidence:.2f}"
        
        if content_issues:
            return True, f"content_issues_{','.join(content_issues)}"
        
        return False, "no_escalation_needed"
    
    async def extract_full_article(self, html_content: str, url: str) -> Tuple[bool, Dict[str, Any]]:
        """
        Perform comprehensive article extraction using heavy AI analysis.
        
        Args:
            html_content: Full HTML content of the page
            url: Article URL for context
            
        Returns:
            Tuple of (success, extracted_content)
        """
        try:
            # Prepare HTML for AI analysis (clean but preserve structure)
            cleaned_html = self._prepare_html_for_ai(html_content)
            if not cleaned_html:
                return False, {}
            
            # Create comprehensive extraction prompt
            prompt = self._create_extraction_prompt(cleaned_html, url)
            
            # Make AI request
            response = await self._make_ai_request(prompt)
            if not response:
                return False, {}
            
            # Parse comprehensive response
            extracted_data = self._parse_extraction_response(response)
            if not extracted_data:
                return False, {}
            
            # Track usage
            self._track_usage(len(prompt) + len(response))
            
            logger.info("heavy_ai_extraction_completed",
                       url=url,
                       content_length=len(extracted_data.get('content', '')),
                       confidence=extracted_data.get('confidence', 0))
            
            return True, extracted_data
            
        except Exception as e:
            logger.error("heavy_ai_extraction_failed", url=url, error=str(e))
            return False, {}
    
    async def analyze_article_structure(self, html_content: str, url: str) -> Dict[str, Any]:
        """
        Analyze HTML structure to provide parsing guidance for future similar articles.
        
        Args:
            html_content: HTML content to analyze
            url: Article URL for context
            
        Returns:
            Dictionary with structural analysis and parsing suggestions
        """
        try:
            # Create structure analysis prompt
            html_sample = self._prepare_html_sample(html_content)
            prompt = self._create_structure_analysis_prompt(html_sample, url)
            
            # Make AI request
            response = await self._make_ai_request(prompt)
            if not response:
                return {}
            
            # Parse structure analysis
            analysis = self._parse_structure_response(response)
            
            # Track usage
            self._track_usage(len(prompt) + len(response))
            
            logger.debug("heavy_ai_structure_analysis_completed", url=url)
            return analysis
            
        except Exception as e:
            logger.error("heavy_ai_structure_analysis_failed", url=url, error=str(e))
            return {}
    
    def _assess_content_quality(self, article: Article) -> List[str]:
        """Assess article content quality and return list of issues."""
        issues = []
        
        # Check content length
        if not article.body or len(article.body) < self.min_content_length:
            issues.append("insufficient_content")
        
        # Check for missing essential metadata
        if not article.title:
            issues.append("missing_title")
        
        # Check for potential extraction errors
        if article.body:
            # Too many repeated characters/words might indicate parsing errors
            if self._has_repetitive_content(article.body):
                issues.append("repetitive_content")
            
            # Check for navigation/UI text leakage
            if self._has_navigation_artifacts(article.body):
                issues.append("navigation_artifacts")
        
        return issues
    
    def _has_repetitive_content(self, content: str) -> bool:
        """Check if content has repetitive patterns suggesting parsing errors."""
        if len(content) < 100:
            return False
        
        # Check for repeated short phrases
        words = content.lower().split()
        if len(words) < 20:
            return False
        
        # Simple repetition detection: check if any 5-word phrase appears more than 3 times
        phrases = {}
        for i in range(len(words) - 4):
            phrase = ' '.join(words[i:i+5])
            phrases[phrase] = phrases.get(phrase, 0) + 1
            if phrases[phrase] > 3:
                return True
        
        return False
    
    def _has_navigation_artifacts(self, content: str) -> bool:
        """Check for navigation/UI artifacts in content."""
        # Common navigation/UI patterns that shouldn't be in article content
        nav_patterns = [
            r'\bmenu\b.*\bmenu\b',
            r'(skip to|jump to) (content|main)',
            r'(sign in|log in|subscribe|newsletter)',
            r'(facebook|twitter|instagram|share this)',
            r'(comments?|related articles?)',
            r'(advertisement|sponsored content)',
        ]
        
        content_lower = content.lower()
        nav_matches = sum(1 for pattern in nav_patterns if re.search(pattern, content_lower))
        
        # If more than 2 navigation patterns found, likely has artifacts
        return nav_matches > 2
    
    def _prepare_html_for_ai(self, html_content: str, max_length: int = 15000) -> str:
        """Clean and prepare HTML for AI analysis while preserving structure."""
        if not html_content:
            return ""
        
        # Remove script and style tags completely
        html_content = re.sub(r'<script[^>]*>.*?</script>', '', html_content, flags=re.DOTALL | re.IGNORECASE)
        html_content = re.sub(r'<style[^>]*>.*?</style>', '', html_content, flags=re.DOTALL | re.IGNORECASE)
        
        # Remove comments
        html_content = re.sub(r'<!--.*?-->', '', html_content, flags=re.DOTALL)
        
        # If still too long, try to extract main content area
        if len(html_content) > max_length:
            # Look for main content containers
            main_patterns = [
                r'<main[^>]*>.*?</main>',
                r'<article[^>]*>.*?</article>',
                r'<div[^>]*class="[^"]*(?:content|article|main|story)[^"]*"[^>]*>.*?</div>',
                r'<section[^>]*class="[^"]*(?:content|article|main|story)[^"]*"[^>]*>.*?</section>'
            ]
            
            for pattern in main_patterns:
                matches = re.findall(pattern, html_content, flags=re.DOTALL | re.IGNORECASE)
                if matches:
                    # Use the longest match as likely main content
                    main_content = max(matches, key=len)
                    if len(main_content) < max_length:
                        html_content = main_content
                        break
            
            # If still too long, truncate intelligently
            if len(html_content) > max_length:
                html_content = html_content[:max_length]
                # Try to end at a complete tag
                last_tag_end = html_content.rfind('>')
                if last_tag_end > max_length * 0.8:
                    html_content = html_content[:last_tag_end + 1]
        
        return html_content
    
    def _prepare_html_sample(self, html_content: str, max_length: int = 5000) -> str:
        """Prepare HTML sample for structure analysis."""
        if len(html_content) <= max_length:
            return html_content
        
        # Take beginning and end samples
        start_sample = html_content[:max_length // 2]
        end_sample = html_content[-max_length // 2:]
        
        return f"{start_sample}\n\n... [CONTENT TRUNCATED] ...\n\n{end_sample}"
    
    def _create_extraction_prompt(self, html_content: str, url: str) -> str:
        """Create comprehensive extraction prompt for heavy AI."""
        return f"""Extract complete article content and metadata from this HTML. This is a fallback when automated parsing failed.

URL: {url}

HTML Content:
{html_content}

Extract and return a JSON object with:
{{
    "title": "Complete article title",
    "author": "Author name(s)",
    "section": "News section/category",
    "summary": "2-3 sentence summary",
    "content": "Full article text content (paragraphs separated by \\n\\n)",
    "publish_date": "Publication date if found (ISO format)",
    "tags": ["relevant", "topic", "tags"],
    "word_count": estimated_word_count,
    "confidence": 0.0-1.0,
    "extraction_notes": "Any issues or observations about the content"
}}

Focus on:
1. Clean, readable article text without navigation/ads
2. Proper paragraph structure
3. Complete metadata extraction
4. High confidence in extracted content quality

Only include content you're confident is part of the actual article."""
    
    def _create_structure_analysis_prompt(self, html_sample: str, url: str) -> str:
        """Create prompt for HTML structure analysis."""
        return f"""Analyze this HTML structure to identify patterns for automated article extraction.

URL: {url}

HTML Sample:
{html_sample}

Return a JSON analysis:
{{
    "content_selectors": ["css selectors for main content"],
    "title_selectors": ["selectors for article title"],
    "author_selectors": ["selectors for author info"],
    "date_selectors": ["selectors for publish date"],
    "content_containers": ["main content container patterns"],
    "noise_patterns": ["selectors for ads/navigation to exclude"],
    "structured_data": "presence of JSON-LD/microdata",
    "framework_detected": "React/Next.js/other framework signs",
    "parsing_difficulty": "easy|medium|hard",
    "recommendations": ["specific parsing strategy suggestions"]
}}

Focus on identifying reliable CSS selectors and structural patterns that could be used for automated extraction of similar articles from this site."""
    
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
            logger.error("heavy_ai_request_failed", 
                        provider=self.ai_client.current_provider_name if self.ai_client else "unknown",
                        error=str(e))
            return None
    
    def _parse_extraction_response(self, response: str) -> Dict[str, Any]:
        """Parse and validate heavy AI extraction response."""
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
            
            data = json.loads(response)
            
            # Validate and clean extracted data
            cleaned = {}
            
            # Required fields with validation
            if isinstance(data.get('title'), str) and len(data['title'].strip()) > 5:
                cleaned['title'] = data['title'].strip()
            
            if isinstance(data.get('content'), str) and len(data['content'].strip()) > 50:
                cleaned['content'] = data['content'].strip()
            
            # Optional fields
            if isinstance(data.get('author'), str) and len(data['author'].strip()) > 2:
                cleaned['author'] = data['author'].strip()
            
            if isinstance(data.get('section'), str) and len(data['section'].strip()) > 2:
                cleaned['section'] = data['section'].strip()
            
            if isinstance(data.get('summary'), str) and len(data['summary'].strip()) > 10:
                cleaned['summary'] = data['summary'].strip()
            
            if isinstance(data.get('publish_date'), str):
                cleaned['publish_date'] = data['publish_date'].strip()
            
            if isinstance(data.get('tags'), list):
                valid_tags = [tag.strip() for tag in data['tags'] if isinstance(tag, str) and len(tag.strip()) > 2]
                if valid_tags:
                    cleaned['tags'] = valid_tags
            
            # Numeric fields
            if isinstance(data.get('word_count'), (int, float)) and data['word_count'] > 0:
                cleaned['word_count'] = int(data['word_count'])
            
            if isinstance(data.get('confidence'), (int, float)):
                cleaned['confidence'] = max(0.0, min(1.0, float(data['confidence'])))
            else:
                cleaned['confidence'] = 0.8  # Default confidence for heavy AI
            
            if isinstance(data.get('extraction_notes'), str):
                cleaned['extraction_notes'] = data['extraction_notes'].strip()
            
            return cleaned
            
        except (json.JSONDecodeError, KeyError, TypeError) as e:
            logger.debug("heavy_ai_extraction_parse_failed", response=response[:500], error=str(e))
            return {}
    
    def _parse_structure_response(self, response: str) -> Dict[str, Any]:
        """Parse structure analysis response."""
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
            
            analysis = json.loads(response)
            
            # Validate structure analysis fields
            validated = {}
            
            list_fields = ['content_selectors', 'title_selectors', 'author_selectors', 
                          'date_selectors', 'content_containers', 'noise_patterns', 'recommendations']
            
            for field in list_fields:
                if isinstance(analysis.get(field), list):
                    valid_items = [item.strip() for item in analysis[field] if isinstance(item, str) and len(item.strip()) > 2]
                    if valid_items:
                        validated[field] = valid_items
            
            string_fields = ['structured_data', 'framework_detected', 'parsing_difficulty']
            for field in string_fields:
                if isinstance(analysis.get(field), str) and len(analysis[field].strip()) > 2:
                    validated[field] = analysis[field].strip()
            
            return validated
            
        except (json.JSONDecodeError, KeyError, TypeError) as e:
            logger.debug("heavy_ai_structure_parse_failed", response=response[:300], error=str(e))
            return {}
    
    def _get_today_usage(self) -> int:
        """Get today's heavy AI usage in tokens."""
        now = datetime.utcnow()
        
        # Check if we need to reset daily usage
        if now.date() > self.last_reset.date():
            self.usage_cache = {}
            self.last_reset = now.replace(hour=config.ai_budget_reset_hour, minute=0, second=0)
        
        today_key = now.strftime('%Y-%m-%d')
        return self.usage_cache.get(today_key, 0)
    
    def _track_usage(self, tokens_used: int):
        """Track heavy AI token usage for budget management."""
        today_key = datetime.utcnow().strftime('%Y-%m-%d')
        self.usage_cache[today_key] = self.usage_cache.get(today_key, 0) + tokens_used
        
        logger.debug("heavy_ai_usage_tracked",
                    tokens_used=tokens_used,
                    daily_total=self.usage_cache[today_key],
                    daily_limit=self.daily_limit)
    
    def get_usage_stats(self) -> Dict[str, Any]:
        """Get comprehensive usage statistics for heavy AI."""
        today_usage = self._get_today_usage()
        
        return {
            "enabled": self.enabled,
            "model": self.model,
            "daily_limit": self.daily_limit,
            "today_usage": today_usage,
            "usage_ratio_limit": self.usage_ratio,
            "remaining_budget": max(0, self.daily_limit - today_usage),
            "budget_exhausted": today_usage >= self.daily_limit,
            "min_confidence_threshold": self.min_confidence_score
        }