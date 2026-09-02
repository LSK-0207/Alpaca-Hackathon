import asyncio
import os
import logging
from typing import Optional
import requests

logger = logging.getLogger(__name__)


def research(symbol: str) -> str:
    """
    Calls Firecrawl to get a 2-4 sentence digest of recent news / analyst commentary.
    Returns empty string if Firecrawl returns nothing useful or fails — never blocks the cycle.

    NOTE: This is a synchronous function called from an async context via
    asyncio.get_event_loop().run_in_executor() in the orchestrator to avoid
    blocking the event loop.
    """
    api_key = os.getenv("FIRECRAWL_API_KEY")
    if not api_key:
        logger.warning("FIRECRAWL_API_KEY not set. Returning empty research string.")
        return ""

    try:
        url = "https://api.firecrawl.dev/v1/search"
        headers = {
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        }
        payload = {
            "query": f"{symbol} stock news analyst commentary latest",
            "limit": 3,
        }
        response = requests.post(url, json=payload, headers=headers, timeout=10)
        if response.status_code != 200:
            logger.warning(
                f"[{symbol}] Firecrawl API error ({response.status_code}): {response.text[:200]}"
            )
            return ""

        data = response.json()
        results = data.get("data", [])
        if not results:
            logger.info(f"[{symbol}] Firecrawl returned no results.")
            return ""

        snippets = []
        for item in results:
            title = item.get("title", "")
            snippet = item.get("description") or item.get("snippet", "")
            if snippet:
                snippets.append(f"- {title}: {snippet}")

        digest = "\n".join(snippets[:3])
        logger.info(f"[{symbol}] Firecrawl research: {len(snippets)} snippet(s) retrieved.")
        return digest

    except requests.exceptions.Timeout:
        logger.warning(f"[{symbol}] Firecrawl request timed out after 10s.")
        return ""
    except requests.exceptions.RequestException as e:
        logger.warning(f"[{symbol}] Firecrawl network error: {e}")
        return ""
    except Exception as e:
        logger.warning(f"[{symbol}] Failed to fetch Firecrawl research: {e}")
        return ""


async def research_async(symbol: str) -> str:
    """
    Async wrapper for research() — runs the blocking HTTP call in a thread pool
    so it does not block the asyncio event loop.
    """
    loop = asyncio.get_event_loop()
    return await loop.run_in_executor(None, research, symbol)
