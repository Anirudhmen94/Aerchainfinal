"""Single entry point for every model call.

Design rules enforced here:
- Structured output is always obtained through a forced tool call whose input
  schema is the Pydantic model. The result is validated; one retry feeds the
  validation error back to the model.
- Every call is appended to a caller-supplied log (model, purpose, tokens,
  latency) so the buyer can inspect what the AI was asked to do.
- If no API key is configured we raise a clear error; nothing is ever faked.
"""
from __future__ import annotations

import base64
import json
import os
import time
from typing import Any, Callable, TypeVar

from pydantic import BaseModel, ValidationError

try:
    import anthropic
except ImportError:  # pragma: no cover
    anthropic = None

T = TypeVar("T", bound=BaseModel)

DEFAULT_MODEL = "claude-sonnet-5"
FALLBACK_MODEL = "claude-sonnet-4-5"


class AINotConfigured(RuntimeError):
    pass


def is_configured() -> bool:
    return bool(os.environ.get("ANTHROPIC_API_KEY")) and anthropic is not None


def model_name() -> str:
    return os.environ.get("ANTHROPIC_MODEL") or DEFAULT_MODEL


def _client():
    if not is_configured():
        raise AINotConfigured(
            "ANTHROPIC_API_KEY is not set. Add it to .env locally or to the Vercel project environment variables."
        )
    return anthropic.Anthropic(max_retries=2, timeout=240)


def _log(log: list | None, purpose: str, model: str, usage: Any, started: float, extra: dict | None = None) -> None:
    if log is None:
        return
    entry = {
        "at": time.strftime("%Y-%m-%d %H:%M:%S"),
        "purpose": purpose,
        "model": model,
        "input_tokens": getattr(usage, "input_tokens", None),
        "output_tokens": getattr(usage, "output_tokens", None),
        "latency_s": round(time.time() - started, 2),
    }
    if extra:
        entry.update(extra)
    log.append(entry)


def _create(client, **kwargs):
    """Call messages.create, falling back to an older model if the pinned one is unknown."""
    try:
        return client.messages.create(**kwargs)
    except anthropic.NotFoundError:
        if kwargs.get("model") != FALLBACK_MODEL:
            kwargs["model"] = FALLBACK_MODEL
            return client.messages.create(**kwargs)
        raise


def image_block(data: bytes, media_type: str) -> dict:
    return {
        "type": "image",
        "source": {"type": "base64", "media_type": media_type, "data": base64.b64encode(data).decode("ascii")},
    }


def text_block(text: str) -> dict:
    return {"type": "text", "text": text}


def structured(
    *,
    purpose: str,
    system: str,
    content: str | list[dict],
    schema: type[T],
    max_tokens: int = 8000,
    log: list | None = None,
) -> T:
    """Ask the model to produce an instance of `schema` via a forced tool call."""
    client = _client()
    model = model_name()
    tool = {
        "name": "emit",
        "description": f"Emit the final structured result. {schema.__doc__ or ''}".strip(),
        "input_schema": schema.model_json_schema(),
    }
    messages = [{"role": "user", "content": content if isinstance(content, list) else [text_block(content)]}]
    last_error = None
    for attempt in range(2):
        started = time.time()
        resp = _create(
            client,
            model=model,
            max_tokens=max_tokens,
            system=system,
            tools=[tool],
            tool_choice={"type": "tool", "name": "emit"},
            messages=messages,
        )
        tool_use = next((b for b in resp.content if b.type == "tool_use"), None)
        _log(log, purpose, resp.model, resp.usage, started, {"attempt": attempt + 1, "stop_reason": resp.stop_reason})
        if tool_use is None:
            last_error = f"model returned no tool call (stop_reason={resp.stop_reason})"
        else:
            try:
                return schema.model_validate(tool_use.input)
            except ValidationError as e:
                last_error = str(e)[:3000]
        feedback = (
            "Your previous output did not validate against the schema. Fix these errors and emit again, "
            "keeping all other content identical:\n" + str(last_error)
        )
        messages.append({"role": "assistant", "content": resp.content})
        if tool_use is not None:
            messages.append({"role": "user", "content": [{"type": "tool_result", "tool_use_id": tool_use.id, "content": feedback, "is_error": True}]})
        else:
            messages.append({"role": "user", "content": [text_block(feedback)]})
    raise RuntimeError(f"Structured output failed after retry: {last_error}")


def agent_loop(
    *,
    purpose: str,
    system: str,
    messages: list[dict],
    tools: list[dict],
    execute: Callable[[str, dict], Any],
    max_rounds: int = 8,
    max_tokens: int = 4000,
    log: list | None = None,
) -> tuple[str, list[dict]]:
    """Run a tool-use loop. Returns (final_text, trace) where trace lists every tool call and its result."""
    client = _client()
    model = model_name()
    trace: list[dict] = []
    convo = list(messages)
    final_text = ""
    for _ in range(max_rounds):
        started = time.time()
        resp = _create(
            client,
            model=model,
            max_tokens=max_tokens,
            system=system,
            tools=tools,
            messages=convo,
        )
        _log(log, purpose, resp.model, resp.usage, started, {"stop_reason": resp.stop_reason})
        text_parts = [b.text for b in resp.content if b.type == "text"]
        tool_uses = [b for b in resp.content if b.type == "tool_use"]
        if text_parts:
            final_text = "\n\n".join(text_parts)
        if resp.stop_reason != "tool_use" or not tool_uses:
            break
        convo.append({"role": "assistant", "content": resp.content})
        results = []
        for tu in tool_uses:
            try:
                out = execute(tu.name, tu.input or {})
                ok = True
            except Exception as e:  # tool errors are returned to the model, not raised
                out = {"error": str(e)}
                ok = False
            trace.append({"tool": tu.name, "input": tu.input, "output": out, "ok": ok})
            results.append(
                {
                    "type": "tool_result",
                    "tool_use_id": tu.id,
                    "content": json.dumps(out, ensure_ascii=False, default=str)[:60000],
                    "is_error": not ok,
                }
            )
        convo.append({"role": "user", "content": results})
    return final_text, trace
