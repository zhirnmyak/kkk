"""Bootstrap with only the Python standard library: it works without .venv."""
import hashlib
import json
import os
from pathlib import Path
import platform
import queue
import re
import shutil
import subprocess
import sys
import threading
import time
import urllib.request
import zipfile

from .state import ROOT, ProcessLock, active_service, read_json, write_json

PHASES = ("Окружение Python", "Библиотеки", "Интерфейс", "Готово")
NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)
IMPORT_CHECK = "import pip, fastapi, uvicorn, httpx, openai, chromadb, fitz, dotenv, apscheduler, webview"


class Cancelled(Exception):
    pass


def file_digest(paths, root):
    digest = hashlib.sha256()
    for path in sorted(paths):
        digest.update(str(path.relative_to(root)).encode("utf-8"))
        digest.update(path.read_bytes())
    return digest.hexdigest()


def clean_line(text):
    text = re.sub(r"\x1b\[[0-9;]*[a-zA-Z]", "", text).strip()
    return re.sub(r"(https?://)[^/\s@]+@", r"\1***@", text)


def command_args(args):
    args = [str(arg) for arg in args]
    if os.name == "nt" and Path(args[0]).suffix.lower() in (".cmd", ".bat"):
        return f'"{os.environ.get("COMSPEC", "cmd.exe")}" /d /s /c "{subprocess.list2cmdline(args)}"'
    return args


class Installer:
    def __init__(self, root=ROOT, report=lambda *_: None, cancelled=None):
        self.root = Path(root).resolve()
        self.report = report
        self.cancelled = cancelled or threading.Event()
        self.phase = 0
        self.venv = self.root / ".venv"
        self.python = self.venv / ("Scripts/python.exe" if os.name == "nt" else "bin/python")
        self.state_dir = self.root / ".launcher"
        self.env = os.environ.copy()
        self.env.update(PYTHONUTF8="1", PYTHONIOENCODING="utf-8", PIP_DISABLE_PIP_VERSION_CHECK="1")
        for name in ("ALL_PROXY", "HTTP_PROXY", "HTTPS_PROXY", "all_proxy", "http_proxy", "https_proxy"):
            self.env.pop(name, None)

    def check_cancelled(self):
        if self.cancelled.is_set():
            raise Cancelled()

    def emit(self, phase, message, fraction=None):
        self.phase = phase
        self.report(phase, message, fraction)

    def run(self, args, *, cwd=None):
        self.check_cancelled()
        self.state_dir.mkdir(parents=True, exist_ok=True)
        lines = queue.Queue()
        process = subprocess.Popen(command_args(args), cwd=cwd or self.root, env=self.env,
                                   stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                                   stderr=subprocess.STDOUT, text=True, encoding="utf-8",
                                   errors="replace", creationflags=NO_WINDOW)
        def read():
            for line in process.stdout:
                lines.put(clean_line(line))
            lines.put(None)
        reader = threading.Thread(target=read, daemon=True)
        reader.start()
        finished = False
        recent = []
        try:
            with (self.state_dir / "install.log").open("a", encoding="utf-8") as log:
                while not finished or process.poll() is None:
                    self.check_cancelled()
                    try:
                        line = lines.get(timeout=.15)
                    except queue.Empty:
                        continue
                    if line is None:
                        finished = True
                    elif line:
                        recent.append(line)
                        recent = recent[-8:]
                        log.write(line + "\n")
                        log.flush()
                        self.report(self.phase, line[:190], None)
            if process.wait() != 0:
                raise RuntimeError("Команда завершилась с ошибкой. " + "\n".join(recent[-3:]))
        except BaseException:
            if process.poll() is None:
                if os.name == "nt":
                    subprocess.run(["taskkill", "/PID", str(process.pid), "/T", "/F"],
                                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                                   creationflags=NO_WINDOW)
                else:
                    process.terminate()
                try:
                    process.wait(timeout=8)
                except subprocess.TimeoutExpired:
                    process.kill()
            raise
        finally:
            process.stdout.close()

    def download(self, url, destination, label):
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        self.check_cancelled()
        with opener.open(url, timeout=30) as response, Path(destination).open("wb") as target:
            total = int(response.headers.get("Content-Length", "0"))
            received = 0
            while True:
                self.check_cancelled()
                block = response.read(256 * 1024)
                if not block:
                    break
                target.write(block)
                received += len(block)
                self.report(self.phase, f"{label} · {received / 1048576:.1f} МБ",
                            received / total if total else None)

    def ensure_node(self):
        candidates = [shutil.which("node")]
        candidates += [str(path) for path in (self.root / ".runtime").glob("node-*/node.exe")]
        for candidate in candidates:
            if not candidate:
                continue
            try:
                result = subprocess.run([candidate, "--version"], capture_output=True,
                                        text=True, timeout=10, creationflags=NO_WINDOW)
                match = re.fullmatch(r"v(\d+)\.(\d+)\.(\d+)\s*", result.stdout)
                version = tuple(map(int, match.groups())) if match else (0, 0, 0)
                npm = Path(candidate).with_name("npm.cmd" if os.name == "nt" else "npm")
                if result.returncode == 0 and version >= (20, 19, 0) and npm.is_file():
                    self.env["PATH"] = str(npm.parent) + os.pathsep + self.env.get("PATH", "")
                    return npm
            except (OSError, subprocess.TimeoutExpired):
                continue
        if os.name != "nt":
            raise RuntimeError("Установите Node.js 20.19+ и повторите подготовку.")
        self.emit(2, "Устанавливаем Node.js рядом с проектом")
        index = self.state_dir / "node-releases.json"
        self.download("https://nodejs.org/dist/index.json", index, "Список версий Node.js")
        releases = json.loads(index.read_text("utf-8"))
        release = next((row for row in releases if re.fullmatch(r"v24\.\d+\.\d+", row["version"])
                        and row.get("lts")), None)
        if not release:
            raise RuntimeError("Не найдена стабильная версия Node.js 24.")
        version = release["version"]
        architecture = "arm64" if platform.machine().lower() in ("arm64", "aarch64") else "x64"
        name = f"node-{version}-win-{architecture}"
        archive_name = name + ".zip"
        checksum_path = self.state_dir / "node-sha256.txt"
        self.download(f"https://nodejs.org/dist/{version}/SHASUMS256.txt", checksum_path, "Проверка Node.js")
        checksums = dict((line.split()[1].lstrip("*"), line.split()[0])
                         for line in checksum_path.read_text("utf-8").splitlines() if len(line.split()) == 2)
        archive = self.state_dir / "node.zip.part"
        self.download(f"https://nodejs.org/dist/{version}/{archive_name}", archive, "Скачиваем Node.js")
        expected = checksums.get(archive_name)
        if not expected or hashlib.sha256(archive.read_bytes()).hexdigest() != expected:
            raise RuntimeError("Контрольная сумма Node.js не совпала. Повторите загрузку.")
        runtime = (self.root / ".runtime").resolve()
        runtime.mkdir(exist_ok=True)
        with zipfile.ZipFile(archive) as zipped:
            for member in zipped.infolist():
                self.check_cancelled()
                target = (runtime / member.filename).resolve()
                if not target.is_relative_to(runtime / name):
                    raise RuntimeError("Некорректный путь в архиве Node.js.")
                zipped.extract(member, runtime)
        archive.unlink()
        npm = runtime / name / "npm.cmd"
        self.env["PATH"] = str(npm.parent) + os.pathsep + self.env.get("PATH", "")
        return npm

    def ensure_webview(self):
        if os.name != "nt":
            return
        import winreg
        key = r"Microsoft\EdgeUpdate\Clients\{F3017226-FE2A-4295-8BDF-00C3A9A7E4C5}"
        for hive in (winreg.HKEY_CURRENT_USER, winreg.HKEY_LOCAL_MACHINE):
            for prefix in ("SOFTWARE\\", "SOFTWARE\\WOW6432Node\\"):
                try:
                    with winreg.OpenKey(hive, prefix + key) as handle:
                        if str(winreg.QueryValueEx(handle, "pv")[0]) not in ("", "0.0.0.0"):
                            return
                except OSError:
                    pass
        self.emit(2, "Подготавливаем системный компонент WebView2")
        setup = self.state_dir / "MicrosoftEdgeWebview2Setup.exe"
        self.download("https://go.microsoft.com/fwlink/p/?LinkId=2124703", setup, "WebView2")
        # Only Microsoft's signed installer may be executed.
        from base64 import b64encode
        literal = str(setup).replace("'", "''")
        script = (f"$s=Get-AuthenticodeSignature -LiteralPath '{literal}'; "
                  "if ($s.Status -ne 'Valid' -or $s.SignerCertificate.Subject -notlike '*Microsoft Corporation*') { exit 1 }")
        self.run(["powershell.exe", "-NoProfile", "-NonInteractive", "-EncodedCommand",
                  b64encode(script.encode("utf-16-le")).decode("ascii")])
        self.run([setup, "/silent", "/install"])

    def ensure_environment(self):
        """Keep an interrupted environment recoverable instead of deleting it."""
        if self.python.is_file():
            try:
                self.run([self.python, "-c", "import sys; from pathlib import Path; "
                          "assert sys.version_info >= (3, 14); "
                          "assert Path(sys.prefix).resolve() == Path(sys.argv[1]).resolve()", self.venv])
                return False
            except (OSError, RuntimeError):
                pass
        if self.venv.exists():
            if self.venv.is_symlink() or self.venv.resolve().parent != self.root:
                raise RuntimeError("Путь .venv должен находиться внутри папки проекта.")
            recovery = self.state_dir / ("venv-interrupted-" + str(time.time_ns()))
            self.emit(0, "Сохраняем прерванное окружение и создаём новое")
            self.venv.rename(recovery)
        executable = Path(sys.executable)
        if executable.name.lower() == "pythonw.exe":
            executable = executable.with_name("python.exe")
        self.emit(0, "Создаём виртуальное окружение .venv")
        self.run([executable, "-m", "venv", self.venv])
        return True

    def ensure_dependencies(self, requirements, *, install):
        repair = False
        if not install:
            try:
                self.run([self.python, "-c", IMPORT_CHECK])
                return
            except (OSError, RuntimeError):
                self.emit(1, "Восстанавливаем библиотеки после прерванной установки")
                repair = True
        self.run([self.python, "-m", "ensurepip", "--upgrade"])
        self.run([self.python, "-m", "pip", "install", "--disable-pip-version-check",
                  "--progress-bar", "off", *(["--force-reinstall"] if repair else []),
                  "-r", requirements[0], "-r", requirements[1]])
        self.run([self.python, "-m", "pip", "check"])
        self.run([self.python, "-c", IMPORT_CHECK])

    def prepare(self):
        if sys.version_info < (3, 14):
            raise RuntimeError("Для этой поставки нужен Python 3.14 или новее. Установите его и запустите start.cmd.")
        self.state_dir.mkdir(parents=True, exist_ok=True)
        with ProcessLock(self.state_dir / "install.lock"):
            if active_service(self.root) and self.python.is_file():
                self.emit(3, "МаксИИмка уже работает — открываем управление", 1)
                return
            marker = read_json(self.state_dir / "install.json")
            requirements = [self.root / "requirements.txt", self.root / "desktop_launcher" / "requirements.txt"]
            dependency_hash = file_digest(requirements, self.root)
            self.emit(0, "Проверяем Python и виртуальное окружение")
            created = self.ensure_environment()
            self.emit(1, "Проверяем библиотеки")
            self.ensure_dependencies(requirements, install=created or marker.get("dependencies") != dependency_hash)
            self.emit(2, "Проверяем сборку интерфейса")
            npm = self.ensure_node()
            self.ensure_webview()
            web = self.root / "webapp"
            package_hash = file_digest([web / "package.json", web / "package-lock.json"], self.root)
            packages_missing = not all((web / "node_modules" / name / "package.json").is_file()
                                       for name in ("vite", "typescript", "react"))
            if packages_missing or marker.get("packages") != package_hash:
                self.emit(2, "Устанавливаем компоненты интерфейса")
                self.run([npm, "ci", "--no-audit", "--no-fund"], cwd=web)
            sources = [path for directory in (web / "src", web / "design-reference")
                       for path in directory.rglob("*") if path.is_file()]
            sources += [web / name for name in ("index.html", "package.json", "package-lock.json", "vite.config.ts", "tsconfig.json")]
            build_hash = file_digest(sources, self.root)
            index = web / "dist" / "index.html"
            assets_missing = not index.is_file() or any(
                not (web / "dist" / asset).is_file()
                for asset in re.findall(r'(?:src|href)="/app/(assets/[^"?]+)"', index.read_text("utf-8"))
            )
            if assets_missing or packages_missing or marker.get("build") != build_hash:
                self.emit(2, "Собираем панель и мастер настройки")
                self.run([npm, "run", "build"], cwd=web)
            write_json(self.state_dir / "install.json", {"dependencies": dependency_hash,
                       "packages": package_hash, "build": build_hash, "node": str(npm.parent), "version": 1})
            self.emit(3, "Всё готово — открываем МаксИИмку", 1)


class BootstrapWindow:
    def __init__(self, root=ROOT):
        import tkinter as tk
        from tkinter import ttk
        self.tk = tk
        self.root_path = Path(root)
        self.window = tk.Tk()
        self.window.title("МаксИИмка")
        self.window.configure(bg="#f7f8fa")
        width, height = 860, 650
        x = max(0, (self.window.winfo_screenwidth() - width) // 2)
        y = max(0, (self.window.winfo_screenheight() - height) // 2)
        self.window.geometry(f"{width}x{height}+{x}+{y}")
        self.window.resizable(False, False)
        self.window.protocol("WM_DELETE_WINDOW", self.close)
        self.cancelled = threading.Event()
        self.messages = queue.Queue()
        self.busy = False
        style = ttk.Style()
        style.theme_use("clam")
        style.configure("Max.Horizontal.TProgressbar", troughcolor="#e9edf3", background="#4262d9",
                        bordercolor="#e9edf3", lightcolor="#4262d9", darkcolor="#4262d9", thickness=5)
        header = tk.Frame(self.window, bg="white", padx=32, pady=22)
        header.pack(fill="x")
        tk.Label(header, text="МаксИИмка.", font=("Segoe UI", 24, "bold"), bg="white", fg="#283044").pack(side="left")
        tk.Label(header, text="Подготовка к запуску", font=("Segoe UI", 10), bg="white", fg="#788194").pack(side="right")
        body = tk.Frame(self.window, bg="#f7f8fa", padx=52, pady=38)
        body.pack(fill="both", expand=True)
        tk.Label(body, text="Сначала подготовим всё необходимое", font=("Segoe UI", 23, "bold"),
                 bg="#f7f8fa", fg="#242936", anchor="w").pack(fill="x")
        tk.Label(body, text="Создадим окружение, установим библиотеки и соберём интерфейс.\n"
                 "После этого останется выбрать модель и подключить бота.",
                 font=("Segoe UI", 11), justify="left", bg="#f7f8fa", fg="#626b7e", pady=16).pack(anchor="w")
        self.labels = []
        for index, name in enumerate(PHASES):
            label = tk.Label(body, text=f"{index + 1}   {name}", font=("Segoe UI", 12),
                             bg="#f7f8fa", fg="#788194", anchor="w", pady=9)
            label.pack(fill="x")
            self.labels.append(label)
        self.progress = ttk.Progressbar(body, style="Max.Horizontal.TProgressbar", maximum=4, value=0)
        self.progress.pack(fill="x", pady=(20, 12))
        self.detail = tk.Label(body, text="Начинаем…", font=("Segoe UI", 10), bg="#f7f8fa",
                               fg="#626b7e", justify="left", anchor="nw", wraplength=750)
        self.detail.pack(fill="x")
        footer = tk.Frame(self.window, bg="white", padx=32, pady=18)
        footer.pack(fill="x", side="bottom")
        tk.Label(footer, text="Первый запуск может занять несколько минут",
                 font=("Segoe UI", 10), bg="white", fg="#788194").pack(side="left")
        self.retry = tk.Button(footer, text="Повторить", command=self.start, bg="#4262d9", fg="white",
                               relief="flat", font=("Segoe UI", 11), padx=20, pady=8)
        self.cancel_button = tk.Button(footer, text="Отмена", command=self.close, bg="#f1f3f7",
                                       fg="#283044", relief="flat", font=("Segoe UI", 11), padx=20, pady=8)
        self.cancel_button.pack(side="right")
        self.window.after(120, self.start)
        self.window.after(100, self.poll)

    def start(self):
        if self.busy:
            return
        self.retry.pack_forget()
        self.cancelled.clear()
        self.busy = True
        self.progress.configure(mode="determinate", value=0)
        self.detail.configure(fg="#626b7e")
        self.cancel_button.configure(text="Отмена", state="normal")
        def work():
            try:
                Installer(self.root_path, lambda *event: self.messages.put(("progress", event)), self.cancelled).prepare()
                self.messages.put(("done", None))
            except Cancelled:
                self.messages.put(("cancelled", None))
            except Exception as exc:
                self.messages.put(("error", clean_line(str(exc))))
        threading.Thread(target=work, daemon=True).start()

    def poll(self):
        try:
            while True:
                event, payload = self.messages.get_nowait()
                if event == "progress":
                    phase, message, fraction = payload
                    self.progress["value"] = max(float(self.progress["value"]), min(4, phase + (fraction or .12)))
                    self.detail.configure(text=message)
                    for index, label in enumerate(self.labels):
                        prefix = "✓" if index < phase else str(index + 1)
                        label.configure(text=f"{prefix}   {PHASES[index]}",
                                        fg="#26734f" if index < phase else "#3856c5" if index == phase else "#788194")
                elif event == "error":
                    self.busy = False
                    self.detail.configure(text=payload + "\nЖурнал: .launcher/install.log", fg="#b53c3c")
                    self.retry.pack(side="right", padx=(0, 12))
                    self.cancel_button.configure(text="Закрыть")
                elif event == "cancelled":
                    self.window.destroy()
                    return
                elif event == "done":
                    self.busy = False
                    python = self.root_path / ".venv" / ("Scripts/pythonw.exe" if os.name == "nt" else "bin/python")
                    subprocess.Popen([str(python), "-m", "desktop_launcher.app"], cwd=self.root_path,
                                     creationflags=NO_WINDOW, env={**os.environ, "PYTHONUTF8": "1"})
                    self.window.destroy()
                    return
        except queue.Empty:
            pass
        self.window.after(100, self.poll)

    def close(self):
        if self.busy:
            self.cancelled.set()
            self.detail.configure(text="Останавливаем подготовку…")
            self.cancel_button.configure(state="disabled")
        else:
            self.window.destroy()

    def run(self):
        self.window.mainloop()


def main():
    try:
        with ProcessLock(ROOT / ".launcher" / "bootstrap.lock"):
            BootstrapWindow().run()
    except Exception as exc:
        if os.name == "nt":
            import ctypes
            ctypes.windll.user32.MessageBoxW(None, str(exc), "МаксИИмка — запуск", 0x10)
        else:
            raise
