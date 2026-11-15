"""Watch the manual-assist inbox and file downloads as they arrive.

The observer runs in a watchdog thread and only schedules work onto the
application's event loop. Matching itself happens in :mod:`manual_assist`,
which is also what the Rescan button calls, so a missed event is recovered
the next time the inbox is walked.
"""

from __future__ import annotations

import asyncio
import logging

from watchdog.events import FileSystemEvent, FileSystemEventHandler
from watchdog.observers import Observer

from app.db import session_scope
from app.services.manual_assist import inbox_root, scan_inbox

log = logging.getLogger(__name__)


class _Handler(FileSystemEventHandler):
    def __init__(self, watcher: InboxWatcher) -> None:
        self.watcher = watcher

    def on_created(self, event: FileSystemEvent) -> None:
        if not event.is_directory:
            self.watcher.nudge()

    def on_moved(self, event: FileSystemEvent) -> None:
        if not event.is_directory:
            self.watcher.nudge()


class InboxWatcher:
    def __init__(self) -> None:
        self.running = False
        self._observer: Observer | None = None
        self._loop: asyncio.AbstractEventLoop | None = None
        self._pending: asyncio.TimerHandle | None = None
        self._scanning = False

    async def start(self) -> None:
        self._loop = asyncio.get_running_loop()
        root = inbox_root()
        self._observer = Observer()
        self._observer.schedule(_Handler(self), str(root), recursive=True)
        self._observer.start()
        self.running = True
        log.info("Inbox watcher started", extra={"path": str(root)})
        await self.scan()

    async def stop(self) -> None:
        self.running = False
        if self._pending is not None:
            self._pending.cancel()
            self._pending = None
        if self._observer is not None:
            self._observer.stop()
            self._observer.join(timeout=5)
            self._observer = None

    def nudge(self) -> None:
        """Schedule a scan from the watchdog thread.

        The delay lets a browser finish writing the file before we read it.
        Repeated events collapse into one scan.
        """
        loop = self._loop
        if loop is None or not self.running:
            return
        loop.call_soon_threadsafe(self._arm)

    def _arm(self) -> None:
        if self._pending is not None:
            self._pending.cancel()
        loop = asyncio.get_running_loop()
        self._pending = loop.call_later(1.5, lambda: asyncio.create_task(self.scan()))

    async def scan(self) -> list[str]:
        if self._scanning:
            return []
        self._scanning = True
        try:
            async with session_scope() as session:
                notes = await scan_inbox(session)
            if notes:
                log.info("Inbox scan filed or held %s file(s)", len(notes))
            return notes
        except Exception:
            log.exception("Inbox scan failed")
            return []
        finally:
            self._scanning = False


_watcher: InboxWatcher | None = None


def get_inbox_watcher() -> InboxWatcher:
    global _watcher
    if _watcher is None:
        _watcher = InboxWatcher()
    return _watcher
