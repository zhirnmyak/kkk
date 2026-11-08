"""A small native window hosting the local Stageshow interface."""
import argparse
from functools import partial
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
import os
import threading
from urllib.parse import urlsplit

from .state import ROOT, ProcessLock


class AppFiles(SimpleHTTPRequestHandler):
    def do_GET(self):
        if not urlsplit(self.path).path.startswith("/app/"):
            self.send_error(404)
            return
        self.path = self.path[len("/app"):]
        super().do_GET()

    def list_directory(self, path):
        self.send_error(404)
        return None

    def end_headers(self):
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        super().end_headers()

    def log_message(self, *_):
        pass


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--demo", action="store_true", help="Preview with explicitly marked sample data.")
    options = parser.parse_args()
    import webview
    from .service import Bridge, SetupService

    with ProcessLock(ROOT / ".launcher" / "window.lock"):
        service = SetupService()
        handler = partial(AppFiles, directory=str(ROOT / "webapp" / "dist"))
        server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
        server.daemon_threads = True
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        suffix = "&demo=1" if options.demo else ""
        window = webview.create_window(
            "МаксИИмка", f"http://127.0.0.1:{server.server_port}/app/?launcher=1{suffix}",
            js_api=Bridge(service), width=860, height=700, min_size=(740, 620),
            background_color="#f7f8fa", resizable=True, confirm_close=False,
            text_select=True)
        def closed():
            service.close()
            server.shutdown()
            server.server_close()
        window.events.closed += closed
        webview.start(gui="edgechromium" if os.name == "nt" else None,
                      debug=False, private_mode=True,
                      storage_path=str(ROOT / ".launcher" / "webview"))


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        if os.name == "nt":
            import ctypes
            ctypes.windll.user32.MessageBoxW(None, "Не удалось открыть МаксИИмку.\n" + str(exc),
                                             "МаксИИмка", 0x10)
        else:
            raise
