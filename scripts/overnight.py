"""Make queued books one after another, overnight, surviving restarts.

    python scripts/overnight.py add --text D:/scripts/habits.md --title "Atomic Habits" --kind summary
    python scripts/overnight.py add --gutenberg 1661 --voice kokoro:bm_george --template chronicle
    python scripts/overnight.py list
    python scripts/overnight.py cancel 0003-Atomic Habits
    python scripts/overnight.py resume 0003-Atomic Habits
    python scripts/overnight.py run

``add`` takes exactly the options of ``make_book.py`` and saves them as a
job in ``<data root>/queue``; the Book Video page adds jobs the same way.
``run`` works through the jobs in the order they were added. Each job is a
``make_book.py`` run, which already keeps every chapter, word timing and
video segment it finishes, so a job that is interrupted by a crash, a lost
connection or a power cut simply carries on the next time ``run`` starts.

``scripts/install_overnight_task.ps1`` registers ``run`` to start at every
logon, so after a reboot the queue continues by itself. While it runs,
Windows is asked not to sleep.

A job that fails is retried, with a pause between attempts in case the
network was down, and set aside in ``queue/failed`` only after many tries
with its log next to it. The queue itself is ``app.services.book_queue``.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.services import book_queue  # noqa: E402


def show() -> int:
    for row in book_queue.list_jobs(limit_finished=1000):
        print(f"{row['state']:9s} {row['id']}  attempts={row.get('attempts', 0)}  {row.get('last_error', '')}")
    return 0


def main(argv: list[str]) -> int:
    command = argv[0] if argv else "run"
    try:
        if command == "add":
            book_queue.add(argv[1:])
            return 0
        if command == "list":
            return show()
        if command in ("cancel", "resume", "remove") and len(argv) == 2:
            getattr(book_queue, command)(argv[1])
            return 0
        if command == "run":
            return book_queue.run()
    except book_queue.QueueError as exc:
        print(exc, file=sys.stderr)
        return 2
    print(__doc__)
    return 2


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
