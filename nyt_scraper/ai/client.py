"""Unified AI client with provider abstraction."""

import os
from typing import Dict, List, Optional, Any, Union
from dataclasses import dataclass

from .providers import AIProvider, AIMessage, AIRequest, AIResponse, create_provider
from ..config import config
from ..monitoring.structured_logger import get_logger

logger = get_logger(__name__)


@dataclass
class ProviderConfig:
    """Configuration for AI provider."""
    name: str
    model: str
    api_key: Optional[str] = None
    base_url: Optional[str] = None
    extra_config: Dict[str, Any] = None


class AIClient:
    """Unified AI client supporting multiple providers."""
    
    def __init__(self, provider_configs: Optional[Dict[str, ProviderConfig]] = None):
        """
        Initialize AI client with provider configurations.
        
        Args:
            provider_configs: Dict mapping provider names to configurations
        """
        self.provider_configs = provider_configs or self._load_default_configs()
        self.providers = {}
        self.current_provider_name = None
        self.current_provider = None
        
        # Initialize with primary provider
        self._initialize_primary_provider()
    
    def _load_default_configs(self) -> Dict[str, ProviderConfig]:
        """Load provider configurations from environment and config."""
        configs = {}
        
        # Anthropic (current default)
        anthropic_key = getattr(config, 'anthropic_api_key', None) or os.getenv('ANTHROPIC_API_KEY')
        if anthropic_key:
            configs['anthropic'] = ProviderConfig(
                name='anthropic',
                model=getattr(config, 'ai_min_model', 'claude-3-haiku'),
                api_key=anthropic_key
            )
        
        # OpenAI
        openai_key = os.getenv('OPENAI_API_KEY')
        if openai_key:
            configs['openai'] = ProviderConfig(
                name='openai',
                model=os.getenv('OPENAI_MODEL', 'gpt-4o-mini'),
                api_key=openai_key
            )
        
        # Google
        google_key = os.getenv('GOOGLE_API_KEY')
        if google_key:
            configs['google'] = ProviderConfig(
                name='google',
                model=os.getenv('GOOGLE_MODEL', 'gemini-1.5-flash'),
                api_key=google_key
            )
        
        # Grok
        xai_key = os.getenv('XAI_API_KEY')
        if xai_key:
            configs['grok'] = ProviderConfig(
                name='grok',
                model=os.getenv('GROK_MODEL', 'grok-beta'),
                api_key=xai_key,
                base_url=os.getenv('XAI_BASE_URL', 'https://api.x.ai/v1')
            )
        
        return configs
    
    def _initialize_primary_provider(self):
        """Initialize primary provider based on environment preference."""
        # Check environment variable for preferred provider
        preferred = os.getenv('AI_PROVIDER', 'anthropic').lower()
        
        # Try preferred provider first
        if preferred in self.provider_configs:
            try:
                self.switch_provider(preferred)
                return
            except Exception as e:
                logger.warning("failed_to_initialize_preferred_provider", 
                             provider=preferred, error=str(e))
        
        # Fallback to any available provider
        for provider_name in ['anthropic', 'openai', 'google', 'grok']:
            if provider_name in self.provider_configs:
                try:
                    self.switch_provider(provider_name)
                    return
                except Exception as e:
                    logger.warning("failed_to_initialize_provider", 
                                 provider=provider_name, error=str(e))
        
        logger.error("no_ai_providers_available", 
                    configured_providers=list(self.provider_configs.keys()))
    
    def switch_provider(self, provider_name: str):
        """
        Switch to a different AI provider.
        
        Args:
            provider_name: Name of provider to switch to
        """
        if provider_name not in self.provider_configs:
            available = ', '.join(self.provider_configs.keys())
            raise ValueError(f"Provider '{provider_name}' not configured. Available: {available}")
        
        # Create provider instance if not exists
        if provider_name not in self.providers:
            provider_config = self.provider_configs[provider_name]
            
            # Convert to provider format
            config_dict = {
                'api_key': provider_config.api_key,
            }
            if provider_config.base_url:
                config_dict['base_url'] = provider_config.base_url
            if provider_config.extra_config:
                config_dict.update(provider_config.extra_config)
            
            provider = create_provider(provider_name, config_dict)
            
            # Check if provider is available
            available, reason = provider.is_available()
            if not available:
                raise ValueError(f"Provider '{provider_name}' not available: {reason}")
            
            self.providers[provider_name] = provider
        
        self.current_provider_name = provider_name
        self.current_provider = self.providers[provider_name]
        
        logger.info("switched_ai_provider", 
                   provider=provider_name,
                   model=self.provider_configs[provider_name].model)
    
    def get_available_providers(self) -> List[str]:
        """Get list of available and configured providers."""
        available = []
        for name, config in self.provider_configs.items():
            try:
                if name not in self.providers:
                    # Test create provider
                    provider = create_provider(name, {'api_key': config.api_key})
                    is_avail, _ = provider.is_available()
                    if is_avail:
                        available.append(name)
                else:
                    available.append(name)
            except Exception:
                continue
        return available
    
    async def generate(
        self,
        messages: Union[List[Dict], List[AIMessage], str],
        model: Optional[str] = None,
        max_tokens: Optional[int] = None,
        temperature: Optional[float] = None,
        system_prompt: Optional[str] = None
    ) -> AIResponse:
        """
        Generate AI response with current provider.
        
        Args:
            messages: Messages to send (can be dict list, AIMessage list, or single string)
            model: Override model name
            max_tokens: Override max tokens
            temperature: Override temperature
            system_prompt: System prompt
        
        Returns:
            AIResponse object
        """
        if not self.current_provider:
            raise RuntimeError("No AI provider available. Check configuration.")
        
        # Convert messages to standard format
        if isinstance(messages, str):
            ai_messages = [AIMessage(role="user", content=messages)]
        elif isinstance(messages, list):
            ai_messages = []
            for msg in messages:
                if isinstance(msg, dict):
                    ai_messages.append(AIMessage(role=msg["role"], content=msg["content"]))
                elif isinstance(msg, AIMessage):
                    ai_messages.append(msg)
                else:
                    raise ValueError(f"Invalid message type: {type(msg)}")
        else:
            raise ValueError(f"Invalid messages type: {type(messages)}")
        
        # Use provider config defaults if not specified
        provider_config = self.provider_configs[self.current_provider_name]
        request = AIRequest(
            messages=ai_messages,
            model=model or provider_config.model,
            max_tokens=max_tokens or getattr(config, 'ai_min_max_tokens', 300),
            temperature=temperature if temperature is not None else getattr(config, 'ai_min_temperature', 0.0),
            system_prompt=system_prompt
        )
        
        # Generate response
        response = await self.current_provider.generate(request)
        
        logger.debug("ai_generation_completed",
                    provider=response.provider,
                    model=response.model,
                    input_tokens=response.usage.get('input_tokens', 0),
                    output_tokens=response.usage.get('output_tokens', 0))
        
        return response
    
    # Backward compatibility methods for existing code
    class Messages:
        """Backward compatibility for anthropic_client.messages.create() pattern."""
        
        def __init__(self, ai_client):
            self.ai_client = ai_client
        
        def create(self, model: str, messages: List[Dict], max_tokens: int, 
                  temperature: float, system: Optional[str] = None):
            """
            Create message completion (sync version for backward compatibility).
            
            This mimics the anthropic client interface for drop-in replacement.
            """
            import asyncio
            
            # Convert to async call
            async def _async_generate():
                return await self.ai_client.generate(
                    messages=messages,
                    model=model,
                    max_tokens=max_tokens,
                    temperature=temperature,
                    system_prompt=system
                )
            
            # Run in event loop
            try:
                loop = asyncio.get_event_loop()
                if loop.is_running():
                    # If we're in an async context, create a task
                    import concurrent.futures
                    with concurrent.futures.ThreadPoolExecutor() as executor:
                        future = executor.submit(asyncio.run, _async_generate())
                        response = future.result()
                else:
                    response = loop.run_until_complete(_async_generate())
            except RuntimeError:
                # No event loop, create new one
                response = asyncio.run(_async_generate())
            
            # Convert to backward-compatible format
            class MockResponse:
                def __init__(self, ai_response: AIResponse):
                    self.content = [MockContent(ai_response.content)]
                    self.usage = MockUsage(ai_response.usage)
            
            class MockContent:
                def __init__(self, text: str):
                    self.text = text
            
            class MockUsage:
                def __init__(self, usage: Dict[str, int]):
                    self.input_tokens = usage.get('input_tokens', 0)
                    self.output_tokens = usage.get('output_tokens', 0)
            
            return MockResponse(response)
    
    @property
    def messages(self):
        """Provide backward compatibility for client.messages.create() pattern."""
        return self.Messages(self)