import os
import json
import logging
from typing import Any, Dict
from google import genai
from google.genai import types

from .schemas import AnalystOutput, CriticOutput
from .prompts import ANALYST_SYSTEM_PROMPT, CRITIC_SYSTEM_PROMPT

logger = logging.getLogger(__name__)


def get_genai_client() -> genai.Client:
    api_key = os.getenv("GEMINI_API_KEY")
    if not api_key:
        raise ValueError("GEMINI_API_KEY environment variable is not set.")
    return genai.Client(api_key=api_key)


def run_analyst(symbol: str, signals: Dict[str, Any], research_summary: str) -> AnalystOutput:
    """
    Executes the Analyst agent to generate long and short cases and determine direction.
    Uses Gemini structured output (response_schema + response_mime_type) per spec §8.
    """
    client = get_genai_client()
    prompt = ANALYST_SYSTEM_PROMPT.format(
        symbol=symbol,
        signals_json=json.dumps(signals, indent=2),
        research_summary=research_summary or "No research summary available.",
    )

    try:
        response = client.models.generate_content(
            model="gemini-2.5-flash",
            contents=prompt,
            config=types.GenerateContentConfig(
                response_mime_type="application/json",
                response_schema=AnalystOutput,
                temperature=0,
            ),
        )
    except Exception as e:
        logger.error(f"[{symbol}] Gemini API call failed in run_analyst: {e}", exc_info=True)
        raise

    result: AnalystOutput = response.parsed
    if result is None:
        # response.parsed is None when schema enforcement fails — fall back to manual parse
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
    """
    client = get_genai_client()
    prompt = CRITIC_SYSTEM_PROMPT.format(
        analyst_output_json=analyst_output.model_dump_json(indent=2)
    )

    try:
        response = client.models.generate_content(
            model="gemini-2.5-flash",
            contents=prompt,
            config=types.GenerateContentConfig(
                response_mime_type="application/json",
                response_schema=CriticOutput,
                temperature=0,
            ),
        )
    except Exception as e:
        logger.error(f"Gemini API call failed in run_critic: {e}", exc_info=True)
        raise

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

    logger.info(f"Critic completed: conviction_penalty={result.conviction_penalty}")
    return result
