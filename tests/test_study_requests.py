from tests import TEST_ROOT
import unittest
from bot import study_requests as requests


class StudyRequestParserTests(unittest.TestCase):
    def test_explicit_command_options_and_quantity(self):
        for text, kind, count in (
            ("по биологии на 4 вопроса", "quiz", 4),
            ("", "quiz", 3),
            ("по химии на четыре вопроса", "quiz", 4),
            ("7 по биологии", "cards", 7),
            ("на тему митоз на 6 карточек", "cards", 6),
            ("по теме ядро на 1 карточку", "cards", 1),
            ("по математике", "summary", 0),
        ):
            with self.subTest(text=text):
                spec = requests.parse_options(kind, text)
                self.assertIsNotNone(spec)
                self.assertEqual((spec["kind"], spec["count"]), (kind, count))
        self.assertEqual(requests.parse_options("quiz", "по биологии на 4 вопроса")["topic"], "биологии")

    def test_natural_language_is_not_a_fixed_command(self):
        from bot.handlers import parse_command
        for text in ("Тест Тьюринга — что это?", "Как составить тест?", "Не создавай тест",
                     "Объясни, как сделать карточки", "Почему биология важна?", "Давай проверим мои знания"):
            self.assertEqual(parse_command(text), ("", ""), text)

    def test_subject_inflections_and_ambiguous_subjects(self):
        titles = ["Биология.pdf", "Математика.pptx", "Биология 2.pdf"]
        self.assertEqual(requests.matching_titles(titles, "биологии"), [titles[0], titles[2]])
        self.assertEqual(requests.matching_titles(titles, "математике"), [titles[1]])
