import os
import json
import logging
import time
import re
from typing import Any, Dict

from google import genai
from google.genai import types
from google.genai import errors as genai_errors

from .schemas import AnalystOutput, CriticOutput
from .prompts import ANALYST_SYSTEM_PROMPT, CRITIC_SYSTEM_PROMPT

logger = logging.getLogger(__name__)

# Use the lite flash model — highest free-tier RPM quota of all available models.
_MODEL = "gemini-3.5-flash-lite"
_MAX_RETRIES = 4


def get_genai_client() -> genai.Client:
    api_key = os.getenv("GEMINI_API_KEY")
    if not api_key:
        raise ValueError("GEMINI_API_KEY environment variable is not set.")
    return genai.Client(api_key=api_key)


def _extract_retry_delay(error: Exception) -> float:
    """
    Parse the `retryDelay` seconds from a Gemini 429 error message.
    Falls back to 60 seconds if it can't be parsed.
    """
    try:
        msg = str(error)
        # Format: 'retryDelay': '49s' or 'Please retry in 49.15s'
        m = re.search(r"retry[^0-9]*(\d+(?:\.\d+)?)\s*s", msg, re.IGNORECASE)
        if m:
            return float(m.group(1)) + 2  # +2s buffer
    except Exception:
        pass
    return 62.0  # conservative fallback: 1 minute + buffer


def _call_with_retry(client: genai.Client, label: str, prompt: str, schema):
    """
    Calls Gemini with automatic retry on 429 RESOURCE_EXHAUSTED.
    Parses the retry delay from the error message so we wait exactly as long
    as the API tells us to — no more, no less.
    """
    for attempt in range(1, _MAX_RETRIES + 1):
        try:
            response = client.models.generate_content(
                model=_MODEL,
                contents=prompt,
                config=types.GenerateContentConfig(
                    response_mime_type="application/json",
                    response_schema=schema,
                    temperature=0,
                ),
            )
            return response
        except genai_errors.ClientError as e:
            if e.code == 429 and attempt < _MAX_RETRIES:
                delay = _extract_retry_delay(e)
                logger.warning(
                    f"[{label}] Gemini 429 rate limit hit (attempt {attempt}/{_MAX_RETRIES}). "
                    f"Waiting {delay:.0f}s before retry..."
                )
                time.sleep(delay)
                continue
            logger.error(f"[{label}] Gemini API error (attempt {attempt}): {e}")
            raise
        except Exception as e:
            logger.error(f"[{label}] Unexpected error calling Gemini: {e}", exc_info=True)
            raise

    raise RuntimeError(f"[{label}] Gemini call failed after {_MAX_RETRIES} retries.")


def run_analyst(symbol: str, signals: Dict[str, Any], research_summary: str) -> AnalystOutput:
    """
    Executes the Analyst agent to generate long and short cases and determine direction.
    Uses Gemini structured output (response_schema + response_mime_type) per spec §8.
    Automatically retries on 429 rate-limit errors.
    """
    client = get_genai_client()
    prompt = ANALYST_SYSTEM_PROMPT.format(
        symbol=symbol,
        signals_json=json.dumps(signals, indent=2),
        research_summary=research_summary or "No research summary available.",
    )

    response = _call_with_retry(client, symbol, prompt, AnalystOutput)

    result: AnalystOutput = response.parsed
    if result is None:
        logger.warning(
            f"[{symbol}] response.parsed is None from Gemini. Attempting manual JSON parse."
        )
        try:
            result = AnalystOutput.model_validate_json(response.text)
        except Exception as parse_err:
            logger.error(
                f"[{symbol}] Manual JSON parse also failed: {parse_err}. "
                f"Raw response: {response.text[:500]}"
            )
            raise ValueError(
                f"Gemini returned unparseable analyst output for {symbol}"
            ) from parse_err

    logger.info(
        f"[{symbol}] Analyst completed: direction={result.direction}, "
        f"confidence={result.confidence_score}"
    )
    return result


def run_critic(analyst_output: AnalystOutput) -> CriticOutput:
    """
    Executes the Critic agent (pre-mortem analysis) on the Analyst output.
    Only called when direction != 'no_trade' (spec §11).
    Automatically retries on 429 rate-limit errors.
    """
    client = get_genai_client()
    symbol = analyst_output.symbol if hasattr(analyst_output, "symbol") else "unknown"
    prompt = CRITIC_SYSTEM_PROMPT.format(
        analyst_output_json=analyst_output.model_dump_json(indent=2)
    )

    response = _call_with_retry(client, f"{symbol}/critic", prompt, CriticOutput)

    result: CriticOutput = response.parsed
    if result is None:
        logger.warning("response.parsed is None from Gemini in run_critic. Attempting manual parse.")
        try:
            result = CriticOutput.model_validate_json(response.text)
        except Exception as parse_err:
            logger.error(
                f"Manual JSON parse also failed in run_critic: {parse_err}. "
                f"Raw response: {response.text[:500]}"
            )
            raise ValueError("Gemini returned unparseable critic output") from parse_err

    logger.info(f"[{symbol}] Critic completed: conviction_penalty={result.conviction_penalty}")
    return result
