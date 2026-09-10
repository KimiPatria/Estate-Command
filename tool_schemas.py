"""
Provider-neutral tool/function-calling schemas (Bedrock migration plan #4).

The source of truth for a tool is a NEUTRAL dict using plain JSON Schema —
no provider wire format:

    {
        "name": "get_weather_history",
        "description": "...",
        "input_schema": {"type": "object", "properties": {...}, "required": []},
    }

Two translators map that to the provider wire formats:

  * to_openai_tools()  — OpenAI/Groq "type: function" format. Also what
    LangChain's bind_tools() accepts, so this is the format handed to
    llm_client.chat(tools=...) regardless of provider (LangChain re-translates
    for Bedrock internally).
  * to_bedrock_tools() — Bedrock Converse API toolSpec format, for any future
    direct boto3 converse() usage (e.g. Bedrock Agents / KB integrations that
    bypass LangChain).

Adding a provider later means adding a translator here — tool definitions and
agent logic stay untouched.
"""


def make_tool(name: str, description: str, properties: dict | None = None,
              required: list[str] | None = None) -> dict:
    """Build a neutral tool definition from name/description/JSON-schema parts."""
    return {
        "name": name,
        "description": description,
        "input_schema": {
            "type": "object",
            "properties": properties or {},
            "required": required or [],
        },
    }


def to_openai_tools(neutral_tools: list[dict]) -> list[dict]:
    """Neutral -> OpenAI/Groq wire format (also accepted by LangChain bind_tools)."""
    return [
        {
            "type": "function",
            "function": {
                "name": t["name"],
                "description": t["description"],
                "parameters": t["input_schema"],
            },
        }
        for t in neutral_tools
    ]


def to_bedrock_tools(neutral_tools: list[dict]) -> list[dict]:
    """Neutral -> Bedrock Converse API toolConfig.tools entries (toolSpec)."""
    return [
        {
            "toolSpec": {
                "name": t["name"],
                "description": t["description"],
                "inputSchema": {"json": t["input_schema"]},
            },
        }
        for t in neutral_tools
    ]
