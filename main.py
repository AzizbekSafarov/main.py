"""
Telegram bot: Topgptcoachbot
Funktsiyalar:
- /start (deep link bilan referral id qabul qiladi)
- Startdan so'ng 2 ta kanal tugmasi (bir qatorda) va pastda "Tekshir" tugmasi
- Tekshir tugmasi bosilganda bot foydalanuvchining ikkala kanalga obuna ekanligini tekshiradi (bot kanallarda admin bo'lishi kerak)
- Agar foydalanuvchi ikkala kanalga obuna bo'lsa: unga referral link yuboriladi va "Share" tugmasi orqali tarqatishga yuborish mumkin
- Har bir referal to'liq (ya'ni referal orqali keltirilgan do'st ham ikkala kanalga obuna bo'lsa) referrerga xabar boradi
- 5 ta to'liq referal to'plagach, referrerg'a maxfiy kanal linki yuboriladi

Eslatma: TOKEN foydalanuvchi tomonidan taqdim etildi va quyida kiritildi. Botni har ikkala kanalda admin qiling va botga a'zo holatini tekshirish uchun kerakli huquqlarni bering.
"""

import logging
import sqlite3
import urllib.parse
from typing import Optional
from telegram import __version__ as TG_VER

try:
    from telegram import (
        Update,
        InlineKeyboardButton,
        InlineKeyboardMarkup,
    )
    from telegram.ext import (
        ApplicationBuilder,
        ContextTypes,
        CommandHandler,
        CallbackQueryHandler,
        MessageHandler,
        filters,
    )
except ImportError:
    raise RuntimeError("Please install python-telegram-bot v20+ (pip install python-telegram-bot)")

# ---------- CONFIGURATION (TOKEN kiritilgan) ----------
TOKEN = "7216166559:AAHJxqADiNAq5wO32OVrf4sJ0ukmQ53JUvA"  # <-- token shu yerda kiritildi
BOT_USERNAME = "Topgptcoachbot"  # username without @ for share links
CHANNELS = ["@harvard_mit", "@stanford777"]
SECRET_LINK = "https://t.me/+SKjzHNPQTPc1MDhi"
REQUIRED_REFERRALS = 5
DB_PATH = "bot_data.db"
# -------------------------------------------------------

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)

# ---------- Database helpers ----------

def init_db():
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()
    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS users (
            user_id INTEGER PRIMARY KEY,
            username TEXT,
            first_name TEXT,
            referred_by INTEGER,
            joined INTEGER DEFAULT 0,
            secret_sent INTEGER DEFAULT 0
        )
        """
    )
    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS referrals (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            referrer_id INTEGER,
            referred_id INTEGER,
            completed INTEGER DEFAULT 0,
            UNIQUE(referrer_id, referred_id)
        )
        """
    )
    conn.commit()
    conn.close()


def db_add_or_update_user(user_id: int, username: Optional[str], first_name: Optional[str], referred_by: Optional[int]):
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()
    cur.execute("SELECT user_id FROM users WHERE user_id = ?", (user_id,))
    if cur.fetchone():
        cur.execute(
            "UPDATE users SET username = ?, first_name = ? WHERE user_id = ?",
            (username, first_name, user_id),
        )
    else:
        cur.execute(
            "INSERT INTO users (user_id, username, first_name, referred_by) VALUES (?, ?, ?, ?)",
            (user_id, username, first_name, referred_by),
        )
        # If referred_by exists, add a referral row (not completed yet)
        if referred_by and referred_by != user_id:
            try:
                cur.execute(
                    "INSERT OR IGNORE INTO referrals (referrer_id, referred_id, completed) VALUES (?, ?, 0)",
                    (referred_by, user_id),
                )
            except Exception:
                pass
    conn.commit()
    conn.close()


def db_mark_joined(user_id: int):
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()
    cur.execute("UPDATE users SET joined = 1 WHERE user_id = ?", (user_id,))
    conn.commit()
    conn.close()


def db_get_user(user_id: int):
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()
    cur.execute("SELECT user_id, username, first_name, referred_by, joined, secret_sent FROM users WHERE user_id = ?", (user_id,))
    row = cur.fetchone()
    conn.close()
    return row


def db_complete_referral_if_any(referred_id: int):
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()
    cur.execute("SELECT referred_by FROM users WHERE user_id = ?", (referred_id,))
    row = cur.fetchone()
    completed_referrer = None
    if row and row[0]:
        referrer_id = row[0]
        cur.execute(
            "SELECT completed FROM referrals WHERE referrer_id = ? AND referred_id = ?",
            (referrer_id, referred_id),
        )
        r = cur.fetchone()
        if r:
            if r[0] == 0:
                cur.execute(
                    "UPDATE referrals SET completed = 1 WHERE referrer_id = ? AND referred_id = ?",
                    (referrer_id, referred_id),
                )
                completed_referrer = referrer_id
        else:
            # maybe referral row wasn't created at start; create it completed
            cur.execute(
                "INSERT OR IGNORE INTO referrals (referrer_id, referred_id, completed) VALUES (?, ?, 1)",
                (referrer_id, referred_id),
            )
            completed_referrer = referrer_id
    conn.commit()
    conn.close()
    return completed_referrer


def db_count_completed_referrals(referrer_id: int) -> int:
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()
    cur.execute(
        "SELECT COUNT(*) FROM referrals WHERE referrer_id = ? AND completed = 1",
        (referrer_id,),
    )
    cnt = cur.fetchone()[0]
    conn.close()
    return cnt


def db_mark_secret_sent(referrer_id: int):
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()
    cur.execute("UPDATE users SET secret_sent = 1 WHERE user_id = ?", (referrer_id,))
    conn.commit()
    conn.close()


def db_secret_already_sent(referrer_id: int) -> bool:
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()
    cur.execute("SELECT secret_sent FROM users WHERE user_id = ?", (referrer_id,))
    r = cur.fetchone()
    conn.close()
    return bool(r and r[0])

# ---------- Bot logic ----------

async def send_start_keyboard(chat_id, application):
    keyboard = [
        [
            InlineKeyboardButton(text=CHANNELS[0], url=f"https://t.me/{CHANNELS[0].lstrip('@')}`"),
            InlineKeyboardButton(text=CHANNELS[1], url=f"https://t.me/{CHANNELS[1].lstrip('@')}`"),
        ],
        [InlineKeyboardButton(text="Tekshir ✅", callback_data="check")],
    ]
    # Note: backticks in urls corrected below
    # rebuild keyboard properly
    keyboard = [
        [
            InlineKeyboardButton(text=CHANNELS[0], url=f"https://t.me/{CHANNELS[0].lstrip('@')}") ,
            InlineKeyboardButton(text=CHANNELS[1], url=f"https://t.me/{CHANNELS[1].lstrip('@')}") ,
        ],
        [InlineKeyboardButton(text="Tekshir ✅", callback_data="check")],
    ]

    await application.bot.send_message(
        chat_id=chat_id,
        text=(
            "Salom! 👋\n\n"
            "Iltimos, quyidagi ikkita kanalda obuna bo'ling:"
        ),
        reply_markup=InlineKeyboardMarkup(keyboard),
    )


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    args = context.args if context.args else []
    referred_by = None
    if args:
        # qoidaviy: /start 12345 yoki /start=12345
        raw = args[0]
        if raw.isdigit():
            referred_by = int(raw)
        else:
            # try to extract digits
            import re

            m = re.search(r"(\d+)", raw)
            if m:
                referred_by = int(m.group(1))

    db_add_or_update_user(user.id, user.username or None, user.first_name or None, referred_by)

    # Send welcome + keyboard
    await send_start_keyboard(chat_id=update.effective_chat.id, application=context.application)


async def check_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    user = query.from_user
    missing = []
    for ch in CHANNELS:
        try:
            member = await context.bot.get_chat_member(chat_id=ch, user_id=user.id)
            status = member.status
            if status in ["left", "kicked"]:
                missing.append(ch)
        except Exception as e:
            logger.warning(f"Error checking membership for {ch}: {e}")
            missing.append(ch)

    if not missing:
        # user joined both
        db_mark_joined(user.id)
        # mark referral completion (if any)
        completed_referrer = db_complete_referral_if_any(user.id)

        # Send referral link to this user (only once)
        ref_link = f"https://t.me/{BOT_USERNAME}?start={user.id}"
        share_url = "https://t.me/share/url?url=" + urllib.parse.quote_plus(ref_link) + "&text=" + urllib.parse.quote_plus(
            "Salom! Bu orqali Topgptcoachbot ga kiring va kanallarga obuna bo'ling: " + ref_link
        )
        keyboard = [
            [InlineKeyboardButton(text="Share referral (ulash)", url=share_url)],
        ]
        await context.bot.send_message(
            chat_id=user.id,
            text=(
                "Ajoyib! Siz ikkala kanalga obuna bo'ldingiz. 🎉\n\n"
                "Endi sizning referal havolangiz quyidagicha: \n" + ref_link + "\n\n"
                "Uni 5 ta do'stingizga yuboring — har bir to'liq obuna qilgan do'stingiz hisoblanadi."
            ),
            reply_markup=InlineKeyboardMarkup(keyboard),
        )

        # If someone referred this user, and we updated referrals, notify the referrer
        if completed_referrer:
            cnt = db_count_completed_referrals(completed_referrer)
            try:
                await context.bot.send_message(
                    chat_id=completed_referrer,
                    text=(
                        f"Sizga xabar: siz tomonidan taklif qilingan do'st @{user.username or user.full_name} \n"
                        f"U endi ikkala kanalda obuna bo'ldi ✅.\n"
                        f"Sizning to'liq takliflar soningiz: {cnt}/{REQUIRED_REFERRALS}"
                    ),
                )
            except Exception as e:
                logger.warning(f"Can't notify referrer {completed_referrer}: {e}")

            # if reached threshold and secret not yet sent
            if cnt >= REQUIRED_REFERRALS and not db_secret_already_sent(completed_referrer):
                try:
                    await context.bot.send_message(
                        chat_id=completed_referrer,
                        text=(
                            "Tabriklaymiz! 🎁 Siz 5 ta to'liq referal to'pladingiz. Bu sizga maxfiy sovg'a linki: \n" + SECRET_LINK
                        ),
                    )
                    db_mark_secret_sent(completed_referrer)
                except Exception as e:
                    logger.warning(f"Failed to send secret link to {completed_referrer}: {e}")

    else:
        # not joined yet
        missing_text = "\n".join([f"- {m}" for m in missing])
        keyboard = [
            [
                InlineKeyboardButton(text=CHANNELS[0], url=f"https://t.me/{CHANNELS[0].lstrip('@')}"),
                InlineKeyboardButton(text=CHANNELS[1], url=f"https://t.me/{CHANNELS[1].lstrip('@')}") ,
            ],
            [InlineKeyboardButton(text="Tekshir yana ✅", callback_data="check")],
        ]
        await context.bot.send_message(
            chat_id=user.id,
            text=(
                "Kechirasiz, quyidagi kanallardan birida yoki bir nechtasida obuna emassiz:\n"
                f"{missing_text}\n\n"
                "Iltimos, yuqoridagi tugmalardan foydalanib obuna bo'ling va keyin Tekshir tugmasini bosing."
            ),
            reply_markup=InlineKeyboardMarkup(keyboard),
        )


async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("/start orqali boshlang. Referal tizim va sovg'alar mavjud.")


async def unknown(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("Iltimos /start bilan botni ishga tushiring yoki tekshirish tugmasidan foydalaning.")


def main():
    init_db()
    application = ApplicationBuilder().token(TOKEN).build()

    application.add_handler(CommandHandler("start", start))
    application.add_handler(CommandHandler("help", help_command))
    application.add_handler(CallbackQueryHandler(check_callback, pattern="^check$"))
    application.add_handler(MessageHandler(filters.ALL, unknown))

    logger.info("Bot ishga tushmoqda...")
    application.run_polling()


if __name__ == "__main__":
    main()