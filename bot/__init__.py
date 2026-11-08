"""Слой мессенджера. Клиент выбирается одной строкой в .env: MESSENGER=max|tg.

MaxClient, TgClient и ConsoleClient дают одинаковый интерфейс
(send_text / answer_callback / download / keyboard / to_event), поэтому
handlers.py, scheduler.py и debug_console.py не знают, в каком мессенджере
они работают: Max — целевая платформа, Telegram — проверка без токена Max."""
import config


def create_client():
    if config.MESSENGER == "tg":
        from .tg_client import TgClient
        return TgClient()
    if config.MESSENGER == "max":
        from .max_client import MaxClient
        return MaxClient()
    raise RuntimeError(f"MESSENGER={config.MESSENGER!r}: ожидался max или tg")
