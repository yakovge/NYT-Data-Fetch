"""AI provider abstraction layer for flexible model switching."""

import asyncio
import json
import os
from abc import ABC, abstractmethod
from typing import Dict, List, Optional, Any, Tuple
from dataclasses import dataclass

from ..monitoring.structured_logger import get_logger

logger = get_logger(__name__)


@dataclass
class AIMessage:
    """Standard AI message format."""
    role: str  # "user", "assistant", "system"
    content: str


@dataclass
class AIRequest:
    """Standard AI request format."""
    messages: List[AIMessage]
    model: str
    max_tokens: int
    temperature: float
    system_prompt: Optional[str] = None


@dataclass 
class AIResponse:
    """Standard AI response format."""
    content: str
    model: str
    usage: Dict[str, int]  # tokens used
    provider: str


class AIProvider(ABC):
    """Abstract base class for AI providers."""
    
    def __init__(self, config: Dict[str, Any]):
        self.config = config
        self.provider_name = self.__class__.__name__.replace('Provider', '').lower()
    
    @abstractmethod
    async def generate(self, request: AIRequest) -> AIResponse:
        """Generate AI response from request."""
        pass
    
    @abstractmethod
    def is_available(self) -> Tuple[bool, str]:
        """Check if provider is available and configured."""
        pass


class AnthropicProvider(AIProvider):
    """Anthropic Claude provider."""
    
    def __init__(self, config: Dict[str, Any]):
        super().__init__(config)
        self.api_key = config.get('api_key') or os.getenv('ANTHROPIC_API_KEY')
        self._client = None
    
    def _get_client(self):
        """Lazy load Anthropic client."""
        if self._client is None:
            try:
                import anthropic
                self._client = anthropic.Anthropic(api_key=self.api_key)
            except ImportError:
                raise ImportError("anthropic package required for Anthropic provider")
        return self._client
    
    def is_available(self) -> Tuple[bool, str]:
        """Check if Anthropic provider is configured."""
        if not self.api_key:
            return False, "ANTHROPIC_API_KEY not configured"
        try:
            self._get_client()
            return True, "available"
        except ImportError as e:
            return False, str(e)
    
    async def generate(self, request: AIRequest) -> AIResponse:
        """Generate response using Anthropic API."""
        client = self._get_client()
        
        # Convert to Anthropic format
        messages = []
        system_prompt = request.system_prompt
        
        for msg in request.messages:
            if msg.role == "system":
                system_prompt = msg.content
            else:
                messages.append({"role": msg.role, "content": msg.content})
        
        def _sync_request():
            kwargs = {
                "model": request.model,
                "max_tokens": request.max_tokens,
                "temperature": request.temperature,
                "messages": messages
            }
            if system_prompt:
                kwargs["system"] = system_prompt
            
            response = client.messages.create(**kwargs)
            return response
        
        # Run in executor to avoid blocking
        loop = asyncio.get_event_loop()
        response = await loop.run_in_executor(None, _sync_request)
        
        content = response.content[0].text if response.content else ""
        usage = {
            "input_tokens": response.usage.input_tokens if response.usage else 0,
            "output_tokens": response.usage.output_tokens if response.usage else 0
        }
        
        return AIResponse(
            content=content,
            model=request.model,
            usage=usage,
            provider="anthropic"
        )


class OpenAIProvider(AIProvider):
    """OpenAI GPT provider."""
    
    def __init__(self, config: Dict[str, Any]):
        super().__init__(config)
        self.api_key = config.get('api_key') or os.getenv('OPENAI_API_KEY')
        self._client = None
    
    def _get_client(self):
        """Lazy load OpenAI client."""
        if self._client is None:
            try:
                from openai import OpenAI
                self._client = OpenAI(api_key=self.api_key)
            except ImportError:
                raise ImportError("openai package required for OpenAI provider")
        return self._client
    
    def is_available(self) -> Tuple[bool, str]:
        """Check if OpenAI provider is configured."""
        if not self.api_key:
            return False, "OPENAI_API_KEY not configured"
        try:
            self._get_client()
            return True, "available"
        except ImportError as e:
            return False, str(e)
    
    async def generate(self, request: AIRequest) -> AIResponse:
        """Generate response using OpenAI API."""
        client = self._get_client()
        
        # Convert to OpenAI format
        messages = []
        for msg in request.messages:
            messages.append({"role": msg.role, "content": msg.content})
        
        # Add system prompt as first message if provided
        if request.system_prompt:
            messages.insert(0, {"role": "system", "content": request.system_prompt})
        
        def _sync_request():
            response = client.chat.completions.create(
                model=request.model,
                messages=messages,
                max_tokens=request.max_tokens,
                temperature=request.temperature
            )
            return response
        
        # Run in executor to avoid blocking
        loop = asyncio.get_event_loop()
        response = await loop.run_in_executor(None, _sync_request)
        
        content = response.choices[0].message.content if response.choices else ""
        usage = {
            "input_tokens": response.usage.prompt_tokens if response.usage else 0,
            "output_tokens": response.usage.completion_tokens if response.usage else 0
        }
        
        return AIResponse(
            content=content,
            model=request.model,
            usage=usage,
            provider="openai"
        )


class GoogleProvider(AIProvider):
    """Google Gemini provider."""
    
    def __init__(self, config: Dict[str, Any]):
        super().__init__(config)
        self.api_key = config.get('api_key') or os.getenv('GOOGLE_API_KEY')
        self._client = None
    
    def _get_client(self):
        """Lazy load Google client."""
        if self._client is None:
            try:
                import google.generativeai as genai
                genai.configure(api_key=self.api_key)
                self._client = genai
            except ImportError:
                raise ImportError("google-generativeai package required for Google provider")
        return self._client
    
    def is_available(self) -> Tuple[bool, str]:
        """Check if Google provider is configured."""
        if not self.api_key:
            return False, "GOOGLE_API_KEY not configured"
        try:
            self._get_client()
            return True, "available"
        except ImportError as e:
            return False, str(e)
    
    async def generate(self, request: AIRequest) -> AIResponse:
        """Generate response using Google Gemini API."""
        genai = self._get_client()
        
        # Convert to Google format - combine all messages
        prompt_parts = []
        if request.system_prompt:
            prompt_parts.append(f"System: {request.system_prompt}")
        
        for msg in request.messages:
            prompt_parts.append(f"{msg.role.title()}: {msg.content}")
        
        prompt = "\n\n".join(prompt_parts)
        
        def _sync_request():
            model = genai.GenerativeModel(request.model)
            
            generation_config = genai.types.GenerationConfig(
                max_output_tokens=request.max_tokens,
                temperature=request.temperature,
            )
            
            response = model.generate_content(
                prompt,
                generation_config=generation_config
            )
            return response
        
        # Run in executor to avoid blocking
        loop = asyncio.get_event_loop()
        response = await loop.run_in_executor(None, _sync_request)
        
        content = response.text if hasattr(response, 'text') else ""
        usage = {
            "input_tokens": 0,  # Google doesn't provide detailed token counts in basic API
            "output_tokens": 0
        }
        
        return AIResponse(
            content=content,
            model=request.model,
            usage=usage,
            provider="google"
        )


class GrokProvider(AIProvider):
    """xAI Grok provider (using OpenAI-compatible API)."""
    
    def __init__(self, config: Dict[str, Any]):
        super().__init__(config)
        self.api_key = config.get('api_key') or os.getenv('XAI_API_KEY')
        self.base_url = config.get('base_url', 'https://api.x.ai/v1')
        self._client = None
    
    def _get_client(self):
        """Lazy load xAI client."""
        if self._client is None:
            try:
                from openai import OpenAI
                self._client = OpenAI(
                    api_key=self.api_key,
                    base_url=self.base_url
                )
            except ImportError:
                raise ImportError("openai package required for Grok provider")
        return self._client
    
    def is_available(self) -> Tuple[bool, str]:
        """Check if Grok provider is configured."""
        if not self.api_key:
            return False, "XAI_API_KEY not configured"
        try:
            self._get_client()
            return True, "available"
        except ImportError as e:
            return False, str(e)
    
    async def generate(self, request: AIRequest) -> AIResponse:
        """Generate response using xAI Grok API."""
        client = self._get_client()
        
        # Convert to OpenAI-compatible format
        messages = []
        for msg in request.messages:
            messages.append({"role": msg.role, "content": msg.content})
        
        # Add system prompt as first message if provided
        if request.system_prompt:
            messages.insert(0, {"role": "system", "content": request.system_prompt})
        
        def _sync_request():
            response = client.chat.completions.create(
                model=request.model,
                messages=messages,
                max_tokens=request.max_tokens,
                temperature=request.temperature
            )
            return response
        
        # Run in executor to avoid blocking
        loop = asyncio.get_event_loop()
        response = await loop.run_in_executor(None, _sync_request)
        
        content = response.choices[0].message.content if response.choices else ""
        usage = {
            "input_tokens": response.usage.prompt_tokens if response.usage else 0,
            "output_tokens": response.usage.completion_tokens if response.usage else 0
        }
        
        return AIResponse(
            content=content,
            model=request.model,
            usage=usage,
            provider="grok"
        )


# Provider registry
PROVIDERS = {
    'anthropic': AnthropicProvider,
    'openai': OpenAIProvider,
    'google': GoogleProvider,
    'grok': GrokProvider,
}


def create_provider(provider_name: str, config: Dict[str, Any]) -> AIProvider:
    """Create AI provider instance."""
    provider_class = PROVIDERS.get(provider_name.lower())
    if not provider_class:
        available = ', '.join(PROVIDERS.keys())
        raise ValueError(f"Unknown provider '{provider_name}'. Available: {available}")
    
    return provider_class(config)