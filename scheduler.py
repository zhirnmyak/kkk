"""Ежедневные напоминания: карточки к повторению и задачи плана."""
import asyncio, logging
from datetime import date
from apscheduler.schedulers.asyncio import AsyncIOScheduler
import config, db
from bot import create_client

log = logging.getLogger("neurotutor.scheduler")
sched = AsyncIOScheduler()


def start():
    sched.start()
    sched.add_job(daily_reminder, "cron", hour=config.REMINDER_HOUR, minute=0)
    log.info("Планировщик запущен, напоминания в %02d:00", config.REMINDER_HOUR)


async def daily_reminder():
    client = create_client()
    today = date.today().isoformat()
    try:
        for user_id, chat_id in db.all_users():
            # консольный «пользователь» живёт только в этом окне; отправлять ему
            # напоминание в MAX/Telegram нельзя — там такого адресата нет
            if user_id == config.CONSOLE_UID:
                continue
            try:
                lines = []
                due = db.due_cards(user_id)
                if due:
                    lines.append(f"🃏 Сегодня на повторении {len(due)} карточек. Напиши /повторение")
                tasks = db.plan_tasks_for(user_id, today)
                if tasks:
                    lines.append("📆 План на сегодня:\n" + "\n".join(f"• {t}" for t in tasks))
                if lines:
                    await client.send_text(chat_id, "\n\n".join(lines))
            except Exception:
                # пропущенный юзер не должен обрывать рассылку остальным
                log.exception("Не удалось отправить напоминание user %s", user_id)
            await asyncio.sleep(1)  # не упираемся в лимит мессенджера (TG: ~30 сообщ/сек)
    finally:
        if hasattr(client, "close"):
            await client.close()
