from agent_world.llm.model_families._base import ModelFamily

LlamaFamily = ModelFamily(
    name="llama",
    supports_json_schema=True,
    supports_tool_calling=True,
    name_patterns=(
        "llama",
        "meta-llama/",
        "meta/llama",
    ),
)
