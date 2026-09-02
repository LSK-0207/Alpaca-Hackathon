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
    """
    client = get_genai_client()
    prompt = ANALYST_SYSTEM_PROMPT.format(
        symbol=symbol,
        signals_json=json.dumps(signals, indent=2),
        research_summary=research_summary or "No research summary available.",
    )

    response = client.models.generate_content(
        model="gemini-2.5-flash",
        contents=prompt,
        config=types.GenerateContentConfig(
            response_mime_type="application/json",
            response_schema=AnalystOutput,
            temperature=0,
        ),
    )
    result: AnalystOutput = response.parsed
    return result


def run_critic(analyst_output: AnalystOutput) -> CriticOutput:
    """
    Executes the Critic agent (pre-mortem analysis) on the Analyst output.
    """
    client = get_genai_client()
    prompt = CRITIC_SYSTEM_PROMPT.format(
        analyst_output_json=analyst_output.model_dump_json(indent=2)
    )

    response = client.models.generate_content(
        model="gemini-2.5-flash",
        contents=prompt,
        config=types.GenerateContentConfig(
            response_mime_type="application/json",
            response_schema=CriticOutput,
            temperature=0,
        ),
    )
    result: CriticOutput = response.parsed
    return result
