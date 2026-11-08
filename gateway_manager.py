"""Управление локальным openai-gateway: запуск без отдельного окна и рестарт при смене настроек.

Шлюз живёт как скрытый дочерний процесс МаксИИмки; pid пишется в data/gateway.pid.
Если на порту уже висит чужой (старый) шлюз, restart аккуратно забирает его, когда
это действительно node.exe/Qoder.exe.

Работает и на Windows, и на Linux/macOS (раньше консоль падала вне Windows на
ctypes.windll/creationflags). Шлюзом управляем, только если LLM_BASE_URL локальный.
"""
import os
import shutil
import signal
import subprocess
import time
import threading
from functools import wraps
from pathlib import Path
from urllib.parse import urlparse

import httpx

import config
import model_checks

APP_ROOT = Path(__file__).resolve().parent
PID_PATH = Path(config.DATA_DIR) / "gateway.pid"
GATEWAY_SCRIPT = APP_ROOT / "openai-gateway" / "gateway.mjs"
LOCAL_HOSTS = {"127.0.0.1", "localhost", "::1", "0.0.0.0"}
_lifecycle_lock = threading.RLock()


def _serialized(fn):
    @wraps(fn)
    def call(*args, **kwargs):
        with _lifecycle_lock:
            model_checks.check_cancelled()
            return fn(*args, **kwargs)
    return call

IS_WINDOWS = os.name == "nt"
# вне Windows Popen/run принимают только creationflags=0
CREATE_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0x08000000) if IS_WINDOWS else 0
CREATE_NEW_CONSOLE = getattr(subprocess, "CREATE_NEW_CONSOLE", 0x00000010) if IS_WINDOWS else 0


def _base(settings=None) -> str:
    base = (settings.get("base_url", config.LLM_BASE_URL) if settings else config.LLM_BASE_URL).rstrip("/")
    return base[:-3] if base.endswith("/v1") else base


def _host_port(settings=None) -> tuple[str, int]:
    parsed = urlparse(_base(settings))
    return parsed.hostname or "127.0.0.1", parsed.port or 8787


def _managed(settings=None) -> str:
    """"" — шлюзом управляем; иначе причина, почему он не нужен. Раньше с облачным
    LLM_BASE_URL (или без .env) консоль 30 с ждала «запуска» шлюза на чужом хосте."""
    if config.LLM_MOCK:
        return "не нужен (LLM_MOCK=1)"
    provider = settings.get("provider", config.LLM_PROVIDER) if settings else config.LLM_PROVIDER
    if provider != "qoder":
        return "не нужен (OpenAI-совместимый API)"
    host, _ = _host_port(settings)
    if host not in LOCAL_HOSTS:
        return f"не нужен (LLM_BASE_URL → {host})"
    if not GATEWAY_SCRIPT.is_file():
        return f"не найден {GATEWAY_SCRIPT.relative_to(APP_ROOT)}"
    return ""


def is_up(settings=None) -> bool:
    try:
        return httpx.get(_base(settings) + "/health", timeout=1.5, trust_env=False).status_code == 200
    except Exception:
        return False


def _read_pid() -> int | None:
    try:
        return int(PID_PATH.read_text().strip())
    except (OSError, ValueError):
        return None


def _alive(pid: int | None) -> bool:
    if not pid:
        return False
    if IS_WINDOWS:
        import ctypes
        handle = ctypes.windll.kernel32.OpenProcess(0x1000, False, pid)  # QUERY_LIMITED_INFORMATION
        if not handle:
            return False
        ctypes.windll.kernel32.CloseHandle(handle)
        return True
    try:
        os.kill(pid, 0)  # сигнал 0 — только проверка существования
    except PermissionError:
        return True      # процесс есть, просто чужой
    except OSError:
        return False
    return True


def _terminate(pid: int) -> None:
    if IS_WINDOWS:
        # /T — заодно гасим дочерние qodercli, иначе они остаются висеть
        subprocess.run(["taskkill", "/PID", str(pid), "/T", "/F"], capture_output=True,
                       creationflags=CREATE_NO_WINDOW)
        return
    # шлюз запускаем в своей сессии → гасим всю группу вместе с qodercli;
    # не вышел за 3 с — SIGKILL (аналог taskkill /F)
    for sig in (signal.SIGTERM, signal.SIGKILL):
        try:
            os.killpg(pid, sig)
        except OSError:
            try:
                os.kill(pid, sig)
            except OSError:
                return
        deadline = time.monotonic() + 3
        while time.monotonic() < deadline:
            try:
                if os.waitpid(pid, os.WNOHANG)[0] == pid:  # наш дочерний — забираем, без зомби
                    return
            except ChildProcessError:
                if not _alive(pid):
                    return
            time.sleep(0.1)


def _kill(pid: int) -> None:
    _terminate(pid)
    try:
        PID_PATH.unlink()
    except OSError:
        pass


def _run_text(args: list[str]) -> str:
    try:
        out = subprocess.run(args, capture_output=True, creationflags=CREATE_NO_WINDOW)
    except OSError:
        return ""
    return ((out.stdout or b"") + (out.stderr or b"")).decode("utf-8", "replace")


def _pid_on_port(port: int) -> int | None:
    if not IS_WINDOWS:
        if not shutil.which("lsof"):
            return None
        for token in _run_text(["lsof", "-nP", f"-iTCP:{port}", "-sTCP:LISTEN", "-t"]).split():
            if token.isdigit():
                return int(token)
        return None
    for line in _run_text(["netstat", "-ano", "-p", "tcp"]).splitlines():
        parts = line.split()
        if len(parts) >= 5 and parts[1].endswith(f":{port}") and parts[3].upper() == "LISTENING":
            try:
                return int(parts[4])
            except ValueError:
                continue
    return None


def _is_gateway_process(pid: int) -> bool:
    if not IS_WINDOWS:
        out = _run_text(["ps", "-p", str(pid), "-o", "command="]).lower()
        return "gateway.mjs" in out or "qoder" in out
    out = _run_text(["tasklist", "/FI", f"PID eq {pid}", "/FO", "CSV", "/NH"]).lower()
    return "node.exe" in out or "qoder.exe" in out


def _straggler_pids() -> list[int]:
    """qodercli, оставшиеся от наших запусков: в их командной строке есть путь проекта."""
    if not IS_WINDOWS:
        pids = []
        for line in _run_text(["ps", "-eo", "pid=,args="]).splitlines():
            parts = line.strip().split(None, 1)
            if len(parts) != 2 or not parts[0].isdigit() or int(parts[0]) == os.getpid():
                continue
            # по имени исполняемого файла (сам qodercli или «node …/qodercli»), а не по
            # подстроке в командной строке — иначе под раздачу попадают shell/редактор
            names = [os.path.basename(arg) for arg in parts[1].split()[:2]]
            if any(name.startswith("qodercli") for name in names) and str(APP_ROOT) in parts[1]:
                pids.append(int(parts[0]))
        return pids
    script = ("Get-CimInstance Win32_Process | Where-Object { $_.Name -like 'qodercli*' -and "
              f"$_.CommandLine -like '*{APP_ROOT}*' }} | Select-Object -ExpandProperty ProcessId")
    out = _run_text(["powershell", "-NoProfile", "-Command", script])
    pids = []
    for token in out.split():
        try:
            pids.append(int(token))
        except ValueError:
            pass
    return pids


def _kill_stragglers() -> None:
    for pid in _straggler_pids():
        _terminate(pid)


def _spawn(settings: dict) -> subprocess.Popen:
    model_checks.check_cancelled()
    if _managed(settings):
        raise RuntimeError("Запуск шлюза разрешён только при выборе подключения Qoder")
    host, port = _host_port(settings)
    env = {**os.environ}
    env.update({
        "GATEWAY_HOST": host,
        "GATEWAY_PORT": str(port),
        "GATEWAY_KEY": str(settings.get("api_key", config.LLM_API_KEY)),
        "GATEWAY_DEFAULT_MODEL": str(settings.get("model") or config.LLM_MODEL),
        "GATEWAY_REASONING_EFFORT": str(settings.get("effort") or ""),
        "GATEWAY_CONTEXT_WINDOW": str(int(settings.get("context") or 0)),
        "GATEWAY_MAX_PARALLEL": str(int(settings.get("gw_parallel") or 3)),
        "GATEWAY_TIMEOUT_MS": str(int(settings.get("gw_timeout") or 180) * 1000),
        "GATEWAY_CWD": str(APP_ROOT),
    })
    script = str(GATEWAY_SCRIPT)
    hidden = (settings.get("gw_window") or "hidden") != "window"
    node = shutil.which("node")
    if node:
        cmd = [node, script]
    elif IS_WINDOWS:
        env["ELECTRON_RUN_AS_NODE"] = "1"
        cmd = [str(Path(os.environ.get("LOCALAPPDATA", "")) / "Programs" / "Qoder" / "Qoder.exe"), script]
    else:
        raise FileNotFoundError("не найден node — установи Node.js")
    sink = subprocess.DEVNULL if hidden else None
    kwargs = {"cwd": str(APP_ROOT), "env": env, "stdout": sink, "stderr": sink}
    if IS_WINDOWS:
        kwargs["creationflags"] = CREATE_NO_WINDOW if hidden else CREATE_NEW_CONSOLE
    else:
        # отдельной консоли нет; своя группа процессов, чтобы _kill закрыл и qodercli,
        # и stdin не делим с консолью МаксИИмки
        kwargs.update(start_new_session=True, stdin=subprocess.DEVNULL)
    proc = subprocess.Popen(cmd, **kwargs)
    PID_PATH.parent.mkdir(parents=True, exist_ok=True)
    PID_PATH.write_text(str(proc.pid))
    return proc


def _wait_up(seconds: float = 30, settings=None) -> bool:
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        model_checks.check_cancelled()
        if is_up(settings):
            return True
        time.sleep(0.5)
    return False


def _start(settings: dict, ok: str) -> str:
    try:
        _spawn(settings)
    except OSError as exc:
        return f"не запущен: {exc}"
    return ok if _wait_up(settings=settings) else "не поднялся — смотри openai-gateway/gateway.log"


@_serialized
def ensure(settings: dict) -> str:
    skip = _managed(settings)
    if skip:
        return skip
    if is_up(settings):
        pid = _read_pid()
        if pid and _alive(pid):
            return "уже работает"
        _, port = _host_port(settings)
        candidate = _pid_on_port(port)
        if candidate and _is_gateway_process(candidate):
            # подхватываем шлюз, запущенный до нас, чтобы /выход мог его закрыть
            PID_PATH.parent.mkdir(parents=True, exist_ok=True)
            PID_PATH.write_text(str(candidate))
            return "уже работает (подхватил старый шлюз)"
        return "уже работает (запущен вне МаксИИмки)"
    return _start(settings, "запущен")


@_serialized
def start_for_setup(settings: dict):
    """The setup window borrows external gateways and owns only its own Popen."""
    if _managed(settings) or is_up(settings):
        return None
    process = _spawn(settings)
    try:
        if not _wait_up(settings=settings):
            raise RuntimeError("Шлюз Qoder не запустился. Проверьте Qoder CLI и openai-gateway/gateway.log.")
        return process
    except BaseException:
        stop_owned(process)
        raise


def stop_owned(process) -> None:
    """Close only a child returned to this setup window, never adopt another PID."""
    with _lifecycle_lock:
        if process.poll() is None:
            _terminate(process.pid)
        if _read_pid() == process.pid:
            PID_PATH.unlink(missing_ok=True)


@_serialized
def restart(settings: dict) -> str:
    skip = _managed(settings)
    if skip:
        return skip
    pid = _read_pid()
    if not (pid and _alive(pid) and _is_gateway_process(pid)):
        _, port = _host_port()
        candidate = _pid_on_port(port)
        pid = candidate if candidate and _is_gateway_process(candidate) else None
    if pid and _alive(pid):
        _kill(pid)
        time.sleep(0.7)
    _kill_stragglers()
    return _start(settings, "перезапущен")


@_serialized
def stop() -> None:
    if _managed():
        return
    pid = _read_pid()
    if pid and _alive(pid) and _is_gateway_process(pid):
        _kill(pid)
    else:
        try:
            PID_PATH.unlink()
        except OSError:
            pass
    _kill_stragglers()
