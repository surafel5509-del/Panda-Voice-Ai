"""Bounded, auditable tool runtime for the PANDA VOICE AI agent.

Only explicitly registered tools can run. No shell, filesystem, arbitrary code,
or unrestricted outbound HTTP tool is exposed to model-generated calls.
"""
import ast
import json
import math
import os
from datetime import datetime, timezone
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError


OPENROUTER_BASE_URL = (os.getenv("OPENROUTER_BASE_URL") or "https://openrouter.ai/api/v1").rstrip("/")
OPENROUTER_MODEL = (os.getenv("OPENROUTER_MODEL") or "z-ai/glm-5.3-flash").strip()
OPENROUTER_API_KEY = (os.getenv("OPENROUTER_API_KEY") or "").strip()
OPENROUTER_REASONING = (os.getenv("OPENROUTER_REASONING_ENABLED") or "true").lower() in {"1", "true", "yes", "on"}
MAX_TOOL_STEPS = min(max(int(os.getenv("AGENT_MAX_TOOL_STEPS", "5")), 1), 8)
MAX_SEARCH_RESULTS = min(max(int(os.getenv("AGENT_MAX_SEARCH_RESULTS", "5")), 1), 8)

TOOLS = [
    {"type": "function", "function": {
        "name": "calculator",
        "description": "Safely evaluate a mathematical expression. Supports arithmetic, parentheses, powers, and common math functions.",
        "parameters": {"type": "object", "properties": {
            "expression": {"type": "string", "description": "Math expression, e.g. (125 * 1.15) / 4"}
        }, "required": ["expression"], "additionalProperties": False}
    }},
    {"type": "function", "function": {
        "name": "get_current_datetime",
        "description": "Get the current date and time. Optionally specify an IANA timezone such as Africa/Addis_Ababa.",
        "parameters": {"type": "object", "properties": {
            "timezone": {"type": "string", "description": "IANA timezone; defaults to UTC"}
        }, "additionalProperties": False}
    }},
    {"type": "function", "function": {
        "name": "web_search",
        "description": "Search the live web for current information. Use for recent facts, research, sources and links. Requires server-side TAVILY_API_KEY configuration.",
        "parameters": {"type": "object", "properties": {
            "query": {"type": "string", "description": "Focused search query"}
        }, "required": ["query"], "additionalProperties": False}
    }},
]

_ALLOWED_BINOPS = {
    ast.Add: lambda a, b: a + b, ast.Sub: lambda a, b: a - b,
    ast.Mult: lambda a, b: a * b, ast.Div: lambda a, b: a / b,
    ast.FloorDiv: lambda a, b: a // b, ast.Mod: lambda a, b: a % b,
    ast.Pow: lambda a, b: a ** b,
}
_ALLOWED_UNARY = {ast.UAdd: lambda a: +a, ast.USub: lambda a: -a}
_ALLOWED_FUNCTIONS = {
    "sqrt": math.sqrt, "abs": abs, "round": round,
    "ceil": math.ceil, "floor": math.floor, "log10": math.log10,
}
_ALLOWED_CONSTANTS = {"pi": math.pi, "e": math.e}


def safe_calculate(expression: str):
    if not isinstance(expression, str) or not expression.strip() or len(expression) > 200:
        raise ValueError("Expression must contain 1–200 characters.")
    root = ast.parse(expression, mode="eval")

    def visit(node):
        if isinstance(node, ast.Expression):
            return visit(node.body)
        if isinstance(node, ast.Constant) and type(node.value) in (int, float):
            return node.value
        if isinstance(node, ast.Name) and node.id in _ALLOWED_CONSTANTS:
            return _ALLOWED_CONSTANTS[node.id]
        if isinstance(node, ast.BinOp) and type(node.op) in _ALLOWED_BINOPS:
            left, right = visit(node.left), visit(node.right)
            if isinstance(node.op, ast.Pow) and abs(right) > 8:
                raise ValueError("Exponent magnitude is limited to 8.")
            return _ALLOWED_BINOPS[type(node.op)](left, right)
        if isinstance(node, ast.UnaryOp) and type(node.op) in _ALLOWED_UNARY:
            return _ALLOWED_UNARY[type(node.op)](visit(node.operand))
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id in _ALLOWED_FUNCTIONS and not node.keywords:
            args = [visit(arg) for arg in node.args]
            if len(args) > 2:
                raise ValueError("Too many function arguments.")
            return _ALLOWED_FUNCTIONS[node.func.id](*args)
        raise ValueError("Unsupported expression. Only basic arithmetic and approved math functions are allowed.")

    result = visit(root)
    if isinstance(result, complex) or not math.isfinite(float(result)) or abs(result) > 1e100:
        raise ValueError("Result is outside the supported numeric range.")
    return result


async def _web_search(query: str):
    import httpx
    key = (os.getenv("TAVILY_API_KEY") or "").strip()
    if not key:
        return {"available": False, "message": "Live web search is not configured. Set TAVILY_API_KEY in Railway variables."}
    query = (query or "").strip()
    if not query or len(query) > 400:
        raise ValueError("Search query must contain 1–400 characters.")
    async with httpx.AsyncClient(timeout=httpx.Timeout(25.0, connect=8.0)) as client:
        response = await client.post(
            "https://api.tavily.com/search",
            json={"api_key": key, "query": query, "search_depth": "advanced",
                  "max_results": MAX_SEARCH_RESULTS, "include_answer": True},
        )
    if response.status_code == 401:
        return {"available": False, "message": "Web search credentials were rejected; check TAVILY_API_KEY."}
    if response.status_code == 429:
        return {"available": False, "message": "Web search rate limit reached; try again later."}
    response.raise_for_status()
    body = response.json()
    return {
        "answer": body.get("answer"),
        "results": [{"title": item.get("title"), "url": item.get("url"),
                     "content": (item.get("content") or "")[:1800]}
                    for item in body.get("results", [])[:MAX_SEARCH_RESULTS]],
    }


async def execute_tool(name: str, arguments: dict):
    if not isinstance(arguments, dict):
        raise ValueError("Tool arguments must be a JSON object.")
    if name == "calculator":
        return {"result": safe_calculate(arguments.get("expression", ""))}
    if name == "get_current_datetime":
        requested = (arguments.get("timezone") or "UTC").strip()
        try:
            zone = ZoneInfo(requested)
        except (ZoneInfoNotFoundError, AttributeError):
            raise ValueError("Unknown timezone. Use an IANA timezone such as Africa/Addis_Ababa.")
        now = datetime.now(zone)
        return {"timezone": requested, "datetime": now.isoformat(), "weekday": now.strftime("%A")}
    if name == "web_search":
        return await _web_search(arguments.get("query", ""))
    raise ValueError("This tool is not available.")


class AgentRuntimeError(Exception):
    def __init__(self, message: str, status_code: int = 502):
        super().__init__(message)
        self.status_code = status_code


async def run_agent(messages: list[dict], headers: dict):
    import httpx
    """Run a bounded OpenAI-compatible tool-calling loop and return the final provider response."""
    if not OPENROUTER_API_KEY:
        raise AgentRuntimeError("AI Chat is not configured. Add OPENROUTER_API_KEY to Railway variables.", 503)
    conversation = list(messages)
    total_usage = None
    async with httpx.AsyncClient(timeout=httpx.Timeout(120.0, connect=15.0)) as client:
        for step in range(MAX_TOOL_STEPS + 1):
            payload = {
                "model": OPENROUTER_MODEL,
                "messages": conversation,
                "tools": TOOLS,
                "tool_choice": "auto",
                "reasoning": {"enabled": OPENROUTER_REASONING},
                "temperature": 0.4,
                "max_tokens": 2048,
            }
            try:
                response = await client.post(f"{OPENROUTER_BASE_URL}/chat/completions", headers=headers, json=payload)
            except httpx.TimeoutException:
                raise AgentRuntimeError("AI Agent timed out. Please try again.", 504)
            except httpx.HTTPError:
                raise AgentRuntimeError("Could not connect to the AI provider.", 502)
            if response.status_code == 401:
                raise AgentRuntimeError("OpenRouter authentication failed. Check OPENROUTER_API_KEY.", 503)
            if response.status_code == 429:
                raise AgentRuntimeError("AI provider rate limit reached. Please try again shortly.", 429)
            if response.status_code >= 500:
                raise AgentRuntimeError("AI provider is temporarily unavailable.", 502)
            if response.status_code >= 400:
                raise AgentRuntimeError("AI provider rejected the agent request. Check the configured model supports tool calling.", 502)
            try:
                data = response.json()
                assistant_message = data["choices"][0]["message"]
            except (ValueError, KeyError, IndexError, TypeError):
                raise AgentRuntimeError("AI provider returned an invalid response.", 502)
            total_usage = data.get("usage") or total_usage
            calls = assistant_message.get("tool_calls") or []
            if not calls:
                answer = assistant_message.get("content")
                if not isinstance(answer, str) or not answer.strip():
                    raise AgentRuntimeError("AI provider returned an empty response.", 502)
                return {"assistant_message": assistant_message, "model": data.get("model", OPENROUTER_MODEL),
                        "usage": total_usage, "tool_steps": step}
            if step >= MAX_TOOL_STEPS:
                raise AgentRuntimeError("The agent reached its tool-call limit. Please narrow the task and try again.", 502)
            conversation.append(assistant_message)
            for call in calls:
                function = call.get("function") or {}
                name = function.get("name", "")
                raw_args = function.get("arguments", "{}")
                try:
                    args = json.loads(raw_args) if isinstance(raw_args, str) else raw_args
                    result = await execute_tool(name, args)
                    tool_content = json.dumps(result, ensure_ascii=False, default=str)
                except Exception as exc:
                    tool_content = json.dumps({"error": str(exc)[:500] or "Tool execution failed."}, ensure_ascii=False)
                conversation.append({
                    "role": "tool", "tool_call_id": call.get("id", ""),
                    "name": name, "content": tool_content[:9000],
                })
    raise AgentRuntimeError("The agent could not complete this request.", 502)
