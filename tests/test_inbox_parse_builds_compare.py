"""Inbox parse-all must hydrate the Compare matrix without a second click."""
from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from orchestrator.pipeline import RFxPipeline


def test_ensure_comparison_builds_from_quotes():
    pipe = RFxPipeline.__new__(RFxPipeline)
    pipe.rfx = MagicMock()
    pipe.quotes = [MagicMock()]
    pipe.comparison = None
    pipe.step = "parsed"
    fake = MagicMock(name="ComparisonTable")
    with patch("orchestrator.pipeline.normalize", return_value=fake) as norm:
        out = pipe.ensure_comparison(force=True)
    assert out is fake
    assert pipe.comparison is fake
    assert pipe.step == "normalized"
    norm.assert_called_once()


def test_set_wizard_step_compare_hydrates():
    pipe = RFxPipeline.__new__(RFxPipeline)
    pipe.rfx = MagicMock()
    pipe.quotes = [MagicMock()]
    pipe.comparison = None
    pipe.wizard_step = "inbox"
    pipe.inbox = []
    pipe.dispatch_log = []
    called = {"n": 0}

    def _ensure(*, force=False):
        called["n"] += 1
        pipe.comparison = MagicMock()
        return pipe.comparison

    pipe.ensure_comparison = _ensure  # type: ignore
    pipe._persist = lambda: None  # type: ignore
    pipe.set_wizard_step("compare")
    assert called["n"] == 1
    assert pipe.wizard_step == "compare"
    assert pipe.comparison is not None


def test_custom_upload_is_in_inbox_for_parse_all(tmp_path):
    """Uploaded file becomes an inbox message so Parse all can include it."""
    from orchestrator.pipeline import RFxPipeline, InboxMessage
    from unittest.mock import MagicMock

    pipe = RFxPipeline.__new__(RFxPipeline)
    pipe.rfx = MagicMock()
    pipe.rfx.vendors = []
    pipe.rfx.rfx_id = "RFX-TEST"
    pipe.inbox = []
    pipe.quotes = []
    pipe.comparison = None
    pipe._persist = lambda: None  # type: ignore
    pipe._inbox_dir = lambda: tmp_path  # type: ignore

    f = tmp_path / "buyer-custom.xlsx"
    f.write_bytes(b"fake")
    msg = pipe.add_inbox_upload(f, vendor_id="")
    assert msg.status == "new"
    assert any(Path(m.path).name == "buyer-custom.xlsx" for m in pipe.inbox)
