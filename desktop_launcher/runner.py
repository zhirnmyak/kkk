"""One supervised bot process, with either a terminal or a hidden console."""
import asyncio
import os
import re
import sys
import threading
import time
import traceback

from .state import ROOT, ProcessLock, read_json, write_json


async def run():
    import config
    import gateway_manager

    run_id = os.environ.get("MAXIMKA_DESKTOP_RUN", "")
    if not re.fullmatch(r"[a-f0-9]{32}", run_id):
        raise RuntimeError("Запускайте МаксИИмку через start.cmd.")
    mode = os.environ.get("MAXIMKA_DESKTOP_MODE", "background")
    panel = os.environ.get("MAXIMKA_RUN_PANEL") == "1"
    state_path = ROOT / ".launcher" / "service.json"
    server = None
    externally_stopped = False
    state = {"run_id": run_id, "pid": os.getpid(), "mode": mode, "panel": panel,
             "status": "starting", "message": "Запускаем МаксИИмку"}
    state_lock = threading.Lock()
    heartbeat_stop = threading.Event()
    write_error_reported = False
    def publish():
        nonlocal write_error_reported
        with state_lock:
            state["heartbeat"] = time.time()
            try:
                write_json(state_path, dict(state))
                write_error_reported = False
            except OSError:
                # Status is advisory: a temporarily locked file must not stop
                # polling, HTTP or the heartbeat thread. The OS lock still
                # prevents launching a second bot while the next write retries.
                if not write_error_reported:
                    print("Не удалось обновить статус запуска; повторяем. Бот продолжает работать.", file=sys.stderr)
                    write_error_reported = True
    def heartbeat():
        while not heartbeat_stop.wait(1):
            publish()
    def server_ready(value):
        nonlocal server
        server = value
    publish()
    heartbeat_thread = threading.Thread(target=heartbeat, daemon=True)
    heartbeat_thread.start()

    async def application():
        if mode == "terminal":
            import debug_console
            await debug_console.main(http_ready=server_ready)
        else:
            import main
            import uvicorn
            current = uvicorn.Server(uvicorn.Config(main.app, host="127.0.0.1", port=config.HTTP_PORT,
                                                    log_level="warning", access_log=False, proxy_headers=False))
            server_ready(current)
            await current.serve()
    task = asyncio.create_task(application())
    try:
        while not task.done():
            if server and server.started:
                state["status"] = "running"
                state["message"] = "МаксИИмка работает"
                state["local_url"] = getattr(server.config.app.state, "local_url", None) if panel else None
            stop = read_json(ROOT / ".launcher" / "stop.json")
            if stop.get("run_id") == run_id:
                externally_stopped = True
                state.update(status="stopping", message="Останавливаем МаксИИмку")
                publish()
                if mode == "background" and server:
                    server.should_exit = True
                else:
                    task.cancel()
                try:
                    await asyncio.wait_for(task, 15)
                except asyncio.CancelledError:
                    pass
                break
            await asyncio.sleep(.5)
        if not task.cancelled():
            await task
    finally:
        if not task.done():
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        try:
            gateway_manager.stop()
        finally:
            heartbeat_stop.set()
            heartbeat_thread.join(timeout=2)
            state.update(status="stopped", message="МаксИИмка остановлена", local_url=None)
            publish()
    if mode == "terminal" and externally_stopped:
        # input() may be waiting in an executor thread. The application's finally
        # above has already closed HTTP, polling and the gateway; do not wait for a key.
        for stream in (sys.stdout, sys.stderr):
            if stream:
                stream.flush()
        os._exit(0)


def main():
    try:
        with ProcessLock(ROOT / ".launcher" / "service.lock"):
            try:
                asyncio.run(run())
            except KeyboardInterrupt:
                pass
            except Exception:
                with (ROOT / ".launcher" / "runtime.log").open("a", encoding="utf-8") as log:
                    traceback.print_exc(file=log)
                previous = read_json(ROOT / ".launcher" / "service.json")
                if previous.get("run_id") == os.environ.get("MAXIMKA_DESKTOP_RUN"):
                    previous.update(status="stopped", heartbeat=time.time(), local_url=None,
                                    message="Не удалось запустить МаксИИмку. Проверьте .launcher/runtime.log.")
                    write_json(ROOT / ".launcher" / "service.json", previous)
                raise
    except RuntimeError as exc:
        print(str(exc), file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
