"""Распознавание ответа «упёрлись в лимит модели».

Шлюз отдаёт лимит не ошибкой HTTP, а обычным текстом ответа («You've reached your
daily usage limit…»). Раньше в llm.py, rag.py, ocr_vision.py и bot/handlers.py жили
четыре разных списка маркеров, и проверка шла по ВСЕМУ тексту: нормальный ответ
или страница лекции про «rate limit» / «too many requests» выбрасывались как лимит.
Теперь одно место и два разных правила:

• is_quota_text — для ТЕКСТА ответа модели / OCR: лимит — это короткое служебное
  сообщение, а не JSON шага агента и не ответ со ссылками на страницы;
• is_quota_error — для текста ИСКЛЮЧЕНИЯ (HTTP 429, наш RuntimeError про лимит):
  там маркер в любом месте — уже достаточный признак.
"""
import re

# однозначные фразы служебного сообщения о лимите
STRONG_MARKERS = (
    "daily usage limit", "come back tomorrow", "you've reached your",
    "you have reached your", "quota exceeded", "insufficient quota",
    "insufficient_quota", "дневной лимит", "квота исчерпана", "повторите завтра",
)
# фразы, которые встречаются и в обычном учебном тексте (лекция про API/сети)
WEAK_MARKERS = (
    "usage limit", "rate limit", "too many requests", "превышен лимит запросов",
    "you've reached",
)
# слова, без которых слабый маркер не считается сообщением об ошибке
ERROR_WORDS = (
    "try again", "please", "later", "exceeded", "error", "429",
    "попробуйте", "позже",
)
# все маркеры — для обратной совместимости (раньше импортировали QUOTA_MARKERS)
QUOTA_MARKERS = STRONG_MARKERS + WEAK_MARKERS

# служебное сообщение о лимите — одна-две строки, а не страница текста
MAX_NOTICE_CHARS = 400

_PAGE_REF = re.compile(r"\(стр\.?\s*\d")


def is_quota_text(text: str) -> bool:
    """True, если ответ модели (или результат OCR) — это сообщение о лимите."""
    s = (text or "").strip()
    if not s or len(s) > MAX_NOTICE_CHARS:
        return False
    if s[0] in "{[" or s.startswith("```"):
        # JSON-команда агента / оглавление: {"tool":"search","query":"rate limit"}
        return False
    low = s.lower()
    if _PAGE_REF.search(low):
        # ответ со ссылкой на страницу — это ответ по материалу, а не лимит
        return False
    if any(marker in low for marker in STRONG_MARKERS):
        return True
    return (any(marker in low for marker in WEAK_MARKERS)
            and any(word in low for word in ERROR_WORDS))


def is_quota_error(exc: BaseException) -> bool:
    """True, если исключение говорит о лимите/квоте (HTTP 429, наш RuntimeError)."""
    low = str(exc).lower()
    return (getattr(exc, "status_code", None) == 429 or getattr(exc, "status", None) == 429
            or "quota" in low or any(marker in low for marker in QUOTA_MARKERS))
