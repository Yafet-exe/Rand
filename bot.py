"""
RandTalk-style Telegram Bot — with rate limiting, spam protection, and admin controls.
"""

import logging
import sqlite3
import os
import math
import json
import time
import requests
from telegram import (
    Update, InlineKeyboardButton, InlineKeyboardMarkup,
    ReplyKeyboardMarkup, KeyboardButton, BotCommand, WebAppInfo
)
from telegram.ext import (
    Application, CommandHandler, MessageHandler,
    CallbackQueryHandler, ContextTypes, filters,
)

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO
)
logger = logging.getLogger(__name__)

BOT_TOKEN  = os.environ.get("BOT_TOKEN",  "YOUR_BOT_TOKEN_HERE")
WEBAPP_URL = os.environ.get("WEBAPP_URL", "https://yafet-exe.github.io/Rand/location.html")
# Your Telegram user_id — get it by messaging @userinfobot
ADMIN_ID   = int(os.environ.get("ADMIN_ID", "0"))
DB_PATH    = "randtalk.db"

GENDERS     = ["Female", "Male"]
LOOKING_FOR = ["Female", "Male"]

STEP_GENDER   = "gender"
STEP_LOOKING  = "looking"
STEP_LOCATION = "location"
STEP_DONE     = "done"

# Rate limiting: max actions per user per time window
RATE_LIMIT_ACTIONS = 10   # max actions
RATE_LIMIT_WINDOW  = 60   # per 60 seconds


# ── DATABASE ─────────────────────────────────────────────────────────────────

def get_db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn

def init_db():
    with get_db() as conn:
        conn.executescript("""
            CREATE TABLE IF NOT EXISTS users (
                user_id      INTEGER PRIMARY KEY,
                username     TEXT,
                first_name   TEXT,
                gender       TEXT,
                looking_for  TEXT,
                lang_code    TEXT DEFAULT 'en',
                city         TEXT,
                latitude     REAL,
                longitude    REAL,
                search_scope TEXT DEFAULT 'Same City',
                bonuses      INTEGER DEFAULT 0,
                referrer_id  INTEGER,
                onboard_step TEXT DEFAULT 'gender',
                is_banned    INTEGER DEFAULT 0,
                created_at   DATETIME DEFAULT CURRENT_TIMESTAMP
            );
            CREATE TABLE IF NOT EXISTS matches (
                id         INTEGER PRIMARY KEY AUTOINCREMENT,
                user1_id   INTEGER,
                user2_id   INTEGER,
                active     INTEGER DEFAULT 1,
                created_at DATETIME DEFAULT CURRENT_TIMESTAMP
            );
            CREATE TABLE IF NOT EXISTS queue (
                user_id   INTEGER PRIMARY KEY,
                joined_at DATETIME DEFAULT CURRENT_TIMESTAMP,
                bonuses   INTEGER DEFAULT 0
            );
            CREATE TABLE IF NOT EXISTS reports (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                reporter_id INTEGER,
                reported_id INTEGER,
                reason      TEXT,
                created_at  DATETIME DEFAULT CURRENT_TIMESTAMP
            );
        """)

def get_user(user_id):
    with get_db() as conn:
        return conn.execute("SELECT * FROM users WHERE user_id=?", (user_id,)).fetchone()

def upsert_user(user_id, username, first_name, lang_code="en"):
    with get_db() as conn:
        conn.execute("""
            INSERT INTO users (user_id, username, first_name, lang_code)
            VALUES (?, ?, ?, ?)
            ON CONFLICT(user_id) DO UPDATE SET
                username=excluded.username,
                first_name=excluded.first_name,
                lang_code=excluded.lang_code
        """, (user_id, username, first_name, lang_code))

def update_user_field(user_id, field, value):
    with get_db() as conn:
        conn.execute(f"UPDATE users SET {field}=? WHERE user_id=?", (value, user_id))

def update_user_location(user_id, lat, lon, city):
    with get_db() as conn:
        conn.execute(
            "UPDATE users SET latitude=?, longitude=?, city=? WHERE user_id=?",
            (lat, lon, city, user_id)
        )

def is_onboarded(db_user):
    return bool(db_user) and db_user["onboard_step"] == STEP_DONE

def is_banned(user_id):
    u = get_user(user_id)
    return bool(u) and bool(u["is_banned"])

def ban_user(user_id):
    with get_db() as conn:
        conn.execute("UPDATE users SET is_banned=1 WHERE user_id=?", (user_id,))

def unban_user(user_id):
    with get_db() as conn:
        conn.execute("UPDATE users SET is_banned=0 WHERE user_id=?", (user_id,))

def add_report(reporter_id, reported_id, reason=""):
    with get_db() as conn:
        conn.execute(
            "INSERT INTO reports (reporter_id, reported_id, reason) VALUES (?,?,?)",
            (reporter_id, reported_id, reason)
        )
        # Auto-ban if reported 3+ times
        count = conn.execute(
            "SELECT COUNT(*) as c FROM reports WHERE reported_id=?", (reported_id,)
        ).fetchone()["c"]
        if count >= 3:
            conn.execute("UPDATE users SET is_banned=1 WHERE user_id=?", (reported_id,))
            return True  # auto-banned
    return False

def get_active_match(user_id):
    with get_db() as conn:
        return conn.execute("""
            SELECT * FROM matches
            WHERE (user1_id=? OR user2_id=?) AND active=1
        """, (user_id, user_id)).fetchone()

def get_partner_id(user_id):
    match = get_active_match(user_id)
    if not match:
        return None
    return match["user2_id"] if match["user1_id"] == user_id else match["user1_id"]

def end_match(user_id):
    with get_db() as conn:
        conn.execute("""
            UPDATE matches SET active=0
            WHERE (user1_id=? OR user2_id=?) AND active=1
        """, (user_id, user_id))

def add_to_queue(user_id, bonuses):
    with get_db() as conn:
        conn.execute("""
            INSERT OR REPLACE INTO queue (user_id, bonuses, joined_at)
            VALUES (?, ?, CURRENT_TIMESTAMP)
        """, (user_id, bonuses))

def remove_from_queue(user_id):
    with get_db() as conn:
        conn.execute("DELETE FROM queue WHERE user_id=?", (user_id,))

def is_in_queue(user_id):
    with get_db() as conn:
        return conn.execute(
            "SELECT 1 FROM queue WHERE user_id=?", (user_id,)
        ).fetchone() is not None

def haversine_km(lat1, lon1, lat2, lon2):
    R = 6371
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp = math.radians(lat2 - lat1)
    dl = math.radians(lon2 - lon1)
    a  = math.sin(dp/2)**2 + math.cos(p1)*math.cos(p2)*math.sin(dl/2)**2
    return 2 * R * math.asin(math.sqrt(a))

def find_match_in_queue(user_id, max_km=50):
    user = get_user(user_id)
    if not user:
        return None
    with get_db() as conn:
        candidates = conn.execute("""
            SELECT u.* FROM queue q
            JOIN users u ON u.user_id=q.user_id
            WHERE q.user_id!=? AND u.gender=? AND u.looking_for=?
            AND u.is_banned=0
            ORDER BY q.bonuses DESC, q.joined_at ASC
        """, (user_id, user["looking_for"], user["gender"])).fetchall()

    if user["search_scope"] == "Same City":
        for c in candidates:
            if c["latitude"] is not None and user["latitude"] is not None:
                if haversine_km(user["latitude"], user["longitude"],
                                c["latitude"],   c["longitude"]) <= max_km:
                    return c["user_id"]
        return None
    return candidates[0]["user_id"] if candidates else None

def create_match(u1, u2):
    with get_db() as conn:
        conn.execute("INSERT INTO matches (user1_id, user2_id) VALUES (?,?)", (u1, u2))

def add_bonus(user_id, amount):
    with get_db() as conn:
        conn.execute(
            "UPDATE users SET bonuses=bonuses+? WHERE user_id=?", (amount, user_id)
        )

def set_referrer(user_id, referrer_id):
    with get_db() as conn:
        row = conn.execute(
            "SELECT referrer_id FROM users WHERE user_id=?", (user_id,)
        ).fetchone()
        if row and row["referrer_id"] is None:
            conn.execute(
                "UPDATE users SET referrer_id=? WHERE user_id=?", (referrer_id, user_id)
            )
            return True
    return False

def get_stats():
    with get_db() as conn:
        total   = conn.execute("SELECT COUNT(*) as c FROM users").fetchone()["c"]
        active  = conn.execute(
            "SELECT COUNT(*) as c FROM matches WHERE active=1"
        ).fetchone()["c"]
        queue   = conn.execute("SELECT COUNT(*) as c FROM queue").fetchone()["c"]
        banned  = conn.execute(
            "SELECT COUNT(*) as c FROM users WHERE is_banned=1"
        ).fetchone()["c"]
        reports = conn.execute("SELECT COUNT(*) as c FROM reports").fetchone()["c"]
    return total, active, queue, banned, reports


# ── RATE LIMITING ─────────────────────────────────────────────────────────────
# Stored in memory (context.bot_data) — simple token bucket per user

def check_rate_limit(context: ContextTypes.DEFAULT_TYPE, user_id: int) -> bool:
    """Returns True if user is within rate limit, False if they're flooding."""
    now    = time.time()
    limits = context.bot_data.setdefault("rate_limits", {})
    bucket = limits.setdefault(user_id, {"count": 0, "window_start": now})

    if now - bucket["window_start"] > RATE_LIMIT_WINDOW:
        bucket["count"]        = 1
        bucket["window_start"] = now
        return True

    bucket["count"] += 1
    return bucket["count"] <= RATE_LIMIT_ACTIONS


# ── GEOCODING ─────────────────────────────────────────────────────────────────

def reverse_geocode_city(lat, lon):
    try:
        r = requests.get(
            "https://nominatim.openstreetmap.org/reverse",
            params={"lat": lat, "lon": lon, "format": "json", "zoom": 10},
            headers={"User-Agent": "RandTalkBot/1.0"},
            timeout=5,
        )
        addr    = r.json().get("address", {})
        city    = (addr.get("city") or addr.get("town") or addr.get("village")
                   or addr.get("municipality") or addr.get("county") or "Unknown")
        country = addr.get("country", "")
        # Store city+country internally for matching, but only show country to user
        return f"{city}, {country}" if country else city
    except Exception as e:
        logger.warning(f"Geocode failed: {e}")
        return "Unknown"

def display_location(city_str):
    """Show only the country part to the user, keep full string for matching."""
    parts = city_str.split(", ")
    return parts[-1] if len(parts) > 1 else city_str


# ── KEYBOARDS ─────────────────────────────────────────────────────────────────

def main_menu_keyboard():
    return ReplyKeyboardMarkup(
        [
            [KeyboardButton("🔍 Start looking"), KeyboardButton("🚫 End chat")],
            [KeyboardButton("⚙️ Setup"),         KeyboardButton("👤 Profile")],
            [KeyboardButton("🔗 Referral"),       KeyboardButton("❓ Help")],
        ],
        resize_keyboard=True, is_persistent=True,
    )

def location_keyboard():
    return ReplyKeyboardMarkup(
        [[KeyboardButton("📍 Share My Real Location", web_app=WebAppInfo(url=WEBAPP_URL))]],
        resize_keyboard=True, one_time_keyboard=True, is_persistent=False,
    )

def setup_inline_keyboard():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("🌐 Search Scope",    callback_data="setup_scope")],
        [InlineKeyboardButton("👤 Your Gender",     callback_data="setup_gender"),
         InlineKeyboardButton("❤️ Looking For",     callback_data="setup_looking")],
        [InlineKeyboardButton("📍 Update Location", callback_data="setup_location")],
        [InlineKeyboardButton("✅ Close Setup",     callback_data="setup_close")],
    ])


# ── ONBOARDING HELPERS ────────────────────────────────────────────────────────

async def send_gender_prompt(bot, chat_id):
    await bot.send_message(
        chat_id=chat_id,
        text="Welcome,\n\nLet's get you started\n\n-> What is your gender?",
        reply_markup=InlineKeyboardMarkup([[
            InlineKeyboardButton("👩 Female", callback_data="onb_gender_Female"),
            InlineKeyboardButton("👨 Male",   callback_data="onb_gender_Male"),
        ]])
    )

async def send_looking_prompt(bot, chat_id):
    await bot.send_message(
        chat_id=chat_id,
        text="-> Who are you looking for?",
        reply_markup=InlineKeyboardMarkup([[
            InlineKeyboardButton("👩 Female", callback_data="onb_looking_Female"),
            InlineKeyboardButton("👨 Male",   callback_data="onb_looking_Male"),
        ]])
    )

async def send_location_prompt(bot, chat_id):
    await bot.send_message(
        chat_id=chat_id,
        text=(
            "-> Last step! Share your verified GPS location so we can "
            "match you with people nearby.\n\n"
            "📍 How to share:\n"
            "1️⃣ Make sure Location Services are ON in your phone settings\n"
            "2️⃣ Tap the [::] button to the left of the message box\n"
            "3️⃣ Tap Location → Share My Location\n"
            "   OR tap the 📍 Share My Real Location button below\n\n"
            "⚠️ Only GPS-verified locations are accepted."
        ),
        reply_markup=location_keyboard()
    )

async def resume_onboarding(bot, chat_id, step):
    await bot.send_message(chat_id=chat_id, text="⚠️ Please complete your profile setup first.")
    if step == STEP_GENDER:
        await send_gender_prompt(bot, chat_id)
    elif step == STEP_LOOKING:
        await send_looking_prompt(bot, chat_id)
    elif step == STEP_LOCATION:
        await send_location_prompt(bot, chat_id)


# ── GUARDS ────────────────────────────────────────────────────────────────────

async def require_onboarding(update: Update, context: ContextTypes.DEFAULT_TYPE) -> bool:
    user = update.effective_user
    db_u = get_user(user.id)
    if not db_u:
        upsert_user(user.id, user.username, user.first_name, user.language_code or "en")
        db_u = get_user(user.id)
    if is_onboarded(db_u):
        return True
    await resume_onboarding(context.bot, user.id, db_u["onboard_step"])
    return False

async def check_ban_and_rate(update: Update, context: ContextTypes.DEFAULT_TYPE) -> bool:
    """Returns True if user is allowed to proceed. Handles ban + rate limit."""
    user_id = update.effective_user.id

    if is_banned(user_id):
        await update.message.reply_text(
            "🚫 Your account has been suspended for violating our terms of service.\n"
            "If you believe this is a mistake, contact support."
        )
        return False

    if not check_rate_limit(context, user_id):
        await update.message.reply_text(
            "⚠️ You're sending messages too fast. Please slow down."
        )
        return False

    return True


# ── COMMANDS ──────────────────────────────────────────────────────────────────

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    if is_banned(user.id):
        await update.message.reply_text("🚫 Your account has been suspended.")
        return

    upsert_user(user.id, user.username, user.first_name, user.language_code or "en")

    if context.args:
        try:
            ref = int(context.args[0])
            if ref != user.id:
                context.user_data["pending_referrer"] = ref
                set_referrer(user.id, ref)
        except ValueError:
            pass

    db_u = get_user(user.id)
    if is_onboarded(db_u):
        await update.message.reply_text(
            f"Welcome back, {user.first_name}! 👋\n\nUse the menu below or /begin to find a chat partner.",
            reply_markup=main_menu_keyboard()
        )
    else:
        step = db_u["onboard_step"] if db_u else STEP_GENDER
        if step == STEP_GENDER:
            await send_gender_prompt(context.bot, user.id)
        elif step == STEP_LOOKING:
            await send_looking_prompt(context.bot, user.id)
        elif step == STEP_LOCATION:
            await send_location_prompt(context.bot, user.id)

async def begin(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await check_ban_and_rate(update, context):
        return
    if not await require_onboarding(update, context):
        return

    user_id = update.effective_user.id
    db_u    = get_user(user_id)

    if get_active_match(user_id):
        await update.message.reply_text("You're already in a chat! Use 🚫 End chat to leave first.")
        return
    if is_in_queue(user_id):
        await update.message.reply_text("Already searching... please wait 🤔")
        return

    partner_id = find_match_in_queue(user_id)
    if partner_id:
        remove_from_queue(partner_id)
        remove_from_queue(user_id)
        create_match(user_id, partner_id)
        msg = "🙂 Your partner is here. Have a nice chat"
        await update.message.reply_text(f"Rand Talk: {msg}", reply_markup=main_menu_keyboard())
        await context.bot.send_message(chat_id=partner_id, text=f"Rand Talk: {msg}")

        ref = context.user_data.pop("pending_referrer", None)
        if ref:
            bonus = 3 if db_u["gender"] == "Female" else 1
            add_bonus(ref, bonus)
            try:
                await context.bot.send_message(
                    chat_id=ref,
                    text=f"🎉 Your referral joined! You earned {bonus} bonus{'es' if bonus>1 else ''}."
                )
            except Exception:
                pass
    else:
        add_to_queue(user_id, db_u["bonuses"])
        ref_link = f"https://t.me/Rando_talk_bot?start={user_id}"
        logger.info(f"Referral link generated for user {user_id}: {ref_link}")
        await update.message.reply_text(
            f"You are looking for a partner who is: *{db_u['looking_for']}*",
            parse_mode="Markdown", reply_markup=main_menu_keyboard()
        )
        await update.message.reply_text("Rand Talk: Looking for a stranger for you 🤔")
        await update.message.reply_text(
            f"Chat lacks *females*! Invite friends and earn bonuses for faster matching!\n\n{ref_link}",
            parse_mode="Markdown",
            link_preview_options={"is_disabled": True}
        )

async def end_chat(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await check_ban_and_rate(update, context):
        return
    user_id    = update.effective_user.id
    partner_id = get_partner_id(user_id)
    remove_from_queue(user_id)
    if partner_id:
        end_match(user_id)
        await update.message.reply_text(
            "Chat ended. Use /begin to find a new partner!",
            reply_markup=main_menu_keyboard()
        )
        await context.bot.send_message(
            chat_id=partner_id,
            text="😾 Rand Talk: Your partner has left chat. Feel free to /begin a new one."
        )
    else:
        await update.message.reply_text("You're not in a chat right now.", reply_markup=main_menu_keyboard())

async def setup(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await check_ban_and_rate(update, context):
        return
    if not await require_onboarding(update, context):
        return
    await update.message.reply_text("What do you like to configure?", reply_markup=setup_inline_keyboard())

async def profile(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await check_ban_and_rate(update, context):
        return
    if not await require_onboarding(update, context):
        return
    u = get_user(update.effective_user.id)
    await update.message.reply_text(
        f"👤 *Your Profile*\n\n"
        f"👤 Gender: {u['gender']}\n"
        f"❤️ Looking For: {u['looking_for']}\n"
        f"📍 City: {display_location(u['city']) if u['city'] else 'Not set'}\n"
        f"🔍 Search Scope: {u['search_scope']}\n"
        f"⭐ Bonuses: {u['bonuses']}",
        parse_mode="Markdown", reply_markup=main_menu_keyboard()
    )

async def exchange_username(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await check_ban_and_rate(update, context):
        return
    if not await require_onboarding(update, context):
        return
    user_id    = update.effective_user.id
    partner_id = get_partner_id(user_id)
    if not partner_id:
        await update.message.reply_text("You're not in a chat. Use /begin first.")
        return
    context.bot_data.setdefault("exchange_requests", {})
    reqs = context.bot_data["exchange_requests"]
    if reqs.get(partner_id) == user_id:
        del reqs[partner_id]
        u = get_user(user_id)
        p = get_user(partner_id)
        u_name = f"@{u['username']}" if u["username"] else u["first_name"]
        p_name = f"@{p['username']}" if p["username"] else p["first_name"]
        await update.message.reply_text(f"Your partner's username: {p_name}")
        await context.bot.send_message(chat_id=partner_id, text=f"Your partner's username: {u_name}")
    else:
        reqs[user_id] = partner_id
        await update.message.reply_text("Username exchange request sent!")
        await context.bot.send_message(
            chat_id=partner_id,
            text="Your partner wants to exchange usernames. Use /exchange_username to accept."
        )

async def referral(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await check_ban_and_rate(update, context):
        return
    if not await require_onboarding(update, context):
        return
    user_id  = update.effective_user.id
    link     = f"https://t.me/Rando_talk_bot?start={user_id}"
    logger.info(f"Referral link for user {user_id}: {link}")
    u        = get_user(user_id)
    await update.message.reply_text(
        f"🔗 *Your Referral Link:*\n{link}\n\n"
        f"Earn *3 bonuses* for every female you invite and *1 bonus* for each male.\n\n"
        f"⭐ Your current bonuses: {u['bonuses']}",
        parse_mode="Markdown",
        reply_markup=main_menu_keyboard(),
        link_preview_options={"is_disabled": True}
    )

async def report_user(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await check_ban_and_rate(update, context):
        return
    if not await require_onboarding(update, context):
        return
    user_id    = update.effective_user.id
    partner_id = get_partner_id(user_id)
    if not partner_id:
        await update.message.reply_text("You can only report someone you're currently chatting with.")
        return
    reason     = " ".join(context.args) if context.args else "No reason given"
    auto_banned = add_report(user_id, partner_id, reason)
    end_match(user_id)
    remove_from_queue(user_id)
    msg = "✅ Report submitted. The user has been flagged for review."
    if auto_banned:
        msg += " They have been automatically suspended."
    await update.message.reply_text(msg, reply_markup=main_menu_keyboard())
    try:
        await context.bot.send_message(
            chat_id=partner_id,
            text="😾 Rand Talk: Your partner has left chat. Feel free to /begin a new one."
        )
    except Exception:
        pass
    # Notify admin
    if ADMIN_ID:
        try:
            await context.bot.send_message(
                chat_id=ADMIN_ID,
                text=f"🚨 Report filed\nReporter: {user_id}\nReported: {partner_id}\nReason: {reason}\nAuto-banned: {auto_banned}"
            )
        except Exception:
            pass

async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "🤖 *Rand Talk Help*\n\n"
        "/begin — Start looking for a chat partner\n"
        "/end — End the current chat\n"
        "/setup — Configure your profile\n"
        "/profile — Show your current profile\n"
        "/exchange\\_username — Exchange usernames with your partner\n"
        "/referral — Get your referral link\n"
        "/report [reason] — Report your current chat partner\n"
        "/help — Show this help\n\n"
        "📍 You are matched with people within ~50km of your location.\n"
        "🚨 Use /report if someone is being inappropriate.",
        parse_mode="Markdown", reply_markup=main_menu_keyboard()
    )


# ── ADMIN COMMANDS ────────────────────────────────────────────────────────────

async def admin_ban(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ADMIN_ID:
        return
    if not context.args:
        await update.message.reply_text("Usage: /ban <user_id>")
        return
    target = int(context.args[0])
    ban_user(target)
    # kick from queue and end active chat
    partner_id = get_partner_id(target)
    remove_from_queue(target)
    if partner_id:
        end_match(target)
        try:
            await context.bot.send_message(
                chat_id=partner_id,
                text="😾 Rand Talk: Your partner has left chat. Feel free to /begin a new one."
            )
        except Exception:
            pass
    await update.message.reply_text(f"✅ User {target} has been banned.")

async def admin_unban(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ADMIN_ID:
        return
    if not context.args:
        await update.message.reply_text("Usage: /unban <user_id>")
        return
    target = int(context.args[0])
    unban_user(target)
    await update.message.reply_text(f"✅ User {target} has been unbanned.")

async def admin_stats(update: Update, context: ContextTypes.DEFAULT_TYPE):
    sender_id = update.effective_user.id
    logger.info(f"/stats called by user_id={sender_id}, ADMIN_ID={ADMIN_ID}")
    if sender_id != ADMIN_ID:
        logger.info(f"Rejected: {sender_id} != {ADMIN_ID}")
        return
    total, active, queue, banned, reports = get_stats()
    await update.message.reply_text(
        f"📊 *Bot Stats*\n\n"
        f"👥 Total users: {total}\n"
        f"💬 Active chats: {active}\n"
        f"⏳ In queue: {queue}\n"
        f"🚫 Banned users: {banned}\n"
        f"🚨 Total reports: {reports}",
        parse_mode="Markdown"
    )

async def admin_broadcast(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ADMIN_ID:
        return
    if not context.args:
        await update.message.reply_text("Usage: /broadcast <message>")
        return
    msg  = " ".join(context.args)
    sent = 0
    with get_db() as conn:
        users = conn.execute(
            "SELECT user_id FROM users WHERE is_banned=0 AND onboard_step='done'"
        ).fetchall()
    for u in users:
        try:
            await context.bot.send_message(chat_id=u["user_id"], text=f"📢 {msg}")
            sent += 1
        except Exception:
            pass
    await update.message.reply_text(f"✅ Broadcast sent to {sent} users.")


# ── TEXT HANDLER ──────────────────────────────────────────────────────────────

async def handle_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await check_ban_and_rate(update, context):
        return
    text = update.message.text
    dispatch = {
        "🔍 Start looking": begin,
        "🚫 End chat":      end_chat,
        "⚙️ Setup":         setup,
        "👤 Profile":       profile,
        "🔗 Referral":      referral,
        "❓ Help":          help_command,
    }
    if text in dispatch:
        await dispatch[text](update, context)
        return

    if not await require_onboarding(update, context):
        return
    partner_id = get_partner_id(update.effective_user.id)
    if partner_id:
        try:
            await context.bot.send_message(chat_id=partner_id, text=text)
        except Exception as e:
            logger.error(f"Relay error: {e}")
            await update.message.reply_text("Could not deliver message. Your partner may have left.")
    else:
        await update.message.reply_text(
            "You're not in a chat. Use the menu or /begin to find a partner.",
            reply_markup=main_menu_keyboard()
        )


# ── MEDIA RELAY ───────────────────────────────────────────────────────────────

async def relay_media(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await check_ban_and_rate(update, context):
        return
    if not await require_onboarding(update, context):
        return
    user_id    = update.effective_user.id
    partner_id = get_partner_id(user_id)
    if not partner_id:
        await update.message.reply_text("You're not in a chat.", reply_markup=main_menu_keyboard())
        return
    try:
        msg = update.message
        if msg.sticker:
            await context.bot.send_sticker(chat_id=partner_id, sticker=msg.sticker.file_id)
        elif msg.photo:
            await context.bot.send_photo(chat_id=partner_id, photo=msg.photo[-1].file_id, caption=msg.caption)
        elif msg.voice:
            await context.bot.send_voice(chat_id=partner_id, voice=msg.voice.file_id)
        elif msg.video:
            await context.bot.send_video(chat_id=partner_id, video=msg.video.file_id, caption=msg.caption)
        elif msg.document:
            await context.bot.send_document(chat_id=partner_id, document=msg.document.file_id, caption=msg.caption)
        elif msg.audio:
            await context.bot.send_audio(chat_id=partner_id, audio=msg.audio.file_id)
        elif msg.video_note:
            await context.bot.send_video_note(chat_id=partner_id, video_note=msg.video_note.file_id)
    except Exception as e:
        logger.error(f"Media relay error: {e}")
        await update.message.reply_text("Could not deliver message. Your partner may have left.")


# ── WEBAPP LOCATION ───────────────────────────────────────────────────────────

async def handle_webapp_location(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    if is_banned(user_id):
        return
    try:
        data = json.loads(update.message.web_app_data.data)
        lat  = float(data["latitude"])
        lon  = float(data["longitude"])
        acc  = data.get("accuracy")
    except Exception as e:
        logger.warning(f"WebApp parse error user={user_id}: {e}")
        await update.message.reply_text("❌ Something went wrong. Please try again.", reply_markup=location_keyboard())
        return

    logger.info(f"WebApp location user={user_id} lat={lat} lon={lon} acc={acc}")
    city = reverse_geocode_city(lat, lon)
    update_user_location(user_id, lat, lon, city)

    db_u = get_user(user_id)
    if db_u and db_u["onboard_step"] != STEP_DONE:
        update_user_field(user_id, "onboard_step", STEP_DONE)
        await update.message.reply_text(
            f"✅ Location verified: *{display_location(city)}*\n\n"
            f"🎉 Profile complete! Use the menu below or /begin to find a chat partner.",
            parse_mode="Markdown", reply_markup=main_menu_keyboard()
        )
    else:
        await update.message.reply_text(
            f"✅ Location updated: *{display_location(city)}*",
            parse_mode="Markdown", reply_markup=main_menu_keyboard()
        )

async def handle_location_fallback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "🔒 Manual location entry is reserved for *Premium* users.\n\n"
        "Please tap the *📍 Share My Real Location* button instead.",
        parse_mode="Markdown", reply_markup=location_keyboard()
    )


# ── CALLBACK HANDLER ──────────────────────────────────────────────────────────

async def callback_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query   = update.callback_query
    await query.answer()
    user_id = query.from_user.id
    data    = query.data

    if is_banned(user_id):
        await query.edit_message_text("🚫 Your account has been suspended.")
        return

    if data.startswith("onb_gender_"):
        gender = data.replace("onb_gender_", "")
        update_user_field(user_id, "gender", gender)
        update_user_field(user_id, "onboard_step", STEP_LOOKING)
        await query.edit_message_text(f"Got it — you are *{gender}*.", parse_mode="Markdown")
        await send_looking_prompt(context.bot, user_id)
        return

    if data.startswith("onb_looking_"):
        looking = data.replace("onb_looking_", "")
        update_user_field(user_id, "looking_for", looking)
        update_user_field(user_id, "onboard_step", STEP_LOCATION)
        await query.edit_message_text(f"Got it — looking for *{looking}*.", parse_mode="Markdown")
        await send_location_prompt(context.bot, user_id)
        return

    if data == "setup_scope":
        btns = [[InlineKeyboardButton(s, callback_data=f"set_scope_{s}")] for s in ["Worldwide", "Same City"]]
        btns.append([InlineKeyboardButton("⬅️ Back", callback_data="setup_back")])
        await query.edit_message_text("🔍 Choose search scope:", reply_markup=InlineKeyboardMarkup(btns))
    elif data == "setup_gender":
        btns = [[InlineKeyboardButton(f"{'👩' if g=='Female' else '👨'} {g}", callback_data=f"set_gender_{g}")] for g in GENDERS]
        btns.append([InlineKeyboardButton("⬅️ Back", callback_data="setup_back")])
        await query.edit_message_text("👤 Choose your gender:", reply_markup=InlineKeyboardMarkup(btns))
    elif data == "setup_looking":
        btns = [[InlineKeyboardButton(f"{'👩' if g=='Female' else '👨'} {g}", callback_data=f"set_looking_{g}")] for g in LOOKING_FOR]
        btns.append([InlineKeyboardButton("⬅️ Back", callback_data="setup_back")])
        await query.edit_message_text("❤️ Who are you looking for?", reply_markup=InlineKeyboardMarkup(btns))
    elif data == "setup_location":
        await query.edit_message_text("Tap the button below to update your location.")
        await send_location_prompt(context.bot, user_id)
    elif data == "setup_back":
        await query.edit_message_text("What do you like to configure?", reply_markup=setup_inline_keyboard())
    elif data == "setup_close":
        await query.edit_message_text("✅ Setup closed.")
    elif data.startswith("set_gender_"):
        val = data.replace("set_gender_", "")
        update_user_field(user_id, "gender", val)
        await query.edit_message_text(f"✅ Gender set to *{val}*", parse_mode="Markdown", reply_markup=setup_inline_keyboard())
    elif data.startswith("set_looking_"):
        val = data.replace("set_looking_", "")
        update_user_field(user_id, "looking_for", val)
        await query.edit_message_text(f"✅ Looking for *{val}*", parse_mode="Markdown", reply_markup=setup_inline_keyboard())
    elif data.startswith("set_scope_"):
        val = data.replace("set_scope_", "")
        update_user_field(user_id, "search_scope", val)
        await query.edit_message_text(f"✅ Search scope: *{val}*", parse_mode="Markdown", reply_markup=setup_inline_keyboard())


# ── BOT COMMANDS ──────────────────────────────────────────────────────────────

async def post_init(app):
    await app.bot.set_my_commands([
        BotCommand("begin",             "Start looking for a chat partner"),
        BotCommand("end",               "End the current chat"),
        BotCommand("setup",             "Configure your profile"),
        BotCommand("profile",           "Show your current profile"),
        BotCommand("exchange_username", "Exchange usernames with your partner"),
        BotCommand("referral",          "Get your referral link"),
        BotCommand("report",            "Report your current chat partner"),
        BotCommand("help",              "Show help"),
    ])


# ── MAIN ──────────────────────────────────────────────────────────────────────

def main():
    init_db()
    app = Application.builder().token(BOT_TOKEN).post_init(post_init).build()

    app.add_handler(CommandHandler("start",             start))
    app.add_handler(CommandHandler("begin",             begin))
    app.add_handler(CommandHandler("end",               end_chat))
    app.add_handler(CommandHandler("setup",             setup))
    app.add_handler(CommandHandler("profile",           profile))
    app.add_handler(CommandHandler("exchange_username", exchange_username))
    app.add_handler(CommandHandler("referral",          referral))
    app.add_handler(CommandHandler("report",            report_user))
    app.add_handler(CommandHandler("help",              help_command))

    # Admin commands
    app.add_handler(CommandHandler("ban",       admin_ban))
    app.add_handler(CommandHandler("unban",     admin_unban))
    app.add_handler(CommandHandler("stats",     admin_stats))
    app.add_handler(CommandHandler("broadcast", admin_broadcast))

    app.add_handler(CallbackQueryHandler(callback_handler))
    app.add_handler(MessageHandler(filters.StatusUpdate.WEB_APP_DATA, handle_webapp_location))
    app.add_handler(MessageHandler(filters.LOCATION, handle_location_fallback))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text))
    app.add_handler(MessageHandler(
        (filters.PHOTO | filters.Sticker.ALL | filters.VOICE | filters.VIDEO |
         filters.Document.ALL | filters.AUDIO | filters.VIDEO_NOTE) & ~filters.COMMAND,
        relay_media
    ))

    logger.info("Bot starting...")
    app.run_polling(drop_pending_updates=True)

if __name__ == "__main__":
    main()
