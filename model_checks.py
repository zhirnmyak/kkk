"""Отменяемая фоновая проверка. Рабочий поток не меняет настройки интерфейса."""
import contextvars
import threading

_cancel = contextvars.ContextVar("model_check_cancel", default=None)


class Cancelled(Exception):
    pass


def check_cancelled():
    event = _cancel.get()
    if event is not None and event.is_set():
        raise Cancelled()


class Job:
    def __init__(self, fn):
        self.cancelled = threading.Event()
        self.done = threading.Event()
        self.result = None
        self.error = None

        def work():
            token = _cancel.set(self.cancelled)
            try:
                check_cancelled()
                self.result = fn()
            except Cancelled:
                pass
            except Exception as exc:
                self.error = exc
            finally:
                _cancel.reset(token)
                self.done.set()

        self.thread = threading.Thread(target=work, daemon=True, name="model-check")
        self.thread.start()

    def cancel(self):
        self.cancelled.set()
