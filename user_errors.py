"""Безопасные подсказки пользователю вместо внутренних ответов API."""
import httpx
import quota


class UserNotice(RuntimeError):
    pass


class ApiFailure(RuntimeError):
    def __init__(self, status_code):
        self.status_code = status_code
        super().__init__(f"Vision API: HTTP {status_code}")


def dialog_unavailable(exc):
    return getattr(exc, "status", None) == 403 and any(
        word in str(exc).lower() for word in ("dialog.suspended", "bot.blocked", "chat.denied"))


def explain(exc):
    if isinstance(exc, UserNotice):
        return str(exc)
    status = getattr(exc, "status_code", None) or getattr(exc, "status", None)
    response = getattr(exc, "response", None)
    status = status or getattr(response, "status_code", None)
    if status == 429 or quota.is_quota_error(exc):
        return "Достигнут лимит запросов или баланса модели. Попробуй позже либо смени модель в /настройки консоли."
    if status == 401:
        return "API-ключ модели не принят. Проверь ключ в /настройки консоли."
    if status == 403:
        return "Нет доступа к выбранной модели. Проверь подключение или выбери другую модель в /настройки консоли."
    if status == 404:
        return "Выбранная модель или адрес API недоступны. Проверь их в /настройки консоли."
    if status and status >= 500:
        return "Сервер модели временно недоступен. Попробуй ещё раз позже."
    if isinstance(exc, (httpx.TimeoutException, TimeoutError)) or "timeout" in type(exc).__name__.lower():
        return "Модель не успела ответить. Попробуй ещё раз или выбери другую модель."
    if isinstance(exc, httpx.TransportError) or "connection" in type(exc).__name__.lower():
        return "Не удалось связаться с API модели. Проверь сеть и адрес в /настройки консоли."
    return None
