"""
dry_run.py — Full agent lifecycle test with market clock bypassed.

Usage:
    .venv/Scripts/python.exe dry_run.py

Logs are written to both the terminal and dry_run_results.log.
"""
import asyncio
import logging
import os
import sys
from pathlib import Path
from unittest.mock import AsyncMock, patch

# Ensure src is on the path
sys.path.insert(0, str(Path(__file__).parent))

from dotenv import load_dotenv
load_dotenv()

# ── Logging: write to both console and file ────────────────────────────────────
log_file = Path(__file__).parent / "dry_run_results.log"
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    handlers=[
        logging.StreamHandler(sys.stdout),
        logging.FileHandler(log_file, encoding="utf-8"),
    ],
    force=True,
)
logger = logging.getLogger("dry_run")

import src.orchestrator as orchestrator  # noqa: E402 — must come after logging setup


async def main():
    logger.info("=" * 60)
    logger.info("DRY RUN — Full Agent Lifecycle")
    logger.info(f"Logs: {log_file}")
    logger.info("=" * 60)

    # Patch market_is_open → always True so we can test outside trading hours
    with patch(
        "src.data.mcp_client.AlpacaMCPClient.market_is_open",
        new_callable=AsyncMock,
        return_value=True,
    ):
        try:
            await orchestrator.run_cycle()
        except Exception as exc:
            logger.error(f"Cycle raised unhandled exception: {exc}", exc_info=True)

    logger.info("=" * 60)
    logger.info("DRY RUN COMPLETE")
    logger.info(f"Full results saved to: {log_file}")
    logger.info("=" * 60)


if __name__ == "__main__":
    asyncio.run(main())
