"""SQLite: состояния диалогов, документы, карточки (Лейтнер), планы, статистика."""
import json, sqlite3
from datetime import date, timedelta
import config

_conn = sqlite3.connect(config.DB_PATH, check_same_thread=False)
_conn.row_factory = sqlite3.Row
_conn.executescript("""
CREATE TABLE IF NOT EXISTS users(
    user_id INTEGER PRIMARY KEY, chat_id INTEGER,
    state TEXT DEFAULT 'idle', state_data TEXT DEFAULT '{}');
CREATE TABLE IF NOT EXISTS documents(
    id INTEGER PRIMARY KEY AUTOINCREMENT, user_id INTEGER,
    title TEXT, created_at TEXT DEFAULT (datetime('now')));
CREATE TABLE IF NOT EXISTS cards(
    id INTEGER PRIMARY KEY AUTOINCREMENT, user_id INTEGER,
    front TEXT, back TEXT, box INTEGER DEFAULT 1, due TEXT,
    good INTEGER DEFAULT 0, bad INTEGER DEFAULT 0);
CREATE TABLE IF NOT EXISTS plans(
    user_id INTEGER PRIMARY KEY, exam_date TEXT, schedule TEXT);
CREATE TABLE IF NOT EXISTS stats(
    user_id INTEGER, key TEXT, value INTEGER DEFAULT 0,
    PRIMARY KEY (user_id, key));
CREATE TABLE IF NOT EXISTS summaries(
    user_id INTEGER, title TEXT, text TEXT, version INTEGER DEFAULT 0,
    updated_at TEXT DEFAULT (datetime('now')),
    PRIMARY KEY (user_id, title));
CREATE TABLE IF NOT EXISTS tocs(
    user_id INTEGER, title TEXT, entries TEXT, version INTEGER DEFAULT 0,
    updated_at TEXT DEFAULT (datetime('now')),
    PRIMARY KEY (user_id, title));
CREATE TABLE IF NOT EXISTS dialog(
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER, question TEXT, answer TEXT,
    created_at TEXT DEFAULT (datetime('now')));
CREATE TABLE IF NOT EXISTS learning_events(
    id INTEGER PRIMARY KEY AUTOINCREMENT, user_id INTEGER NOT NULL,
    kind TEXT NOT NULL, session_id TEXT NOT NULL, item_id TEXT NOT NULL,
    title TEXT DEFAULT '', prompt TEXT, expected TEXT, chosen TEXT,
    correct INTEGER NOT NULL, created_at TEXT DEFAULT (datetime('now')),
    UNIQUE(user_id,kind,session_id,item_id));
CREATE INDEX IF NOT EXISTS learning_events_user ON learning_events(user_id,id);
CREATE TABLE IF NOT EXISTS quiz_runs(
    session_id TEXT NOT NULL, user_id INTEGER NOT NULL, title TEXT NOT NULL,
    questions TEXT NOT NULL, completed INTEGER DEFAULT 0,
    created_at TEXT DEFAULT (datetime('now')), PRIMARY KEY(user_id,session_id));
CREATE INDEX IF NOT EXISTS quiz_runs_user_title ON quiz_runs(user_id,title);
""")
if "title" not in {r["name"] for r in _conn.execute("PRAGMA table_info(cards)")}:
    _conn.execute("ALTER TABLE cards ADD COLUMN title TEXT DEFAULT ''")
_summary_cols = {row["name"] for row in _conn.execute("PRAGMA table_info(summaries)")}
if "version" not in _summary_cols:
    _conn.execute("ALTER TABLE summaries ADD COLUMN version INTEGER DEFAULT 0")
_conn.commit()

SUMMARY_VERSION = 2  # старые сводки без ссылок на страницы перегенерируются
TOC_VERSION = 1


def _run(sql, params=()):
    cur = _conn.execute(sql, params)
    _conn.commit()
    return cur


# ---------- пользователи и состояния ----------
def touch_user(user_id, chat_id):
    _run("INSERT INTO users(user_id, chat_id) VALUES(?,?) "
         "ON CONFLICT(user_id) DO UPDATE SET chat_id=excluded.chat_id", (user_id, chat_id))


def get_state(user_id):
    row = _run("SELECT state, state_data FROM users WHERE user_id=?", (user_id,)).fetchone()
    if not row:
        return "idle", {}
    try:
        return row["state"], json.loads(row["state_data"])
    except json.JSONDecodeError:
        return row["state"], {}


def set_state(user_id, state, sdata=None):
    _run("INSERT INTO users(user_id, state, state_data) VALUES(?,?,?) "
         "ON CONFLICT(user_id) DO UPDATE SET state=excluded.state, state_data=excluded.state_data",
         (user_id, state, json.dumps(sdata or {}, ensure_ascii=False)))


def all_users():
    return [(r["user_id"], r["chat_id"])
            for r in _run("SELECT user_id, chat_id FROM users WHERE chat_id IS NOT NULL").fetchall()]


# ---------- документы ----------
def add_document_row(user_id, title):
    _run("INSERT INTO documents(user_id, title) VALUES(?,?)", (user_id, title))


def has_document(user_id, title) -> bool:
    return _run("SELECT 1 FROM documents WHERE user_id=? AND title=? LIMIT 1",
                (user_id, title)).fetchone() is not None


def document_id(user_id, title):
    row = _run("SELECT id FROM documents WHERE user_id=? AND title=? ORDER BY id DESC LIMIT 1",
               (user_id, title)).fetchone()
    return row[0] if row else None


def ensure_document_row(user_id, title) -> bool:
    """Строка в documents без дублей (страницы фото-конспекта и части длинной
    вставки дописываются в один материал). True — документ новый."""
    if has_document(user_id, title):
        return False
    add_document_row(user_id, title)
    return True


def list_documents(user_id):
    """[(заголовок, дата загрузки)] — свежие первыми."""
    return [(r["title"], r["created_at"]) for r in
            _run("SELECT title, created_at FROM documents WHERE user_id=? ORDER BY id DESC",
                 (user_id,)).fetchall()]


def web_documents(user_id):
    return [dict(row) for row in _run(
        "SELECT MAX(id) AS id, title, MAX(created_at) AS created_at FROM documents "
        "WHERE user_id=? GROUP BY title ORDER BY id DESC", (user_id,)).fetchall()]


def web_document(document_id, user_id):
    row = _run("SELECT id, title, created_at FROM documents WHERE id=? AND user_id=?",
               (document_id, user_id)).fetchone()
    return dict(row) if row else None


def document_owners():
    return [dict(row) for row in _run(
        "SELECT user_id, COUNT(DISTINCT title) AS files FROM documents "
        "GROUP BY user_id ORDER BY files DESC, user_id").fetchall()]


def remove_document_rows(user_id, title):
    remove_learning(user_id, title)
    _run("DELETE FROM documents WHERE user_id=? AND title=?", (user_id, title))
    _run("DELETE FROM summaries WHERE user_id=? AND title=?", (user_id, title))
    _run("DELETE FROM tocs WHERE user_id=? AND title=?", (user_id, title))


def clear_documents(user_id):
    remove_learning(user_id)
    _run("DELETE FROM documents WHERE user_id=?", (user_id,))
    _run("DELETE FROM summaries WHERE user_id=?", (user_id,))
    _run("DELETE FROM tocs WHERE user_id=?", (user_id,))


def remove_learning(user_id, title=None):
    """Удалить только связанные с материалом задания; без title — все задания."""
    suffix = " AND title=?" if title is not None else ""
    args = (user_id, title) if title is not None else (user_id,)
    cards = _run("SELECT COUNT(*) FROM cards WHERE user_id=?" + suffix, args).fetchone()[0]
    quizzes = _run("SELECT COUNT(*) FROM quiz_runs WHERE user_id=? AND completed=1" + suffix, args).fetchone()[0]
    with _conn:
        for table in ("cards", "learning_events", "quiz_runs"):
            _conn.execute(f"DELETE FROM {table} WHERE user_id=?" + suffix, args)
        _conn.execute("UPDATE stats SET value=MAX(0,value-?) WHERE user_id=? AND key='cards_created'", (cards, user_id))
        _conn.execute("UPDATE stats SET value=MAX(0,value-?) WHERE user_id=? AND key='quizzes'", (quizzes, user_id))
        if title is None:
            _conn.execute("DELETE FROM stats WHERE user_id=? AND key IN ('cards_created','quizzes')", (user_id,))
    state, data = get_state(user_id)
    if state == "quiz" and (title is None or data.get("title") == title):
        set_state(user_id, "idle")
    elif state == "card_review":
        remaining = [cid for cid in (data.get("queue") or [])[data.get("idx", 0):] if get_card(cid, user_id)]
        if not remaining:
            set_state(user_id, "idle")
        # Текущая очередь хранит прежние индексы, send_card пропустит удалённые.


def create_quiz_run(user_id, session_id, title, questions):
    _run("INSERT OR IGNORE INTO quiz_runs(session_id,user_id,title,questions) VALUES(?,?,?,?)",
         (session_id, user_id, title, json.dumps(questions, ensure_ascii=False)))


def finish_quiz_run(user_id, session_id):
    with _conn:
        changed = _conn.execute("UPDATE quiz_runs SET completed=1 WHERE session_id=? AND user_id=? AND completed=0",
                                (session_id, user_id)).rowcount
        if changed:
            _conn.execute("INSERT INTO stats(user_id,key,value) VALUES(?,'quizzes',1) "
                          "ON CONFLICT(user_id,key) DO UPDATE SET value=value+1", (user_id,))


def invalidate_summary(user_id, title):
    """Документ дополнили (ещё одно фото, следующая часть текста) — кэш сводки и
    оглавления больше не описывает его целиком; пересоберутся при первом вопросе."""
    _run("DELETE FROM summaries WHERE user_id=? AND title=?", (user_id, title))
    _run("DELETE FROM tocs WHERE user_id=? AND title=?", (user_id, title))


def get_summary(user_id, title):
    row = _run("SELECT text FROM summaries WHERE user_id=? AND title=? AND version>=?",
               (user_id, title, SUMMARY_VERSION)).fetchone()
    return row["text"] if row else None


def set_summary(user_id, title, text):
    _run("INSERT INTO summaries(user_id, title, text, version, updated_at) "
         "VALUES(?,?,?,?,datetime('now')) "
         "ON CONFLICT(user_id, title) DO UPDATE SET text=excluded.text, "
         "version=excluded.version, updated_at=datetime('now')",
         (user_id, title, text, SUMMARY_VERSION))


def get_toc(user_id, title):
    """Список пунктов оглавления [{title, from, to, note}] или None."""
    row = _run("SELECT entries FROM tocs WHERE user_id=? AND title=? AND version>=?",
               (user_id, title, TOC_VERSION)).fetchone()
    if not row:
        return None
    try:
        data = json.loads(row["entries"])
    except (TypeError, json.JSONDecodeError):
        return None
    return data if isinstance(data, list) else None


def set_toc(user_id, title, entries):
    _run("INSERT INTO tocs(user_id, title, entries, version, updated_at) "
         "VALUES(?,?,?,?,datetime('now')) "
         "ON CONFLICT(user_id, title) DO UPDATE SET entries=excluded.entries, "
         "version=excluded.version, updated_at=datetime('now')",
         (user_id, title, json.dumps(entries, ensure_ascii=False), TOC_VERSION))


def last_document_title(user_id):
    row = _run("SELECT title FROM documents WHERE user_id=? ORDER BY id DESC LIMIT 1",
               (user_id,)).fetchone()
    return row["title"] if row else None


# ---------- история диалога ----------
def add_exchange(user_id, question, answer):
    _run("INSERT INTO dialog(user_id, question, answer) VALUES(?,?,?)",
         (user_id, question, answer))


def recent_exchanges(user_id, limit=3):
    """Последние пары вопрос–ответ, старые первыми — для уточнений вида «подробнее»."""
    rows = _run("SELECT question, answer FROM dialog WHERE user_id=? ORDER BY id DESC LIMIT ?",
                (user_id, limit)).fetchall()
    return [{"question": r["question"], "answer": r["answer"]} for r in reversed(rows)]


def clear_dialog(user_id):
    _run("DELETE FROM dialog WHERE user_id=?", (user_id,))


# ---------- карточки: алгоритм Лейтнера ----------
def add_cards(user_id, cards, title=""):
    today = date.today().isoformat()
    for c in cards:
        _run("INSERT INTO cards(user_id, front, back, box, due, title) VALUES(?,?,?,1,?,?)",
             (user_id, c["front"], c["back"], today, title))


def get_card(card_id, user_id=None):
    """Карточка по id; с user_id — только если она принадлежит этому ученику
    (кнопка из чужого/старого сообщения не должна трогать чужие карточки)."""
    if user_id is None:
        return _run("SELECT * FROM cards WHERE id=?", (card_id,)).fetchone()
    return _run("SELECT * FROM cards WHERE id=? AND user_id=?", (card_id, user_id)).fetchone()


def due_cards(user_id):
    return _run("SELECT * FROM cards WHERE user_id=? AND due<=? ORDER BY due",
                (user_id, date.today().isoformat())).fetchall()


def review_card(card_id, good: bool, session_id=None):
    """Знаю → уровень +1 (интервал растёт), не знаю → сброс на уровень 1."""
    card = get_card(card_id)
    if not card:
        return
    import uuid
    session_id = session_id or uuid.uuid4().hex
    box = min(card["box"] + 1, len(config.LEITNER_INTERVALS)) if good else 1
    days = config.LEITNER_INTERVALS[box - 1]
    due = (date.today() + timedelta(days=days)).isoformat()
    with _conn:
        inserted = _conn.execute(
            "INSERT OR IGNORE INTO learning_events(user_id,kind,session_id,item_id,title,prompt,expected,chosen,correct) VALUES(?,?,?,?,?,?,?,?,?)",
            (card["user_id"], "card", session_id, str(card_id), card["title"], card["front"],
             card["back"], "знаю" if good else "не знаю", int(good))).rowcount
        if inserted:
            _conn.execute("UPDATE cards SET box=?, due=?, good=good+?, bad=bad+? WHERE id=?",
                          (box, due, int(good), int(not good), card_id))


def record_quiz_answer(user_id, session_id, index, title, question, chosen):
    correct = chosen == question["answer"]
    _run("INSERT OR IGNORE INTO learning_events(user_id,kind,session_id,item_id,title,prompt,expected,chosen,correct) VALUES(?,?,?,?,?,?,?,?,?)",
         (user_id, "quiz", session_id, str(index), title, question["question"],
          question["options"][question["answer"]], question["options"][chosen], int(correct)))


def has_learning_data(user_id):
    return bool(_run("SELECT 1 FROM learning_events WHERE user_id=? LIMIT 1", (user_id,)).fetchone()
                or _run("SELECT 1 FROM cards WHERE user_id=? LIMIT 1", (user_id,)).fetchone()
                or get_stats(user_id).get("quizzes"))


def learning_summary(user_id):
    events = [dict(row) for row in _run(
        "SELECT kind,COUNT(*) answers,SUM(correct) correct,COUNT(DISTINCT session_id) sessions FROM learning_events WHERE user_id=? GROUP BY kind", (user_id,)).fetchall()]
    return {"history": events, "cards_total_good_bad": card_totals(user_id),
            "completed_quizzes": get_stats(user_id).get("quizzes", 0),
            "note": "Подробная история ведётся с обновления. Старые тесты доступны только счётчиком. Карточки оценивает сам ученик: знаю/не знаю, это не проверка знаний моделью."}


def learning_results(user_id, kind="quiz", limit=10, offset=0, mistakes_only=False, title=""):
    limit, offset = max(1, min(int(limit), 20)), max(0, min(int(offset), 5000))
    where, args = "user_id=? AND kind=?", [user_id, kind]
    if mistakes_only:
        where += " AND correct=0"
    if title:
        where += " AND title=?"
        args.append(title)
    total = _run(f"SELECT COUNT(*) FROM learning_events WHERE {where}", args).fetchone()[0]
    rows = [dict(r) for r in _run(
        f"SELECT id,title,prompt,expected,chosen,correct,created_at,session_id FROM learning_events WHERE {where} ORDER BY id DESC LIMIT ? OFFSET ?",
        (*args, limit, offset)).fetchall()]
    # Ограничиваем размер контекста даже для старых карточек с длинными ответами.
    for row in rows:
        for key in ("prompt", "expected", "chosen", "title"):
            row[key] = (row[key] or "")[:400]
    result = {"kind": kind, "total": total, "offset": offset, "results": rows,
              "next_offset": offset + limit if offset + limit < total else None}
    if kind == "card":
        card_where, card_args = "user_id=?", [user_id]
        if title:
            card_where += " AND title=?"
            card_args.append(title)
        if mistakes_only:
            card_where += " AND bad>0"
        card_total = _run(f"SELECT COUNT(*) FROM cards WHERE {card_where}", card_args).fetchone()[0]
        result["card_total"] = card_total
        result["next_card_offset"] = offset + limit if offset + limit < card_total else None
        result["card_state"] = [dict(r) for r in _run(
            f"SELECT id,substr(front,1,400) front,substr(back,1,400) back,substr(title,1,200) title,box,due,good,bad FROM cards WHERE {card_where} ORDER BY bad DESC,id DESC LIMIT ? OFFSET ?",
            (*card_args, limit, offset)).fetchall()]
        result["note"] = "card_state включает старые карточки; даты прошлых повторений для них не восстанавливаются."
    return result


def card_totals(user_id):
    """(всего карточек, верных ответов, ошибок). Считаются из самих карточек:
    review_card пишет именно в columns good/bad таблицы cards, а не в stats."""
    row = _run("SELECT COUNT(*) n, COALESCE(SUM(good),0) g, COALESCE(SUM(bad),0) b "
               "FROM cards WHERE user_id=?", (user_id,)).fetchone()
    return row["n"], row["g"], row["b"]


# ---------- план подготовки ----------
def set_plan(user_id, exam_date, days):
    _run("INSERT INTO plans(user_id, exam_date, schedule) VALUES(?,?,?) "
         "ON CONFLICT(user_id) DO UPDATE SET exam_date=excluded.exam_date, "
         "schedule=excluded.schedule", (user_id, exam_date, json.dumps(days, ensure_ascii=False)))


def plan_tasks_for(user_id, iso_day):
    row = _run("SELECT schedule FROM plans WHERE user_id=?", (user_id,)).fetchone()
    if not row:
        return []
    try:
        days = json.loads(row["schedule"])
    except json.JSONDecodeError:
        return []
    for d in days if isinstance(days, list) else []:
        if isinstance(d, dict) and d.get("date") == iso_day:
            tasks = d.get("tasks") or []
            return [str(t) for t in tasks] if isinstance(tasks, list) else [str(tasks)]
    return []


def get_plan(user_id):
    return _run("SELECT * FROM plans WHERE user_id=?", (user_id,)).fetchone()


# ---------- статистика ----------
def bump_stat(user_id, key, delta=1):
    _run("INSERT INTO stats(user_id, key, value) VALUES(?,?,?) "
         "ON CONFLICT(user_id, key) DO UPDATE SET value = value + ?",
         (user_id, key, delta, delta))


def get_stats(user_id):
    return {r["key"]: r["value"] for r in
            _run("SELECT key, value FROM stats WHERE user_id=?", (user_id,)).fetchall()}
