"""Обёртка над LLM. Работает с любым OpenAI-совместимым API —
при смене провайдера меняется только .env."""
import contextvars, json, os, re, logging, time
import config
import quota
import reasoning_support
from datetime import date, timedelta

log = logging.getLogger("neurotutor.llm")

# Шлюз отдаёт лимит как обычный текст ответа. Это не материал для конспекта:
# такую строку нужно превратить в понятную ошибку, а не сохранять в базу.
# Маркеры и правила живут в quota.py (одни на весь проект).
QUOTA_MARKERS = quota.QUOTA_MARKERS


def _raise_if_quota(text: str) -> None:
    # проверяем только короткое служебное сообщение: обычный ответ про «rate limit»
    # или JSON шага агента с таким запросом раньше выбрасывался как «дневной лимит»
    if quota.is_quota_text(text):
        raise RuntimeError(
            "Модель вернула дневной лимит вместо ответа — смени модель в /настройки "
            f"или попробуй позже. Ответ модели: {text[:180]!r}")


# Клиент создаём один раз и только когда он нужен (раньше — дважды подряд)
if config.LLM_MOCK:
    client = None
else:
    from openai import AsyncOpenAI
    client = AsyncOpenAI(base_url=config.LLM_BASE_URL, api_key=config.LLM_API_KEY or "empty")
# ContextVar, а не глобальная переменная: в одном процессе с консолью работает
# MAX-бот, и его размышления/статистика не должны печататься в консоль.
_reason_sink = contextvars.ContextVar("reason_sink", default=None)
_stats_sink = contextvars.ContextVar("stats_sink", default=None)


def set_reasoning_sink(fn) -> None:
    _reason_sink.set(fn)


def set_stats_sink(fn) -> None:
    _stats_sink.set(fn)


def _reason_text(delta) -> str:
    value = getattr(delta, "reasoning_content", None)
    if value is None and getattr(delta, "model_extra", None):
        value = delta.model_extra.get("reasoning_content")
    return value or ""


def _usage_field(usage, name, default=None):
    if usage is None:
        return default
    value = getattr(usage, name, None)
    if value is None and getattr(usage, "model_extra", None):
        value = usage.model_extra.get(name)
    return default if value is None else value


def _report(usage, model, effort, started, text) -> None:
    sink = _stats_sink.get()
    if not sink:
        return
    sink({
        "model": model,
        "effort": effort or "auto",
        "seconds": round(time.monotonic() - started, 2),
        "prompt_tokens": _usage_field(usage, "prompt_tokens", 0),
        "completion_tokens": _usage_field(usage, "completion_tokens", 0),
        "estimated": bool(_usage_field(usage, "estimated", True)),
        "credits": _usage_field(usage, "credits"),
    })


async def chat(system: str, user: str, temperature: float = 0.3, max_tokens: int = 1500,
               effort: str | None = None) -> str:
    if config.LLM_MOCK:
        if '"tool":"answer","answer"' in system:
            return json.dumps({"tool": "answer", "answer": _mock_chat(user)}, ensure_ascii=False)
        if "На каждом шаге возвращай СТРОГО один JSON" in system:
            return '{"tool":"answer","status":"готовлю ответ"}'
        return _mock_chat(user)
    base_url, api_key, request_model, provider = (
        config.LLM_BASE_URL, config.LLM_API_KEY or "", config.LLM_MODEL, config.LLM_PROVIDER)
    request_client = client.with_options(base_url=base_url, api_key=api_key or "empty")
    # effort читаем из окружения на каждый вызов: консоль меняет его на лету;
    # явный аргумент нужен служебным вызовам (сводка), которым рассуждения не нужны
    if effort is None:
        effort = (os.getenv("LLM_REASONING_EFFORT") or "").strip().lower()
    else:
        effort = effort.strip().lower()
    kwargs = dict(
        model=request_model,
        messages=[{"role": "system", "content": system},
                  {"role": "user", "content": user}],
        temperature=temperature,
        max_tokens=max_tokens,
    )
    extra = {}
    support = reasoning_support.get(base_url, api_key, request_model)
    if support and support["supported"] is False:
        effort = None
    elif support and support["efforts"] and effort not in ("", "auto", *support["efforts"]):
        effort = None
    if support and effort == "auto":
        effort = None  # «auto» в меню означает настройки сервера по умолчанию.
    if effort and (provider == "qoder" or effort != "auto"):
        extra["reasoning_effort"] = effort
    ctx = (os.getenv("LLM_CONTEXT_WINDOW") or "").strip()
    if ctx.isdigit() and provider == "qoder":
        extra["context_window"] = int(ctx)
    if extra:
        kwargs["extra_body"] = extra

    started = time.monotonic()

    async def create_completion(**options):
        nonlocal effort
        from openai import BadRequestError
        try:
            return await request_client.chat.completions.create(**kwargs, **options)
        except BadRequestError as exc:
            if ("reasoning_effort" not in kwargs.get("extra_body", {})
                    or not reasoning_support.rejected(str(exc))):
                raise
            reasoning_support.remember(base_url, api_key, request_model, False)
            # Совместимые шлюзы поддерживают не все параметры OpenAI.
            # Повторяем только явный отказ от необязательного параметра;
            # ошибки авторизации, лимитов и прочих аргументов не скрываем.
            extra_body = dict(kwargs["extra_body"])
            extra_body.pop("reasoning_effort")
            if extra_body:
                kwargs["extra_body"] = extra_body
            else:
                kwargs.pop("extra_body")
            effort = None
            log.warning("API не поддерживает reasoning_effort — повторяю запрос без него")
            return await request_client.chat.completions.create(**kwargs, **options)

    reason_sink = _reason_sink.get()
    if reason_sink is None:
        resp = await create_completion()
        text = resp.choices[0].message.content or ""
        _raise_if_quota(text)
        _report(resp.usage, resp.model, effort, started, text)
        return text

    # размышления приходят только потоком (блоком, не по токенам)
    parts, usage, model = [], None, request_model
    odd_count, odd_sample = 0, ""
    stream = await create_completion(stream=True)
    async for chunk in stream:
        if getattr(chunk, "model", None):
            model = chunk.model
        if getattr(chunk, "usage", None):
            usage = chunk.usage
        if not chunk.choices:
            continue
        delta = chunk.choices[0].delta
        reason = _reason_text(delta)
        if reason:
            reason_sink(reason)
        content = getattr(delta, "content", None)
        if content:
            # потоковый эндпоинт изредка отдаёт кусок не текстом, а числом.
            # str() вставил бы цифру внутрь слова, поэтому такой кусок не считаем
            # текстом и решаем по итогам: если набралось пусто — переспросим без потока
            if isinstance(content, str):
                parts.append(content)
            else:
                odd_count += 1
                odd_sample = odd_sample or repr(content)
    text = "".join(parts)
    if odd_count:
        if text.strip():
            log.warning("провайдер прислал %d не-текстовых кусков (первый: %s) — пропущены",
                        odd_count, odd_sample)
        else:
            log.warning("поток дал только не-текст (%s) — повторяю запрос без стрима", odd_sample)
            resp = await create_completion()
            text = resp.choices[0].message.content or ""
    _raise_if_quota(text)
    _report(usage, model, effort, started, text)
    return text


async def chat_json(system: str, user: str, temperature: float = 0.2, max_tokens: int = 2000):
    """LLM-вызов с гарантией возврата JSON (dict или list — проверяет вызывающий)."""
    if config.LLM_MOCK:
        return _mock_json(system)
    text = await chat(system + "\nВерни ТОЛЬКО валидный JSON, без пояснений и markdown-обёрток.",
                      user, temperature, max_tokens)
    return extract_json(text)


def _mock_chat(user: str) -> str:
    q = user.rsplit("Вопрос:", 1)[-1].strip() if "Вопрос:" in user else user[:200]
    return (f"🔧 [MOCK] Отвечаю по загруженным материалам.\n\n"
            f"Вопрос был: «{q}»\nЗдесь будет настоящий ответ LLM.\n\nИсточники: [1]")


def _mock_json(system: str):
    low = system.lower()
    number = re.search(r"ровно\s+(\d+)", low)
    if "карточк" in low:   # CARDS_SYSTEM
        return {"cards": [{"front": f"Термин {i}: дай определение",
                           "back": f"Определение термина {i} из материала."} for i in range(1, (int(number[1]) if number else 5) + 1)]}
    if "тест" in low:      # QUIZ_SYSTEM
        return {"questions": [{"question": f"Вопрос {i}: что верно?",
                               "options": ["А", "Б", "В", "Г"],
                               "answer": i % 4,
                               "explanation": "См. материал, фрагмент [1]."} for i in range(1, (int(number[1]) if number else 3) + 1)]}
    start = date.today()   # PLAN_SYSTEM
    return {"days": [{"date": (start + timedelta(days=i)).isoformat(),
                      "tasks": [f"Блок {i+1}: тема", "5 задач"]} for i in range(3)]}


def extract_json(text: str):
    text = text.strip()
    fence = re.search(r"```(?:json)?(.*?)```", text, re.S)
    if fence:
        text = fence.group(1).strip()
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass
    for pattern in (r"\{.*\}", r"\[.*\]"):
        m = re.search(pattern, text, re.S)
        if m:
            try:
                return json.loads(m.group(0))
            except json.JSONDecodeError:
                continue
    raise ValueError(f"LLM не вернул валидный JSON: {text[:200]!r}")
