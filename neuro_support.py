import logging
import os
from datetime import datetime, timezone
from typing import Any, Optional

import psycopg
from psycopg.rows import dict_row
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
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
DATABASE_URL = os.getenv("DATABASE_URL", "").strip()


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


def get_conn() -> psycopg.Connection:
    if not DATABASE_URL:
        raise RuntimeError("Не задан DATABASE_URL (Supabase Postgres connection string)")
    return psycopg.connect(DATABASE_URL, row_factory=dict_row)


def init_db() -> None:
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                CREATE TABLE IF NOT EXISTS tickets (
                    id BIGSERIAL PRIMARY KEY,
                    user_id BIGINT NOT NULL,
                    status TEXT NOT NULL,
                    assigned_agent_id BIGINT,
                    created_at TIMESTAMPTZ NOT NULL,
                    updated_at TIMESTAMPTZ NOT NULL,
                    closed_at TIMESTAMPTZ
                )
                """
            )
            cur.execute(
                """
                CREATE TABLE IF NOT EXISTS ticket_agent_decisions (
                    ticket_id BIGINT NOT NULL REFERENCES tickets(id) ON DELETE CASCADE,
                    agent_id BIGINT NOT NULL,
                    decision TEXT NOT NULL,
                    decided_at TIMESTAMPTZ NOT NULL,
                    PRIMARY KEY (ticket_id, agent_id)
                )
                """
            )
            cur.execute(
                """
                CREATE TABLE IF NOT EXISTS ticket_notifications (
                    ticket_id BIGINT NOT NULL REFERENCES tickets(id) ON DELETE CASCADE,
                    agent_id BIGINT NOT NULL,
                    message_id BIGINT NOT NULL,
                    PRIMARY KEY (ticket_id, agent_id)
                )
                """
            )
            cur.execute("CREATE INDEX IF NOT EXISTS idx_tickets_user_status ON tickets(user_id, status)")
            cur.execute(
                "CREATE INDEX IF NOT EXISTS idx_tickets_agent_status ON tickets(assigned_agent_id, status)"
            )


def get_open_ticket_for_user(user_id: int) -> Optional[dict[str, Any]]:
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT * FROM tickets
                WHERE user_id = %s AND status IN ('pending', 'active')
                ORDER BY id DESC
                LIMIT 1
                """,
                (user_id,),
            )
            return cur.fetchone()


def get_active_ticket_for_agent(agent_id: int) -> Optional[dict[str, Any]]:
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT * FROM tickets
                WHERE assigned_agent_id = %s AND status = 'active'
                ORDER BY id DESC
                LIMIT 1
                """,
                (agent_id,),
            )
            return cur.fetchone()


def create_ticket(user_id: int) -> int:
    now = datetime.now(timezone.utc)
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO tickets (user_id, status, assigned_agent_id, created_at, updated_at, closed_at)
                VALUES (%s, 'pending', NULL, %s, %s, NULL)
                RETURNING id
                """,
                (user_id, now, now),
            )
            row = cur.fetchone()
            if not row:
                raise RuntimeError("Не удалось создать тикет")
            return int(row["id"])


def set_ticket_status(ticket_id: int, status: str, agent_id: Optional[int] = None, close: bool = False) -> None:
    now = datetime.now(timezone.utc)
    with get_conn() as conn:
        with conn.cursor() as cur:
            if close:
                cur.execute(
                    """
                    UPDATE tickets
                    SET status = %s, assigned_agent_id = %s, updated_at = %s, closed_at = %s
                    WHERE id = %s
                    """,
                    (status, agent_id, now, now, ticket_id),
                )
            else:
                cur.execute(
                    """
                    UPDATE tickets
                    SET status = %s, assigned_agent_id = %s, updated_at = %s
                    WHERE id = %s
                    """,
                    (status, agent_id, now, ticket_id),
                )


def try_accept_ticket(ticket_id: int, agent_id: int) -> bool:
    now = datetime.now(timezone.utc)
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                UPDATE tickets
                SET status = 'active', assigned_agent_id = %s, updated_at = %s
                WHERE id = %s AND status = 'pending'
                RETURNING id
                """,
                (agent_id, now, ticket_id),
            )
            return cur.fetchone() is not None


def get_ticket(ticket_id: int) -> Optional[dict[str, Any]]:
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT * FROM tickets WHERE id = %s", (ticket_id,))
            return cur.fetchone()


def save_decision(ticket_id: int, agent_id: int, decision: str) -> None:
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO ticket_agent_decisions (ticket_id, agent_id, decision, decided_at)
                VALUES (%s, %s, %s, %s)
                ON CONFLICT(ticket_id, agent_id) DO UPDATE SET
                  decision = excluded.decision,
                  decided_at = excluded.decided_at
                """,
                (ticket_id, agent_id, decision, datetime.now(timezone.utc)),
            )


def count_rejections(ticket_id: int) -> int:
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT COUNT(*) AS total FROM ticket_agent_decisions
                WHERE ticket_id = %s AND decision = 'rejected'
                """,
                (ticket_id,),
            )
            row = cur.fetchone()
            return int(row["total"]) if row else 0


def save_notification(ticket_id: int, agent_id: int, message_id: int) -> None:
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO ticket_notifications (ticket_id, agent_id, message_id)
                VALUES (%s, %s, %s)
                ON CONFLICT(ticket_id, agent_id) DO UPDATE SET
                  message_id = excluded.message_id
                """,
                (ticket_id, agent_id, message_id),
            )


def get_notifications(ticket_id: int) -> list[dict[str, Any]]:
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT ticket_id, agent_id, message_id FROM ticket_notifications WHERE ticket_id = %s",
                (ticket_id,),
            )
            return list(cur.fetchall())


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


def summarize_message(message) -> str:
    if message.text:
        return message.text
    if message.photo:
        return "[фото]"
    if message.document:
        return f"[файл] {message.document.file_name or ''}".strip()
    if message.voice:
        return "[voice]"
    if message.audio:
        return f"[audio] {message.audio.file_name or ''}".strip()
    if message.video:
        return "[video]"
    if message.video_note:
        return "[video_note]"
    if message.sticker:
        return "[sticker]"
    if message.caption:
        return message.caption
    return "[сообщение]"


async def forward_message_between_chats(
    context: ContextTypes.DEFAULT_TYPE,
    source_chat_id: int,
    message_id: int,
    target_chat_id: int,
    ticket_id: int,
    from_agent: bool,
) -> None:
    prefix = f"Агент (тикет #{ticket_id}):" if from_agent else f"Пользователь (тикет #{ticket_id}):"
    await context.bot.send_message(chat_id=target_chat_id, text=prefix)
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
        )
        return

    if ticket and ticket["status"] == "pending":
        await message.reply_text("Ваша заявка уже отправлена. Ожидайте подключения агента.")
        return

    ticket_id = create_ticket(user.id)
    incoming_text = summarize_message(message)
    text_for_agent = (
        f"Новая заявка #{ticket_id}\n"
        f"User ID: {user.id}\n\n"
        f"Сообщение:\n{incoming_text}"
    )

    for agent_id in AGENT_IDS:
        sent = await context.bot.send_message(
            chat_id=agent_id,
            text=text_for_agent,
            reply_markup=support_keyboard(ticket_id),
        )
        save_notification(ticket_id, agent_id, sent.message_id)

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

    await query.answer()

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

        save_decision(ticket_id, user.id, "accepted")
        accepted = try_accept_ticket(ticket_id, user.id)
        if not accepted:
            await query.answer("Тикет уже принят другим агентом.", show_alert=True)
            return

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


def main() -> None:
    if not BOT_TOKEN:
        raise RuntimeError("Не задан BOT_TOKEN")
    if not AGENT_IDS:
        raise RuntimeError("Не задан AGENT_IDS (через запятую: 123,456)")
    if not DATABASE_URL:
        raise RuntimeError("Не задан DATABASE_URL (Postgres из Supabase)")

    init_db()
    logging.basicConfig(
        format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
        level=logging.INFO,
    )

    application = Application.builder().token(BOT_TOKEN).build()
    application.add_handler(CommandHandler("start", start))
    application.add_handler(CommandHandler("my", my_ticket))
    application.add_handler(CommandHandler("finish", finish))
    application.add_handler(CallbackQueryHandler(handle_callback))
    application.add_handler(MessageHandler(filters.ALL & ~filters.COMMAND, handle_user_message))

    application.run_polling(drop_pending_updates=True)


if __name__ == "__main__":
    main()
