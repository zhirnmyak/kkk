"""Small, dependency-free state helpers shared with the bootstrap."""
import json
from contextlib import contextmanager
import os
from pathlib import Path
import tempfile
import time

ROOT = Path(__file__).resolve().parents[1]


def replace_file(source, destination):
    """Windows readers/scanners may briefly deny atomic replacement."""
    for attempt in range(12):
        try:
            Path(source).replace(destination)
            return
        except PermissionError:
            if attempt == 11:
                raise
            time.sleep(min(.005 * 2 ** attempt, .1))


def write_bytes(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(dir=path.parent, prefix=".save-", suffix=".tmp", delete=False) as stream:
        temporary = Path(stream.name)
        stream.write(value)
    try:
        replace_file(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


@contextmanager
def file_transaction(paths):
    """Restore the old setup if a later file cannot be written."""
    previous = {Path(path): Path(path).read_bytes() if Path(path).exists() else None for path in paths}
    try:
        yield
    except BaseException:
        for path, value in previous.items():
            if value is None:
                path.unlink(missing_ok=True)
            else:
                write_bytes(path, value)
        raise


def read_json(path, default=None):
    try:
        value = json.loads(Path(path).read_text("utf-8"))
        return value if isinstance(value, dict) else (default or {})
    except (OSError, ValueError):
        return default or {}


def write_json(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=path.parent,
                                     prefix=".state-", suffix=".tmp", delete=False) as stream:
        temporary = Path(stream.name)
        json.dump(value, stream, ensure_ascii=False, indent=2)
        stream.write("\n")
    try:
        replace_file(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def active_service(root=ROOT):
    state = read_json(Path(root) / ".launcher" / "service.json")
    if (state.get("status") in ("starting", "running", "stopping")
            and isinstance(state.get("heartbeat"), (int, float))
            and (time.time() - state["heartbeat"] < 8 or service_lock_held(root))):
        return state
    return None


def service_lock_held(root=ROOT):
    path = Path(root) / ".launcher" / "service.lock"
    if not path.exists():
        return False
    try:
        with ProcessLock(path):
            return False
    except RuntimeError:
        return True


class ProcessLock:
    """An OS lock is released even when an interrupted process leaves the file."""
    def __init__(self, path):
        self.path = Path(path)
        self.file = None

    def __enter__(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.file = self.path.open("a+b")
        if self.file.tell() == 0:
            self.file.write(b"0")
            self.file.flush()
        self.file.seek(0)
        try:
            if os.name == "nt":
                import msvcrt
                msvcrt.locking(self.file.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(self.file.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            self.file.close()
            self.file = None
            raise RuntimeError("МаксИИмка уже запущена. Вернитесь в открытое окно.") from None
        return self

    def __exit__(self, *_):
        if self.file:
            if os.name == "nt":
                import msvcrt
                self.file.seek(0)
                msvcrt.locking(self.file.fileno(), msvcrt.LK_UNLCK, 1)
            self.file.close()
