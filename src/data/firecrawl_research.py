import os
import logging
from typing import Optional
import requests

logger = logging.getLogger(__name__)


def research(symbol: str) -> str:
    """
    Calls Firecrawl to get a 2-4 sentence digest of recent news / analyst commentary for the given symbol.
    Returns empty string if Firecrawl returns nothing useful or fails, so the cycle is not blocked.
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
            "query": f"{symbol} stock news market analysis latest",
            "limit": 3,
        }
        response = requests.post(url, json=payload, headers=headers, timeout=10)
        if response.status_code != 200:
            logger.warning(f"Firecrawl API error ({response.status_code}): {response.text}")
            return ""

        data = response.json()
        results = data.get("data", [])
        if not results:
            return ""

        snippets = []
        for item in results:
            title = item.get("title", "")
            snippet = item.get("description") or item.get("snippet", "")
            if snippet:
                snippets.append(f"- {title}: {snippet}")

        return "\n".join(snippets[:3])

    except Exception as e:
        logger.warning(f"Failed to fetch Firecrawl research for {symbol}: {e}")
        return ""
