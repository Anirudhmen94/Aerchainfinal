"""Crew pipeline dual-write: local STORE_DIR + core.storage (Blob when token set)."""
from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from core import storage
from orchestrator.pipeline import RFxPipeline, STORE_DIR, load_pipeline
from shared_models import LineItem, QuestionnaireItem, RFx, Vendor


def _minimal_rfx(rfx_id: str = "RFX-TEST-BLOB") -> RFx:
    return RFx(
        rfx_id=rfx_id,
        title="Corrugated packaging test",
        scope="Shippers and trays for snacks plant.",
        terms="Delivered, ex-GST, 60-day validity.",
        line_items=[
            LineItem(line_id="LI-1", description="3-ply shipper", qty=1000, uom="pcs")
        ],
        questionnaire=[
            QuestionnaireItem(id="Q1", question="ISO 9001?", knockout=True),
        ],
        vendors=[
            Vendor(vendor_id="V01", name="PackForge", email="v1@example.com"),
        ],
    )


def test_save_load_state_local_backend(tmp_path, monkeypatch):
    """core.storage local backend (no token) still round-trips."""
    monkeypatch.delenv("BLOB_READ_WRITE_TOKEN", raising=False)
    monkeypatch.setenv("LOCAL_STORE_DIR", str(tmp_path / "store"))
    # Re-bind LOCAL_ROOT used by storage helpers
    monkeypatch.setattr(storage, "LOCAL_ROOT", tmp_path / "store")
    assert storage.backend_name() == "local-files"
    payload = {"rfx": {"rfx_id": "RFX-LOCAL", "title": "Local"}, "quotes": [], "step": "draft"}
    storage.save_state("RFX-LOCAL", dict(payload))
    loaded = storage.load_state("RFX-LOCAL")
    assert loaded is not None
    assert loaded["rfx"]["rfx_id"] == "RFX-LOCAL"
    assert "RFX-LOCAL" in storage.list_rfx_ids()


def test_persist_and_load_local_only(tmp_path, monkeypatch):
    """Without Blob token, _persist writes flat JSON under STORE_DIR; load_pipeline reads it."""
    monkeypatch.delenv("BLOB_READ_WRITE_TOKEN", raising=False)
    store = tmp_path / "crew-store"
    store.mkdir()
    monkeypatch.setattr("orchestrator.pipeline.STORE_DIR", store)

    pipe = RFxPipeline()
    pipe.rfx = _minimal_rfx("RFX-LOCAL-1")
    pipe.step = "drafted"
    pipe.wizard_step = "send"
    pipe.brief = "test brief"
    pipe._persist()

    assert (store / "RFX-LOCAL-1.json").exists()
    loaded = load_pipeline("RFX-LOCAL-1")
    assert loaded.rfx is not None
    assert loaded.rfx.rfx_id == "RFX-LOCAL-1"
    assert loaded.wizard_step == "send"


def test_vercel_tmp_without_token(tmp_path, monkeypatch):
    """VERCEL=1 without token: /tmp (or AERCHAIN_DATA_ROOT) still works for flat snapshots."""
    monkeypatch.delenv("BLOB_READ_WRITE_TOKEN", raising=False)
    monkeypatch.setenv("VERCEL", "1")
    root = tmp_path / "aerchain-data"
    monkeypatch.setenv("AERCHAIN_DATA_ROOT", str(root))
    # STORE_DIR was bound at import; point persist at our root/store
    store = root / "store"
    store.mkdir(parents=True)
    monkeypatch.setattr("orchestrator.pipeline.STORE_DIR", store)

    pipe = RFxPipeline()
    pipe.rfx = _minimal_rfx("RFX-VERCEL-TMP")
    pipe.quotes = []
    pipe._persist()
    assert (store / "RFX-VERCEL-TMP.json").exists()

    # Simulate cold start: wipe in-memory, load from disk
    again = load_pipeline("RFX-VERCEL-TMP")
    assert again.rfx is not None
    assert again.rfx.rfx_id == "RFX-VERCEL-TMP"


def test_persist_dual_writes_via_save_state(tmp_path, monkeypatch):
    """When Blob token is set, _persist also calls storage.save_state with snapshot dict."""
    monkeypatch.setenv("BLOB_READ_WRITE_TOKEN", "vercel_blob_rw_store_fake_token_for_test")
    store = tmp_path / "crew-store"
    store.mkdir()
    monkeypatch.setattr("orchestrator.pipeline.STORE_DIR", store)

    calls: list[tuple[str, dict]] = []

    def fake_save(rfx_id: str, state: dict) -> None:
        calls.append((rfx_id, state))

    monkeypatch.setattr(storage, "save_state", fake_save)
    monkeypatch.setattr(storage, "backend_name", lambda: "vercel-blob")

    pipe = RFxPipeline()
    pipe.rfx = _minimal_rfx("RFX-DUAL")
    pipe.step = "parsed"
    pipe.wizard_step = "inbox"
    pipe._persist()

    assert (store / "RFX-DUAL.json").exists()
    assert len(calls) == 1
    assert calls[0][0] == "RFX-DUAL"
    assert calls[0][1]["rfx"]["rfx_id"] == "RFX-DUAL"
    assert calls[0][1]["wizard_step"] == "inbox"


def test_load_pipeline_falls_back_to_storage(tmp_path, monkeypatch):
    """When local files miss, load_pipeline uses storage.load_state."""
    monkeypatch.delenv("BLOB_READ_WRITE_TOKEN", raising=False)
    store = tmp_path / "empty-store"
    store.mkdir()
    monkeypatch.setattr("orchestrator.pipeline.STORE_DIR", store)

    snap = {
        "step": "parsed",
        "wizard_step": "compare",
        "brief": "from blob",
        "rfx": _minimal_rfx("RFX-BLOB-LOAD").model_dump(),
        "dispatch_log": [{"kind": "rfx"}],
        "quotes": [],
        "inbox": [],
        "awards": {"V01": ["LI-1"]},
    }

    monkeypatch.setattr(storage, "load_state", lambda rid: snap if rid == "RFX-BLOB-LOAD" else None)

    loaded = load_pipeline("RFX-BLOB-LOAD")
    assert loaded.rfx is not None
    assert loaded.rfx.rfx_id == "RFX-BLOB-LOAD"
    assert loaded.wizard_step == "compare"
    assert loaded.awards.get("V01") == ["LI-1"]


def test_ensure_inbox_files_reseeds_empty_dir(tmp_path, monkeypatch):
    """After cold start, empty /tmp inbox is re-seeded from VENDOR_DIR before parse."""
    from orchestrator import pipeline as pl

    inbox_root = tmp_path / "inbox" / "RFX-SEED"
    monkeypatch.setattr(pl, "INBOX_DIR", tmp_path / "inbox")

    pipe = RFxPipeline()
    pipe.rfx = _minimal_rfx("RFX-SEED")
    # Snapshot-like inbox pointing at vanished /tmp paths
    from shared_models import InboxMessage

    pipe.inbox = [
        InboxMessage(
            msg_id="msg-1",
            vendor_id="V01",
            vendor_name="PackForge",
            subject="Re: quote",
            from_addr="v1@example.com",
            path=str(inbox_root / "V01_PackForge_response.json"),
            body_preview="gone",
            status="new",
        )
    ]
    assert not Path(pipe.inbox[0].path).exists()
    pipe._ensure_inbox_files()
    assert inbox_root.exists()
    assert any(inbox_root.iterdir())
    assert Path(pipe.inbox[0].path).exists()
