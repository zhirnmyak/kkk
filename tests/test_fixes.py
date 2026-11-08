from tests import TEST_ROOT  # set up offline isolation before app imports
"""Регрессионные тесты к исправлениям из FIXES.md.

Полностью офлайн: LLM_MOCK=1, EMBED_MOCK=1, OCR выключен, база и индекс —
во временной папке. Ни сети, ни токенов, ни твоей настоящей data/ не трогают.

Запуск из папки проекта:
    python -m unittest discover -s tests -v
"""
import asyncio
import logging
import os
import shutil
import sys
import tempfile
import time
import unittest
import zipfile
from datetime import date, datetime, timedelta
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
TMP = Path(tempfile.mkdtemp(prefix="maximka-tests-"))
# до импорта config: load_dotenv() не перезаписывает уже заданные переменные
os.environ.update({
    "LLM_MOCK": "1", "EMBED_MOCK": "1", "OCR_ENABLED": "0", "IMAGE_ENABLED": "0",
    "DATA_DIR": str(TMP / "data"), "MESSENGER": "max", "UPDATES_MODE": "polling",
    "WEBHOOK_SECRET": "", "ANONYMIZED_TELEMETRY": "False",
})
sys.path.insert(0, str(ROOT))
logging.basicConfig(level=logging.CRITICAL)

import config  # noqa: E402
import db  # noqa: E402
import quota  # noqa: E402
import rag  # noqa: E402
import agent  # noqa: E402
import llm  # noqa: E402
from bot import handlers  # noqa: E402
from bot.console_client import ConsoleClient  # noqa: E402

LOOP = asyncio.new_event_loop()


def run(coro):
    """Один цикл событий на все тесты: блокировки и семафоры handlers
    привязываются к циклу при первом использовании."""
    return LOOP.run_until_complete(coro)


def tearDownModule():
    LOOP.close()
    shutil.rmtree(TMP, ignore_errors=True)


LECTURE = (
    "Клетка — элементарная единица строения и жизнедеятельности всех живых организмов. "
    "Клеточная мембрана отделяет содержимое клетки от внешней среды и регулирует обмен веществ. "
    "Ядро хранит наследственную информацию в виде молекул ДНК и управляет синтезом белка. "
    "Митохондрии называют энергетическими станциями клетки: на их кристах идёт синтез АТФ. "
    "Рибосомы собирают белки из аминокислот по матрице информационной РНК. "
    "Эндоплазматическая сеть бывает гладкой и шероховатой, на шероховатой расположены рибосомы. "
    "Аппарат Гольджи сортирует и упаковывает белки, формирует лизосомы. "
    "Лизосомы содержат ферменты и переваривают отработавшие структуры клетки. "
    "Хлоропласты растительной клетки осуществляют фотосинтез и содержат хлорофилл. "
    "Клеточный центр участвует в делении клетки и образовании веретена деления. "
    "Цитоскелет поддерживает форму клетки и обеспечивает движение органоидов. "
    "Вакуоли растительной клетки запасают воду и поддерживают тургорное давление."
)
LECTURE_2 = (
    "Митоз — способ деления соматических клеток, при котором дочерние клетки получают "
    "одинаковый набор хромосом. Профаза начинается со спирализации хромосом. "
    "В метафазе хромосомы выстраиваются по экватору клетки, образуя метафазную пластинку. "
    "В анафазе хроматиды расходятся к полюсам клетки под действием нитей веретена. "
    "Телофаза завершает деление ядра, затем происходит цитокинез — деление цитоплазмы. "
    "Мейоз отличается от митоза двумя последовательными делениями и кроссинговером, "
    "поэтому половые клетки получают половинный набор хромосом и новое сочетание генов."
)
PAGE_1 = ("Первая страница скана. Фотосинтез идёт в хлоропластах растительной клетки, "
          "световая фаза протекает на мембранах тилакоидов, темновая — в строме. ")
PAGE_3 = ("Третья страница скана. Гликолиз проходит в цитоплазме клетки и даёт две молекулы "
          "АТФ, а дальнейшее окисление пирувата идёт в матриксе митохондрий. ")

_uids = iter(range(1000, 10 ** 6))


def new_user() -> int:
    uid = next(_uids)
    db.touch_user(uid, uid)
    return uid


class Rec(ConsoleClient):
    """Консольный клиент, который не печатает, а запоминает ответы бота."""

    def __init__(self):
        super().__init__()
        self.out = []

    async def send_text(self, chat_id, text, keyboard=None, keyboard_last=False,
                        return_id=False):
        self.buttons = [b for row in (keyboard or []) for b in row]
        self.out.append(text)
        return None

    async def send_image(self, chat_id, path, caption=""):
        self.out.append(f"[картинка] {caption}")

    @property
    def text(self) -> str:
        return "\n".join(self.out)


async def say(client, uid, text="", attachments=None):
    await handlers.on_message(client, uid, uid, {"text": text, "attachments": attachments or []})
    await handlers.wait_for_indexing()


async def press(client, uid, payload):
    await handlers.on_callback(client, uid, uid, payload)
    await handlers.wait_for_indexing()


def add_doc(uid, title, text=LECTURE):
    n = rag.add_document(uid, title, full_text=text)
    db.ensure_document_row(uid, title)
    return n


def write(name: str, data: bytes) -> str:
    path = TMP / name
    path.write_bytes(data)
    return str(path)


def make_pdf(page_texts) -> bytes:
    import pymupdf
    doc = pymupdf.open()
    for text in page_texts:
        page = doc.new_page()
        if text:
            page.insert_text((72, 72), text, fontsize=11)
    try:
        return doc.tobytes()
    finally:
        doc.close()


class AnswerRecorder:
    """Подмена answer_question: проверяем, что сообщение ушло именно в вопрос."""

    def __init__(self):
        self.questions = []

    async def __call__(self, client, user_id, chat_id, question):
        self.questions.append(question)


# ---------------------------------------------------------------- команды

class CommandParsingTest(unittest.TestCase):
    def test_slash_commands(self):
        self.assertEqual(handlers.parse_command("/тест"), ("quiz", ""))
        self.assertEqual(handlers.parse_command("/конспект генетика"), ("summary", "генетика"))
        self.assertEqual(handlers.parse_command("/тест@maximka_bot"), ("quiz", ""))
        self.assertEqual(handlers.parse_command("/непонятно"), ("unknown", ""))

    def test_bare_word_only_alone(self):
        self.assertEqual(handlers.parse_command("тест"), ("quiz", ""))
        self.assertEqual(handlers.parse_command("Меню"), ("menu", ""))
        for phrase in ("Тест Тьюринга — что это такое?", "Удалить лишнее и объяснить",
                       "План клетки: какие органоиды главные?", "Помощь нужна срочно"):
            self.assertEqual(handlers.parse_command(phrase), ("", ""), phrase)

    def test_questions_with_command_words_reach_answer(self):
        uid, client, rec = new_user(), Rec(), AnswerRecorder()
        add_doc(uid, "клетка.txt")
        questions = ["Тест Тьюринга — что это такое?",
                     "Удалить лишнее и объяснить, что такое ядро",
                     "План клетки: какие органоиды главные?",
                     "Помощь нужна: зачем митохондриям кристы?"]
        with mock.patch.object(handlers, "answer_question", rec):
            for q in questions:
                run(say(client, uid, q))
        self.assertEqual(rec.questions, questions)
        self.assertEqual(db.get_state(uid)[0], "idle")
        self.assertEqual([t for t, _ in db.list_documents(uid)], ["клетка.txt"])

    def test_card_review_does_not_swallow_messages(self):
        uid, client, rec = new_user(), Rec(), AnswerRecorder()
        db.set_state(uid, "card_review", {"queue": [], "idx": 0, "total": 0})
        with mock.patch.object(handlers, "answer_question", rec):
            run(say(client, uid, "А что такое ядро?"))
        self.assertEqual(rec.questions, ["А что такое ядро?"])

    def test_upload_window_expires(self):
        uid, client, rec = new_user(), Rec(), AnswerRecorder()
        db.set_state(uid, "upload", {"until": time.time() - 1})
        with mock.patch.object(handlers, "answer_question", rec):
            run(say(client, uid, "Что такое рибосома?"))
        self.assertEqual(rec.questions, ["Что такое рибосома?"])
        self.assertEqual(db.get_state(uid)[0], "idle")


# ---------------------------------------------------------------- загрузка

class IngestTest(unittest.TestCase):
    def test_pptx_ingests_all_fifteen_slides_without_gateway(self):
        from tests.pptx_fixtures import make_pptx
        import gateway_manager
        uid, client = new_user(), Rec()
        path = write("lecture.pptx", make_pptx([LECTURE] * 15))
        with mock.patch.object(gateway_manager, "_spawn", side_effect=AssertionError("Qoder must not start")):
            run(say(client, uid, "", [{"name": "lecture.pptx", "handle": path, "mime": "application/zip"}]))
        chunks = rag.document_chunks(uid, "lecture.pptx")
        self.assertEqual({chunk["page"] for chunk in chunks}, set(range(1, 16)))
        self.assertIn("в базе знаний", client.text)

    def test_oversized_pptx_preserves_previous_document(self):
        from tests.pptx_fixtures import make_pptx
        uid, client = new_user(), Rec()
        add_doc(uid, "lecture.pptx")
        before = rag.count_fragments(uid, "lecture.pptx")
        path = write("too-many.pptx", make_pptx([LECTURE] * 16))
        run(say(client, uid, "", [{"name": "lecture.pptx", "handle": path, "mime": handlers.PPTX_MIME}]))
        self.assertIn("16 слайдов", client.text)
        self.assertIn("до 15", client.text)
        self.assertEqual(rag.count_fragments(uid, "lecture.pptx"), before)

    def test_ocr_api_limit_has_actionable_message(self):
        from user_errors import ApiFailure
        uid, client = new_user(), Rec()
        path = write("limited.jpg", b"image")
        with mock.patch.object(config, "OCR_ENABLED", True), \
                mock.patch.object(rag, "extract_image_auto", side_effect=ApiFailure(429)):
            run(say(client, uid, "", [{"name": "limited.jpg", "handle": path, "mime": "image/jpeg"}]))
        self.assertIn("лимит запросов", client.text)
        self.assertNotIn("Traceback", client.text)

    def test_long_text_is_indexed_without_upload(self):
        uid, client = new_user(), Rec()
        run(say(client, uid, LECTURE))
        titles = [t for t, _ in db.list_documents(uid)]
        self.assertEqual(len(titles), 1)
        self.assertTrue(titles[0].startswith("вставленный текст"))
        self.assertGreater(rag.count_fragments(uid, titles[0]), 0)
        self.assertIn("проиндексирован", client.text)

    def test_paste_parts_merge_into_one_document(self):
        uid, client = new_user(), Rec()
        run(say(client, uid, LECTURE))
        title = db.list_documents(uid)[0][0]
        before = rag.count_fragments(uid, title)
        run(say(client, uid, LECTURE_2))
        self.assertEqual([t for t, _ in db.list_documents(uid)], [title])
        self.assertGreater(rag.count_fragments(uid, title), before)
        self.assertIn("Дописал", client.text)
        self.assertEqual(db.get_stats(uid).get("docs"), 1)

    def test_question_right_after_paste_is_a_question(self):
        uid, client, rec = new_user(), Rec(), AnswerRecorder()
        run(say(client, uid, LECTURE))
        title = db.list_documents(uid)[0][0]
        before = rag.count_fragments(uid, title)
        question = ("Почему митохондрии называют энергетическими станциями клетки, как это "
                    "связано с кристами и синтезом АТФ из лекции выше и что произойдёт с клеткой, "
                    "если митохондрий станет заметно меньше, чем нужно для её работы?")
        self.assertGreater(len(question), handlers.PASTE_MIN_UPLOAD)
        with mock.patch.object(handlers, "answer_question", rec):
            run(say(client, uid, question))
        self.assertEqual(rec.questions, [question])
        self.assertEqual(rag.count_fragments(uid, title), before)

    def test_cp1251_txt(self):
        uid, client = new_user(), Rec()
        path = write("lekciya_cp1251.txt", LECTURE.encode("cp1251"))
        run(say(client, uid, "", [{"name": "lekciya.txt", "handle": path, "mime": "text/plain"}]))
        chunks = rag.document_chunks(uid, "lekciya.txt")
        self.assertTrue(chunks)
        self.assertIn("митохондрии", " ".join(c["text"] for c in chunks).lower())

    def test_decode_text_variants(self):
        self.assertEqual(handlers._decode_text("привет".encode("utf-16")), "привет")
        self.assertEqual(handlers._decode_text("\ufeffпривет".encode("utf-8")), "привет")
        self.assertEqual(handlers._decode_text("привет".encode("cp1251")), "привет")

    def test_photo_with_ocr_off_explains(self):
        uid, client = new_user(), Rec()
        path = write("photo.jpg", b"\xff\xd8\xff\xe0 not really a photo")
        run(say(client, uid, "", [{"name": "photo.jpg", "handle": path, "mime": "image/jpeg"}]))
        self.assertIn("OCR_ENABLED=0", client.text)
        self.assertEqual(db.list_documents(uid), [])

    def test_scan_pdf_with_ocr_off_explains(self):
        try:
            data = make_pdf(["", ""])
        except ImportError:
            self.skipTest("pymupdf не установлен")
        uid, client = new_user(), Rec()
        path = write("scan.pdf", data)
        run(say(client, uid, "", [{"name": "scan.pdf", "handle": path, "mime": "application/pdf"}]))
        self.assertIn("нет текстового слоя", client.text)
        self.assertEqual(db.list_documents(uid), [])

    def test_failed_reupload_keeps_old_version(self):
        try:
            data = make_pdf(["", ""])
        except ImportError:
            self.skipTest("pymupdf не установлен")
        uid, client = new_user(), Rec()
        add_doc(uid, "лекция.pdf")
        before = rag.count_fragments(uid, "лекция.pdf")
        path = write("scan2.pdf", data)
        run(say(client, uid, "", [{"name": "лекция.pdf", "handle": path, "mime": "application/pdf"}]))
        self.assertEqual(rag.count_fragments(uid, "лекция.pdf"), before)
        self.assertEqual([t for t, _ in db.list_documents(uid)], ["лекция.pdf"])

    def test_zip_with_texts_keeps_pages(self):
        uid, client = new_user(), Rec()
        path = TMP / "konspekt.zip"
        with zipfile.ZipFile(path, "w") as archive:
            archive.writestr("2.txt", LECTURE_2)
            archive.writestr("1.txt", LECTURE)
            archive.writestr("readme.exe", b"MZ")
        run(say(client, uid, "", [{"name": "конспект.zip", "handle": str(path),
                                   "mime": "application/zip"}]))
        stats = rag.document_stats(uid)
        self.assertEqual(stats["конспект.zip"]["pages"], [1, 2])
        first = [c for c in rag.document_chunks(uid, "конспект.zip") if c["page"] == 1]
        self.assertIn("Клетка", first[0]["text"])

    def test_unreadable_page_keeps_numbering(self):
        try:
            data = make_pdf(["1", "2", "3"])
        except ImportError:
            self.skipTest("pymupdf не установлен")
        import ocr
        with mock.patch.object(config, "OCR_ENABLED", True), \
                mock.patch.object(config, "TRANSCRIBE_BACKEND", "easyocr"), \
                mock.patch.object(ocr, "pdf_pages_to_text",
                                  lambda data, progress=None: [PAGE_1, "", PAGE_3]):
            pages = rag.extract_pdf_auto(data)
        self.assertEqual(pages[1], rag.UNREADABLE_PAGE)
        uid = new_user()
        rag.add_document(uid, "скан.pdf", pages=pages)
        self.assertEqual(rag.document_stats(uid)["скан.pdf"]["pages"], [1, 3])
        third = [c for c in rag.document_chunks(uid, "скан.pdf") if c["page"] == 3]
        self.assertIn("Гликолиз", third[0]["text"])


# ---------------------------------------------------------------- лимиты

class QuotaTest(unittest.TestCase):
    def test_real_limit_messages(self):
        self.assertTrue(quota.is_quota_text("You've reached your daily usage limit. Come back tomorrow."))
        self.assertTrue(quota.is_quota_text("Error 429: rate limit exceeded, please try again later"))

    def test_study_text_is_not_a_limit(self):
        self.assertFalse(quota.is_quota_text(
            "Rate limit — это ограничение числа запросов к API в единицу времени."))
        self.assertFalse(quota.is_quota_text('{"tool":"search","query":"rate limit exceeded"}'))
        self.assertFalse(quota.is_quota_text("(стр. 3) Когда превышен лимит запросов, сервер "
                                             "отвечает 429 — попробуйте позже."))
        self.assertFalse(quota.is_quota_text(("Лекция о сетях. " * 40) + "quota exceeded"))

    def test_quota_errors(self):
        self.assertTrue(quota.is_quota_error(RuntimeError("HTTP 429 Too Many Requests")))
        self.assertFalse(quota.is_quota_error(ValueError("LLM не вернул валидный JSON")))


# ---------------------------------------------------------------- карточки и тест

class CardsAndQuizTest(unittest.TestCase):
    def _with_cards(self):
        uid, client = new_user(), Rec()
        add_doc(uid, "клетка.txt")
        run(say(client, uid, "/карточки"))
        run(say(client, uid, "/повторение"))
        state, s = db.get_state(uid)
        self.assertEqual(state, "card_review")
        return uid, client, s["queue"][0]

    def test_double_tap_counts_once(self):
        uid, client, first = self._with_cards()
        run(press(client, uid, {"action": "card_good", "id": first}))
        run(press(client, uid, {"action": "card_good", "id": first}))
        card = db.get_card(first)
        self.assertEqual((card["box"], card["good"]), (2, 1))
        self.assertEqual(db.get_state(uid)[1]["idx"], 1)

    def test_stale_card_button_during_quiz(self):
        uid, client, first = self._with_cards()
        run(say(client, uid, "/тест"))
        self.assertEqual(db.get_state(uid)[0], "quiz")
        run(press(client, uid, {"action": "card_good", "id": first}))   # не должно падать
        state, s = db.get_state(uid)
        self.assertEqual((state, s["idx"]), ("quiz", 0))
        self.assertEqual(db.get_card(first)["good"], 0)
        self.assertIn("не из текущей серии", client.text)

    def test_foreign_card_is_hidden(self):
        _owner, _client, first = self._with_cards()
        stranger, client = new_user(), Rec()
        run(press(client, stranger, {"action": "card_show", "id": first}))
        self.assertIn("не найдена", client.text)
        self.assertNotIn(db.get_card(first)["back"], client.text)

    def test_quiz_double_tap_and_stat(self):
        uid, client = new_user(), Rec()
        add_doc(uid, "клетка.txt")
        run(say(client, uid, "/тест"))
        self.assertEqual(db.get_stats(uid).get("quizzes", 0), 0)   # начать ≠ пройти
        button = client.buttons[0][1]
        run(press(client, uid, button))
        run(press(client, uid, button))   # двойное нажатие
        self.assertEqual(db.get_state(uid)[1]["idx"], 1)
        run(say(client, uid, "1"))
        run(say(client, uid, "1"))
        self.assertEqual(db.get_state(uid)[0], "idle")
        self.assertEqual(db.get_stats(uid).get("quizzes"), 1)
        self.assertIn("Тест завершён", client.text)


# ---------------------------------------------------------------- удаление

class DeleteTest(unittest.TestCase):
    def test_delete_button_is_exact(self):
        uid, client = new_user(), Rec()
        add_doc(uid, "лекция.txt")
        add_doc(uid, "лекция 2.txt", LECTURE_2)
        run(press(client, uid, {"action": "delete_file", "title": "лекция.txt"}))
        self.assertEqual([t for t, _ in db.list_documents(uid)], ["лекция 2.txt"])
        run(press(client, uid, {"action": "delete_file", "title": "лекция.txt"}))   # повторно
        self.assertEqual([t for t, _ in db.list_documents(uid)], ["лекция 2.txt"])
        self.assertIn("уже удалён", client.text)
        self.assertGreater(rag.count_fragments(uid, "лекция 2.txt"), 0)

    def test_clear_confirm_button_is_stale_outside_confirmation(self):
        uid, client = new_user(), Rec()
        add_doc(uid, "лекция.txt")
        run(press(client, uid, {"action": "clear_confirm"}))
        self.assertEqual([t for t, _ in db.list_documents(uid)], ["лекция.txt"])
        self.assertIn("устарело", client.text)


# ---------------------------------------------------------------- план

class PlanTest(unittest.TestCase):
    def test_parse_exam_date(self):
        parse = handlers._parse_exam_date
        self.assertEqual(parse("20.06.2031"), date(2031, 6, 20))
        self.assertEqual(parse("20.06.31"), date(2031, 6, 20))
        self.assertEqual(parse("20/06/2031"), date(2031, 6, 20))
        self.assertEqual(parse("2031-06-20"), date(2031, 6, 20))
        self.assertIsNone(parse("завтра"))
        self.assertIsNone(parse("31.02.2031"))
        day = parse("15.03")
        self.assertEqual((day.day, day.month), (15, 3))
        self.assertGreaterEqual(day, date.today())
        example = datetime.strptime(handlers._example_date(), "%d.%m.%Y").date()
        self.assertGreater(example, date.today())

    def test_past_date_rejected(self):
        uid, client = new_user(), Rec()
        db.set_state(uid, "plan_date")
        run(say(client, uid, "01.01.2020"))
        self.assertEqual(db.get_state(uid)[0], "plan_date")
        self.assertIn("уже прошла", client.text)

    def test_clean_plan_normalizes(self):
        today = date.today()
        plan = {"days": [
            {"date": (today + timedelta(days=1)).strftime("%d.%m.%Y"), "tasks": "повторить конспект"},
            {"date": (today + timedelta(days=2)).isoformat(), "tasks": ["задача 1", " "]},
            {"date": "2000-01-01", "tasks": ["из прошлого"]},
            {"date": "когда-нибудь", "tasks": ["мусор"]},
            "не словарь",
        ]}
        self.assertEqual(handlers._clean_plan(plan, today + timedelta(days=10)), [
            {"date": (today + timedelta(days=1)).isoformat(), "tasks": ["повторить конспект"]},
            {"date": (today + timedelta(days=2)).isoformat(), "tasks": ["задача 1"]},
        ])

    def test_plan_flow_and_reminder_lookup(self):
        uid, client = new_user(), Rec()
        run(say(client, uid, "/план"))
        run(say(client, uid, (date.today() + timedelta(days=20)).strftime("%d.%m.%Y")))
        run(say(client, uid, "клетка, митоз"))
        self.assertEqual(db.get_state(uid)[0], "idle")
        self.assertTrue(db.plan_tasks_for(uid, date.today().isoformat()))

    def test_empty_plan_keeps_old_one(self):
        uid, client = new_user(), Rec()
        today = date.today().isoformat()
        db.set_plan(uid, today, [{"date": today, "tasks": ["старое"]}])
        db.set_state(uid, "plan_topics", {"exam": (date.today() + timedelta(days=5)).isoformat()})

        async def empty_plan(*_a, **_k):
            return {"days": []}

        with mock.patch.object(handlers.llm, "chat_json", empty_plan):
            run(say(client, uid, "генетика"))
        self.assertEqual(db.plan_tasks_for(uid, today), ["старое"])
        self.assertIn("пустой план", client.text)


# ---------------------------------------------------------------- агент и поиск

class StudyCreationTest(unittest.TestCase):
    def materials(self):
        uid, client = new_user(), Rec()
        add_doc(uid, "Биология.pdf", "BIO_MARKER. " + LECTURE)
        add_doc(uid, "Математика.pdf", "MATH_MARKER. " + LECTURE_2)
        return uid, client

    def test_quiz_cards_and_summary_prompt_for_material(self):
        uid, client = self.materials()
        for command in ("/тест", "/карточки", "/конспект"):
            with mock.patch.object(llm, "chat_json", mock.AsyncMock()) as create, \
                    mock.patch.object(llm, "chat", mock.AsyncMock()) as summary:
                run(say(client, uid, command))
            self.assertEqual(db.get_state(uid)[0], "study_select")
            self.assertIn("Биология.pdf", client.text)
            self.assertIn("Математика.pdf", client.text)
            create.assert_not_called()
            summary.assert_not_called()

    def test_explicit_subject_creates_exact_count_from_one_source(self):
        uid, client = self.materials()
        with mock.patch.object(llm, "chat_json", mock.AsyncMock(wraps=llm.chat_json)) as create:
            run(say(client, uid, "/тест по биологии на 4 вопроса с объяснениями"))
        state, data = db.get_state(uid)
        self.assertEqual(state, "quiz")
        self.assertEqual((data["title"], len(data["questions"])), ("Биология.pdf", 4))
        prompt = create.call_args.args[1]
        self.assertIn("BIO_MARKER", prompt)
        self.assertNotIn("MATH_MARKER", prompt)
        self.assertIn("с объяснениями", prompt)

    def test_card_count_and_source_are_saved(self):
        uid, client = self.materials()
        run(say(client, uid, "/карточки по биологии на 7 карточек"))
        cards = db.due_cards(uid)
        self.assertEqual(len(cards), 7)
        self.assertEqual({c["title"] for c in cards}, {"Биология.pdf"})

    def test_topic_search_stays_inside_selected_subject(self):
        uid, client = self.materials()
        with mock.patch.object(rag, "search", wraps=rag.search) as search:
            run(say(client, uid, "/тест по теме ядро клетки из биологии на 4 вопроса"))
        self.assertEqual(db.get_state(uid)[1]["title"], "Биология.pdf")
        self.assertEqual(search.call_args.kwargs["title"], "Биология.pdf")
        self.assertIn("ядро", search.call_args.args[1])

    def test_file_deletion_does_not_touch_another_user(self):
        uid, client = self.materials()
        other = new_user()
        add_doc(other, "Биология.pdf")
        db.add_cards(other, [{"front": "Чужая карточка", "back": "Ответ"}], title="Биология.pdf")
        run(handlers.delete_file(client, uid, uid, "Биология.pdf", exact=True))
        self.assertEqual(db.card_totals(other)[0], 1)
        self.assertTrue(db.has_document(other, "Биология.pdf"))

    def test_choice_preserves_count_and_stale_choice_is_rejected(self):
        uid, client = self.materials()
        run(say(client, uid, "/тест на 6 вопросов"))
        pending = db.get_state(uid)[1]
        index = pending["titles"].index("Биология.pdf")
        button = {"action": "study_pick", "s": pending["token"], "i": index}
        run(press(client, uid, button))
        data = db.get_state(uid)[1]
        self.assertEqual(len(data["questions"]), 6)
        session = data["session_id"]
        run(press(client, uid, button))
        self.assertEqual(db.get_state(uid)[1]["session_id"], session)
        self.assertIn("устарел", client.text)

    def test_text_choice_can_refine_count(self):
        uid, client = self.materials()
        run(say(client, uid, "/тест"))
        run(say(client, uid, "на 4 вопроса"))
        run(say(client, uid, "Биология.pdf"))
        self.assertEqual(db.get_state(uid)[1]["title"], "Биология.pdf")
        self.assertEqual(len(db.get_state(uid)[1]["questions"]), 4)

    def test_summary_selection_scopes_context(self):
        uid, client = self.materials()
        run(say(client, uid, "/конспект"))
        pending = db.get_state(uid)[1]
        index = pending["titles"].index("Биология.pdf")
        with mock.patch.object(llm, "chat", mock.AsyncMock(return_value="Выжимка")) as create:
            run(say(client, uid, str(index + 1)))
        self.assertIn("BIO_MARKER", create.call_args.args[1])
        self.assertNotIn("MATH_MARKER", create.call_args.args[1])

    def test_deleted_selection_cannot_switch_to_another_file(self):
        uid, client = self.materials()
        run(say(client, uid, "/карточки"))
        pending = db.get_state(uid)[1]
        index = pending["titles"].index("Биология.pdf")
        run(handlers.delete_file(client, uid, uid, "Биология.pdf", exact=True))
        run(press(client, uid, {"action": "study_pick", "s": pending["token"], "i": index}))
        self.assertEqual(db.card_totals(uid)[0], 0)
        self.assertIn("уже удалён", client.text)

    def test_deletion_removes_related_learning_and_keeps_other_subject(self):
        uid, client = self.materials()
        run(say(client, uid, "/карточки по биологии на 2 карточки"))
        first = db.due_cards(uid)[0]
        db.review_card(first["id"], False, "review-bio")
        run(say(client, uid, "/тест по биологии на 1 вопрос"))
        old_button = client.buttons[0][1]
        correct = db.get_state(uid)[1]["questions"][0]["answer"] + 1
        run(say(client, uid, str(correct)))
        self.assertEqual(db.get_stats(uid)["quizzes"], 1)
        run(say(client, uid, "/карточки по математике на 3 карточки"))
        run(say(client, uid, "/тест по математике на 4 вопроса"))
        session = db.get_state(uid)[1]["session_id"]
        run(handlers.delete_file(client, uid, uid, "Биология.pdf", exact=True))
        self.assertEqual(db.get_state(uid)[1]["session_id"], session)
        self.assertEqual(db.card_totals(uid)[0], 3)
        self.assertEqual(db.get_stats(uid)["quizzes"], 0)
        self.assertEqual(db.learning_results(uid, title="Биология.pdf")["total"], 0)
        self.assertEqual(db.learning_results(uid, kind="card", title="Биология.pdf")["total"], 0)
        self.assertEqual(db._run("SELECT COUNT(*) FROM quiz_runs WHERE user_id=? AND title=?", (uid, "Биология.pdf")).fetchone()[0], 0)
        run(press(client, uid, old_button))
        self.assertEqual(db.get_state(uid)[1]["idx"], 0)

    def test_deleting_active_quiz_and_clear_all(self):
        uid, client = self.materials()
        run(say(client, uid, "/тест по биологии"))
        button = client.buttons[0][1]
        run(handlers.delete_file(client, uid, uid, "Биология.pdf", exact=True))
        self.assertEqual(db.get_state(uid)[0], "idle")
        run(press(client, uid, button))
        self.assertEqual(db.learning_results(uid)["total"], 0)
        db.add_cards(uid, [{"front": "Старая карточка", "back": "Без привязки"}])
        run(handlers.perform_clear(client, uid, uid))
        self.assertEqual(db.card_totals(uid)[0], 0)
        self.assertFalse(db.has_learning_data(uid))

    def test_deleted_while_model_generates_does_not_resurrect_cards(self):
        uid, client = self.materials()
        async def generate(system, prompt, **kwargs):
            await handlers.delete_file(client, uid, uid, "Биология.pdf", exact=True)
            return llm._mock_json(system)
        with mock.patch.object(llm, "chat_json", generate):
            run(say(client, uid, "/карточки по биологии на 4 карточки"))
        self.assertEqual(db.card_totals(uid)[0], 0)
        self.assertFalse(db.has_document(uid, "Биология.pdf"))
        self.assertIn("не сохранены", client.text)

    def test_count_limit_and_underfilled_model_response(self):
        uid, client = self.materials()
        with mock.patch.object(llm, "chat_json", mock.AsyncMock()) as create:
            run(say(client, uid, "/тест по биологии на 21 вопрос"))
        create.assert_not_called()
        self.assertIn("от 1 до 20", client.text)
        partial = llm._mock_json(handlers.QUIZ_SYSTEM + " Ровно 2 вопроса")
        complete = llm._mock_json(handlers.QUIZ_SYSTEM + " Ровно 4 вопроса")
        with mock.patch.object(llm, "chat_json", mock.AsyncMock(side_effect=[partial, complete])) as create:
            run(say(client, uid, "/тест по биологии на 4 вопроса"))
        self.assertEqual(create.await_count, 2)
        self.assertEqual(len(db.get_state(uid)[1]["questions"]), 4)


class AgentStudyToolsTest(unittest.TestCase):
    def material(self):
        uid, client = new_user(), Rec()
        add_doc(uid, "Биология.pdf")
        return uid, client

    def decide(self, steps, permissions=None):
        import contextlib
        from study_intent import CURRENT_REQUEST_SYSTEM
        stack = contextlib.ExitStack()
        self.addCleanup(stack.close)
        stack.enter_context(mock.patch.object(agent, "ensure_summary", mock.AsyncMock(return_value=LECTURE)))
        stack.enter_context(mock.patch.object(agent, "ensure_toc", mock.AsyncMock(return_value="")))
        planner = mock.AsyncMock(side_effect=steps)
        self.permission_requests = []
        async def chat(system, prompt, *args, **kwargs):
            if system == CURRENT_REQUEST_SYSTEM:
                self.permission_requests.append(prompt)
                decision = permissions if permissions is not None else {"quiz": True, "cards": True}
                return decision if isinstance(decision, str) else llm.json.dumps(decision)
            return await planner(system, prompt, *args, **kwargs)
        stack.enter_context(mock.patch.object(llm, "chat", mock.AsyncMock(side_effect=chat)))
        return planner

    def test_paraphrase_creates_real_quiz_and_buttons_grade_answers(self):
        uid, client = self.material()
        planner = self.decide(['{"tool":"create_quiz","title":"Биология.pdf","topic":"клетка","count":4,"status":"проверяю знания"}'])
        with mock.patch.object(agent, "_final", mock.AsyncMock()) as final:
            run(say(client, uid, "Давай проверим, насколько я разобрался в этом материале. Четыре задания хватит."))
        final.assert_not_called()
        self.assertEqual(planner.await_count, 1)
        state, data = db.get_state(uid)
        self.assertEqual((state, len(data["questions"])), ("quiz", 4))
        self.assertTrue(client.buttons)
        button = client.buttons[0][1]
        self.assertEqual(button["action"], "quiz_answer")
        run(press(client, uid, button))
        self.assertEqual(db.get_state(uid)[1]["idx"], 1)
        self.assertEqual(db.learning_results(uid)["total"], 1)
        self.assertFalse(any(text.startswith('{"tool"') for text in client.out))

    def test_results_can_be_read_before_quiz_and_reach_generator(self):
        uid, client = self.material()
        q = {"question": "Что производят митохондрии?", "options": ["АТФ", "Крахмал"], "answer": 0}
        db.record_quiz_answer(uid, "old", 0, "Биология.pdf", q, 1)
        self.decide([
            '{"tool":"learning_results","kind":"quiz","mistakes_only":true,"status":"смотрю ошибки"}',
            '{"tool":"create_quiz","title":"Биология.pdf","topic":"митохондрии","count":4,"instructions":"потренировать ошибку с АТФ","status":"собираю тренировку"}',
        ])
        with mock.patch.object(llm, "chat_json", mock.AsyncMock(wraps=llm.chat_json)) as generate:
            run(say(client, uid, "Посмотри мои ошибки и дай мне возможность потренироваться на четырёх заданиях."))
        prompt = generate.call_args.args[1]
        self.assertIn("Что производят митохондрии?", prompt)
        self.assertIn("Крахмал", prompt)
        self.assertIn("АТФ", prompt)
        self.assertEqual(len(db.get_state(uid)[1]["questions"]), 4)

    def test_cards_are_saved_when_model_decides_to_create_them(self):
        uid, client = self.material()
        self.decide(['{"tool":"create_cards","title":"Биология.pdf","topic":"ядро","count":6,"status":"готовлю повторение"}'])
        run(say(client, uid, "Хочу закрепить это через шесть маленьких заданий для запоминания."))
        self.assertEqual(db.card_totals(uid)[0], 6)
        self.assertEqual({card["title"] for card in db.due_cards(uid)}, {"Биология.pdf"})

    def test_source_selection_after_agent_tool_preserves_count(self):
        uid, client = self.material()
        add_doc(uid, "Математика.pdf", LECTURE_2)
        self.decide(['{"tool":"create_quiz","title":"","topic":"","count":6,"status":"выбираю предмет"}'])
        run(say(client, uid, "Проверь меня шестью вопросами, предмет пока не выбрал."))
        state, pending = db.get_state(uid)
        self.assertEqual(state, "study_select")
        self.assertEqual(pending["spec"]["count"], 6)
        index = pending["titles"].index("Биология.pdf")
        run(press(client, uid, {"action": "study_pick", "s": pending["token"], "i": index}))
        data = db.get_state(uid)[1]
        self.assertEqual((data["title"], len(data["questions"])), ("Биология.pdf", 6))
        self.assertEqual(client.buttons[0][1]["action"], "quiz_answer")

    def test_pending_selection_can_be_refined_in_free_language(self):
        uid, client = self.material()
        add_doc(uid, "Математика.pdf", LECTURE_2)
        run(say(client, uid, "/тест на 6 вопросов"))
        planner = self.decide(['{"tool":"create_quiz","title":"Биология.pdf","count":4,"topic":"ядро"}'])
        run(say(client, uid, "Создай тест по биологии на четыре коротких вопроса."))
        self.assertIn("ожидается выбор материала", planner.call_args.args[1])
        self.assertIn('"count": 6', planner.call_args.args[1])
        self.assertEqual((db.get_state(uid)[1]["title"], len(db.get_state(uid)[1]["questions"])), ("Биология.pdf", 4))

    def test_free_language_during_quiz_reaches_agent(self):
        uid, client = self.material()
        run(say(client, uid, "/тест"))
        previous = db.get_state(uid)[1]["session_id"]
        planner = self.decide(['{"tool":"create_quiz","title":"Биология.pdf","count":4,"topic":"митохондрии"}'])
        run(say(client, uid, "Давай другой набор, четыре вопроса про митохондрии."))
        self.assertIn("тест начат", planner.call_args.args[1])
        self.assertNotEqual(db.get_state(uid)[1]["session_id"], previous)
        self.assertEqual(len(db.get_state(uid)[1]["questions"]), 4)

    def test_real_max_adapter_sends_quiz_keyboard_not_plain_text(self):
        from bot.max_client import MaxClient
        uid, _client = self.material()
        client = MaxClient.__new__(MaxClient)
        client._send_message = mock.AsyncMock(return_value={})
        self.decide(['{"tool":"create_quiz","title":"Биология.pdf","count":4}'])
        run(say(client, uid, "Пора проверить мои знания, пусть будет четыре задания."))
        body = client._send_message.call_args.args[1]
        keyboard = body["attachments"][0]["payload"]["buttons"]
        self.assertEqual(len(keyboard), 4)
        payload = llm.json.loads(keyboard[0][0]["payload"])
        self.assertEqual(payload["action"], "quiz_answer")
        self.assertEqual(payload["q"], 0)
        self.assertTrue(payload["s"])

    def test_model_can_force_source_choice_even_with_specific_topic(self):
        uid, client = self.material()
        add_doc(uid, "Математика.pdf", LECTURE_2)
        self.decide(['{"tool":"create_quiz","title":"Биология.pdf","topic":"биология","count":4,"choose_source":true}'])
        run(say(client, uid, "По биологии, но сначала хочу сам выбрать файл."))
        self.assertEqual(db.get_state(uid)[0], "study_select")
        self.assertEqual(len(db.get_state(uid)[1]["titles"]), 2)

    def test_model_can_create_summary_through_shared_handler(self):
        uid, client = self.material()
        self.decide(['{"tool":"create_summary","title":"Биология.pdf","topic":"клетка"}', "Краткий конспект о клетке."])
        run(say(client, uid, "Мне нужна короткая выжимка по этому материалу."))
        self.assertIn("Краткий конспект о клетке", client.text)

    def test_plain_text_quiz_is_repaired_to_action_in_agent_loop(self):
        uid, client = self.material()
        draft = "Вот тест: 1. Что такое клетка? А) ... Б) ... Ответь самостоятельно."
        planner = self.decide([draft, '{"tool":"create_quiz","title":"Биология.pdf","count":4,"status":"создаю тест"}'])
        run(say(client, uid, "Проверь, что я запомнил, четырьмя вопросами."))
        self.assertEqual(planner.await_count, 2)
        self.assertNotIn(draft, client.out)
        self.assertEqual(len(db.get_state(uid)[1]["questions"]), 4)

    def test_final_stage_can_dispatch_creation_instead_of_plain_quiz(self):
        uid, client = self.material()
        self.decide([
            '{"tool":"answer","status":"проверяю готовность"}',
            '{"tool":"create_quiz","title":"Биология.pdf","count":4}',
        ])
        run(say(client, uid, "Мне нужна проверка знаний по этому файлу на четыре вопроса."))
        self.assertEqual(db.get_state(uid)[0], "quiz")
        self.assertEqual(len(db.get_state(uid)[1]["questions"]), 4)

    def test_final_stage_raw_draft_cannot_bypass_action_protocol(self):
        uid, client = self.material()
        draft = "Список тестовых вопросов: 1. Что такое митоз? 2. Что такое ядро?"
        self.decide([
            '{"tool":"answer","status":"готовлю проверку"}', draft,
            '{"tool":"create_quiz","title":"Биология.pdf","count":4}',
        ])
        run(say(client, uid, "Давай потренируемся, мне нужно четыре задания."))
        self.assertNotIn(draft, client.out)
        self.assertEqual(db.get_state(uid)[0], "quiz")

    def test_explanation_remains_normal_answer_without_creating_quiz(self):
        uid, client = self.material()
        self.decide([
            '{"tool":"answer","status":"объясняю термин"}',
            '{"tool":"answer","answer":"Тест Тьюринга — способ оценки поведения системы."}',
        ])
        run(say(client, uid, "Что такое тест Тьюринга? Не нужно проводить его со мной."))
        self.assertIn("способ оценки", client.text)
        self.assertNotEqual(db.get_state(uid)[0], "quiz")
        self.assertEqual(db._run("SELECT COUNT(*) FROM quiz_runs WHERE user_id=?", (uid,)).fetchone()[0], 0)

    def test_bad_creation_parameters_are_returned_to_model(self):
        uid, client = self.material()
        planner = self.decide([
            '{"tool":"create_quiz","title":"Биология.pdf","count":0,"status":"готовлю тест"}',
            '{"tool":"create_quiz","title":"Биология.pdf","count":4,"status":"исправляю количество"}',
        ])
        run(say(client, uid, "Четыре вопроса, чтобы проверить знания."))
        self.assertIn("count должен быть от 1 до 20", planner.call_args_list[1].args[1])
        self.assertEqual(len(db.get_state(uid)[1]["questions"]), 4)

    def test_tool_cannot_select_other_users_document_or_invalid_counts(self):
        uid, _client = self.material()
        other = new_user()
        add_doc(other, "Секрет.pdf")
        result = run(agent._run_tool(uid, {"tool": "create_quiz", "title": "Секрет.pdf", "count":4, "user_id":other}))
        self.assertIsInstance(result, str)
        for count in (True, 0, 21, 4.5, "четыре"):
            result = run(agent._run_tool(uid, {"tool": "create_quiz", "title":"Биология.pdf", "count":count}))
            self.assertIsInstance(result, str)
        self.assertEqual(db._run("SELECT COUNT(*) FROM quiz_runs WHERE user_id=?", (other,)).fetchone()[0], 0)

    def test_old_quiz_request_cannot_authorize_new_creation(self):
        uid, client = self.material()
        old = "Создай тест по биологии на 4 вопроса"
        db.add_exchange(uid, old, "Запрос передан в создание теста")
        planner = self.decide([
            '{"tool":"create_quiz","title":"Биология.pdf","count":4}',
            '{"tool":"answer","status":"объясняю митоз"}',
            '{"tool":"answer","answer":"Митоз — это деление клетки."}',
        ], permissions={"quiz": False, "cards": False})
        with mock.patch.object(llm, "chat_json", mock.AsyncMock()) as generate:
            run(say(client, uid, "Объясни митоз"))
        generate.assert_not_called()
        self.assertNotEqual(db.get_state(uid)[0], "quiz")
        self.assertIn("Митоз — это", client.text)
        self.assertIn(old, planner.call_args_list[0].args[1])
        self.assertEqual(llm.json.loads(self.permission_requests[0]), {"current_message": "Объясни митоз"})

    def test_old_card_request_cannot_authorize_cards_after_thanks(self):
        uid, client = self.material()
        db.add_exchange(uid, "Создай карточки по ядру", "Карточки готовы")
        self.decide([
            '{"tool":"create_cards","title":"Биология.pdf","count":5}',
            '{"tool":"answer"}', '{"tool":"answer","answer":"Пожалуйста!"}',
        ], permissions={"quiz": False, "cards": False})
        run(say(client, uid, "Спасибо"))
        self.assertEqual(db.card_totals(uid)[0], 0)
        self.assertIn("Пожалуйста", client.text)

    def test_final_stage_cannot_create_from_history_either(self):
        uid, client = self.material()
        db.add_exchange(uid, "Сделай тест", "Тест готов")
        self.decide([
            '{"tool":"answer"}',
            '{"tool":"create_quiz","title":"Биология.pdf","count":4}',
            '{"tool":"answer","answer":"Ядро хранит ДНК."}',
        ], permissions={"quiz": False, "cards": False})
        run(say(client, uid, "Что делает ядро?"))
        self.assertIn("Ядро хранит ДНК", client.text)
        self.assertNotEqual(db.get_state(uid)[0], "quiz")

    def test_explicit_quiz_request_does_not_authorize_cards(self):
        uid, client = self.material()
        self.decide([
            '{"tool":"create_cards","title":"Биология.pdf","count":5}',
            '{"tool":"create_quiz","title":"Биология.pdf","count":4}',
        ], permissions={"quiz": True, "cards": False})
        run(say(client, uid, "Создай тест на 4 вопроса"))
        self.assertEqual(db.card_totals(uid)[0], 0)
        self.assertEqual(len(db.get_state(uid)[1]["questions"]), 4)
        self.assertEqual(len(self.permission_requests), 1)

    def test_unreadable_permission_response_blocks_creation(self):
        uid, client = self.material()
        self.decide([
            '{"tool":"create_quiz","title":"Биология.pdf","count":4}',
            '{"tool":"create_quiz","title":"Биология.pdf","count":4}',
            '{"tool":"answer"}', '{"tool":"answer","answer":"Вот объяснение темы."}',
        ], permissions="not valid JSON")
        run(say(client, uid, "Расскажи про клетку"))
        self.assertNotEqual(db.get_state(uid)[0], "quiz")
        self.assertEqual(len(self.permission_requests), 1)

    def test_explanation_during_active_quiz_does_not_restart_it(self):
        uid, client = self.material()
        run(say(client, uid, "/тест"))
        session = db.get_state(uid)[1]["session_id"]
        self.decide([
            '{"tool":"create_quiz","title":"Биология.pdf","count":5}',
            '{"tool":"answer"}', '{"tool":"answer","answer":"Митохондрии синтезируют АТФ."}',
        ], permissions={"quiz": False, "cards": False})
        run(say(client, uid, "Почему митохондрии вырабатывают АТФ?"))
        self.assertEqual(db.get_state(uid)[1]["session_id"], session)
        self.assertEqual(db.get_state(uid)[1]["idx"], 0)

    def test_pending_selection_does_not_authorize_creation_in_an_explanation(self):
        uid, client = self.material()
        add_doc(uid, "Математика.pdf", LECTURE_2)
        run(say(client, uid, "/тест"))
        token = db.get_state(uid)[1]["token"]
        self.decide([
            '{"tool":"create_quiz","title":"Биология.pdf","count":4}',
            '{"tool":"answer"}', '{"tool":"answer","answer":"В клетке есть ядро."}',
        ], permissions={"quiz": False, "cards": False})
        run(say(client, uid, "Объясни строение клетки"))
        state, pending = db.get_state(uid)
        self.assertEqual(state, "study_select")
        self.assertEqual(pending["token"], token)
        self.assertEqual(db._run("SELECT COUNT(*) FROM quiz_runs WHERE user_id=?", (uid,)).fetchone()[0], 0)


class LearningResultsTest(unittest.TestCase):
    def test_quiz_history_is_saved_once_and_keeps_wrong_answer(self):
        uid, client = new_user(), Rec()
        q = {"question": "Что производят митохондрии?", "options": ["АТФ", "Крахмал"],
             "answer": 0, "explanation": "Синтезируют АТФ"}
        db.set_state(uid, "quiz", {"questions": [q], "idx": 0, "score": 0,
                                   "session_id": "one-quiz", "title": "клетка.pdf"})
        with mock.patch.object(rag, "search", return_value=[]):
            run(handlers.process_quiz_answer(client, uid, uid, "2", q_index=0))
            run(handlers.process_quiz_answer(client, uid, uid, "2", q_index=0))
        data = db.learning_results(uid, mistakes_only=True)
        self.assertEqual(data["total"], 1)
        self.assertEqual(data["results"][0]["chosen"], "Крахмал")
        self.assertEqual(data["results"][0]["expected"], "АТФ")
        self.assertEqual(data["results"][0]["title"], "клетка.pdf")

    def test_card_events_keep_self_assessment_and_legacy_counts(self):
        uid = new_user()
        db.add_cards(uid, [{"front": "Определение клетки", "back": "Единица живого"}], title="биология")
        card = db.due_cards(uid)[0]
        db.review_card(card["id"], False, "review1")
        db.review_card(card["id"], False, "review1")
        self.assertEqual(db.get_card(card["id"], uid)["bad"], 1)
        data = db.learning_results(uid, kind="card", mistakes_only=True)
        self.assertEqual(data["total"], 1)
        self.assertEqual(data["results"][0]["chosen"], "не знаю")
        self.assertEqual(data["card_state"][0]["bad"], 1)
        self.assertIn("оценивает сам", db.learning_summary(uid)["note"])

    def test_agent_tools_are_scoped_and_paginated(self):
        uid, other = new_user(), new_user()
        q = {"question": "Секрет другого ученика", "options": ["да", "нет"], "answer": 0}
        db.record_quiz_answer(other, "other", 0, "чужое", q, 1)
        action = {"tool": "learning_results", "kind": "quiz", "user_id": other}
        self.assertEqual(llm.extract_json(run(agent._run_tool(uid, action)))["results"], [])
        q["question"] = "Свой вопрос"
        for index in range(7):
            db.record_quiz_answer(uid, "own", index, "своё", q, 1)
        data = llm.extract_json(run(agent._run_tool(uid, dict(action, limit=999))))
        self.assertEqual(len(data["results"]), 5)
        self.assertEqual(data["next_offset"], 5)
        self.assertEqual(data["total"], 7)

    def test_agent_can_read_results_without_documents(self):
        uid, client = new_user(), Rec()
        q = {"question": "Что такое митоз?", "options": ["Деление", "Питание"], "answer": 0}
        db.record_quiz_answer(uid, "quiz", 0, "биология", q, 1)
        steps = [
            '{"tool":"learning_summary","status":"смотрю успеваемость"}',
            '{"tool":"learning_results","kind":"quiz","mistakes_only":true,"status":"разбираю ошибки"}',
            '{"tool":"answer","status":"объясняю ошибку"}',
        ]
        with mock.patch.object(rag, "search", return_value=[]), \
                mock.patch.object(llm, "chat", mock.AsyncMock(side_effect=steps)), \
                mock.patch.object(agent, "_final", mock.AsyncMock(return_value="Повтори митоз")) as final:
            run(handlers.answer_question(client, uid, uid, "Где я ошибался в тестах?"))
        self.assertIn("Повтори митоз", client.text)
        transcript = final.call_args.args[0]
        self.assertIn("Что такое митоз?", transcript)
        self.assertIn("Питание", transcript)
        self.assertIn("Деление", transcript)


class AgentAndRagTest(unittest.TestCase):
    def test_resolve_title(self):
        names = ["Лекция_3.pdf", "Генетика.pdf"]
        resolve = agent._resolve_title
        self.assertEqual(resolve(0, "лекция 3", names), "Лекция_3.pdf")
        self.assertEqual(resolve(0, "ГЕНЕТИКА", names), "Генетика.pdf")
        self.assertEqual(resolve(0, "", names), "Лекция_3.pdf")
        self.assertIsNone(resolve(0, "физика", names))
        self.assertEqual(resolve(0, "лекция", ["Генетика.pdf"]), "Генетика.pdf")

    def test_unknown_title_lists_documents(self):
        uid = new_user()
        add_doc(uid, "клетка.txt")
        add_doc(uid, "митоз.txt", LECTURE_2)
        title, error = agent._title_or_error(uid, "физика")
        self.assertIsNone(title)
        self.assertIn("не найден", error)
        self.assertIn("клетка.txt", error)

    def test_doc_stats_and_title_search(self):
        uid = new_user()
        add_doc(uid, "клетка.txt")
        add_doc(uid, "митоз.txt", LECTURE_2)
        stats = agent._doc_stats(uid)
        self.assertEqual(list(stats), ["митоз.txt", "клетка.txt"])   # свежие первыми
        self.assertGreater(stats["клетка.txt"]["fragments"], 0)
        hits = rag.search(uid, "митохондрии и АТФ", k=4, title="митоз.txt")
        self.assertTrue(hits)
        self.assertEqual({h["title"] for h in hits}, {"митоз.txt"})

    def test_mock_embeddings_use_own_collection(self):
        self.assertEqual(rag._collection_name(5, True), "user_5_mock")
        self.assertEqual(rag._collection_name(5, False), "user_5")

    def test_legacy_mock_collection_is_kept(self):
        # база, собранная мок-эмбеддингами до обновления, не «пропадает» из поиска
        uid = new_user()
        collection = rag._chroma.get_or_create_collection(f"user_{uid}",
                                                          metadata={"hnsw:space": "cosine"})
        emb = rag.MockEmbedder().encode([f"passage: {LECTURE}"]).tolist()
        collection.add(ids=[f"legacy-{uid}"], documents=[LECTURE],
                       metadatas=[{"title": "старое.txt"}], embeddings=emb)
        rag._LEGACY_MOCK.pop(uid, None)
        self.assertEqual(rag._collection_name(uid), f"user_{uid}")
        self.assertEqual(rag.document_stats(uid)["старое.txt"]["fragments"], 1)
        self.assertTrue(rag.search(uid, "митохондрии", k=2))
        rag.clear_user(uid)
        self.assertEqual(rag._collection_name(uid), f"user_{uid}_mock")


# ---------------------------------------------------------------- вебхук, клиенты, конфиг

class FakeRequest:
    def __init__(self, headers=None, body=None):
        self.headers = headers or {}
        self.client = None
        self._body = body

    async def json(self):
        if isinstance(self._body, Exception):
            raise self._body
        return self._body


class WebhookTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        try:
            import main
        except ImportError as exc:   # нет fastapi/uvicorn/apscheduler
            raise unittest.SkipTest(f"main.py не импортируется: {exc}")
        cls.main = main

    def test_secret_is_required(self):
        auth = self.main.webhook_authorized
        with mock.patch.object(config, "MESSENGER", "max"):
            with mock.patch.object(config, "WEBHOOK_SECRET", ""):
                self.assertFalse(auth({"X-Max-Bot-Api-Secret": ""}))
            with mock.patch.object(config, "WEBHOOK_SECRET", "s3cret"):
                self.assertTrue(auth({"X-Max-Bot-Api-Secret": "s3cret"}))
                self.assertFalse(auth({"X-Max-Bot-Api-Secret": "wrong"}))
                self.assertFalse(auth({}))
        with mock.patch.object(config, "MESSENGER", "tg"), \
                mock.patch.object(config, "WEBHOOK_SECRET", "s3cret"):
            self.assertTrue(auth({"X-Telegram-Bot-Api-Secret-Token": "s3cret"}))

    def test_endpoint(self):
        calls = []
        good = {"X-Max-Bot-Api-Secret": "s3cret"}
        with mock.patch.object(config, "MESSENGER", "max"), \
                mock.patch.object(config, "WEBHOOK_SECRET", "s3cret"), \
                mock.patch.object(self.main.handlers, "spawn_update",
                                  lambda client, update: calls.append(update)):
            with mock.patch.object(config, "UPDATES_MODE", "polling"):
                self.assertEqual(run(self.main.webhook(FakeRequest(good, {}))).status_code, 404)
            with mock.patch.object(config, "UPDATES_MODE", "webhook"):
                self.assertEqual(run(self.main.webhook(FakeRequest({}, {"x": 1}))).status_code, 403)
                self.assertEqual(run(self.main.webhook(
                    FakeRequest(good, ValueError("bad")))).status_code, 400)
                self.assertEqual(run(self.main.webhook(FakeRequest(good, {"update_type": "x"}))),
                                 {"ok": True})
        self.assertEqual(calls, [{"update_type": "x"}])


class ClientsAndConfigTest(unittest.TestCase):
    def test_max_token_only_to_max_hosts(self):
        from bot.max_client import _token_allowed
        self.assertTrue(_token_allowed("https://i.oneme.ru/i?r=abc"))
        self.assertTrue(_token_allowed("https://fd.okcdn.ru/file"))
        self.assertTrue(_token_allowed("https://platform-api2.max.ru/uploads"))
        self.assertFalse(_token_allowed("http://max.ru/file"))
        self.assertFalse(_token_allowed("https://max.ru.evil.example/file"))
        self.assertFalse(_token_allowed("https://evil.example/?u=max.ru"))

    def test_tg_callback_payload_fits_64_bytes(self):
        from bot.tg_client import CALLBACK_LIMIT, encode_payload
        data = encode_payload({"action": "delete_file",
                               "title": "Очень длинное название лекции по молекулярной биологии.pdf"})
        self.assertLessEqual(len(data.encode("utf-8")), CALLBACK_LIMIT)

    def test_bad_int_env_does_not_crash(self):
        with mock.patch.dict(os.environ, {"TG_POLL_TIMEOUT": "abc"}):
            self.assertEqual(config._int("TG_POLL_TIMEOUT", 30), 30)
        with mock.patch.dict(os.environ, {"TG_POLL_TIMEOUT": " 45 "}):
            self.assertEqual(config._int("TG_POLL_TIMEOUT", 30), 45)

    def test_gateway_not_started_for_remote_llm(self):
        import gateway_manager
        with mock.patch.object(config, "LLM_MOCK", False), \
                mock.patch.object(config, "LLM_BASE_URL", "https://api.openai.com/v1"):
            self.assertTrue(gateway_manager.ensure({}).startswith("не нужен"))
        self.assertTrue(gateway_manager._alive(os.getpid()))


class FakePollingClient(Rec):
    """Транспорт без сети: одно сообщение, затем 401 (токен отозван)."""

    def __init__(self, uid):
        super().__init__()
        self.uid, self.polls, self.closed = uid, 0, False

    async def get_me(self):
        return {"username": "maximka_test_bot"}

    async def set_commands(self):
        pass

    async def get_updates(self, marker=None):
        from bot.tg_client import TgApiError
        self.polls += 1
        if self.polls == 1:
            return [{"kind": "message", "user_id": self.uid, "chat_id": self.uid,
                     "text": "/start", "attachments": []}], 6
        raise TgApiError("Unauthorized", 401)

    async def close(self):
        self.closed = True


class RuntimeTest(unittest.TestCase):
    def test_polling_saves_offset_and_stops_on_401(self):
        from bot.runtime import BotRuntime
        uid = new_user()
        client = FakePollingClient(uid)

        async def scenario():
            runtime = BotRuntime(client)
            await runtime.start()
            await asyncio.wait_for(runtime._task, 5)      # 401 → цикл завершился сам
            await asyncio.gather(*runtime._handlers, return_exceptions=True)
            await runtime.stop()
            return runtime

        runtime = run(scenario())
        self.assertEqual(runtime.username, "maximka_test_bot")
        self.assertEqual(runtime.marker_path.name, "tg_update_offset.txt")
        self.assertEqual(runtime.marker_path.read_text("utf-8"), "6")
        self.assertEqual(client.polls, 2)
        self.assertIn("МаксИИмка", client.text)
        self.assertTrue(client.closed)


if __name__ == "__main__":
    unittest.main()
