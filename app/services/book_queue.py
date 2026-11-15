"""The overnight queue of book videos, kept as files so it survives anything.

Each job is a JSON file in ``<data root>/queue`` holding the ``make_book.py``
options for one book. One runner (``scripts/overnight.py run``, started at
logon by the scheduled task or by the Book Video page) works through them
in name order. A job is a ``make_book.py`` child process, and that already
keeps every chapter, word timing and video segment it finishes, so an
interrupted job carries on the next time it runs.

Folders: ``queue/`` waiting and running, ``done/``, ``failed/`` (the
attempt limit used), ``cancelled/`` (stopped from the page; its finished work
is kept, and *resume* puts it back), and ``logs/`` with one log per job.
Previews are numbered ``0000`` so they run before the long books waiting.
"""

from __future__ import annotations

import ctypes
import json
import os
import subprocess
import sys
import time
from collections.abc import Callable
from datetime import datetime
from pathlib import Path
from typing import Any

from app.config import get_settings
from app.services.paths import safe_filename

ROOT = Path(__file__).resolve().parents[2]
MAKE_BOOK = ROOT / "scripts" / "make_book.py"
OVERNIGHT = ROOT / "scripts" / "overnight.py"
#: A long book is interrupted by sleep, a reboot or a full disk several times
#: before it finishes. Giving up after a few tries is how a book never completes.
MAX_ATTEMPTS = 20
RETRY_PAUSE_S = 300
ES_CONTINUOUS = 0x80000000
ES_SYSTEM_REQUIRED = 0x00000001
#: The scheduled task starts below normal, so a browser always beats the book.
NORMAL_PRIORITY_CLASS = 0x00000020
#: Keeps a long recording going while the screen is off, including on battery.
#: Closing the lid still follows the Windows power setting.
ES_AWAYMODE_REQUIRED = 0x00000040
STILL_ACTIVE = 259
STATES = ("waiting", "done", "failed", "cancelled")
SUBFOLDERS = {"waiting": "", "done": "done", "failed": "failed", "cancelled": "cancelled"}
JOB_GLOB = "[0-9][0-9][0-9][0-9]-*.json"


class QueueError(Exception):
    """A queue action could not be done."""


def queue_dir() -> Path:
    folder = get_settings().data_root / "queue"
    for sub in ("", "done", "failed", "cancelled", "logs"):
        (folder / sub).mkdir(parents=True, exist_ok=True)
    return folder


def say(message: str) -> None:
    line = f"[{datetime.now():%Y-%m-%d %H:%M:%S}] {message}"
    if sys.stdout is not None:
        print(line, flush=True)
    with (queue_dir() / "logs" / "overnight.log").open("a", encoding="utf-8") as handle:
        handle.write(line + "\n")


def _write(path: Path, data: dict) -> None:
    partial = path.with_suffix(".json.part")
    partial.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")
    partial.replace(path)


def _read(path: Path) -> dict[str, Any]:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}


def absolute(argv: list[str]) -> list[str]:
    out = list(argv)
    for i, item in enumerate(out[:-1]):
        if item in ("--text", "--art", "--about-file", "--out"):
            out[i + 1] = str(Path(out[i + 1]).resolve())
    return out


def _option(argv: list[str], *names: str) -> str:
    return next((argv[i + 1] for i, a in enumerate(argv[:-1]) if a in names), "")


# --- Adding and listing ---------------------------------------------------------------


def add(argv: list[str], *, book_folder: Path | None = None, label: str = "", preview: bool = False) -> str:
    """Queue a ``make_book.py`` run. Returns the job id."""
    if not argv:
        raise QueueError("A job needs the make_book.py options for the book.")
    folder = queue_dir()
    title = label or _option(argv, "--title", "--name")
    if not title:
        number = _option(argv, "--gutenberg")
        title = f"gutenberg-{number}" if number else "book"
    if preview:
        stem = f"0000-{datetime.now():%Y%m%d%H%M%S}-{safe_filename(title, fallback='book')[:40]}"
    else:
        taken = [int(p.name[:4]) for sub in SUBFOLDERS.values() for p in (folder / sub).glob(JOB_GLOB)]
        number = 1 + max(taken or [0])
        stem = f"{number:04d}-{safe_filename(title, fallback='book')[:50]}"
    job = folder / f"{stem}.json"
    _write(job, {
        "argv": absolute(argv), "added": datetime.now().isoformat(timespec="seconds"),
        "attempts": 0, "status": "queued", "title": title, "preview": preview,
        "folder": str(book_folder) if book_folder else "",
    })
    say(f"Queued {job.name}.")
    return stem


def waiting() -> list[Path]:
    return sorted(queue_dir().glob(JOB_GLOB))


def find(job_id: str) -> tuple[str, Path] | None:
    folder = queue_dir()
    if not job_id or "/" in job_id or "\\" in job_id or ".." in job_id:
        return None
    for state, sub in SUBFOLDERS.items():
        path = folder / sub / f"{job_id}.json"
        if path.is_file():
            return state, path
    return None


def list_jobs(limit_finished: int = 30) -> list[dict[str, Any]]:
    """Every job, waiting first in run order, then the most recently finished."""
    folder = queue_dir()
    rows: list[dict[str, Any]] = []
    for path in waiting():
        data = _read(path)
        state = "running" if data.get("status") == "running" and _alive(int(data.get("pid") or 0)) else "waiting"
        rows.append({"id": path.stem, "state": state, **data})
    finished = []
    for state in ("done", "failed", "cancelled"):
        for path in (folder / SUBFOLDERS[state]).glob(JOB_GLOB):
            data = _read(path)
            finished.append({"id": path.stem, "state": state, **data,
                             "_sort": data.get("finished") or data.get("added") or ""})
    finished.sort(key=lambda row: row["_sort"], reverse=True)
    for row in finished:
        row.pop("_sort", None)
    return rows + finished[:limit_finished]


def log_tail(job_id: str, lines: int = 12) -> str:
    path = queue_dir() / "logs" / f"{job_id}.log"
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ""
    return "\n".join(text.strip().splitlines()[-lines:])


# --- Cancel, resume, remove -------------------------------------------------------------


def _kill_tree(pid: int) -> None:
    if not pid or not _alive(pid):
        return
    if os.name == "nt":
        subprocess.run(["taskkill", "/PID", str(pid), "/T", "/F"], stdout=subprocess.DEVNULL,
                       stderr=subprocess.DEVNULL, check=False,
                       creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    else:
        try:
            os.kill(pid, 9)
        except OSError:
            pass


def cancel(job_id: str) -> None:
    """Stop a waiting or running job. What it already finished stays in the book's folder."""
    found = find(job_id)
    if found is None or found[0] != "waiting":
        raise QueueError("Only a waiting or running job can be cancelled.")
    _, path = found
    data = _read(path)
    target = queue_dir() / "cancelled" / path.name
    data.update(status="cancelled", finished=datetime.now().isoformat(timespec="seconds"))
    _write(path, data)
    # Moved before the kill, so the runner sees the job is gone and does not retry it.
    path.replace(target)
    _kill_tree(int(data.get("pid") or 0))
    say(f"Cancelled {job_id}.")


def resume(job_id: str) -> None:
    """Put a failed or cancelled job back in the queue; it carries on from its finished work."""
    found = find(job_id)
    if found is None or found[0] not in ("failed", "cancelled", "done"):
        raise QueueError("Only a failed, cancelled or finished job can be run again.")
    _, path = found
    data = _read(path)
    data.update(status="queued", attempts=0, pid=None, last_error="")
    target = queue_dir() / path.name
    _write(path, data)
    path.replace(target)
    say(f"Re-queued {job_id}.")


def remove(job_id: str) -> None:
    """Forget a finished job. The book's folder is left alone."""
    found = find(job_id)
    if found is None or found[0] == "waiting":
        raise QueueError("Cancel the job before removing it.")
    found[1].unlink(missing_ok=True)


# --- The runner ----------------------------------------------------------------------------


def _alive(pid: int) -> bool:
    if pid <= 0:
        return False
    if os.name != "nt":
        try:
            os.kill(pid, 0)
            return True
        except OSError:
            return False
    handle = ctypes.windll.kernel32.OpenProcess(0x1000, False, pid)
    if not handle:
        return False
    code = ctypes.c_ulong()
    ctypes.windll.kernel32.GetExitCodeProcess(handle, ctypes.byref(code))
    ctypes.windll.kernel32.CloseHandle(handle)
    return code.value == STILL_ACTIVE


def _lock_file() -> Path:
    return queue_dir() / "overnight.lock"


def runner_pid() -> int:
    try:
        pid = int(_lock_file().read_text().strip())
    except (OSError, ValueError):
        return 0
    return pid if _alive(pid) else 0


def take_lock() -> Path | None:
    lock = _lock_file()
    pid = runner_pid()
    if pid and pid != os.getpid():
        return None
    lock.write_text(str(os.getpid()))
    return lock


def _stay_awake(on: bool) -> None:
    if os.name == "nt":
        flags = ES_CONTINUOUS
        if on:
            flags |= ES_SYSTEM_REQUIRED | ES_AWAYMODE_REQUIRED
        ctypes.windll.kernel32.SetThreadExecutionState(flags)


def _priority(pid: int) -> None:
    """Run this work at normal priority so it is not last in line for the CPU."""
    if os.name != "nt" or pid <= 0:
        return
    handle = ctypes.windll.kernel32.OpenProcess(0x0200, False, pid)
    if not handle:
        return
    ctypes.windll.kernel32.SetPriorityClass(handle, NORMAL_PRIORITY_CLASS)
    ctypes.windll.kernel32.CloseHandle(handle)


def _wait_exit(pid: int) -> int | None:
    """Wait until ``pid`` ends and return its exit code. None if it cannot be watched."""
    if os.name != "nt" or pid <= 0:
        return None
    # SYNCHRONIZE plus enough access to read the exit code.
    handle = ctypes.windll.kernel32.OpenProcess(0x00100400, False, pid)
    if not handle:
        return None
    code = ctypes.c_ulong()
    while True:
        waited = ctypes.windll.kernel32.WaitForSingleObject(handle, 30_000)
        _stay_awake(True)
        if waited == 0:
            break
        if waited != 0x102:
            ctypes.windll.kernel32.CloseHandle(handle)
            return None
    ok = ctypes.windll.kernel32.GetExitCodeProcess(handle, ctypes.byref(code))
    ctypes.windll.kernel32.CloseHandle(handle)
    if not ok or code.value == STILL_ACTIVE:
        return None
    return int(code.value)


def is_speaking(folder: Path) -> bool:
    """True when a live queue process is recording this folder right now."""
    try:
        target = folder.resolve()
    except OSError:
        return False
    for path in waiting():
        data = _read(path)
        job_folder = Path(data.get("folder") or "")
        if not str(job_folder):
            continue
        try:
            same = job_folder.resolve() == target
        except OSError:
            continue
        if same and data.get("status") == "running" and _alive(int(data.get("pid") or 0)):
            return True
    return False


def park_orphans() -> list[str]:
    """Put a job back in the queue when its process died mid-book.

    Sleep, a dropped connection, or a closed app leaves the job file saying
    "running" with a dead process id. Finished chapters stay on disk. The
    next run skips them and starts at the first chapter that has no file.
    """
    parked: list[str] = []
    for path in list(waiting()):
        data = _read(path)
        if data.get("status") != "running":
            continue
        if _alive(int(data.get("pid") or 0)):
            continue
        data["status"] = "queued"
        data["pid"] = None
        data["attempts"] = 0
        data["last_error"] = (
            "Stopped before it finished. Recorded chapters are kept; "
            "it continues from the next chapter."
        )
        data["parked"] = datetime.now().isoformat(timespec="seconds")
        _write(path, data)
        parked.append(path.stem)
        say(f"Parked {path.stem}; it will continue from the last finished chapter.")
    return parked


def python_exe(windowless: bool = False) -> str:
    exe = Path(sys.executable)
    wanted = exe.with_name("pythonw.exe" if windowless else "python.exe")
    return str(wanted if wanted.is_file() else exe)


def ensure_runner() -> bool:
    """Start the runner in the background unless one is already working. True if one was started."""
    if runner_pid() or not waiting():
        return False
    flags = 0
    if os.name == "nt":
        flags = (subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP
                 | getattr(subprocess, "CREATE_NO_WINDOW", 0))
    subprocess.Popen([python_exe(windowless=True), "-X", "utf8", str(OVERNIGHT), "run"], cwd=str(ROOT),
                     stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                     creationflags=flags, close_fds=True)
    return True


def _record_result(path: Path, code: int) -> bool:
    """Store how a job ended. True when it finished or was cancelled."""
    if not path.is_file():
        say(f"{path.stem} was cancelled.")
        return True
    log_file = queue_dir() / "logs" / f"{path.stem}.log"
    data = {**_read(path), "pid": None, "finished": datetime.now().isoformat(timespec="seconds")}
    data["attempts"] = data.get("attempts") or 1
    if code == 0:
        data["status"] = "done"
        _write(path, data)
        path.replace(queue_dir() / "done" / path.name)
        say(f"Finished {path.stem}.")
        return True
    tail = log_file.read_text(encoding="utf-8", errors="replace").strip().splitlines()[-1:] or [""]
    data["last_error"] = tail[0][-300:]
    if int(data["attempts"]) >= MAX_ATTEMPTS:
        data["status"] = "failed"
        _write(path, data)
        path.replace(queue_dir() / "failed" / path.name)
        say(f"Gave up on {path.stem} after {data['attempts']} attempts: {data['last_error']}")
    else:
        data["status"] = "queued"
        _write(path, data)
        say(f"{path.stem} stopped ({data['last_error']}); it will be tried again.")
    return False


def run_job(path: Path, command: Callable[[list[str]], list[str]] | None = None) -> bool:
    """Run one job. True when it finished or was cancelled, False when it should be retried."""
    data = _read(path)
    pid = int(data.get("pid") or 0)
    if data.get("status") == "running" and _alive(pid):
        say(f"{path.stem} is already running; waiting for it to finish.")
        _priority(pid)
        code = _wait_exit(pid)
        if code is None:
            say(f"{path.stem} is still going and this window could not watch it.")
            return False
        return _record_result(path, code)
    data["attempts"] = int(data.get("attempts", 0)) + 1
    data["status"] = "running"
    data["started"] = datetime.now().isoformat(timespec="seconds")
    _write(path, data)
    log_file = queue_dir() / "logs" / f"{path.stem}.log"
    say(f"Starting {path.stem} (attempt {data['attempts']}). Log: {log_file}")
    args = (command or (lambda argv: [python_exe(), "-X", "utf8", str(MAKE_BOOK), *argv]))(data["argv"])
    with log_file.open("a", encoding="utf-8") as handle:
        handle.write(f"\n===== attempt {data['attempts']} at {data['started']} =====\n")
        handle.flush()
        process = subprocess.Popen(args, cwd=str(ROOT), stdout=handle, stderr=subprocess.STDOUT,
                                   creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        data["pid"] = process.pid
        _priority(process.pid)
        if path.is_file():
            _write(path, data)
        code = _wait_exit(process.pid)
        if code is None:
            code = process.wait()
    return _record_result(path, code)


def _pause(seconds: float) -> None:
    """Wait before retrying, but start at once if a fresh job (a preview, say) arrives."""
    end = time.monotonic() + seconds
    while time.monotonic() < end:
        if any(_read(p).get("attempts", 0) == 0 for p in waiting()):
            return
        time.sleep(5)


def run(pause_s: float = RETRY_PAUSE_S) -> int:
    lock = take_lock()
    if lock is None:
        say("The queue is already being worked on by another window.")
        return 0
    _stay_awake(True)
    _priority(os.getpid())
    try:
        while True:
            jobs = waiting()
            if not jobs:
                say("The queue is empty.")
                return 0
            failures = 0
            for path in jobs:
                if not path.is_file():
                    continue
                if not run_job(path):
                    failures += 1
                fresh = [p for p in waiting() if _read(p).get("attempts", 0) == 0 and p.name < path.name]
                if fresh:
                    break
            if failures and waiting() and not any(_read(p).get("attempts", 0) == 0 for p in waiting()):
                say(f"Waiting {pause_s / 60:.0f} minutes before retrying.")
                _pause(pause_s)
    finally:
        _stay_awake(False)
        lock.unlink(missing_ok=True)
