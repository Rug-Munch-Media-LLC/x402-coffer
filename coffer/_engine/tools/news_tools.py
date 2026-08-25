"""x402 news tools - get_news, get_news_sentiment."""

from typing import Any


async def get_news(token: str, chain: str = "solana") -> dict[str, Any]:
    """Get news mentions for a token."""
    return {
        "token": token,
        "chain": chain,
        "news": [],
        "count": 0,
    }


async def get_news_sentiment(token: str, chain: str = "solana") -> dict[str, Any]:
    """Get news sentiment analysis."""
    return {
        "token": token,
        "chain": chain,
        "sentiment": "neutral",
        "articles": 0,
    }


async def social_signal(query: str) -> dict[str, Any]:
    """Get social signal."""
    return {
        "query": query,
        "signal": "pending",
    }
