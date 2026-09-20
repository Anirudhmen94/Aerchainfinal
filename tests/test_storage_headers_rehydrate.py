"""Storage header parsing + browser rehydrate cold-start fallback."""
from __future__ import annotations

import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from core import storage
from orchestrator.pipeline import RFxPipeline
from shared_models import LineItem, QuestionnaireItem, RFx, Vendor


def _minimal_rfx(rfx_id: str = "RFX-REHYDRATE") -> RFx:
    return RFx(
        rfx_id=rfx_id,
        title="Rehydrate test",
        scope="Scope",
        terms="Terms",
        line_items=[LineItem(line_id="LI-1", description="box", qty=10, uom="pcs")],
        questionnaire=[QuestionnaireItem(id="Q1", question="ISO?", knockout=True)],
        vendors=[Vendor(vendor_id="V01", name="PackForge", email="v1@example.com")],
    )


def test_parse_store_id_from_vercel_blob_rw_token(monkeypatch):
    monkeypatch.delenv("BLOB_STORE_ID", raising=False)
    monkeypatch.delenv("VERCEL_BLOB_STORE_ID", raising=False)
    tok = "vercel_blob_rw_storeABC123_secretpart_with_underscores"
    assert storage.parse_store_id(tok) == "storeABC123"


def test_parse_store_id_env_fallback(monkeypatch):
    monkeypatch.setenv("BLOB_STORE_ID", "envStore99")
    # Non-rw-shaped token → env wins
    assert storage.parse_store_id("some_other_token_shape") == "envStore99"


def test_headers_omits_empty_store_id(monkeypatch):
    monkeypatch.setenv("BLOB_READ_WRITE_TOKEN", "not_a_standard_shape")
    monkeypatch.delenv("BLOB_STORE_ID", raising=False)
    monkeypatch.delenv("VERCEL_BLOB_STORE_ID", raising=False)
    h = storage._headers()
    assert "authorization" in h
    assert "x-vercel-blob-store-id" not in h


def test_headers_includes_store_id_from_token(monkeypatch):
    monkeypatch.setenv(
        "BLOB_READ_WRITE_TOKEN", "vercel_blob_rw_myStore_sekrit_tail"
    )
    monkeypatch.delenv("BLOB_STORE_ID", raising=False)
    h = storage._headers()
    assert h["x-vercel-blob-store-id"] == "myStore"


def test_encrypted_envelope_token_rejected(monkeypatch):
    monkeypatch.setenv("BLOB_READ_WRITE_TOKEN", "eyJ2IjoiMiIsImRhdGEiOiJhYmMifQ==")
    assert storage._token() is None
    assert storage.blob_configured() is False


def test_persist_reraises_blob_failure_on_vercel(tmp_path, monkeypatch):
    monkeypatch.setenv("VERCEL", "1")
    monkeypatch.setenv("BLOB_READ_WRITE_TOKEN", "vercel_blob_rw_store_fake")
    store = tmp_path / "crew-store"
    store.mkdir()
    monkeypatch.setattr("orchestrator.pipeline.STORE_DIR", store)

    def boom(rfx_id: str, state: dict) -> None:
        raise RuntimeError("403 Forbidden: Cannot get store id from token or header")

    monkeypatch.setattr(storage, "save_state", boom)
    monkeypatch.setattr(storage, "blob_configured", lambda: True)
    monkeypatch.setattr(storage, "backend_name", lambda: "vercel-blob")

    pipe = RFxPipeline()
    pipe.rfx = _minimal_rfx("RFX-BLOB-FAIL")
    with pytest.raises(RuntimeError, match="Blob persist failed"):
        pipe._persist()


def test_rehydrate_and_session_json(tmp_path, monkeypatch):
    monkeypatch.delenv("BLOB_READ_WRITE_TOKEN", raising=False)
    monkeypatch.delenv("VERCEL", raising=False)
    store = tmp_path / "crew-store"
    store.mkdir()
    monkeypatch.setattr("orchestrator.pipeline.STORE_DIR", store)
    monkeypatch.setattr("app_crew.STORE_DIR", store)

    import app_crew

    app_crew._SESSIONS.clear()
    client = TestClient(app_crew.app)

    pipe = RFxPipeline()
    pipe.rfx = _minimal_rfx("RFX-LS-1")
    pipe.wizard_step = "compare"
    pipe.brief = "cold start demo"
    snap = pipe.snapshot()

    # Cold: nothing on server
    r = client.get("/crew/RFX-LS-1/wizard")
    assert r.status_code == 404
    assert b"Session missing" in r.content or b"localStorage" in r.content

    # Rehydrate from browser-held JSON
    r2 = client.post("/crew/rehydrate", json=snap)
    assert r2.status_code in (200, 303)
    if r2.status_code == 200:
        body = r2.json()
        assert body["ok"] is True
        assert body["rfx_id"] == "RFX-LS-1"
        assert "/wizard" in body["redirect"]

    assert (store / "RFX-LS-1.json").exists()

    r3 = client.get("/crew/RFX-LS-1/wizard")
    assert r3.status_code == 200

    r4 = client.get("/crew/RFX-LS-1/session.json")
    assert r4.status_code == 200
    data = r4.json()
    assert data["rfx"]["rfx_id"] == "RFX-LS-1"
    assert data["wizard_step"] == "compare"


def test_healthz_storage_local(tmp_path, monkeypatch):
    monkeypatch.delenv("BLOB_READ_WRITE_TOKEN", raising=False)
    monkeypatch.setenv("LOCAL_STORE_DIR", str(tmp_path / "store"))
    monkeypatch.setattr(storage, "LOCAL_ROOT", tmp_path / "store")
    import app_crew

    client = TestClient(app_crew.app)
    r = client.get("/healthz/storage")
    assert r.status_code == 200
    body = r.json()
    assert body["backend"] == "local-files"
    assert body["write_ok"] is True
    assert body["read_ok"] is True
    assert body["ok"] is True
