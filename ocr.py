"""OCR для сканов и рукописных конспектов: PDF → картинки страниц → текст."""
_reader = None


def _get_reader():
    """Ленивая инициализация. Первый запуск скачивает модели EasyOCR (~120 МБ)."""
    global _reader
    if _reader is None:
        import easyocr
        _reader = easyocr.Reader(["ru", "en"], gpu=False, verbose=False)
    return _reader


def image_to_text(data: bytes) -> str:
    """Фото или скан одной страницы (bytes) → текст. EasyOCR умеет картинку
    напрямую, без прокладки через PDF."""
    import io
    import numpy as np
    from PIL import Image, ImageOps

    # фото с телефона лежит «на боку» и поворачивается только EXIF-тегом:
    # без exif_transpose EasyOCR читал строки поперёк и выдавал мусор
    img = ImageOps.exif_transpose(Image.open(io.BytesIO(data))).convert("RGB")
    arr = np.asarray(img)
    return "\n".join(_get_reader().readtext(arr, detail=0, paragraph=True))


def pdf_pages_to_text(data: bytes, dpi: int = 200, progress=None) -> list[str]:
    """Рендерим страницы PDF и распознаём рукописный текст."""
    import pymupdf          # PyMuPDF
    import numpy as np

    reader = _get_reader()
    doc = pymupdf.open(stream=data, filetype="pdf")
    pages = []
    try:
        total = doc.page_count
        if progress:
            try:
                progress(0, total)
            except Exception:
                pass
        for number, page in enumerate(doc, 1):
            pix = page.get_pixmap(dpi=dpi)
            img = np.frombuffer(pix.samples, dtype=np.uint8).reshape(
                pix.height, pix.width, pix.n)
            if pix.n == 4:                       # RGBA → RGB
                img = img[:, :, :3]
            elif pix.n == 1:                     # оттенки серого
                img = img[:, :, 0]
            lines = reader.readtext(img, detail=0, paragraph=True)
            pages.append("\n".join(lines))
            if progress:
                try:
                    progress(number, total)
                except Exception:
                    pass
    finally:
        doc.close()                              # и при ошибке OCR на середине
    return pages