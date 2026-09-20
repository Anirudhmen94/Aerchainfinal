"""Orchestrator — Drafter → Dispatcher → Parser → Normalizer → Analyst.

Calls public functions from agents/*. Persists JSON snapshots under data/store/
so the FastAPI crew UI can resume an event across requests.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any, Optional, Union

from agents.analyst import ask_question
from agents.document_parser import parse_response, parse_vendor_dir
from agents.normalizer import normalize
from agents.rfx_drafter import draft_rfx
from agents.vendor_dispatcher import VendorDispatcherAgent
from shared_models import ComparisonTable, ExtractedQuote, RFx

ROOT = Path(__file__).resolve().parent.parent
VENDOR_DIR = ROOT / "data" / "vendor_responses"
STORE_DIR = ROOT / "data" / "store"
OUTBOX_DIR = ROOT / "data" / "outbox"

# Aliases expected by app_crew.py
STORE_DIR = STORE_DIR
VENDOR_DIR = VENDOR_DIR

_STRUCTURED = {".json", ".csv"}


def _to_quote(payload: Any, rfx: Optional[RFx] = None) -> ExtractedQuote:
    """Accept ExtractedQuote, filesystem path, or parser dict."""
    if isinstance(payload, ExtractedQuote):
        return payload
    if isinstance(payload, (str, Path)):
        return parse_response(payload, "", rfx)
    if isinstance(payload, dict):
        fields = {k: payload[k] for k in ExtractedQuote.model_fields if k in payload}
        if "source_format" not in fields:
            fields["source_format"] = (
                payload.get("source_format")
                or payload.get("parse_method")
                or "unknown"
            )
        if "vendor_id" not in fields:
            fields["vendor_id"] = payload.get("vendor_id") or "UNKNOWN"
        if "notes" not in fields:
            fields["notes"] = (
                payload.get("notes") or payload.get("extraction_notes") or ""
            )
        return ExtractedQuote.model_validate(fields)
    raise TypeError(f"Cannot coerce quote from {type(payload)!r}")


def _has_anthropic() -> bool:
    return bool(os.environ.get("ANTHROPIC_API_KEY", "").strip())


class RFxPipeline:
    """In-memory crew session for one sourcing event."""

    def __init__(self) -> None:
        self.brief: str = ""
        self.rfx: Optional[RFx] = None
        self.dispatch_log: list[dict[str, Any]] = []
        self.dispatch_paths: list[str] = []
        self.quotes: list[ExtractedQuote] = []
        self.comparison: Optional[ComparisonTable] = None
        self.chat: list[dict[str, Any]] = []
        self.step: str = "idle"

    def draft(self, brief: str, **kwargs: Any) -> RFx:
        self.brief = brief
        self.rfx = draft_rfx(brief, **kwargs)
        self.step = "drafted"
        self._persist()
        return self.rfx

    def dispatch(self, *, stub: bool = True) -> list[dict[str, Any]]:
        if not self.rfx:
            raise RuntimeError("Draft an RFx before dispatching.")
        agent = VendorDispatcherAgent(stub=stub, outbox_dir=OUTBOX_DIR)
        self.dispatch_paths = [str(p) for p in agent.dispatch(self.rfx)]
        self.dispatch_log = list(agent.dispatch_log)
        for row in self.dispatch_log:
            row.setdefault("delivery", row.get("status") or row.get("delivery") or "stubbed")
        self.step = "dispatched"
        self._persist()
        return self.dispatch_log

    def ingest(
        self,
        paths: Optional[list[Union[str, Path]]] = None,
        directory: Optional[Union[str, Path]] = None,
    ) -> list[ExtractedQuote]:
        if not self.rfx:
            raise RuntimeError("Draft an RFx before ingesting vendor files.")
        quotes: list[ExtractedQuote] = []
        if paths:
            for p in paths:
                quotes.append(self._parse_one(p))
        else:
            target = Path(directory) if directory else VENDOR_DIR
            if _has_anthropic():
                raw = parse_vendor_dir(str(target))
                quotes = [_to_quote(item, self.rfx) for item in raw]
            else:
                # Offline demo: only deterministic JSON/CSV parsers (no LLM).
                for path in sorted(target.iterdir()):
                    if not path.is_file() or path.name.startswith("."):
                        continue
                    if path.suffix.lower() not in _STRUCTURED:
                        continue
                    quotes.append(self._parse_one(path))
        self.quotes = quotes
        self.step = "parsed"
        self._persist()
        return self.quotes

    def _parse_one(self, path: Union[str, Path]) -> ExtractedQuote:
        p = Path(path)
        try:
            return parse_response(p, "", self.rfx)
        except Exception:
            # Fallback via dict wrapper (adds helper fields)
            from agents.document_parser import parse_vendor_file
            return _to_quote(parse_vendor_file(str(p), rfx=self.rfx), self.rfx)

    def normalize(self) -> ComparisonTable:
        if not self.rfx:
            raise RuntimeError("No RFx loaded.")
        if not self.quotes:
            raise RuntimeError("No vendor quotes parsed yet.")
        self.comparison = normalize(self.rfx, self.quotes)
        self.step = "normalized"
        self._persist()
        return self.comparison

    def ask(self, question: str) -> dict[str, Any]:
        if not self.comparison:
            raise RuntimeError("Normalize quotes before asking questions.")
        result = ask_question(question, self.rfx, self.comparison)
        turn = {
            "question": question,
            "answer": result.get("answer") or result.get("markdown") or "",
            "data": result.get("data")
            if result.get("data") is not None
            else result.get("tables"),
            "caveats": result.get("caveats") or [],
            "tool": result.get("tool"),
            "model": result.get("model"),
            "raw": result,
        }
        self.chat.append(turn)
        self.step = "asked"
        self._persist()
        return turn

    def run(
        self,
        brief: str,
        vendor_dir: Optional[Union[str, Path]] = None,
        skip_dispatch: bool = False,
        **draft_kwargs: Any,
    ) -> dict[str, Any]:
        self.draft(brief, **draft_kwargs)
        if not skip_dispatch:
            self.dispatch()
        self.ingest(directory=vendor_dir or VENDOR_DIR)
        self.normalize()
        return self.snapshot()

    def snapshot(self) -> dict[str, Any]:
        return {
            "step": self.step,
            "brief": self.brief,
            "rfx": self.rfx.model_dump() if self.rfx else None,
            "dispatch_log": self.dispatch_log,
            "dispatch_paths": self.dispatch_paths,
            "quotes": [q.model_dump() for q in self.quotes],
            "comparison": self.comparison.model_dump() if self.comparison else None,
            "chat": self.chat,
        }

    def load_snapshot(self, data: dict[str, Any]) -> None:
        self.step = data.get("step", "idle")
        self.brief = data.get("brief") or ""
        self.rfx = RFx.model_validate(data["rfx"]) if data.get("rfx") else None
        self.dispatch_log = list(data.get("dispatch_log") or [])
        self.dispatch_paths = list(data.get("dispatch_paths") or [])
        self.quotes = [ExtractedQuote.model_validate(q) for q in data.get("quotes") or []]
        self.comparison = (
            ComparisonTable.model_validate(data["comparison"])
            if data.get("comparison")
            else None
        )
        self.chat = list(data.get("chat") or [])

    def _persist(self) -> None:
        if not self.rfx:
            return
        STORE_DIR.mkdir(parents=True, exist_ok=True)
        path = STORE_DIR / f"{self.rfx.rfx_id}.json"
        path.write_text(
            json.dumps(self.snapshot(), indent=2, default=str), encoding="utf-8"
        )


def run_pipeline(
    brief: str,
    vendor_dir: Optional[Union[str, Path]] = None,
    **kwargs: Any,
) -> dict[str, Any]:
    return RFxPipeline().run(brief, vendor_dir=vendor_dir, **kwargs)


def load_pipeline(rfx_id: str) -> RFxPipeline:
    path = STORE_DIR / f"{rfx_id}.json"
    pipe = RFxPipeline()
    if path.exists():
        pipe.load_snapshot(json.loads(path.read_text(encoding="utf-8")))
    return pipe
