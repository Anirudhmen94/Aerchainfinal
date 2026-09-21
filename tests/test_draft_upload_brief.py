"""Draft-page requirements file upload → brief + generate (LLM mocked)."""
from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
from fastapi.testclient import TestClient

from orchestrator.pipeline import RFxPipeline
from shared_models import LineItem, QuestionnaireItem, RFx, Vendor


def _empty_rfx(rfx_id: str = "RFX-UPLOAD-1") -> RFx:
    return RFx(
        rfx_id=rfx_id,
        title="Upload brief test",
        scope="",
        terms="INR per piece",
        line_items=[],
        questionnaire=[],
        vendors=[],
    )


def _drafted_rfx(rfx_id: str = "RFX-UPLOAD-1") -> RFx:
    return RFx(
        rfx_id=rfx_id,
        title="Chakan packaging",
        scope="Corrugated cartons for Chakan plant",
        terms="INR per piece",
        line_items=[
            LineItem(line_id="L01", description="3-ply RSC 300x200x150", qty=12000, uom="ea"),
            LineItem(line_id="L02", description="5-ply RSC 400x300x200", qty=8000, uom="ea"),
        ],
        questionnaire=[QuestionnaireItem(id="Q1", question="ISO 9001?", knockout=True)],
        vendors=[Vendor(vendor_id="V01", name="PackForge", email="v1@example.com")],
    )


@pytest.fixture()
def client(tmp_path, monkeypatch):
    monkeypatch.delenv("BLOB_READ_WRITE_TOKEN", raising=False)
    monkeypatch.delenv("VERCEL", raising=False)
    store = tmp_path / "crew-store"
    store.mkdir()
    uploads = tmp_path / "uploads"
    uploads.mkdir()
    monkeypatch.setattr("orchestrator.pipeline.STORE_DIR", store)
    monkeypatch.setattr("app_crew.STORE_DIR", store)
    monkeypatch.setattr("app_crew.DATA_ROOT", tmp_path)

    import app_crew

    app_crew._SESSIONS.clear()
    return TestClient(app_crew.app), app_crew, tmp_path


def test_brief_text_from_txt_upload():
    import app_crew

    text = (
        "Need ~30 corrugated SKUs for Chakan plant FY26. "
        "ISO 9001 and FSC food-contact knockouts required."
    )
    out = app_crew._brief_text_from_upload("reqs.txt", text.encode("utf-8"))
    assert "Chakan" in out
    assert "ISO 9001" in out
    assert len(out) >= 10


def test_brief_text_rejects_unsupported():
    import app_crew

    with pytest.raises(ValueError, match="Unsupported"):
        app_crew._brief_text_from_upload("photo.png", b"\x89PNG")


def test_upload_txt_sets_brief_and_generates(client):
    tc, app_crew, tmp_path = client
    pipe = RFxPipeline()
    pipe.rfx = _empty_rfx()
    pipe.brief = ""
    pipe.wizard_step = "draft"
    pipe._persist = lambda: None  # type: ignore
    app_crew._SESSIONS[pipe.rfx.rfx_id] = pipe

    brief_body = (
        "Buyer needs corrugated packaging for Chakan: about thirty SKUs, "
        "ISO 9001 and FSC certified board, food-contact compliant inks."
    )

    drafted = _drafted_rfx()

    def fake_draft(self, brief, **kwargs):
        self.brief = (brief or "").strip()
        self.rfx = drafted
        self.rfx.rfx_id = "RFX-UPLOAD-1"
        return self.rfx

    with patch.object(RFxPipeline, "draft", fake_draft), patch.object(
        RFxPipeline, "regenerate_lines", MagicMock()
    ):
        r = tc.post(
            "/crew/RFX-UPLOAD-1/draft/upload-brief",
            files={"file": ("requirements.txt", brief_body.encode("utf-8"), "text/plain")},
            follow_redirects=False,
        )

    assert r.status_code == 303, r.text
    assert "step=draft" in r.headers.get("location", "")
    assert "Chakan" in pipe.brief
    assert "thirty SKUs" in pipe.brief or "thirty" in pipe.brief.lower()
    assert pipe.rfx is not None
    assert len(pipe.rfx.line_items) >= 1
    # Original file saved under uploads
    saved = list((tmp_path / "uploads" / "RFX-UPLOAD-1").glob("requirements.txt"))
    assert saved, "uploaded file should be persisted"


def test_upload_empty_file_errors(client):
    tc, app_crew, _ = client
    pipe = RFxPipeline()
    pipe.rfx = _empty_rfx("RFX-UPLOAD-EMPTY")
    pipe._persist = lambda: None  # type: ignore
    app_crew._SESSIONS[pipe.rfx.rfx_id] = pipe

    r = tc.post(
        "/crew/RFX-UPLOAD-EMPTY/draft/upload-brief",
        files={"file": ("empty.txt", b"", "text/plain")},
        follow_redirects=False,
    )
    assert r.status_code == 400
    assert b"empty" in r.content.lower() or b"enough text" in r.content.lower()
