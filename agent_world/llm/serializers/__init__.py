"""Message serializers for each protocol format."""

from agent_world.llm.serializers.anthropic_format import AnthropicMessageSerializer
from agent_world.llm.serializers.bedrock_format import AWSBedrockMessageSerializer
from agent_world.llm.serializers.google_format import GoogleMessageSerializer
from agent_world.llm.serializers.oci_format import OCIRawMessageSerializer
from agent_world.llm.serializers.ollama_format import OllamaMessageSerializer
from agent_world.llm.serializers.openai_format import OpenAIMessageSerializer
from agent_world.llm.serializers.openai_responses import ResponsesAPIMessageSerializer

# Aliases — these providers use the same OpenAI message format
DeepSeekMessageSerializer = OpenAIMessageSerializer
CerebrasMessageSerializer = OpenAIMessageSerializer
OpenRouterMessageSerializer = OpenAIMessageSerializer
VercelMessageSerializer = OpenAIMessageSerializer
GroqMessageSerializer = OpenAIMessageSerializer

__all__ = [
    "OpenAIMessageSerializer",
    "ResponsesAPIMessageSerializer",
    "AnthropicMessageSerializer",
    "GoogleMessageSerializer",
    "AWSBedrockMessageSerializer",
    "OCIRawMessageSerializer",
    "OllamaMessageSerializer",
    "DeepSeekMessageSerializer",
    "CerebrasMessageSerializer",
    "OpenRouterMessageSerializer",
    "VercelMessageSerializer",
    "GroqMessageSerializer",
]
