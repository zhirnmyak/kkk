"""Native window bridge: setup uses the same provider and capability code as the bot."""
import json
import os
from pathlib import Path
import secrets
import socket
import subprocess
import threading
import time
import webbrowser

import admin_setup
import config
import console_settings
import gateway_manager
import model_checks
import runtime_settings
from .state import ROOT, ProcessLock, active_service, file_transaction, read_json, replace_file, write_bytes, write_json


def launch_options(value):
    if not isinstance(value, dict) or set(value) - {"panel", "mode"}:
        raise ValueError("Некорректные параметры запуска.")
    if type(value.get("panel")) is not bool or value.get("mode") not in ("terminal", "background"):
        raise ValueError("Выберите панель и режим: терминал или фон.")
    return {"panel": value["panel"], "mode": value["mode"]}


def save_environment(path, updates):
    """Preserve unrelated .env entries while atomically replacing owned keys."""
    path = Path(path)
    original = path.read_text("utf-8-sig") if path.exists() else ""
    from dotenv import set_key
    temporary = path.with_name(".env.launcher.tmp")
    try:
        temporary.write_text(original, encoding="utf-8")
        for name, value in updates.items():
            set_key(str(temporary), name, str(value), quote_mode="always")
        replace_file(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


class RuntimeManager:
    def __init__(self, root=ROOT):
        self.root = Path(root)
        self.lock = threading.Lock()

    def status(self):
        current = active_service(self.root)
        if current:
            return {key: current.get(key) for key in ("status", "mode", "panel", "pid", "message")}
        previous = read_json(self.root / ".launcher" / "service.json")
        message = previous.get("message", "")
        if previous.get("status") in ("starting", "running", "stopping"):
            message = "Процесс завершился или не отвечает. Проверьте .launcher/runtime.log и повторите запуск."
        return {"status": "stopped", "message": message}

    def start(self, options):
        options = launch_options(options)
        with self.lock, ProcessLock(self.root / ".launcher" / "start.lock"):
            return self._start(options)

    def _start(self, options):
        if active_service(self.root):
            raise ValueError("МаксИИмка уже работает. Сначала остановите её.")
        # Check the process lock as well: an old status file is not proof that
        # the previous bot stopped, and must not be overwritten by a new run.
        with ProcessLock(self.root / ".launcher" / "service.lock"):
            pass
        state = read_json(self.root / ".launcher" / "state.json")
        if not state.get("setup_complete"):
            raise ValueError("Сначала завершите настройку.")
        with socket.socket() as sock:
            try:
                sock.bind(("127.0.0.1", config.HTTP_PORT))
            except OSError:
                sock.bind(("127.0.0.1", 0))
            port = sock.getsockname()[1]
        run_id = secrets.token_hex(16)
        env = os.environ.copy()
        env.update(PYTHONUTF8="1", PYTHONIOENCODING="utf-8", HTTP_HOST="127.0.0.1",
                   HTTP_PORT=str(port), MAX_LOCAL_MINIAPP="1" if options["panel"] else "0",
                   MAXIMKA_DESKTOP_RUN=run_id, MAXIMKA_DESKTOP_MODE=options["mode"],
                   MAXIMKA_RUN_PANEL="1" if options["panel"] else "0")
        node = read_json(self.root / ".launcher" / "install.json").get("node")
        if node:
            env["PATH"] = node + os.pathsep + env.get("PATH", "")
        python = self.root / ".venv" / ("Scripts/python.exe" if os.name == "nt" else "bin/python")
        flags = 0
        if os.name == "nt":
            flags = (subprocess.CREATE_NEW_CONSOLE if options["mode"] == "terminal"
                     else subprocess.CREATE_NO_WINDOW)
        runtime_log = self.root / ".launcher" / "runtime.log"
        runtime_log.parent.mkdir(exist_ok=True)
        state["launch"] = options
        write_json(self.root / ".launcher" / "state.json", state)
        service_path = self.root / ".launcher" / "service.json"
        write_json(service_path, {"run_id": run_id, "status": "starting", "heartbeat": time.time(),
                                 "message": "Запускаем МаксИИмку", **options})
        try:
            with runtime_log.open("a", encoding="utf-8") as log:
                process = subprocess.Popen([str(python), "-m", "desktop_launcher.runner"], cwd=self.root,
                    env=env, creationflags=flags, stdin=None if options["mode"] == "terminal" else subprocess.DEVNULL,
                    stdout=None if options["mode"] == "terminal" else log,
                    stderr=None if options["mode"] == "terminal" else log)
        except OSError:
            write_json(service_path, {"run_id": run_id, "status": "stopped", "message": "Не удалось создать процесс. Повторите запуск через start.cmd."})
            raise
        deadline = time.monotonic() + 90
        while time.monotonic() < deadline:
            current = active_service(self.root)
            if current and current.get("run_id") == run_id and current["status"] == "running":
                if options["panel"]:
                    self.open_panel()
                return self.status()
            if process.poll() is not None:
                detail = read_json(self.root / ".launcher" / "service.json")
                raise RuntimeError(detail.get("message") or "Запуск прерван. Проверьте .launcher/runtime.log.")
            time.sleep(.2)
        write_json(self.root / ".launcher" / "stop.json", {"run_id": run_id})
        raise RuntimeError("Запуск не завершился за 90 секунд. Проверьте .launcher/runtime.log и повторите.")

    def stop(self):
        current = active_service(self.root)
        if not current:
            return self.status()
        write_json(self.root / ".launcher" / "stop.json", {"run_id": current["run_id"]})
        deadline = time.monotonic() + 20
        while time.monotonic() < deadline and active_service(self.root):
            time.sleep(.2)
        if active_service(self.root):
            raise RuntimeError("Остановка ещё выполняется. Дождитесь завершения текущего запроса.")
        return self.status()

    def open_panel(self):
        current = active_service(self.root)
        if not current or current["status"] != "running" or not current.get("panel"):
            raise ValueError("Панель не запущена. Включите её при следующем старте.")
        from urllib.parse import urlsplit
        url = current.get("local_url") or ""
        parsed = urlsplit(url)
        if parsed.scheme != "http" or parsed.hostname != "127.0.0.1" or parsed.path != "/app/":
            raise ValueError("Некорректный адрес панели. Перезапустите МаксИИмку.")
        webbrowser.open(url)
        return {"ok": True}


class SetupService:
    def __init__(self, root=ROOT):
        self.root = Path(root)
        self.runtime = RuntimeManager(root)
        self.probe_lock = threading.Lock()
        self.cancel_requested = threading.Event()
        self.job = None
        self.owned_gateway = None
        self.gateway_profile = None

    def load(self):
        saved = read_json(self.root / ".launcher" / "state.json")
        try:
            admin = admin_setup.get_admin_id()
        except ValueError:
            admin = None
        return {"setup_complete": saved.get("setup_complete") is True,
                "settings": runtime_settings.public_snapshot(),
                "bot": {"enabled": config.MAX_BOT_ENABLED and bool(config.MAX_TOKEN),
                        "admin_id": str(admin or ""), "token_set": bool(config.MAX_TOKEN)},
                "launch": saved.get("launch", {"panel": True, "mode": "terminal"}),
                "runtime": self.runtime.status()}

    def candidate(self, patch):
        with console_settings.SETTINGS_LOCK:
            return runtime_settings.prepare(console_settings.load(), patch)

    def check(self, action, patch, force=False):
        if action not in ("load_models", "capabilities", "test_connection"):
            raise ValueError("Неизвестная проверка.")
        if not self.probe_lock.acquire(blocking=False):
            raise ValueError("Проверка уже выполняется.")
        try:
            self.cancel_requested.clear()
            candidate = self.candidate(patch)
            def work():
                profile = tuple(candidate[key] for key in ("provider", "base_url", "api_key", "gw_parallel", "gw_timeout", "gw_window"))
                if self.gateway_profile != profile:
                    self.close_gateway()
                if candidate["provider"] == "qoder":
                    if not self.owned_gateway or self.owned_gateway.poll() is not None:
                        self.owned_gateway = gateway_manager.start_for_setup(candidate)
                    self.gateway_profile = profile
                if action == "load_models":
                    names = console_settings.fetch_models(candidate)
                    message = f"Доступно моделей: {len(names)}"
                elif action == "capabilities":
                    names = []
                    warnings = [console_settings.detect_reasoning(candidate, force=bool(force)),
                                console_settings.detect_vision(candidate, force=bool(force))]
                    message = "; ".join(warning for warning in warnings if warning) or "Проверка возможностей завершена."
                else:
                    names = []
                    message = console_settings.test_connection(candidate)
                return {"models": names, "message": runtime_settings.redact(message, candidate),
                        "capabilities": {"vision": console_settings._vision(candidate),
                                         "reasoning": console_settings._support(candidate),
                                         "efforts": console_settings._efforts(candidate)}}
            self.job = model_checks.Job(work)
            if self.cancel_requested.is_set():
                self.job.cancel()
            self.job.done.wait()
            if self.job.cancelled.is_set():
                raise ValueError("Проверка отменена.")
            if self.job.error:
                raise ValueError(runtime_settings.redact(str(self.job.error), candidate))
            return self.job.result
        finally:
            self.job = None
            self.probe_lock.release()

    def cancel_check(self):
        self.cancel_requested.set()
        if self.job:
            self.job.cancel()
        return {"ok": True}

    def close_gateway(self):
        if self.owned_gateway:
            gateway_manager.stop_owned(self.owned_gateway)
            self.owned_gateway = None
        self.gateway_profile = None

    def close(self):
        self.cancel_check()
        # The probe releases its lock even when cancellation interrupts startup.
        with self.probe_lock:
            self.close_gateway()

    def start(self, options):
        if not self.probe_lock.acquire(blocking=False):
            raise ValueError("Дождитесь завершения проверки модели.")
        try:
            self.close_gateway()
            return self.runtime.start(options)
        finally:
            self.probe_lock.release()

    def save(self, payload):
        if not isinstance(payload, dict) or set(payload) - {"patch", "bot", "launch"}:
            raise ValueError("Некорректные настройки.")
        if active_service(self.root):
            raise ValueError("Остановите МаксИИмку перед изменением настроек запуска.")
        options = launch_options(payload.get("launch"))
        bot = payload.get("bot")
        if not isinstance(bot, dict) or set(bot) - {"enabled", "admin_id", "token"}:
            raise ValueError("Проверьте настройки MAX.")
        if type(bot.get("enabled")) is not bool:
            raise ValueError("Выберите, подключать ли MAX.")
        admin = admin_setup.parse_admin_id(bot["admin_id"]) if bot.get("admin_id") else None
        token = bot.get("token", config.MAX_TOKEN)
        if not isinstance(token, str) or len(token) > 4096 or any(c in token for c in "\r\n\0"):
            raise ValueError("Токен MAX должен занимать одну строку.")
        token = token.strip()
        if bot["enabled"] and not token:
            raise ValueError("Укажите токен MAX или пока выключите подключение бота.")
        with self.probe_lock, console_settings.SETTINGS_LOCK:
            settings = self.candidate(payload.get("patch", {}))
            env_path = self.root / ".env"
            token_path = self.root / "max_token.txt"
            marker_path = self.root / ".launcher" / "state.json"
            previous_env = os.environ.copy()
            previous_config = {key: value for key, value in vars(config).items() if key.isupper()}
            try:
                with file_transaction((console_settings.SETTINGS_PATH, console_settings.MODEL_HINT_PATH,
                                       env_path, token_path, admin_setup.ADMIN_PATH, marker_path)):
                    console_settings.apply(settings)
                    console_settings.save(settings)
                    if not env_path.exists():
                        write_bytes(env_path, (self.root / ".env.example").read_bytes())
                    save_environment(env_path, {"MESSENGER": "max", "UPDATES_MODE": "polling",
                        "MAX_BOT_ENABLED": "1" if bot["enabled"] else "0", "MAX_ADMIN_ID": str(admin or ""),
                        "MAX_BOT_TOKEN": "", "MAX_TOKEN_FILE": "max_token.txt"})
                    if "token" in bot or not token_path.exists():
                        write_bytes(token_path, (token + "\n").encode("utf-8"))
                    if admin:
                        write_json(admin_setup.ADMIN_PATH, {"admin_id": admin})
                    else:
                        admin_setup.ADMIN_PATH.unlink(missing_ok=True)
                    config.MAX_TOKEN = token
                    config.MAX_BOT_ENABLED = bot["enabled"]
                    config.MESSENGER = "max"
                    config.UPDATES_MODE = "polling"
                    os.environ.update(MESSENGER="max", UPDATES_MODE="polling",
                                      MAX_BOT_ENABLED="1" if bot["enabled"] else "0",
                                      MAX_ADMIN_ID=str(admin or ""), MAX_BOT_TOKEN=token,
                                      MAX_TOKEN_FILE=str(token_path))
                    write_json(marker_path, {"setup_complete": True, "version": 1, "launch": options})
            except BaseException:
                vars(config).update(previous_config)
                os.environ.clear()
                os.environ.update(previous_env)
                raise
        return self.load()


class Bridge:
    """Expose only explicitly listed methods; every error stays readable in the UI."""
    def __init__(self, service):
        self._service = service

    def _call(self, operation, *args):
        try:
            return {"ok": True, "data": operation(*args)}
        except (ValueError, RuntimeError, OSError) as exc:
            return {"ok": False, "error": str(exc)}
        except Exception:
            return {"ok": False, "error": "Не удалось выполнить действие. Повторите попытку."}

    def load(self):
        return self._call(self._service.load)

    def check(self, action, patch, force=False):
        return self._call(self._service.check, action, patch, force)

    def cancel_check(self):
        return self._call(self._service.cancel_check)

    def save(self, payload):
        return self._call(self._service.save, payload)

    def start(self, options):
        return self._call(self._service.start, options)

    def status(self):
        return self._call(self._service.runtime.status)

    def stop(self):
        return self._call(self._service.runtime.stop)

    def open_panel(self):
        return self._call(self._service.runtime.open_panel)
