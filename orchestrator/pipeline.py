"""Orchestrator — runs Drafter → Dispatcher → Parser → Normalizer → Analyst.

Keeps agent modules decoupled: each step calls one public function from agents/.
State is a plain dict so the FastAPI UI can pickle it into a session store.
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Optional

from agents.analyst import ask_question
from agents.document_parser import parse_vendor_dir, parse_vendor_file
from agents.normalizer import normalize_quotes
from agents.rfx_drafter import draft_rfx
from agents.vendor_dispatcher import dispatch_rfx
from shared_models import ComparisonTable, ExtractedQuote, RFx

ROOT = Path(__file__).resolve().parent.parent
VENDOR_DIR = ROOT / "data" / "vendor_responses"
STORE_DIR = ROOT / "data" / "store"


class RFxPipeline:
    """In-memory crew session. Steps are idempotent enough for a demo UI."""

    def __init__(self) -> None:
        self.brief: str = ""
        self.rfx: Optional[RFx] = None
        self.dispatch_log: list[dict[str, Any]] = []
        self.quotes: list[ExtractedQuote] = []
        self.comparison: Optional[ComparisonTable] = None
        self.chat: list[dict[str, Any]] = []
        self.step: str = "idle"

    # -- individual stages -------------------------------------------------

    def draft(self, brief: str) -> RFx:
        self.brief = brief
        self.rfx = draft_rfx(brief)
        self.step = "drafted"
        self._persist()
        return self.rfx

    def dispatch(self) -> list[dict[str, Any]]:
        if not self.rfx:
            raise RuntimeError("Draft an RFx before dispatching.")
        self.dispatch_log = dispatch_rfx(self.rfx)
        self.step = "dispatched"
        self._persist()
        return self.dispatch_log

    def ingest(self, paths: Optional[list[str | Path]] = None, directory: Optional[str | Path] = None) -> list[ExtractedQuote]:
        if paths:
            self.quotes = [parse_vendor_file(p) for p in paths]
        else:
            target = Path(directory) if directory else VENDOR_DIR
            self.quotes = parse_vendor_dir(target)
        self.step = "parsed"
        self._persist()
        return self.quotes

    def normalize(self) -> ComparisonTable:
        if not self.rfx:
            raise RuntimeError("No RFx loaded.")
        if not self.quotes:
            raise RuntimeError("No vendor quotes parsed yet.")
        self.comparison = normalize_quotes(self.rfx, self.quotes)
        self.step = "normalized"
        self._persist()
        return self.comparison

    def ask(self, question: str) -> dict[str, Any]:
        if not self.comparison:
            raise RuntimeError("Normalize quotes before asking questions.")
        result = ask_question(self.comparison, question, self.rfx)
        self.chat.append({"question": question, **result})
        self.step = "asked"
        self._persist()
        return result

    # -- full run ----------------------------------------------------------

    def run(
        self,
        brief: str,
        vendor_dir: Optional[str | Path] = None,
        skip_dispatch: bool = False,
    ) -> dict[str, Any]:
        """Run Drafter → Dispatcher → Parser → Normalizer. Analyst is interactive."""
        self.draft(brief)
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
            "quotes": [q.model_dump() for q in self.quotes],
            "comparison": self.comparison.model_dump() if self.comparison else None,
            "chat": self.chat,
        }

    def load_snapshot(self, data: dict[str, Any]) -> None:
        self.step = data.get("step", "idle")
        self.brief = data.get("brief") or ""
        self.rfx = RFx.model_validate(data["rfx"]) if data.get("rfx") else None
        self.dispatch_log = list(data.get("dispatch_log") or [])
        self.quotes = [ExtractedQuote.model_validate(q) for q in data.get("quotes") or []]
        self.comparison = (
            ComparisonTable.model_validate(data["comparison"]) if data.get("comparison") else None
        )
        self.chat = list(data.get("chat") or [])

    def _persist(self) -> None:
        if not self.rfx:
            return
        STORE_DIR.mkdir(parents=True, exist_ok=True)
        path = STORE_DIR / f"{self.rfx.rfx_id}.json"
        path.write_text(json.dumps(self.snapshot(), indent=2), encoding="utf-8")


def run_pipeline(brief: str, vendor_dir: Optional[str | Path] = None) -> dict[str, Any]:
    """Convenience: one-shot Drafter→…→Normalizer for CLI / tests."""
    return RFxPipeline().run(brief, vendor_dir=vendor_dir)


def load_pipeline(rfx_id: str) -> RFxPipeline:
    path = STORE_DIR / f"{rfx_id}.json"
    pipe = RFxPipeline()
    if path.exists():
        pipe.load_snapshot(json.loads(path.read_text(encoding="utf-8")))
    return pipe
