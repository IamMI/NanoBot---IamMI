from typing import Any
from nanobot.agent.tools.base import Tool

class WeatherTool(Tool):
    """
    A toy tool that returns the weather for any location.
    Always returns 'Sunny' as a demonstration of tool execution.
    """

    @property
    def name(self) -> str:
        return "get_weather"

    @property
    def description(self) -> str:
        return (
            "Get the current weather for a specific location. "
            "Use this tool when a user asks about the weather in any city."
        )

    @property
    def parameters(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "location": {
                    "type": "string",
                    "description": "The city and state, e.g. San Francisco, CA",
                },
                "unit": {
                    "type": "string",
                    "enum": ["celsius", "fahrenheit"],
                    "description": "The temperature unit to use.",
                },
            },
            "required": ["location"],
        }

    async def execute(self, location: str, unit: str = "celsius", **kwargs: Any) -> str:
        """
        Execute the toy weather tool.
        """
        # 即使我们拿到了 location 参数，我们也只返回固定的字符串
        return f"The weather in {location} is currently Sunny (25°C)."