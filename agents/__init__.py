"""Five-agent RFx crew: Drafter → Dispatcher → Parser → Normalizer → Analyst."""

from __future__ import annotations

__all__: list[str] = []

try:
    from agents.rfx_drafter import draft_rfx

    __all__.append("draft_rfx")
except Exception:  # teammate module may still be in flux
    pass

try:
    from agents.vendor_dispatcher import VendorDispatcherAgent, dispatch_rfx

    __all__.extend(["dispatch_rfx", "VendorDispatcherAgent"])
except Exception:
    pass

try:
    from agents.document_parser import (
        DocumentParserAgent,
        parse_response,
        parse_vendor_dir,
        parse_vendor_file,
    )

    __all__.extend(
        [
            "parse_response",
            "parse_vendor_file",
            "parse_vendor_dir",
            "DocumentParserAgent",
        ]
    )
except Exception:
    pass

try:
    from agents.normalizer import NormalizerAgent, normalize

    normalize_quotes = normalize  # alias for older call sites
    __all__.extend(["normalize", "normalize_quotes", "NormalizerAgent"])
except Exception:
    pass

try:
    from agents.analyst import AnalystAgent, answer, ask_question

    __all__.extend(["AnalystAgent", "answer", "ask_question"])
except Exception:
    pass
