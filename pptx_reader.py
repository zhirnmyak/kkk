"""PPTX → текст слайдов без PowerPoint, Qoder и внешнего конвертера."""
from io import BytesIO
import posixpath
import zipfile
from xml.etree import ElementTree as ET

from user_errors import UserNotice

MAX_SLIDES = 15
MAX_UNPACKED = 150 * 1024 * 1024
MAX_XML = 8 * 1024 * 1024
P = "http://schemas.openxmlformats.org/presentationml/2006/main"
A = "http://schemas.openxmlformats.org/drawingml/2006/main"
R = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"
C = "http://schemas.openxmlformats.org/drawingml/2006/chart"


def extract(data, image_reader=None, progress=None):
    try:
        with zipfile.ZipFile(BytesIO(data)) as archive:
            infos = archive.infolist()
            if len(infos) > 10000 or sum(i.file_size for i in infos) > MAX_UNPACKED:
                raise UserNotice("PPTX слишком большой после распаковки. Уменьши изображения и пришли снова.")
            if any(i.flag_bits & 1 for i in infos):
                raise UserNotice("PPTX защищён паролем. Сними пароль и пришли снова.")

            def xml(path):
                if archive.getinfo(path).file_size > MAX_XML:
                    raise UserNotice("Содержимое PPTX слишком большое. Раздели презентацию на части.")
                return ET.fromstring(archive.read(path))

            def rels(path):
                folder, base = posixpath.split(path)
                rel_path = f"{folder}/_rels/{base}.rels"
                if rel_path not in archive.namelist():
                    return {}
                result = {}
                for rel in xml(rel_path):
                    if rel.get("TargetMode") == "External":
                        continue
                    target = rel.get("Target", "")
                    target = posixpath.normpath(target.lstrip("/") if target.startswith("/")
                                               else posixpath.join(folder, target))
                    if target.startswith("../"):
                        raise UserNotice("В PPTX некорректные ссылки на содержимое.")
                    result[rel.get("Id")] = target
                return result

            deck = xml("ppt/presentation.xml")
            ids = deck.findall(f"{{{P}}}sldIdLst/{{{P}}}sldId")
            if not ids:
                raise UserNotice("В презентации нет слайдов.")
            if len(ids) > MAX_SLIDES:
                raise UserNotice(f"В PPTX {len(ids)} слайдов. Принимаю до {MAX_SLIDES} — раздели презентацию на части.")
            slide_rels = rels("ppt/presentation.xml")
            pages = []
            for number, slide_id in enumerate(ids, 1):
                path = slide_rels[slide_id.get(f"{{{R}}}id")]
                slide = xml(path)
                # Параграфы включают обычный текст, группы фигур и ячейки таблиц.
                parts = [" ".join(t.text or "" for t in para.iter(f"{{{A}}}t"))
                         for para in slide.iter(f"{{{A}}}p")]
                links = rels(path)
                for chart in slide.iter(f"{{{C}}}chart"):
                    target = links.get(chart.get(f"{{{R}}}id"))
                    if target:
                        parts.append("Диаграмма: " + "; ".join(
                            el.text or "" for el in xml(target).iter(f"{{{C}}}v")))
                # Для слайдов-картинок OCR читается из вложенных изображений.
                if image_reader and sum(c.isalpha() for c in " ".join(parts)) < 40:
                    seen = set()
                    for blip in slide.iter(f"{{{A}}}blip"):
                        target = links.get(blip.get(f"{{{R}}}embed"))
                        if target and target not in seen:
                            seen.add(target)
                            text = image_reader(archive.read(target))
                            if text:
                                parts.append(text)
                text = "\n".join(p.strip() for p in parts if p.strip())
                # Пустая страница сохраняет номер слайда, но не попадает в поиск.
                pages.append(text or "[страница не распознана]")
                if progress:
                    progress(number, len(ids))
            return pages
    except UserNotice:
        raise
    except (zipfile.BadZipFile, KeyError, ET.ParseError, OSError) as exc:
        raise UserNotice("Не удалось прочитать PPTX. Проверь файл и сохрани его заново без пароля.") from exc
