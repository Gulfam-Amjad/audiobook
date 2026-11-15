"""Path validation.

Every path that arrives from the browser is untrusted. These helpers reject
traversal attempts and confine file reads to directories the user has
explicitly registered, so a crafted request cannot make the server read or
serve an arbitrary file from the disk.
"""

from __future__ import annotations

from pathlib import Path, PurePath

from app.config import get_settings


class UnsafePathError(ValueError):
    """Raised when a path fails validation."""


def normalize(raw: str) -> Path:
    """Resolve user input to an absolute path, rejecting obvious nonsense."""
    text = (raw or "").strip().strip('"').strip("'")
    if not text:
        raise UnsafePathError("Path is empty.")
    if "\x00" in text:
        raise UnsafePathError("Path contains a null byte.")
    try:
        candidate = Path(text).expanduser()
    except (OSError, RuntimeError) as exc:
        raise UnsafePathError(f"Path could not be parsed: {exc}") from exc
    if not candidate.is_absolute():
        raise UnsafePathError("An absolute path is required, for example D:\\Pictures\\set1.")
    try:
        return candidate.resolve(strict=False)
    except OSError as exc:
        raise UnsafePathError(f"Path could not be resolved: {exc}") from exc


def require_existing_directory(raw: str) -> Path:
    path = normalize(raw)
    if not path.exists():
        raise UnsafePathError(f"Folder does not exist: {path}")
    if not path.is_dir():
        raise UnsafePathError(f"Not a folder: {path}")
    return path


def is_within(child: Path, parent: Path) -> bool:
    try:
        child.resolve(strict=False).relative_to(parent.resolve(strict=False))
        return True
    except ValueError:
        return False


def is_within_any(child: Path, parents: list[Path]) -> bool:
    return any(is_within(child, parent) for parent in parents)


def assert_within_data_root(path: Path) -> Path:
    """Confine a path to the application's own data directory."""
    root = get_settings().data_root
    resolved = path.resolve(strict=False)
    if not is_within(resolved, root):
        raise UnsafePathError(f"Path is outside the data root: {resolved}")
    return resolved


def safe_filename(name: str, *, fallback: str = "file") -> str:
    """Strip anything that could escape a directory or upset Windows."""
    cleaned = PurePath(name).name
    cleaned = "".join(ch for ch in cleaned if ch.isprintable() and ch not in '<>:"/\\|?*')
    cleaned = cleaned.strip(" .")
    # Windows refuses these names regardless of extension.
    reserved = {
        "CON", "PRN", "AUX", "NUL",
        *(f"COM{i}" for i in range(1, 10)),
        *(f"LPT{i}" for i in range(1, 10)),
    }
    if not cleaned or PurePath(cleaned).stem.upper() in reserved:
        return fallback
    return cleaned[:180]


def validate_output_dir(raw: str) -> str | None:
    """Check a user-supplied output folder. Returns a problem, or None.

    Videos are large and this machine's C: drive has very little room, so a
    folder that does not exist or cannot be written to needs to be caught
    while the user is looking at the field, not when a download lands.
    """
    try:
        path = normalize(raw)
    except UnsafePathError as exc:
        return str(exc)

    if path.exists() and not path.is_dir():
        return f"{path} is a file, not a folder."

    try:
        path.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        return f"Could not create {path}: {exc.strerror or exc}"

    probe = path / ".avm-write-test"
    try:
        probe.write_bytes(b"")
        probe.unlink()
    except OSError as exc:
        return f"{path} is not writable: {exc.strerror or exc}"
    return None


def unique_path(target: Path) -> Path:
    """Return ``target``, or ``name (2).ext`` style variant if it exists.

    Used everywhere a file is written so an existing output is never
    silently overwritten.
    """
    if not target.exists():
        return target
    stem, suffix, parent = target.stem, target.suffix, target.parent
    for index in range(2, 1000):
        candidate = parent / f"{stem} ({index}){suffix}"
        if not candidate.exists():
            return candidate
    raise UnsafePathError(f"Could not find a free filename near {target}")
