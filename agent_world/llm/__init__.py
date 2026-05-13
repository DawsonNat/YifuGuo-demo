"""LLM abstraction layer — model_type + provider dual-layer design.

model_type = pure communication protocol (discriminator level 0)
provider   = nested sub-provider (discriminator level 1, hierarchical keys)
"""

from typing import TYPE_CHECKING

# Lightweight imports that are commonly used
from agent_world.llm._registrar import ChatModelBase, LLMRegistrar
from agent_world.llm.base import BaseChatModel
from agent_world.llm.messages import (
    AssistantMessage,
    BaseMessage,
    SystemMessage,
    UserMessage,
)
from agent_world.llm.messages import (
    ContentPartImageParam as ContentImage,
)
from agent_world.llm.messages import (
    ContentPartRefusalParam as ContentRefusal,
)
from agent_world.llm.messages import (
    ContentPartTextParam as ContentText,
)

# Type stubs for lazy imports
if TYPE_CHECKING:
    from agent_world.llm.providers.anthropic import ChatAnthropic
    from agent_world.llm.providers.anthropic_aws import ChatAnthropicAWS
    from agent_world.llm.providers.anthropic_base import AnthropicCompatBase
    from agent_world.llm.providers.bedrock import ChatBedrock
    from agent_world.llm.providers.google import ChatGoogle
    from agent_world.llm.providers.mistral import ChatMistral
    from agent_world.llm.providers.oci import ChatOCI
    from agent_world.llm.providers.ollama import ChatOllama
    from agent_world.llm.providers.openai import ChatOpenAI
    from agent_world.llm.providers.openai_azure import ChatAzureOpenAI
    from agent_world.llm.providers.openai_base import OpenAICompatBase
    from agent_world.llm.providers.openai_cerebras import ChatCerebras
    from agent_world.llm.providers.openai_deepseek import ChatDeepSeek
    from agent_world.llm.providers.openai_groq import ChatGroq
    from agent_world.llm.providers.openai_openrouter import ChatOpenRouter
    from agent_world.llm.providers.openai_vercel import ChatVercel

# Lazy imports mapping for heavy chat models
_LAZY_IMPORTS = {
    # --- Primary classes ---
    "ChatOpenAI": ("agent_world.llm.providers.openai", "ChatOpenAI"),
    "ChatAzureOpenAI": ("agent_world.llm.providers.openai_azure", "ChatAzureOpenAI"),
    "ChatGroq": ("agent_world.llm.providers.openai_groq", "ChatGroq"),
    "ChatDeepSeek": ("agent_world.llm.providers.openai_deepseek", "ChatDeepSeek"),
    "ChatCerebras": ("agent_world.llm.providers.openai_cerebras", "ChatCerebras"),
    "ChatOpenRouter": ("agent_world.llm.providers.openai_openrouter", "ChatOpenRouter"),
    "ChatVercel": ("agent_world.llm.providers.openai_vercel", "ChatVercel"),
    "OpenAICompatBase": ("agent_world.llm.providers.openai_base", "OpenAICompatBase"),
    "ChatAnthropic": ("agent_world.llm.providers.anthropic", "ChatAnthropic"),
    "ChatAnthropicAWS": ("agent_world.llm.providers.anthropic_aws", "ChatAnthropicAWS"),
    "AnthropicCompatBase": ("agent_world.llm.providers.anthropic_base", "AnthropicCompatBase"),
    "ChatGoogle": ("agent_world.llm.providers.google", "ChatGoogle"),
    "ChatBedrock": ("agent_world.llm.providers.bedrock", "ChatBedrock"),
    "ChatOllama": ("agent_world.llm.providers.ollama", "ChatOllama"),
    "ChatMistral": ("agent_world.llm.providers.mistral", "ChatMistral"),
    "ChatOCI": ("agent_world.llm.providers.oci", "ChatOCI"),
    # --- Legacy aliases ---
    "ChatAnthropicBedrock": ("agent_world.llm.providers.anthropic_aws", "ChatAnthropicAWS"),
    "ChatAWSBedrock": ("agent_world.llm.providers.bedrock", "ChatBedrock"),
    "ChatOCIRaw": ("agent_world.llm.providers.oci", "ChatOCI"),
}

# Cache for model instances - only created when accessed
_model_cache: dict[str, "BaseChatModel"] = {}


def __getattr__(name: str):
    """Lazy import mechanism for heavy chat model imports and model instances."""
    if name in _LAZY_IMPORTS:
        module_path, attr_name = _LAZY_IMPORTS[name]
        try:
            from importlib import import_module

            module = import_module(module_path)
            attr = getattr(module, attr_name)
            return attr
        except ImportError as e:
            raise ImportError(f"Failed to import {name} from {module_path}: {e}") from e

    # Check cache first for model instances
    if name in _model_cache:
        return _model_cache[name]

    # Try to get model instances from models module on-demand
    try:
        from agent_world.llm.models import __getattr__ as models_getattr

        attr = models_getattr(name)
        _model_cache[name] = attr
        return attr
    except (AttributeError, ImportError):
        pass

    raise AttributeError(f"module '{__name__}' has no attribute '{name}'")


__all__ = [
    # Message types
    "BaseMessage",
    "UserMessage",
    "SystemMessage",
    "AssistantMessage",
    "ContentText",
    "ContentRefusal",
    "ContentImage",
    # Registry
    "LLMRegistrar",
    "ChatModelBase",
    # Base classes
    "BaseChatModel",
    "OpenAICompatBase",
    "AnthropicCompatBase",
    # OpenAI-compatible providers
    "ChatOpenAI",
    "ChatAzureOpenAI",
    "ChatGroq",
    "ChatDeepSeek",
    "ChatCerebras",
    "ChatOpenRouter",
    "ChatVercel",
    # Anthropic providers
    "ChatAnthropic",
    "ChatAnthropicAWS",
    # Other providers
    "ChatGoogle",
    "ChatBedrock",
    "ChatOllama",
    "ChatMistral",
    "ChatOCI",
]
