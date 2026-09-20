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
        align_questionnaire_to_rfx,
        list_inbox,
        parse_all,
        parse_one,
        parse_response,
        parse_vendor_dir,
        parse_vendor_file,
        seed_inbox,
    )

    # Optional aliases if present under alternate names
    try:
        from agents.document_parser import DocumentParserAgent as _DPA  # noqa: F401
    except Exception:
        pass

    __all__.extend(
        [
            "parse_response",
            "parse_vendor_file",
            "parse_vendor_dir",
            "parse_one",
            "parse_all",
            "seed_inbox",
            "list_inbox",
            "align_questionnaire_to_rfx",
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
    from agents.analyst import (
        AnalystAgent,
        answer,
        ask_question,
        shortlist_vendors,
        suggest_split_award,
        validate_award,
    )

    __all__.extend([
        "AnalystAgent",
        "answer",
        "ask_question",
        "shortlist_vendors",
        "suggest_split_award",
        "validate_award",
    ])
except Exception:
    pass
