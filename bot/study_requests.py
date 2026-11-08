"""Параметры команд и выбора материала. Намерение в обычном чате определяет агент."""
import re

DEFAULT_COUNTS = {"quiz": 3, "cards": 5, "summary": 0}
MAX_COUNT = 20
NUMBERS = {"один": 1, "одну": 1, "одного": 1, "два": 2, "две": 2, "двух": 2,
           "три": 3, "трех": 3, "четыре": 4, "четырех": 4, "пять": 5, "пяти": 5,
           "шесть": 6, "семь": 7, "восемь": 8, "девять": 9, "десять": 10,
           "одиннадцать": 11, "двенадцать": 12, "тринадцать": 13, "четырнадцать": 14,
           "пятнадцать": 15, "шестнадцать": 16, "семнадцать": 17, "восемнадцать": 18,
           "девятнадцать": 19, "двадцать": 20, "тридцать": 30, "сорок": 40,
           "пятьдесят": 50, "сто": 100}
NUMBER = r"(?:\d+|" + "|".join(sorted(NUMBERS, key=len, reverse=True)) + r")"


def parse_options(kind, text, instructions=""):
    raw = (text or "").replace("ё", "е").replace("Ё", "Е").strip().strip(" .!?")
    count = DEFAULT_COUNTS[kind]
    explicit = False
    if kind != "summary":
        unit = r"вопрос(?:а|ов)?" if kind == "quiz" else r"карточ(?:ки|ек|ку|ка)"
        match = re.search(rf"\b(?:на\s+|из\s+)?({NUMBER})\s+{unit}\b", raw, re.I)
        if match is None:
            match = re.search(rf"\bна\s+({NUMBER})\s*$", raw, re.I)
        if match is None and re.match(r"^\d+\b", raw):
            match = re.match(r"(\d+)\b", raw)
        if match:
            explicit = True
            value = match.group(1).lower().replace("ё", "е")
            count = int(value) if value.isdigit() else NUMBERS[value]
            raw = (raw[:match.start()] + " " + raw[match.end():]).strip()
    topic = re.sub(r"^(?:по\s+теме|на\s+тему|по|про|о|об)\s+", "", raw, flags=re.I).strip()
    return {"kind": kind, "topic": topic, "count": count,
            "count_explicit": explicit, "instructions": (instructions or text or "")[:2000]}


def _stems(text):
    words = re.findall(r"[а-яёa-z0-9]+", text.lower().replace("ё", "е"))
    endings = r"(?:иями|ами|ями|ого|ему|ому|ией|иям|иях|ии|ия|ие|ую|ой|ый|ий|ая|ое|ы|и|а|я|е|у|ю)$"
    return {re.sub(endings, "", word) if len(word) > 4 else word for word in words}


def matching_titles(titles, topic):
    query = (topic or "").strip().strip('"«»').lower()
    exact = [title for title in titles if title.lower() == query]
    if exact:
        return exact
    partial = [title for title in titles if query and query in title.lower()]
    if partial:
        return partial
    stems = {s for s in _stems(query) if s.isalpha() and len(s) >= 3} - {
        "тест", "тем", "предмет", "вопрос", "карточк", "пожалуйста", "pdf", "pptx", "txt"}
    return [title for title in titles if stems & _stems(title)] if stems else []


def is_document_topic(title, topic):
    """Название предмета целиком, в отличие от отдельной темы внутри файла."""
    words = {s for s in _stems(topic) if s.isalpha() and len(s) >= 3}
    words -= {"тем", "предмет", "материал", "файл", "весь", "всему", "конспект"}
    return bool(words) and words <= _stems(title)
