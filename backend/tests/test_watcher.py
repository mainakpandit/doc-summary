"""Tests for the inbox watcher (task_breakdown Step 30 (1)).

`wait_until_stable` is tested standalone (no DB) with the stability window
monkeypatched down to a few milliseconds so the test doesn't actually wait
seconds. `process_new_file` -- the ingest-then-enqueue half -- is tested
against a real Postgres directly (skipped if unreachable, matching every
other integration test in this suite) without going through `watchdog`'s
`Observer`/OS file-events machinery, which isn't worth the flakiness of
driving in a unit test: the `Observer` thread is a thin, untested-here
adapter that just calls `process_new_file` for every stable path.
"""

from __future__ import annotations

import asyncio

import pytest
from sqlalchemy import delete, select

from backend.app.db import AsyncSessionLocal, engine
from backend.app.models import Chunk, Corpus, Document, Run
from backend.app.services import watcher


async def _db_reachable() -> bool:
    try:
        conn = await engine.connect()
    except Exception:
        return False
    await conn.close()
    return True


async def test_wait_until_stable_returns_true_once_size_settles(tmp_path, monkeypatch):
    monkeypatch.setattr(watcher, "STABILITY_WINDOW_SECONDS", 0.15)
    monkeypatch.setattr(watcher, "POLL_INTERVAL_SECONDS", 0.05)

    path = tmp_path / "growing.md"
    path.write_text("partial")

    assert await watcher.wait_until_stable(path) is True


async def test_wait_until_stable_returns_false_if_file_vanishes(tmp_path, monkeypatch):
    monkeypatch.setattr(watcher, "STABILITY_WINDOW_SECONDS", 10.0)
    monkeypatch.setattr(watcher, "POLL_INTERVAL_SECONDS", 0.02)

    path = tmp_path / "transient.md"
    path.write_text("here then gone")

    async def _delete_soon():
        await asyncio.sleep(0.05)
        path.unlink()

    deleter = asyncio.create_task(_delete_soon())
    try:
        assert await watcher.wait_until_stable(path) is False
    finally:
        await deleter


@pytest.fixture
async def inbox_corpus(tmp_path):
    if not await _db_reachable():
        pytest.skip("DATABASE_URL is unreachable")

    inbox_dir = tmp_path / "inbox"
    inbox_dir.mkdir()

    async with AsyncSessionLocal() as session:
        corpus = Corpus(name="watcher-test", inbox_path=str(inbox_dir))
        session.add(corpus)
        await session.commit()
        await session.refresh(corpus)
        corpus_id = corpus.id

    yield corpus_id, inbox_dir

    async with AsyncSessionLocal() as session:
        doc_ids = (
            await session.scalars(select(Document.id).where(Document.corpus_id == corpus_id))
        ).all()
        await session.execute(delete(Run).where(Run.triggering_document_id.in_(doc_ids)))
        await session.execute(delete(Chunk).where(Chunk.document_id.in_(doc_ids)))
        await session.execute(delete(Document).where(Document.corpus_id == corpus_id))
        await session.execute(delete(Corpus).where(Corpus.id == corpus_id))
        await session.commit()


@pytest.mark.integration
async def test_process_new_file_ingests_and_enqueues_update_run(inbox_corpus, monkeypatch):
    corpus_id, inbox_dir = inbox_corpus
    monkeypatch.setattr(watcher, "STABILITY_WINDOW_SECONDS", 0.05)
    monkeypatch.setattr(watcher, "POLL_INTERVAL_SECONDS", 0.02)

    path = inbox_dir / "new_prd.md"
    path.write_text("# New Feature\n\nOwned by nobody yet.")

    run_id = await watcher.process_new_file(inbox_dir, path)
    assert run_id is not None

    async with AsyncSessionLocal() as session:
        run = await session.get(Run, run_id)
        assert run is not None
        assert run.kind == "update"
        assert run.status == "pending"
        assert run.corpus_id == corpus_id
        assert run.triggering_document_id is not None

        document = await session.get(Document, run.triggering_document_id)
        assert document is not None
        assert document.corpus_id == corpus_id
        assert document.filename == "new_prd.md"
        assert len(document.chunks) >= 1


@pytest.mark.integration
async def test_process_new_file_skips_when_no_corpus_matches_inbox(tmp_path):
    if not await _db_reachable():
        pytest.skip("DATABASE_URL is unreachable")

    orphan_inbox = tmp_path / "unregistered-inbox"
    orphan_inbox.mkdir()
    path = orphan_inbox / "orphan.md"
    path.write_text("# Nobody claims this corpus")

    watcher.STABILITY_WINDOW_SECONDS = 0.05
    watcher.POLL_INTERVAL_SECONDS = 0.02
    try:
        run_id = await watcher.process_new_file(orphan_inbox, path)
    finally:
        watcher.STABILITY_WINDOW_SECONDS = 2.0
        watcher.POLL_INTERVAL_SECONDS = 0.5

    assert run_id is None
