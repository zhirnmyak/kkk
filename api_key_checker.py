"""Small local GUI for checking OpenAI-compatible API credentials."""

import json
import threading
import urllib.error
import urllib.request
import tkinter as tk
from tkinter import messagebox, ttk


def api_url(base_url: str, path: str) -> str:
    base = base_url.strip().rstrip("/")
    if not base:
        raise ValueError("Укажите адрес API")
    if not base.lower().startswith(("https://", "http://")):
        raise ValueError("Адрес должен начинаться с https:// или http://")
    if base.endswith("/v1"):
        return f"{base}/{path}"
    return f"{base}/v1/{path}"


def fetch_models(base_url: str, key: str) -> list[str]:
    request = urllib.request.Request(
        api_url(base_url, "models"),
        headers={"Authorization": f"Bearer {key}", "Accept": "application/json"},
    )
    try:
        with urllib.request.urlopen(request, timeout=20) as response:
            payload = json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        details = exc.read(500).decode("utf-8", errors="replace")
        raise RuntimeError(f"HTTP {exc.code}: {details}") from None
    except urllib.error.URLError as exc:
        raise RuntimeError(f"Не удалось подключиться: {exc.reason}") from None
    if not isinstance(payload, dict) or not isinstance(payload.get("data"), list):
        raise RuntimeError("Ответ API не содержит список моделей (data)")
    return sorted({str(item["id"]) for item in payload["data"] if isinstance(item, dict) and item.get("id")})


def fetch_balance(base_url: str, key: str) -> str:
    """Try the legacy credit-grants endpoint used by some OpenAI-compatible hosts."""
    base = base_url.strip().rstrip("/")
    if base.lower().endswith("/v1"):
        base = base[:-3]
    request = urllib.request.Request(
        f"{base}/dashboard/billing/credit_grants",
        headers={"Authorization": f"Bearer {key}", "Accept": "application/json"},
    )
    try:
        with urllib.request.urlopen(request, timeout=20) as response:
            payload = json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        if exc.code in (401, 403, 404, 405):
            return f"Баланс недоступен (HTTP {exc.code}; endpoint может не поддерживаться)"
        details = exc.read(300).decode("utf-8", errors="replace")
        return f"Ошибка HTTP {exc.code}: {details}"
    except urllib.error.URLError as exc:
        return f"Нет подключения: {exc.reason}"
    amount = payload.get("total_available") if isinstance(payload, dict) else None
    if isinstance(amount, (float, int)):
        return f"Доступно кредитов: {amount:g} USD"
    return "Ответ получен, но сумма баланса в нём не найдена"


def send_chat(base_url: str, key: str, model: str, prompt: str) -> str:
    request = urllib.request.Request(
        api_url(base_url, "chat/completions"),
        data=json.dumps({
            "model": model,
            "messages": [{"role": "user", "content": prompt}],
            "stream": False,
        }).encode("utf-8"),
        headers={
            "Authorization": f"Bearer {key}",
            "Content-Type": "application/json",
            "Accept": "application/json",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=90) as response:
            payload = json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        details = exc.read(1000).decode("utf-8", errors="replace")
        raise RuntimeError(f"HTTP {exc.code}: {details}") from None
    except urllib.error.URLError as exc:
        raise RuntimeError(f"Не удалось подключиться: {exc.reason}") from None
    try:
        content = payload["choices"][0]["message"]["content"]
    except (KeyError, IndexError, TypeError):
        raise RuntimeError("Неожиданный формат ответа API") from None
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "\n".join(str(part.get("text", "")) for part in content if isinstance(part, dict))
    return str(content)


class Checker(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title("API Key Checker")
        self.geometry("760x760")
        self.minsize(600, 600)
        self._build()

    def _build(self):
        frame = ttk.Frame(self, padding=18)
        frame.pack(fill="both", expand=True)
        frame.columnconfigure(0, weight=1)
        ttk.Label(frame, text="OpenAI-compatible API checker", font=("Segoe UI", 16, "bold")).grid(row=0, column=0, sticky="w")
        ttk.Label(frame, text="Ключи обрабатываются только в памяти и не сохраняются.").grid(row=1, column=0, sticky="w", pady=(3, 14))

        ttk.Label(frame, text="Base URL").grid(row=2, column=0, sticky="w")
        self.base = ttk.Entry(frame)
        self.base.insert(0, "https://api.openai.com/v1")
        self.base.grid(row=3, column=0, sticky="ew", pady=(4, 12))

        ttk.Label(frame, text="API ключи (по одному на строку)").grid(row=4, column=0, sticky="w")
        self.keys = tk.Text(frame, height=5, wrap="none", font=("Consolas", 10), undo=True)
        self.keys.grid(row=5, column=0, sticky="ew", pady=(4, 12))
        # Explicit paste bindings and a context menu help on Windows setups
        # where Tk's default Text bindings are intercepted by the window manager.
        self._add_paste_menu(self.base)
        self._add_paste_menu(self.keys)

        row = ttk.Frame(frame)
        row.grid(row=6, column=0, sticky="ew")
        ttk.Label(row, text="Модель").pack(side="left")
        self.model = ttk.Combobox(row, state="normal", width=36)
        self.model.pack(side="left", padx=8, fill="x", expand=True)
        self.check_button = ttk.Button(row, text="Загрузить модели / проверить ключи", command=self.check)
        self.check_button.pack(side="right")
        self.balance_button = ttk.Button(row, text="Проверить баланс", command=self.check_balance)
        self.balance_button.pack(side="right", padx=(0, 8))

        ttk.Label(frame, text="Сообщение модели").grid(row=7, column=0, sticky="w", pady=(14, 4))
        self.prompt = tk.Text(frame, height=5, wrap="word", undo=True)
        self.prompt.grid(row=8, column=0, sticky="ew")
        self._add_paste_menu(self.prompt)
        self.send_button = ttk.Button(frame, text="Отправить запрос", command=self.ask_model)
        self.send_button.grid(row=9, column=0, sticky="e", pady=(6, 10))

        ttk.Label(frame, text="Ответ модели / результат проверки").grid(row=10, column=0, sticky="w", pady=(4, 4))
        self.output = tk.Text(frame, height=12, wrap="word", state="disabled")
        self.output.grid(row=11, column=0, sticky="nsew")
        frame.rowconfigure(11, weight=1)
        ttk.Label(frame, text="Запрос отправляется на /chat/completions с первым ключом. Баланс доступен только у провайдеров, поддерживающих credit_grants.", wraplength=690).grid(row=12, column=0, sticky="w", pady=(10, 0))

    def _add_paste_menu(self, widget):
        menu = tk.Menu(widget, tearoff=False)
        menu.add_command(label="Вставить", command=lambda: self._paste(widget))
        menu.add_command(label="Копировать", command=lambda: widget.event_generate("<<Copy>>"))
        menu.add_command(label="Вырезать", command=lambda: widget.event_generate("<<Cut>>"))
        widget.bind("<Control-v>", lambda event: self._paste(widget))
        widget.bind("<Control-V>", lambda event: self._paste(widget))
        widget.bind("<Button-3>", lambda event: menu.tk_popup(event.x_root, event.y_root))

    @staticmethod
    def _paste(widget):
        try:
            value = widget.clipboard_get()
            if isinstance(widget, tk.Text):
                widget.insert("insert", value)
            else:
                widget.insert("insert", value)
        except tk.TclError:
            pass
        return "break"

    def _show(self, message: str):
        self.output.configure(state="normal")
        self.output.delete("1.0", "end")
        self.output.insert("end", message)
        self.output.configure(state="disabled")

    def check(self):
        base = self.base.get().strip()
        keys = [line.strip() for line in self.keys.get("1.0", "end").splitlines() if line.strip()]
        if not keys:
            messagebox.showinfo("Нужен ключ", "Введите один или несколько API ключей.")
            return
        self.check_button.configure(state="disabled")
        self._show(f"Проверяю {len(keys)} ключ(ей)…\n")

        def worker():
            results, found_models = [], []
            for index, key in enumerate(keys, 1):
                try:
                    models = fetch_models(base, key)
                    found_models = sorted(set(found_models).union(models))
                    results.append(f"Ключ {index}: OK — получено моделей: {len(models)}")
                except Exception as exc:
                    results.append(f"Ключ {index}: ОШИБКА — {exc}")
            self.after(0, lambda: self._finish(results, found_models))

        threading.Thread(target=worker, daemon=True).start()

    def check_balance(self):
        base = self.base.get().strip()
        keys = [line.strip() for line in self.keys.get("1.0", "end").splitlines() if line.strip()]
        if not keys:
            messagebox.showinfo("Нужен ключ", "Введите один или несколько API ключей.")
            return
        self.balance_button.configure(state="disabled")
        self._show(f"Проверяю баланс для {len(keys)} ключ(ей)…\n")

        def worker():
            results = [f"Ключ {i}: {fetch_balance(base, key)}" for i, key in enumerate(keys, 1)]
            self.after(0, lambda: self._balance_done(results))

        threading.Thread(target=worker, daemon=True).start()

    def ask_model(self):
        base = self.base.get().strip()
        keys = [line.strip() for line in self.keys.get("1.0", "end").splitlines() if line.strip()]
        model = self.model.get().strip()
        prompt = self.prompt.get("1.0", "end").strip()
        if not keys:
            messagebox.showinfo("Нужен ключ", "Введите API ключ.")
            return
        if not model:
            messagebox.showinfo("Нужна модель", "Выберите модель из списка или введите её название.")
            return
        if not prompt:
            messagebox.showinfo("Нужно сообщение", "Введите сообщение для модели.")
            return
        self.send_button.configure(state="disabled")
        self._show("Запрос отправлен, ожидаю ответ…")

        def worker():
            try:
                answer = send_chat(base, keys[0], model, prompt)
                result = f"Модель: {model}\n\n{answer}"
            except Exception as exc:
                result = f"Ошибка запроса: {exc}"
            self.after(0, lambda: self._chat_done(result))

        threading.Thread(target=worker, daemon=True).start()

    def _chat_done(self, result):
        self._show(result)
        self.send_button.configure(state="normal")

    def _balance_done(self, results):
        self._show("\n".join(results))
        self.balance_button.configure(state="normal")

    def _finish(self, results, models):
        self._show("\n".join(results))
        self.model["values"] = models
        if models:
            self.model.current(0)
        self.check_button.configure(state="normal")


if __name__ == "__main__":
    Checker().mainloop()
