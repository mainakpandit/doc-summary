"""Inbox watcher (task_breakdown Step 30 (1)): watches `settings.CORPUS_ROOT /
"inbox"` with `watchdog` and turns a stable new file into an `update` run.

"Stable" means the file's size hasn't changed for `STABILITY_WINDOW_SECONDS`
(polled every `POLL_INTERVAL_SECONDS`) -- the guard against picking up a
file mid-copy that the task text calls out. `watchdog`'s `Observer` runs its
own OS-level watcher thread and calls `FileSystemEventHandler` callbacks
synchronously from that thread; `_InboxEventHandler` bridges into this
process's asyncio loop with `call_soon_threadsafe` onto a queue, so the
actual stability wait and DB writes (`ingest_file`, inserting the `runs`
row) run as ordinary async code in `run_watcher`'s loop rather than
blocking the watcher thread.

Which `corpus_id` a dropped file belongs to is resolved by matching the
watched directory's path against `corpora.inbox_path` (`Corpus` is the only
place that association lives -- see implementation_plan.md section 6.1). A
file dropped in before any corpus declares this directory as its inbox is
logged and skipped, not queued against a guess.

Run as `python -m backend.app.services.watcher`, a separate process from
`worker.py` per implementation_plan.md section 2 ("File watcher" is its own
process in the five-process architecture) -- it only ever inserts a
`pending` `runs` row; `worker.py` is what actually drives it.
"""

from __future__ import annotations

import asyncio
import time
import uuid
from pathlib import Path

import structlog
from sqlalchemy import select
from watchdog.events import FileSystemEvent, FileSystemEventHandler
from watchdog.observers import Observer

from backend.app.config import get_settings
from backend.app.db import AsyncSessionLocal
from backend.app.models.corpus import Corpus
from backend.app.models.run import Run
from backend.app.services.ingestion import ingest_file
from backend.app.services.parsers import UnsupportedFormat

logger = structlog.get_logger(__name__)

STABILITY_WINDOW_SECONDS = 2.0
POLL_INTERVAL_SECONDS = 0.5


def _is_hidden(path: Path) -> bool:
    return path.name.startswith(".")


class _InboxEventHandler(FileSystemEventHandler):
    """Forwards `created`/`moved-in` filesystem events onto an asyncio queue.
    Runs on watchdog's own thread -- `call_soon_threadsafe` is the only safe
    way to hand a path to the asyncio loop driving `run_watcher` from here.
    """

    def __init__(self, loop: asyncio.AbstractEventLoop, queue: asyncio.Queue[Path]) -> None:
        self._loop = loop
        self._queue = queue

    def _enqueue(self, raw_path: str) -> None:
        path = Path(raw_path)
        if path.is_dir() or _is_hidden(path):
            return
        self._loop.call_soon_threadsafe(self._queue.put_nowait, path)

    def on_created(self, event: FileSystemEvent) -> None:
        if not event.is_directory:
            self._enqueue(str(event.src_path))

    def on_moved(self, event: FileSystemEvent) -> None:
        if not event.is_directory:
            self._enqueue(str(event.dest_path))


async def wait_until_stable(path: Path) -> bool:
    """Poll `path`'s size until it hasn't changed for
    `STABILITY_WINDOW_SECONDS`. Returns `False` if the file disappears first
    (e.g. a transient temp file an editor deletes after a rename)."""
    last_size: int | None = None
    stable_since: float | None = None
    while True:
        try:
            size = path.stat().st_size
        except FileNotFoundError:
            return False

        now = time.monotonic()
        if size != last_size:
            last_size, stable_since = size, now
        elif now - stable_since >= STABILITY_WINDOW_SECONDS:
            return True
        await asyncio.sleep(POLL_INTERVAL_SECONDS)


async def _resolve_corpus_id(inbox_dir: Path) -> uuid.UUID | None:
    async with AsyncSessionLocal() as session:
        corpus = await session.scalar(
            select(Corpus).where(Corpus.inbox_path == str(inbox_dir)).order_by(Corpus.created_at)
        )
        return corpus.id if corpus else None


async def process_new_file(inbox_dir: Path, path: Path) -> uuid.UUID | None:
    """Stabilize, ingest, and enqueue an `update` run for one file dropped
    into `inbox_dir`. Returns the new run's id, or `None` if the file
    vanished before stabilizing, its corpus couldn't be resolved, or its
    format isn't supported (each logged, none fail the watcher process)."""
    if not await wait_until_stable(path):
        logger.info("watcher_file_vanished_before_stable", path=str(path))
        return None

    corpus_id = await _resolve_corpus_id(inbox_dir)
    if corpus_id is None:
        logger.warning("watcher_no_corpus_for_inbox", inbox_path=str(inbox_dir), file=str(path))
        return None

    async with AsyncSessionLocal() as session:
        try:
            document = await ingest_file(session, path, corpus_id)
        except UnsupportedFormat as exc:
            logger.warning("watcher_unsupported_format", path=str(path), error=str(exc))
            return None

        run = Run(
            corpus_id=corpus_id,
            kind="update",
            status="pending",
            triggering_document_id=document.id,
        )
        session.add(run)
        await session.commit()
        await session.refresh(run)

    logger.info(
        "watcher_enqueued_update_run",
        run_id=str(run.id),
        corpus_id=str(corpus_id),
        document_id=str(document.id),
        path=str(path),
    )
    return run.id


async def run_watcher() -> None:
    settings = get_settings()
    inbox_dir = settings.CORPUS_ROOT / "inbox"
    inbox_dir.mkdir(parents=True, exist_ok=True)

    loop = asyncio.get_running_loop()
    queue: asyncio.Queue[Path] = asyncio.Queue()

    observer = Observer()
    observer.schedule(_InboxEventHandler(loop, queue), str(inbox_dir), recursive=False)
    observer.start()
    logger.info("watcher_started", inbox_path=str(inbox_dir))

    try:
        while True:
            path = await queue.get()
            asyncio.create_task(process_new_file(inbox_dir, path))
    finally:
        observer.stop()
        observer.join()


def main() -> None:
    asyncio.run(run_watcher())


if __name__ == "__main__":
    main()
