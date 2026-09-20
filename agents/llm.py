"""Thin Anthropic client helper shared by AI agents."""
from __future__ import annotations

import os
from functools import lru_cache

import anthropic


@lru_cache(maxsize=1)
def get_client() -> anthropic.Anthropic:
    key = os.environ.get("ANTHROPIC_API_KEY", "").strip()
    if not key:
        raise RuntimeError(
            "ANTHROPIC_API_KEY is required for AI agents. "
            "Set the environment variable before calling LLM-backed parsers."
        )
    return anthropic.Anthropic(api_key=key)


def complete(
    *,
    model: str,
    system: str,
    messages: list[dict],
    max_tokens: int = 4096,
    temperature: float = 0.2,
) -> str:
    """Call Anthropic Messages API and return concatenated text.

    ``temperature`` is accepted for call-site compatibility but ignored when the
    installed SDK/API no longer supports it.
    """
    del temperature  # newer anthropic SDKs omit temperature on messages.create
    client = get_client()
    resp = client.messages.create(
        model=model,
        max_tokens=max_tokens,
        system=system,
        messages=messages,
    )
    parts: list[str] = []
    for block in resp.content:
        if getattr(block, "type", None) == "text":
            parts.append(block.text)
        elif hasattr(block, "text"):
            parts.append(block.text)
    return "\n".join(parts).strip()
