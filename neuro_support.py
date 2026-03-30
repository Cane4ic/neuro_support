import logging
import os
import time
from collections.abc import Callable
from datetime import datetime, timezone
from typing import Any, Optional

from postgrest.exceptions import APIError
from pydantic import ValidationError
from supabase import Client, create_client
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update, User
from telegram.error import Conflict
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)


BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()
AGENT_IDS_RAW = os.getenv("AGENT_IDS", "").strip()
SUPABASE_URL = os.getenv("SUPABASE_URL", "").strip()
SUPABASE_SERVICE_ROLE_KEY = os.getenv("SUPABASE_SERVICE_ROLE_KEY", "").strip()

_supabase: Optional[Client] = None


def parse_agent_ids(raw: str) -> set[int]:
    result: set[int] = set()
    for chunk in raw.split(","):
        item = chunk.strip()
        if not item:
            continue
        try:
            result.add(int(item))
        except ValueError:
            logging.warning("Некорректный AGENT_ID пропущен: %s", item)
    return result


AGENT_IDS = parse_agent_ids(AGENT_IDS_RAW)


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def get_supabase() -> Client:
    global _supabase
    if _supabase is not None:
        return _supabase
    if not SUPABASE_URL:
        raise RuntimeError("Не задан SUPABASE_URL (https://xxxx.supabase.co)")
    if not SUPABASE_SERVICE_ROLE_KEY:
        raise RuntimeError(
            "Не задан SUPABASE_SERVICE_ROLE_KEY. "
            "Возьмите service_role в Supabase → Project Settings → API (только для сервера, не публикуйте)."
        )
    _supabase = create_client(SUPABASE_URL, SUPABASE_SERVICE_ROLE_KEY)
    return _supabase


def _ts_now() -> str:
    return utc_now_iso()


def _is_transient_supabase_failure(exc: BaseException) -> bool:
    """502/503 от API, HTML вместо JSON и т.п."""
    if isinstance(exc, ValidationError):
        es = str(exc).lower()
        if "json" in es or "invalid" in es:
            return True
    msg = str(exc).lower()
    if "502" in msg or "503" in msg or "504" in msg:
        return True
    if "bad gateway" in msg or "service unavailable" in msg or "gateway time" in msg:
        return True
    if isinstance(exc, APIError):
        code = (exc.code or "") or ""
        if code in ("502", "503", "504", "PGRST301", "PGRST302"):
            return True
    return False


def supabase_execute(build: Callable[[], Any], *, retries: int = 4) -> Any:
    """
    postgrest при 502 иногда отдаёт HTML — падает парсинг JSON.
    Повторяем запрос с backoff (кратковременные сбои Supabase/CDN).
    """
    last: Optional[BaseException] = None
    for attempt in range(retries):
        try:
            return build().execute()
        except Exception as exc:
            last = exc
            if attempt < retries - 1 and _is_transient_supabase_failure(exc):
                delay = 0.35 * (2**attempt)
                logging.warning(
                    "Supabase: временная ошибка, повтор через %.1fs (%s/%s): %s",
                    delay,
                    attempt + 1,
                    retries,
                    exc,
                )
                time.sleep(delay)
                continue
            raise
    assert last is not None
    raise last


def init_db() -> None:
    sb = get_supabase()
    try:
        supabase_execute(lambda: sb.table("tickets").select("id").limit(1))
    except Exception as exc:
        raise RuntimeError(
            "Не удаётся прочитать таблицу tickets. Выполните SQL из supabase/schema.sql "
            "в Supabase → SQL Editor."
        ) from exc


def get_open_ticket_for_user(user_id: int) -> Optional[dict[str, Any]]:
    sb = get_supabase()
    res = supabase_execute(
        lambda: sb.table("tickets")
        .select("*")
        .eq("user_id", user_id)
        .in_("status", ["pending", "active"])
        .order("id", desc=True)
        .limit(1)
    )
    rows = res.data or []
    return rows[0] if rows else None


def get_active_ticket_for_agent(agent_id: int) -> Optional[dict[str, Any]]:
    sb = get_supabase()
    res = supabase_execute(
        lambda: sb.table("tickets")
        .select("*")
        .eq("assigned_agent_id", agent_id)
        .eq("status", "active")
        .order("id", desc=True)
        .limit(1)
    )
    rows = res.data or []
    return rows[0] if rows else None


def create_ticket(user_id: int) -> int:
    now = _ts_now()
    sb = get_supabase()
    res = supabase_execute(
        lambda: sb.table("tickets").insert(
            {
                "user_id": user_id,
                "status": "pending",
                "assigned_agent_id": None,
                "created_at": now,
                "updated_at": now,
                "closed_at": None,
            }
        )
    )
    rows = res.data or []
    if not rows:
        raise RuntimeError("Не удалось создать тикет")
    return int(rows[0]["id"])


def set_ticket_status(ticket_id: int, status: str, agent_id: Optional[int] = None, close: bool = False) -> None:
    now = _ts_now()
    sb = get_supabase()
    payload: dict[str, Any] = {"status": status, "assigned_agent_id": agent_id, "updated_at": now}
    if close:
        payload["closed_at"] = now
    supabase_execute(lambda: sb.table("tickets").update(payload).eq("id", ticket_id))


def try_accept_ticket(ticket_id: int, agent_id: int) -> bool:
    now = _ts_now()
    sb = get_supabase()
    res = supabase_execute(
        lambda: sb.table("tickets")
        .update({"status": "active", "assigned_agent_id": agent_id, "updated_at": now})
        .eq("id", ticket_id)
        .eq("status", "pending")
    )
    rows = res.data or []
    return bool(rows)


def get_ticket(ticket_id: int) -> Optional[dict[str, Any]]:
    sb = get_supabase()
    res = supabase_execute(lambda: sb.table("tickets").select("*").eq("id", ticket_id).limit(1))
    rows = res.data or []
    return rows[0] if rows else None


def save_decision(ticket_id: int, agent_id: int, decision: str) -> None:
    sb = get_supabase()
    supabase_execute(
        lambda: sb.table("ticket_agent_decisions").upsert(
            {
                "ticket_id": ticket_id,
                "agent_id": agent_id,
                "decision": decision,
                "decided_at": _ts_now(),
            },
            on_conflict="ticket_id,agent_id",
        )
    )


def count_rejections(ticket_id: int) -> int:
    sb = get_supabase()
    res = supabase_execute(
        lambda: sb.table("ticket_agent_decisions")
        .select("*", count="exact")
        .eq("ticket_id", ticket_id)
        .eq("decision", "rejected")
    )
    return int(res.count) if res.count is not None else len(res.data or [])


def save_notification(ticket_id: int, agent_id: int, message_id: int) -> None:
    sb = get_supabase()
    supabase_execute(
        lambda: sb.table("ticket_notifications").upsert(
            {"ticket_id": ticket_id, "agent_id": agent_id, "message_id": message_id},
            on_conflict="ticket_id,agent_id",
        )
    )


def get_notifications(ticket_id: int) -> list[dict[str, Any]]:
    sb = get_supabase()
    res = supabase_execute(
        lambda: sb.table("ticket_notifications")
        .select("ticket_id, agent_id, message_id")
        .eq("ticket_id", ticket_id)
    )
    return list(res.data or [])


def is_agent(user_id: int) -> bool:
    return user_id in AGENT_IDS


def support_keyboard(ticket_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton("Принять", callback_data=f"accept:{ticket_id}"),
                InlineKeyboardButton("Отклонить", callback_data=f"reject:{ticket_id}"),
            ]
        ]
    )


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user = update.effective_user
    if not user or not update.message:
        return

    if is_agent(user.id):
        await update.message.reply_text(
            "Вы агент поддержки.\n"
            "Команды:\n"
            "/my - показать активный тикет\n"
            "/finish - завершить активный диалог"
        )
    else:
        await update.message.reply_text(
            "Напишите ваш вопрос в этот чат, и агент поддержки подключится к диалогу."
        )


async def finish(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user = update.effective_user
    if not user or not update.message:
        return
    if not is_agent(user.id):
        await update.message.reply_text("Эта команда доступна только агентам поддержки.")
        return

    ticket = get_active_ticket_for_agent(user.id)
    if not ticket:
        await update.message.reply_text("У вас нет активного диалога.")
        return

    set_ticket_status(ticket["id"], "closed", user.id, close=True)
    await update.message.reply_text(f"Диалог #{ticket['id']} завершен.")
    await context.bot.send_message(
        chat_id=ticket["user_id"],
        text="Диалог с поддержкой завершен агентом. Если нужна помощь снова, отправьте новое сообщение.",
    )


async def my_ticket(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user = update.effective_user
    if not user or not update.message:
        return
    if not is_agent(user.id):
        await update.message.reply_text("Эта команда доступна только агентам поддержки.")
        return

    ticket = get_active_ticket_for_agent(user.id)
    if not ticket:
        await update.message.reply_text("Активного тикета нет.")
        return

    await update.message.reply_text(
        f"Ваш активный тикет: #{ticket['id']}\n"
        f"User ID: {ticket['user_id']}\n"
        f"Статус: {ticket['status']}"
    )


def format_username_line(user: User) -> str:
    if user.username:
        return f"Username: @{user.username}"
    return "Username: не указан"


def build_message_header(title: str, user: User) -> str:
    return f"{title}\n{format_username_line(user)}\nID: {user.id}\n\nСообщение:"


async def forward_message_between_chats(
    context: ContextTypes.DEFAULT_TYPE,
    source_chat_id: int,
    message_id: int,
    target_chat_id: int,
    ticket_id: int,
    from_agent: bool,
    peer: Optional[User] = None,
) -> None:
    if from_agent:
        # Пользователь видит только «как обычное сообщение», без служебных заголовков
        await context.bot.copy_message(
            chat_id=target_chat_id,
            from_chat_id=source_chat_id,
            message_id=message_id,
        )
        return
    if peer is None:
        raise RuntimeError("peer обязателен для пересылки сообщения пользователя агенту")
    title = f"Пользователь (тикет #{ticket_id})"
    await context.bot.send_message(
        chat_id=target_chat_id,
        text=build_message_header(title, peer),
    )
    await context.bot.copy_message(
        chat_id=target_chat_id,
        from_chat_id=source_chat_id,
        message_id=message_id,
    )


async def handle_user_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    message = update.message
    user = update.effective_user
    if not message or not user:
        return

    if is_agent(user.id):
        await handle_agent_message(update, context)
        return

    ticket = get_open_ticket_for_user(user.id)

    if ticket and ticket["status"] == "active" and ticket.get("assigned_agent_id"):
        agent_id = int(ticket["assigned_agent_id"])
        await forward_message_between_chats(
            context=context,
            source_chat_id=message.chat_id,
            message_id=message.message_id,
            target_chat_id=agent_id,
            ticket_id=int(ticket["id"]),
            from_agent=False,
            peer=user,
        )
        return

    if ticket and ticket["status"] == "pending":
        await message.reply_text("Ваша заявка уже отправлена. Ожидайте подключения агента.")
        return

    ticket_id = create_ticket(user.id)
    header = build_message_header(f"Новая заявка #{ticket_id}", user)

    for agent_id in AGENT_IDS:
        sent = await context.bot.send_message(
            chat_id=agent_id,
            text=header,
            reply_markup=support_keyboard(ticket_id),
        )
        save_notification(ticket_id, agent_id, sent.message_id)
        await context.bot.copy_message(
            chat_id=agent_id,
            from_chat_id=message.chat_id,
            message_id=message.message_id,
        )

    await message.reply_text("Заявка отправлена. Как только агент примет ее, вы получите уведомление.")


async def handle_agent_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    message = update.message
    user = update.effective_user
    if not message or not user:
        return

    if not is_agent(user.id):
        return

    ticket = get_active_ticket_for_agent(user.id)
    if not ticket:
        return

    await forward_message_between_chats(
        context=context,
        source_chat_id=message.chat_id,
        message_id=message.message_id,
        target_chat_id=int(ticket["user_id"]),
        ticket_id=int(ticket["id"]),
        from_agent=True,
    )


async def handle_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    user = update.effective_user
    if not query or not user:
        return

    if not is_agent(user.id):
        await query.answer("Только агент может нажимать эти кнопки.", show_alert=True)
        return

    data = query.data or ""
    try:
        action, ticket_id_raw = data.split(":")
        ticket_id = int(ticket_id_raw)
    except (ValueError, AttributeError):
        await query.answer("Некорректные данные кнопки.", show_alert=True)
        return

    ticket = get_ticket(ticket_id)
    if not ticket:
        await query.answer("Тикет не найден.", show_alert=True)
        return

    if action == "accept":
        if ticket["status"] == "closed":
            await query.answer("Тикет уже закрыт.", show_alert=True)
            return

        if get_active_ticket_for_agent(user.id):
            await query.answer("Сначала завершите текущий активный диалог через /finish.", show_alert=True)
            return

        accepted = try_accept_ticket(ticket_id, user.id)
        if not accepted:
            await query.answer("Тикет уже принят другим агентом.", show_alert=True)
            return

        save_decision(ticket_id, user.id, "accepted")
        await query.answer("Принято.")

        await context.bot.send_message(
            chat_id=ticket["user_id"],
            text="Агент поддержки подключился к чату. Можете продолжать диалог.",
        )
        await context.bot.send_message(
            chat_id=user.id,
            text=f"Вы приняли тикет #{ticket_id}. Пишите сообщения сюда. Для завершения: /finish",
        )

        for row in get_notifications(ticket_id):
            try:
                await context.bot.edit_message_reply_markup(
                    chat_id=row["agent_id"],
                    message_id=row["message_id"],
                    reply_markup=None,
                )
            except Exception:
                pass
        return

    if action == "reject":
        if ticket["status"] != "pending":
            await query.answer("Нельзя отклонить: тикет уже обработан.", show_alert=True)
            return

        save_decision(ticket_id, user.id, "rejected")
        await query.answer("Тикет отклонен вами.")

        rejected = count_rejections(ticket_id)
        if AGENT_IDS and rejected >= len(AGENT_IDS):
            set_ticket_status(ticket_id, "closed", None, close=True)
            await context.bot.send_message(
                chat_id=ticket["user_id"],
                text="Сейчас нет доступных агентов. Попробуйте отправить сообщение позже.",
            )
            for row in get_notifications(ticket_id):
                try:
                    await context.bot.edit_message_reply_markup(
                        chat_id=row["agent_id"],
                        message_id=row["message_id"],
                        reply_markup=None,
                    )
                except Exception:
                    pass
        return

    await query.answer("Неизвестное действие.", show_alert=True)


async def telegram_error_handler(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    err = context.error
    if isinstance(err, Conflict):
        logging.warning(
            "Конфликт getUpdates: с тем же BOT_TOKEN уже идёт long polling (второй процесс). "
            "Остановите дубликат: локальный запуск, второй деплой Railway или старый контейнер."
        )
        return
    logging.error("Ошибка в обработчике Telegram", exc_info=err)


def main() -> None:
    if not BOT_TOKEN:
        raise RuntimeError("Не задан BOT_TOKEN")
    if not AGENT_IDS:
        raise RuntimeError("Не задан AGENT_IDS (через запятую: 123,456)")

    logging.basicConfig(
        format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
        level=logging.INFO,
    )
    init_db()

    application = Application.builder().token(BOT_TOKEN).build()
    application.add_error_handler(telegram_error_handler)
    application.add_handler(CommandHandler("start", start))
    application.add_handler(CommandHandler("my", my_ticket))
    application.add_handler(CommandHandler("finish", finish))
    application.add_handler(CallbackQueryHandler(handle_callback))
    application.add_handler(MessageHandler(filters.ALL & ~filters.COMMAND, handle_user_message))

    application.run_polling(drop_pending_updates=True)


if __name__ == "__main__":
    main()
