"""Транскрипция рукописных/сканированных страниц через Vision-LLM.

Включается строкой в .env:
    TRANSCRIBE_BACKEND=vision

Настройки vision-провайдера задаются отдельно от текстового LLM (config.py):
    VISION_BASE_URL  — напр. https://api.cloudflare.com/client/v4/accounts/<ACCOUNT_ID>/ai/v1
    VISION_API_KEY   — токен (для Cloudflare: право Workers AI -> Run)
    VISION_MODEL     — напр. @cf/meta/llama-3.2-11b-vision-instruct
    VISION_PROXY     — опционально, напр. http://127.0.0.1:10809

Особенности:
• запрос идёт на нативный /ai/run, а не в OpenAI-совместимый /ai/v1: второй
  отказывается принимать картинки (400 про «no user-supplied messages»);
• для @cf/meta/*-моделей Cloudflare требует один раз принять лицензию —
  POST {"prompt": "agree"} на тот же /ai/run (см. ошибку в transcribe_image);
• страницы PDF конвертируются в JPEG (PNG при dpi=200 слишком тяжёлый для
  лимита размера запроса Cloudflare; JPEG в 5–8 раз легче);
• постраничная обработка: ошибка на одной странице не роняет весь документ;
• один повтор при сетевом сбое / 429 (лимит free-тарифа).
"""
import base64
import os
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from io import BytesIO

import config
import quota
import vision_support
from user_errors import UserNotice, ApiFailure

# Когда stdout перенаправлен в файл или в пайп, Windows берёт cp1251/cp866, и
# любой print с эмодзи (⚠️) падает UnicodeEncodeError — причём вместо исходной
# ошибки OCR. Ошибки кодирования не должны ронять обработку: заменяем символы.
for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(errors="replace")
    except (AttributeError, OSError, ValueError):
        pass

RESET_LINE = "\r" + " " * 100 + "\r"

QUOTA_MARKERS = quota.QUOTA_MARKERS


def _is_quota_error(exc: Exception) -> bool:
    return quota.is_quota_error(exc)


def _raise_if_quota(text: str) -> None:
    # только короткое служебное сообщение: страница лекции про «rate limit»
    # раньше роняла распознавание всего PDF как «дневной лимит»
    if quota.is_quota_text(text):
        raise RuntimeError(f"модель вернула дневной лимит вместо OCR: {text[:180]!r}")


def _say(text: str) -> None:
    """print, который не имеет права уронить индексацию из-за кодировки."""
    try:
        print(text, flush=True)
    except Exception:
        try:
            print(text.encode("ascii", "replace").decode("ascii"), flush=True)
        except Exception:
            pass


def _progress(done: int, total: int) -> None:
    """Одна меняющаяся строка вместо строки на каждую страницу."""
    try:
        sys.stdout.write(f"\r   OCR [{config.LLM_MODEL}]: {done}/{total}")
        sys.stdout.flush()
    except Exception:
        pass


def _progress_done() -> None:
    try:
        sys.stdout.write(RESET_LINE)
        sys.stdout.flush()
    except Exception:
        pass

PROMPT = (
    "Это страница конспекта, слайда или учебника. Перепиши ТОЛЬКО то, что реально "
    "написано на картинке, построчно и без изменений. Не дополняй, не объясняй, "
    "не придумывай заголовки, определения и примеры, которых на картинке нет. "
    "Пиши обычным текстом, без LaTeX и без обёрток вида \\begin{align*}. Таблицы "
    "переписывай построчно в виде «строка: колонка — значение, колонка — значение»; "
    "пустых ячеек из вертикальных черт быть не должно. Если ячейка не читается, "
    "пиши в ней [неразборчиво]. Если на картинке нет читаемого текста, верни одну "
    "строку [текст не найден]."
)


def _run_url() -> str:
    """Нативный эндпоинт Workers AI: …/accounts/<id>/ai/run/<модель>.

    OpenAI-совместимый /ai/v1/chat/completions картинки этого модели НЕ принимает —
    отвечает 400 «Unable to add image when there are no user-supplied … messages»,
    поэтому ходим напрямую, а не через SDK."""
    base = config.VISION_BASE_URL.rstrip("/")
    if base.endswith("/v1"):
        base = base[:-3]
    return f"{base}/run/{config.VISION_MODEL}"


def _post(payload: dict) -> str:
    """Один запрос к Workers AI. Клиент создаётся на вызов — безопасно при смене
    прокси/ключа на лету."""
    import httpx

    with httpx.Client(timeout=120, proxy=config.VISION_PROXY or None) as client:
        r = client.post(_run_url(),
                        headers={"Authorization": f"Bearer {config.VISION_API_KEY}"},
                        json=payload)
    data = r.json()
    if not data.get("success"):
        err = (data.get("errors") or [{}])[0].get("message") or f"HTTP {r.status_code}"
        raise RuntimeError(err)
    return (data.get("result") or {}).get("response") or ""


def _to_jpeg(image_bytes: bytes, quality: int = 85, max_side: int = 1600) -> bytes:
    """Workers AI принимает JPEG; PNG он не разбирает, и это не ошибка, а молчаливая
    галлюцинация: модель получает пустоту и сочиняет «конспект» по одному промпту.
    Заодно режем размер — фото с телефона на 4000 px это тысячи токенов впустую."""
    from PIL import Image, ImageOps

    img = ImageOps.exif_transpose(Image.open(BytesIO(image_bytes)))
    img.thumbnail((max_side, max_side))
    if img.mode != "RGB":
        img = img.convert("RGB")
    out = BytesIO()
    img.save(out, "JPEG", quality=quality)
    return out.getvalue()


def transcribe_image(image_bytes: bytes, mime: str = "image/jpeg") -> str:
    """Картинка (bytes) → распознанный текст. Одна повторная попытка при сбое.

    mime остаётся в сигнатуре ради вызывающего кода: нативный запрос принимает
    только base64 JPEG, поля для формата в нём нет."""
    b64 = base64.b64encode(_to_jpeg(image_bytes)).decode()
    payload = {"prompt": PROMPT, "image": b64, "max_tokens": 2000}
    last_err = None
    for attempt in (1, 2):
        try:
            text = _post(payload).strip()
            _raise_if_quota(text)
            return text
        except Exception as e:
            last_err = e
            _say(f"   ⚠️ Попытка {attempt} не удалась: {e}")
            time.sleep(3)
    if "Model Agreement" in str(last_err):
        raise RuntimeError(
            "Cloudflare требует сначала принять лицензию vision-модели (Workers AI, "
            "prompt 'agree'). Либо поставь в .env TRANSCRIBE_BACKEND=easyocr — локальный "
            "EasyOCR уже скачан и читает фото не хуже.") from last_err
    raise last_err


def transcribe_png(png_bytes: bytes, mime: str = "image/png") -> str:
    """Обратная совместимость со старым именем функции."""
    return transcribe_image(png_bytes, mime)


def _page_to_jpeg(page, dpi: int, quality: int) -> bytes:
    """Страница PDF → JPEG (bytes)."""
    from PIL import Image
    pix = page.get_pixmap(dpi=dpi)
    img = Image.frombytes("RGB", (pix.width, pix.height), pix.samples)
    buf = BytesIO()
    img.save(buf, "JPEG", quality=quality)
    return buf.getvalue()


def _workers_for(total: int) -> int:
    """Больше, чем шлюз умеет параллельно, отправлять бессмысленно: лишние
    запросы просто висят в очереди и упираются в queue_full (429)."""
    setting = int(getattr(config, "OCR_WORKERS", 3) or 0)
    gateway_parallel = int(os.getenv("GATEWAY_MAX_PARALLEL", "3") or 3)
    wanted = total if setting <= 0 else setting
    return max(1, min(wanted, total, max(1, gateway_parallel)))


def _pages_to_text(jpegs: list[bytes], transcribe, progress=None) -> list[str]:
    """Параллельное распознавание страниц; порядок результата = порядок страниц.
    progress(done, total) вызывается из рабочих потоков — бот использует его,
    чтобы показывать «распознаю страницы: 3/12» прямо в чате."""
    total = len(jpegs)
    if not total:
        return []
    texts = [""] * total
    finished = 0
    first_error = None
    lock = threading.Lock()
    _progress(0, total)
    if progress:
        try:
            progress(0, total)
        except Exception:
            pass

    def work(index: int):
        return index, transcribe(jpegs[index], mime="image/jpeg")

    with ThreadPoolExecutor(max_workers=_workers_for(total)) as pool:
        futures = {pool.submit(work, index): index for index in range(total)}
        for future in as_completed(futures):
            index = futures[future]
            try:
                _, text = future.result()
                texts[index] = text
            except Exception as e:
                if _is_quota_error(e):
                    raise
                _say(f"\n   ⚠️ Страница {index + 1} не распознана: {e}")
                first_error = first_error or e
            with lock:
                finished += 1
                _progress(finished, total)
                if progress:
                    try:
                        progress(finished, total)
                    except Exception:
                        pass
    if not any(texts) and first_error:
        raise first_error
    return texts


def _pdf_to_jpegs(data: bytes, dpi: int, quality: int) -> list[bytes]:
    import pymupdf

    doc = pymupdf.open(stream=data, filetype="pdf")
    try:
        return [_page_to_jpeg(page, dpi, quality) for page in doc]
    finally:
        doc.close()


def pdf_pages_to_text_vision(data: bytes, dpi: int = 200, quality: int = 85,
                             progress=None) -> list[str]:
    """PDF (bytes) → список текстов страниц через vision-модель.

    Сигнатура совместима с extract_pdf/extract_pdf_auto из rag.py —
    бэкенд переключается только строкой TRANSCRIBE_BACKEND в .env.
    """
    jpegs = _pdf_to_jpegs(data, dpi, quality)
    try:
        return _pages_to_text(jpegs, transcribe_image, progress)
    finally:
        _progress_done()


def transcribe_image_llm(image_bytes: bytes, mime: str = "image/jpeg") -> str:
    """Картинка → текст через выбранные OpenAI-совместимый API и модель (LLM_*)."""
    import httpx

    base_url, api_key, model = config.LLM_BASE_URL, config.LLM_API_KEY or "", config.LLM_MODEL
    info = vision_support.get(base_url, api_key, model)
    if info and info["supported"] is False:
        raise UserNotice("Выбранная модель не поддерживает изображения. Выбери Vision-модель или EasyOCR в настройках.")
    b64 = base64.b64encode(_to_jpeg(image_bytes)).decode()
    payload = {
        "model": model,
        "temperature": 0,
        "max_tokens": 2000,
        "messages": [{
            "role": "user",
            "content": [
                {"type": "text", "text": PROMPT},
                {"type": "image_url", "image_url": {"url": f"data:image/jpeg;base64,{b64}"}},
            ],
        }],
    }
    with httpx.Client(timeout=200) as client:
        for attempt in range(1, 4):
            r = client.post(f"{base_url.rstrip('/')}/chat/completions",
                            headers={"Authorization": f"Bearer {api_key}"},
                            json=payload)
            if r.status_code == 429 and attempt < 3:
                time.sleep(2 * attempt)
                continue
            break
    if r.status_code >= 400:
        if r.status_code in (400, 422) and vision_support.rejected(r.text):
            vision_support.remember(base_url, api_key, model, False)
            raise UserNotice("API не поддерживает изображения для выбранной модели. Выбери Vision-модель или EasyOCR в настройках.")
        raise ApiFailure(r.status_code)
    data = r.json()
    if data.get("error"):
        raise RuntimeError("Vision API вернул ошибку вместо текста OCR")
    text = (data["choices"][0]["message"]["content"] or "").strip()
    _raise_if_quota(text)
    return text


def pdf_pages_to_text_llm(data: bytes, dpi: int = 150, quality: int = 85,
                          progress=None) -> list[str]:
    """PDF → тексты страниц через выбранные API и Vision-модель."""
    jpegs = _pdf_to_jpegs(data, dpi, quality)
    try:
        return _pages_to_text(jpegs, transcribe_image_llm, progress)
    finally:
        _progress_done()
