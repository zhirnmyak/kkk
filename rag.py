"""RAG-слой: PDF → чанки → эмбеддинги → векторный поиск (Chroma, база на каждого ученика)."""
import re, threading, uuid
import chromadb
import config
import logging
import quota

_chroma = chromadb.PersistentClient(path=str(config.CHROMA_DIR))
_embedder = None

log = logging.getLogger("neurotutor.rag")

# Заглушка на месте нераспознанной страницы: короче порога чанка (40 симв.),
# поэтому в поиск не попадает, но номер страницы у следующих сохраняется.
UNREADABLE_PAGE = "[страница не распознана]"


def _is_quota_error(exc: Exception) -> bool:
    return quota.is_quota_error(exc)


class MockEmbedder:
    """Офлайн-эмбеддер (мешок слов по хешам). Для отладки логики хватает."""
    DIM = 512

    def encode(self, texts, normalize_embeddings=True, **_):
        import hashlib
        import numpy as np
        vecs = np.zeros((len(texts), self.DIM))
        for i, t in enumerate(texts):
            for w in t.lower().split():
                h = int(hashlib.md5(w.encode()).hexdigest(), 16)
                vecs[i, h % self.DIM] += 1
        norm = np.linalg.norm(vecs, axis=1, keepdims=True)
        norm[norm == 0] = 1
        return vecs / norm


_embedder_lock = threading.Lock()


def _get_embedder():
    if config.EMBED_MOCK:
        return MockEmbedder()
    global _embedder
    if _embedder is None:
        # Блокировка обязательна: несколько фоновых индексов стартуют одновременно,
        # и без неё каждый поток грузил свою копию модели (~1.5 ГБ и секунды на старт).
        with _embedder_lock:
            if _embedder is None:
                from sentence_transformers import SentenceTransformer
                _embedder = SentenceTransformer(config.EMBED_MODEL)
    return _embedder


def _collection_name(user_id, mock: bool | None = None) -> str:
    """Мок-эмбеддинги (512) и e5-small (384) несовместимы по размерности: общая
    коллекция после смены EMBED_MOCK падала на каждом поиске «Embedding dimension
    … does not match». Поэтому у мок-режима своя коллекция; имя для настоящей
    модели не меняется — существующие базы подхватываются как есть."""
    mock = config.EMBED_MOCK if mock is None else mock
    if mock and _legacy_mock(user_id):
        return f"user_{user_id}"
    return f"user_{user_id}" + ("_mock" if mock else "")


_LEGACY_MOCK: dict = {}


def _legacy_mock(user_id) -> bool:
    """База, собранная мок-эмбеддингами ДО разделения коллекций, лежит в user_N.
    Пока включён EMBED_MOCK, продолжаем пользоваться ею — иначе после обновления
    материалы «пропали» бы из поиска. Признак — размерность векторов мок-эмбеддера."""
    if user_id in _LEGACY_MOCK:
        return _LEGACY_MOCK[user_id]
    legacy = False
    try:
        _chroma.get_collection(f"user_{user_id}_mock")
    except Exception:
        try:
            got = _chroma.get_collection(f"user_{user_id}").get(limit=1, include=["embeddings"])
            embeddings = got.get("embeddings")
            legacy = (embeddings is not None and len(embeddings) > 0
                      and len(embeddings[0]) == MockEmbedder.DIM)
        except Exception:
            legacy = False
    _LEGACY_MOCK[user_id] = legacy
    return legacy


def _collection(user_id):
    # Изоляция данных: у каждого студента своя коллекция
    return _chroma.get_or_create_collection(_collection_name(user_id),
                                            metadata={"hnsw:space": "cosine"})


def _dimension_hint(exc: Exception) -> Exception:
    """Понятная ошибка вместо «dimension mismatch» после смены модели эмбеддингов."""
    if "dimension" in str(exc).lower():
        return RuntimeError(
            "база фрагментов построена другой моделью эмбеддингов (менялся EMBED_MODEL?) — "
            "выполни /очистить и загрузи материалы заново")
    return exc


def _fix_symbol_font(text: str) -> str:
    """Символьные шрифты (SymbolMT и т.п.) кладут знаки в частную область Unicode:
    U+F020…U+F07E — это обычный ASCII со сдвигом на 0xF000. Без возврата к ASCII
    «N = {A, B, C}» доезжает до LLM как нечитаемый мусор."""
    return "".join(chr(ord(c) - 0xF000) if 0xF020 <= ord(c) <= 0xF07E else c
                   for c in text)


def extract_pdf(data: bytes) -> list[str]:
    """PDF → список текстов страниц (номера страниц сохраняем для ссылок на источник).

    PyMuPDF, а не pypdf: на математических лекциях pypdf не раскодирует CFF-шрифты и
    выдаёт артефакты (.g234 вместо минуса, «отличны от н уля» вместо «нуля»).
    sort=True обязателен — без него формулы рассыпаются по символу на строку
    («D\\nC\\nB\\nA\\n» вместо «A, B, C, D»)."""
    import pymupdf
    doc = pymupdf.open(stream=data, filetype="pdf")
    try:
        return [_fix_symbol_font(page.get_text(sort=True)) for page in doc]
    finally:
        doc.close()

def extract_pdf_auto(data: bytes, progress=None) -> list[str]:
    """Берём текстовый слой PDF; если его нет (скан/рукопись) — включаем OCR.
    progress(done, total) — необязательный отчёт о страницах для чата."""
    pages = extract_pdf(data)
    avg = sum(len(p.strip()) for p in pages) / max(len(pages), 1)
    if avg >= 120:                       # нормальный текстовый слой
        return pages
    if not config.OCR_ENABLED:
        log.warning("Текстового слоя нет, но OCR выключен (OCR_ENABLED=0)")
        return pages
    log.info("Похоже на скан (%.0f симв./стр.) — запускаю OCR…", avg)

    def _easyocr_pdf():
        from ocr import pdf_pages_to_text
        log.warning("LLM-OCR упёрся в лимит — дочитываю PDF локальным EasyOCR")
        return pdf_pages_to_text(data, progress=progress)

    if config.TRANSCRIBE_BACKEND == "vision":
        from ocr_vision import pdf_pages_to_text_vision
        try:
            pages = pdf_pages_to_text_vision(data, progress=progress)
        except Exception as exc:
            if config.OCR_FALLBACK_EASYOCR and _is_quota_error(exc):
                pages = _easyocr_pdf()
            else:
                raise
    elif config.TRANSCRIBE_BACKEND == "llm":
        from ocr_vision import pdf_pages_to_text_llm
        try:
            pages = pdf_pages_to_text_llm(data, progress=progress)
        except Exception as exc:
            if config.OCR_FALLBACK_EASYOCR and _is_quota_error(exc):
                pages = _easyocr_pdf()
            else:
                raise
    else:
        from ocr import pdf_pages_to_text
        pages = pdf_pages_to_text(data, progress=progress)
    # Нечитаемую страницу НЕ выкидываем, а оставляем пустой заглушкой: иначе все
    # следующие страницы сдвигались, и ссылка «(стр. 5)» вела на шестую.
    checked = [p if ocr_usable(p) else UNREADABLE_PAGE for p in pages]
    bad = sum(1 for p in checked if p == UNREADABLE_PAGE)
    if bad:
        log.warning("OCR: %d страниц из %d без читаемого текста — в базу не пойдут",
                    bad, len(pages))
    return checked


def pdf_has_text(pages: list[str]) -> bool:
    """Есть ли в извлечённом PDF хоть одна страница с текстом (для честного отказа)."""
    return any(len((p or "").strip()) > 40 and p != UNREADABLE_PAGE for p in pages)


MIN_OCR_LETTERS = 60


def _repetitive(text: str) -> bool:
    """Вырождение vision-модели: она зацикливается и перебирает один и тот же
    шаблон («…в виде матрицы», «…в виде графа», «…в виде диаграммы»). Букв много,
    смысла нет, и по такому фрагменту поиск будет врать. Считаем долю уникальных
    четвёрок слов: у обычного текста она >0.9, у конспекта с формулами и матрицами
    опускается до ~0.7, а у настоящего цикла — ниже 0.2."""
    words = re.findall(r"[а-яa-zё]+", (text or "").lower())
    if len(words) < 80:
        return False
    total = len(words) - 3
    return len({tuple(words[i:i + 4]) for i in range(total)}) / total < 0.5


def ocr_usable(text: str) -> bool:
    """Порог «это вообще текст». На плохом фото vision возвращает скелет таблицы
    из вертикальных черт или LaTeX-обёртку: символов много, букв ноль. Молча
    положить это в базу знаний хуже, чем честно попросить переснять."""
    text = text or ""
    # только короткое служебное сообщение: страница лекции про «usage limit»
    # раньше отбрасывалась как ответ о лимите
    if quota.is_quota_text(text):
        log.warning("OCR вернул сообщение о лимите модели — в базу не пойдёт")
        return False
    if sum(c.isalpha() for c in text) < MIN_OCR_LETTERS:
        return False
    if _repetitive(text):
        log.warning("OCR выдал зацикленный текст (%d симв.) — в базу не пойдёт", len(text))
        return False
    return True


def extract_image_auto(data: bytes, mime: str = "image/jpeg", progress=None) -> str:
    """Фото страницы конспекта → текст. Бэкенд тот же, что для сканов PDF:
    vision-модель (ocr_vision) или локальный EasyOCR (ocr)."""
    if not config.OCR_ENABLED:
        log.warning("OCR выключен (OCR_ENABLED=0) — фото не распознаётся")
        return ""
    if progress:
        try:
            progress(0, 1)
        except Exception:
            pass
    def _easyocr_image():
        from ocr import image_to_text
        log.warning("LLM-OCR упёрся в лимит — читаю фото локальным EasyOCR")
        return image_to_text(data)

    if config.TRANSCRIBE_BACKEND == "vision":
        from ocr_vision import transcribe_image
        log.info("Фото → vision-модель (%s)", config.VISION_MODEL)
        try:
            text = transcribe_image(data, mime or "image/jpeg")
        except Exception as exc:
            if config.OCR_FALLBACK_EASYOCR and _is_quota_error(exc):
                text = _easyocr_image()
            else:
                raise
    elif config.TRANSCRIBE_BACKEND == "llm":
        from ocr_vision import transcribe_image_llm
        log.info("Фото → LLM vision через шлюз (%s)", config.LLM_MODEL)
        try:
            text = transcribe_image_llm(data, mime or "image/jpeg")
        except Exception as exc:
            if config.OCR_FALLBACK_EASYOCR and _is_quota_error(exc):
                text = _easyocr_image()
            else:
                raise
    else:
        from ocr import image_to_text
        log.info("Фото → EasyOCR")
        text = image_to_text(data)
    if progress:
        try:
            progress(1, 1)
        except Exception:
            pass
    letters = sum(c.isalpha() for c in text)
    if not ocr_usable(text):
        log.warning("OCR вернул только %d букв из %d символов — считаем нечитаемым",
                    letters, len(text))
        return ""
    return text


def extract_pptx_auto(data: bytes, progress=None) -> list[str]:
    import pptx_reader
    return pptx_reader.extract(data, image_reader=extract_image_auto if config.OCR_ENABLED else None,
                               progress=progress)


def split_chunks(text: str, size: int = config.CHUNK_SIZE,
                 overlap: int = config.CHUNK_OVERLAP) -> list[str]:
    text = " ".join((text or "").split())
    chunks, start = [], 0
    while start < len(text):
        end = min(start + size, len(text))
        if end < len(text):  # стараемся резать по границе предложения
            dot = text.rfind(". ", start + size // 2, end)
            if dot != -1:
                end = dot + 1
        chunk = text[start:end].strip()
        if len(chunk) > 40:
            chunks.append(chunk)
        if end >= len(text):
            break
        start = max(end - overlap, start + 1)
    return chunks


def add_document(user_id, title, pages=None, full_text=None) -> int:
    """Индексация документа. pages — тексты страниц (PDF), иначе full_text."""
    texts, metas = [], []
    if pages:
        for pno, ptext in enumerate(pages, start=1):
            for chunk in split_chunks(ptext):
                texts.append(chunk); metas.append({"title": title, "page": pno})
    else:
        for chunk in split_chunks(full_text or ""):
            texts.append(chunk); metas.append({"title": title})
    if not texts:
        return 0
    emb = _get_embedder().encode([f"passage: {t}" for t in texts],
                                 normalize_embeddings=True).tolist()
    try:
        _collection(user_id).add(ids=[uuid.uuid4().hex for _ in texts],
                                 documents=texts, metadatas=metas, embeddings=emb)
    except Exception as exc:
        raise _dimension_hint(exc) from exc
    return len(texts)


def count_fragments(user_id, title: str) -> int:
    col = _collection(user_id)
    return len(col.get(where={"title": title}, include=[]).get("ids", []))


def remove_document(user_id, title: str):
    """Перезагрузка файла с тем же именем должна заменять его фрагменты,
    а не удваивать их — иначе поиск начинает показывать и старую, и новую версию."""
    _collection(user_id).delete(where={"title": title})


def document_chunks(user_id, title: str) -> list[dict]:
    """Все фрагменты документа в порядке страниц — для сводки и подробного пересказа."""
    got = _collection(user_id).get(where={"title": title}, include=["documents", "metadatas"])
    pairs = list(zip(got.get("documents") or [], got.get("metadatas") or []))
    pairs.sort(key=lambda pair: (pair[1] or {}).get("page") or 0)
    return [{"text": text, "page": (meta or {}).get("page")} for text, meta in pairs]


def document_stats(user_id) -> dict[str, dict]:
    """{заголовок: {"fragments": N, "pages": [номера]}} одним запросом и без текстов.
    Агент раньше на каждый вопрос тянул ВСЕ тексты ВСЕХ документов (дважды) только
    ради номеров страниц — на больших конспектах это секунды в event loop."""
    got = _collection(user_id).get(include=["metadatas"])
    stats: dict[str, dict] = {}
    for meta in got.get("metadatas") or []:
        meta = meta or {}
        item = stats.setdefault(meta.get("title", ""), {"fragments": 0, "pages": set()})
        item["fragments"] += 1
        if meta.get("page"):
            item["pages"].add(meta["page"])
    return {title: {"fragments": item["fragments"], "pages": sorted(item["pages"])}
            for title, item in stats.items()}


def clear_user(user_id) -> int:
    """Удаляет все коллекции фрагментов пользователя (и мок, и настоящую);
    возвращает число удалённых фрагментов."""
    count = 0
    for name in (f"user_{user_id}", f"user_{user_id}_mock"):
        try:
            collection = _chroma.get_collection(name)
        except Exception:
            continue
        count += collection.count()
        _chroma.delete_collection(name)
    _LEGACY_MOCK.pop(user_id, None)
    return count


def search(user_id, query, k: int = config.TOP_K, title: str | None = None) -> list[dict]:
    """Поиск по смыслу. title — искать только внутри одного документа
    (/карточки, /тест по конкретному файлу)."""
    col = _collection(user_id)
    total = count_fragments(user_id, title) if title else col.count()
    if total == 0:
        return []
    q_emb = _get_embedder().encode([f"query: {query}"], normalize_embeddings=True)[0].tolist()
    kwargs = {"where": {"title": title}} if title else {}
    try:
        res = col.query(query_embeddings=[q_emb], n_results=min(k, total), **kwargs)
    except Exception as exc:
        raise _dimension_hint(exc) from exc
    hits = []
    for doc, meta, dist in zip(res["documents"][0], res["metadatas"][0], res["distances"][0]):
        hits.append({"text": doc, "title": meta.get("title", ""),
                     "page": meta.get("page"), "score": round(1 - dist, 3)})
    return hits
