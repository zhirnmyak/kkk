"""Заглушка мессенджера: печатает сообщения бота в консоль.

Тот же интерфейс, что у MaxClient и TgClient, поэтому вся логика из
handlers.py прогоняется здесь без сети и без токенов."""
from pathlib import Path


class ConsoleClient:
    def __init__(self):
        self.buttons = []  # кнопки последнего сообщения: [(текст, payload_dict)]

    def keyboard(self, rows) -> list:
        return rows  # в консоли разметка кнопок не нужна, оставляем нейтральные rows

    async def send_text(self, chat_id, text, keyboard=None, keyboard_last=False,
                        return_id=False):
        self.buttons = [b for row in (keyboard or []) for b in row]
        print("\n" + "─" * 72)
        print("🤖 БОТ:")
        print(text)
        if self.buttons:
            print("   Кнопки:", " | ".join(
                f"[{i}] {t}" for i, (t, _p) in enumerate(self.buttons, 1)))
        print("─" * 72)
        return None

    async def send_image(self, chat_id, path: str, caption: str = ""):
        print("\n" + "─" * 72)
        print("🖼 БОТ: картинка" + (f" — {caption}" if caption else ""))
        print(f"   файл: {path}")
        print("─" * 72)

    async def answer_callback(self, callback_id):
        pass  # в Max это «убирает часики», в консоли не нужно
    async def download(self, path: str, max_bytes: int | None = None) -> bytes:
        # в консольном режиме «handle» — локальный путь к файлу
        data = Path(path).read_bytes()
        if max_bytes and len(data) > max_bytes:
            raise RuntimeError(f"файл больше {max_bytes // 1048576} МБ — не качаю")
        return data

    def to_event(self, ev: dict) -> dict | None:
        """В консоли апдейт уже нейтральный (его строит debug_console.event),
        разбирать нечего — так handle_update работает и с этим клиентом."""
        return ev if ev.get("kind") in {"message", "callback"} else None
