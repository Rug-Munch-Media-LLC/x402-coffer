"""x402 tools registry - main entry point for x402 tool implementations.

Per v4.0 §T26. Provides a unified interface to all x402-powered tools.
Tools are delegated to domain/x402/tools/*.py for organization.

Registry shape:
  tools = {
    "tool_name": {
      "function": async function,
      "free_tier": bool,
      "pro_cents": int | None,
      "ent_cents": int | None,
      "description": str,
    }
  }
"""

from typing import Any

from .label_tools import get_entity_labels, get_wallet_labels
from .market_tools import get_market_overview, get_trending
from .news_tools import get_news, get_news_sentiment
from .report_tools import generate_report, get_report
from .scanner_tools import get_scan_result, run_scan
from .token_tools import get_token_info, get_token_risk
from .wallet_tools import get_wallet_analysis, get_wallet_history

# Tool registry
TOOLS: dict[str, dict[str, Any]] = {
    "get_token_risk": {
        "function": get_token_risk,
        "free_tier": True,
        "pro_cents": 50,
        "ent_cents": 40,
        "description": "Analyze token for rug pull indicators, honeypots, mintability",
    },
    "get_token_info": {
        "function": get_token_info,
        "free_tier": True,
        "pro_cents": 10,
        "ent_cents": 8,
        "description": "Get basic token metadata (name, symbol, supply, holders)",
    },
    "get_wallet_analysis": {
        "function": get_wallet_analysis,
        "free_tier": False,
        "pro_cents": 200,
        "ent_cents": 150,
        "description": "Deep wallet behavior analysis (PnL, trading patterns, personas)",
    },
    "get_wallet_history": {
        "function": get_wallet_history,
        "free_tier": False,
        "pro_cents": 100,
        "ent_cents": 80,
        "description": "Get transaction history for a wallet",
    },
    "get_market_overview": {
        "function": get_market_overview,
        "free_tier": True,
        "pro_cents": 50,
        "ent_cents": 40,
        "description": "Market overview for a chain (top tokens, volume, trend)",
    },
    "get_trending": {
        "function": get_trending,
        "free_tier": True,
        "pro_cents": 30,
        "ent_cents": 25,
        "description": "Get trending tokens on a chain",
    },
    "run_scan": {
        "function": run_scan,
        "free_tier": True,
        "pro_cents": 100,
        "ent_cents": 80,
        "description": "Run full SENTINEL scan on a token",
    },
    "get_scan_result": {
        "function": get_scan_result,
        "free_tier": False,
        "pro_cents": 50,
        "ent_cents": 40,
        "description": "Get scan result for a token",
    },
    "generate_report": {
        "function": generate_report,
        "free_tier": False,
        "pro_cents": 500,
        "ent_cents": 400,
        "description": "Full AI research report (7 sections, $5)",
    },
    "get_report": {
        "function": get_report,
        "free_tier": False,
        "pro_cents": 200,
        "ent_cents": 150,
        "description": "Get previously generated report",
    },
    "get_news": {
        "function": get_news,
        "free_tier": True,
        "pro_cents": 30,
        "ent_cents": 25,
        "description": "Get news mentions for a token/wallet",
    },
    "get_news_sentiment": {
        "function": get_news_sentiment,
        "free_tier": True,
        "pro_cents": 50,
        "ent_cents": 40,
        "description": "Get news sentiment analysis",
    },
    "get_wallet_labels": {
        "function": get_wallet_labels,
        "free_tier": True,
        "pro_cents": 20,
        "ent_cents": 15,
        "description": "Get labels for a wallet address",
    },
    "get_entity_labels": {
        "function": get_entity_labels,
        "free_tier": False,
        "pro_cents": 50,
        "ent_cents": 40,
        "description": "Get entity labels from Neo4j graph",
    },
}


def list_tools() -> list[dict]:
    """List all available x402 tools."""
    return [
        {
            "tool_id": name,
            "description": info["description"],
            "free_tier": info["free_tier"],
            "pro_cents": info.get("pro_cents"),
            "ent_cents": info.get("ent_cents"),
        }
        for name, info in TOOLS.items()
    ]


def get_tool(tool_id: str) -> dict | None:
    """Get a tool by ID."""
    return TOOLS.get(tool_id)


async def call_tool(tool_id: str, **kwargs) -> dict:
    """Call a tool by ID with arguments."""
    tool = TOOLS.get(tool_id)
    if not tool:
        return {"error": f"Unknown tool: {tool_id}"}
    try:
        result = await tool["function"](**kwargs)
        return {"tool_id": tool_id, "result": result}
    except Exception as e:
        return {"error": str(e)}
