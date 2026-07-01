"""
RandTalk-style Telegram Bot
Matches random users based on gender preference and shared city (via location pin).
"""

import logging
import sqlite3
import os
import math
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

BOT_TOKEN = os.environ.get("BOT_TOKEN", "YOUR_BOT_TOKEN_HERE")
# After deploying location.html to GitHub Pages, replace this URL:
WEBAPP_URL = os.environ.get("WEBAPP_URL", "https://YOUR_GITHUB_USERNAME.github.io/YOUR_REPO_NAME/location.html")
DB_PATH = "randtalk.db"

GENDERS = ["Female", "Male"]
LOOKING_FOR = ["Female", "Male"]
SEARCH_SCOPES = ["Worldwide", "Same City"]

# Onboarding steps, in strict order
STEP_GENDER = "gender"
STEP_LOOKING = "looking"
STEP_LOCATION = "location"
STEP_DONE = "done"


# ─── DATABASE ────────────────────────────────────────────────────────────────

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
                onboard_step TEXT DEFAULT 'gender'
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
        """)

def get_user(user_id):
    with get_db() as conn:
        return conn.execute("SELECT * FROM users WHERE user_id = ?", (user_id,)).fetchone()

def upsert_user(user_id, username, first_name, lang_code="en"):
    with get_db() as conn:
        conn.execute("""
            INSERT INTO users (user_id, username, first_name, lang_code)
            VALUES (?, ?, ?, ?)
            ON CONFLICT(user_id) DO UPDATE SET
                username   = excluded.username,
                first_name = excluded.first_name,
                lang_code  = excluded.lang_code
        """, (user_id, username, first_name, lang_code))

def update_user_field(user_id, field, value):
    with get_db() as conn:
        conn.execute(f"UPDATE users SET {field} = ? WHERE user_id = ?", (value, user_id))

def update_user_location(user_id, lat, lon, city):
    with get_db() as conn:
        conn.execute(
            "UPDATE users SET latitude = ?, longitude = ?, city = ? WHERE user_id = ?",
            (lat, lon, city, user_id)
        )

def is_onboarded(db_user):
    return bool(db_user) and db_user["onboard_step"] == STEP_DONE

def get_active_match(user_id):
    with get_db() as conn:
        return conn.execute("""
            SELECT * FROM matches
            WHERE (user1_id = ? OR user2_id = ?) AND active = 1
        """, (user_id, user_id)).fetchone()

def get_partner_id(user_id):
    match = get_active_match(user_id)
    if not match:
        return None
    return match["user2_id"] if match["user1_id"] == user_id else match["user1_id"]

def end_match(user_id):
    with get_db() as conn:
        conn.execute("""
            UPDATE matches SET active = 0
            WHERE (user1_id = ? OR user2_id = ?) AND active = 1
        """, (user_id, user_id))

def add_to_queue(user_id, bonuses):
    with get_db() as conn:
        conn.execute("""
            INSERT OR REPLACE INTO queue (user_id, bonuses, joined_at)
            VALUES (?, ?, CURRENT_TIMESTAMP)
        """, (user_id, bonuses))

def remove_from_queue(user_id):
    with get_db() as conn:
        conn.execute("DELETE FROM queue WHERE user_id = ?", (user_id,))

def is_in_queue(user_id):
    with get_db() as conn:
        return conn.execute("SELECT 1 FROM queue WHERE user_id = ?", (user_id,)).fetchone() is not None

def haversine_km(lat1, lon1, lat2, lon2):
    R = 6371
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dphi = math.radians(lat2 - lat1)
    dlambda = math.radians(lon2 - lon1)
    a = math.sin(dphi / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dlambda / 2) ** 2
    return 2 * R * math.asin(math.sqrt(a))

def find_match_in_queue(user_id, max_distance_km=50):
    user = get_user(user_id)
    if not user:
        return None
    with get_db() as conn:
        candidates = conn.execute("""
            SELECT u.* FROM queue q
            JOIN users u ON u.user_id = q.user_id
            WHERE q.user_id != ?
            AND u.gender = ?
            AND u.looking_for = ?
            ORDER BY q.bonuses DESC, q.joined_at ASC
        """, (user_id, user["looking_for"], user["gender"])).fetchall()

    if user["search_scope"] == "Same City":
        for c in candidates:
            if c["latitude"] is not None and user["latitude"] is not None:
                dist = haversine_km(user["latitude"], user["longitude"], c["latitude"], c["longitude"])
                if dist <= max_distance_km:
                    return c["user_id"]
        return None
    else:
        # Worldwide — just take the first candidate (already bonus/time sorted)
        return candidates[0]["user_id"] if candidates else None

def create_match(user1_id, user2_id):
    with get_db() as conn:
        conn.execute("INSERT INTO matches (user1_id, user2_id) VALUES (?, ?)", (user1_id, user2_id))

def add_bonus(user_id, amount):
    with get_db() as conn:
        conn.execute("UPDATE users SET bonuses = bonuses + ? WHERE user_id = ?", (amount, user_id))

def set_referrer(user_id, referrer_id):
    with get_db() as conn:
        existing = conn.execute("SELECT referrer_id FROM users WHERE user_id = ?", (user_id,)).fetchone()
        if existing and existing["referrer_id"] is None:
            conn.execute("UPDATE users SET referrer_id = ? WHERE user_id = ?", (referrer_id, user_id))
            return True
    return False


# ─── REVERSE GEOCODING ───────────────────────────────────────────────────────

def reverse_geocode_city(lat, lon):
    """Resolve lat/lon to a city name using OpenStreetMap Nominatim (free, no API key)."""
    try:
        resp = requests.get(
            "https://nominatim.openstreetmap.org/reverse",
            params={"lat": lat, "lon": lon, "format": "json", "zoom": 10},
            headers={"User-Agent": "RandTalkBot/1.0"},
            timeout=5,
        )
        data = resp.json()
        addr = data.get("address", {})
        city = (
            addr.get("city") or addr.get("town") or addr.get("village")
            or addr.get("municipality") or addr.get("county") or "Unknown"
        )
        country = addr.get("country", "")
        return f"{city}, {country}" if country else city
    except Exception as e:
        logger.warning(f"Reverse geocode failed: {e}")
        return "Unknown"


# ─── KEYBOARDS ───────────────────────────────────────────────────────────────

def main_menu_keyboard():
    return ReplyKeyboardMarkup(
        [
            [KeyboardButton("🔍 Start looking"), KeyboardButton("🚫 End chat")],
            [KeyboardButton("⚙️ Setup"),         KeyboardButton("👤 Profile")],
            [KeyboardButton("🔗 Referral"),       KeyboardButton("❓ Help")],
        ],
        resize_keyboard=True,
        is_persistent=True,
    )

def location_request_keyboard():
    """
    Opens a Telegram WebApp that uses navigator.geolocation with
    enableHighAccuracy:true to get a real hardware GPS fix.
    Data comes back as update.message.web_app_data — a separate,
    unforgeable update type that can only originate from a WebApp tap,
    not from any manual location flow in Telegram.
    """
    return ReplyKeyboardMarkup(
        [[KeyboardButton("📍 Share My Real Location", web_app=WebAppInfo(url=WEBAPP_URL))]],
        resize_keyboard=True,
        one_time_keyboard=True,
        is_persistent=False,
    )

def setup_inline_keyboard():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("🌐 Search Scope",  callback_data="setup_scope")],
        [InlineKeyboardButton("👤 Your Gender",   callback_data="setup_gender"),
         InlineKeyboardButton("❤️ Looking For",   callback_data="setup_looking")],
        [InlineKeyboardButton("📍 Update Location", callback_data="setup_location")],
        [InlineKeyboardButton("✅ Close Setup",   callback_data="setup_close")],
    ])


# ─── ONBOARDING FLOW (strict, sequential, mandatory) ─────────────────────────

async def prompt_for_step(update_or_query, step, context: ContextTypes.DEFAULT_TYPE, edit=False):
    """Sends the prompt for whichever onboarding step the user is currently on."""
    if step == STEP_GENDER:
        text = "Welcome,\n\nLet's get you started\n\n-> What is your gender?"
        kb = InlineKeyboardMarkup([[
            InlineKeyboardButton("👩 Female", callback_data="onb_gender_Female"),
            InlineKeyboardButton("👨 Male",   callback_data="onb_gender_Male"),
        ]])
        if edit:
            await update_or_query.edit_message_text(text, reply_markup=kb)
        else:
            await update_or_query.message.reply_text(text, reply_markup=kb)

    elif step == STEP_LOOKING:
        text = "-> Who are you looking for?"
        kb = InlineKeyboardMarkup([[
            InlineKeyboardButton("👩 Female", callback_data="onb_looking_Female"),
            InlineKeyboardButton("👨 Male",   callback_data="onb_looking_Male"),
        ]])
        # Always send a new message for this step — editing in place causes
        # button taps to silently fail on some Telegram clients
        if edit:
            await update_or_query.message.reply_text(text, reply_markup=kb)
        else:
            await update_or_query.message.reply_text(text, reply_markup=kb)

    elif step == STEP_LOCATION:
        text = (
            "-> Last step! Share your verified GPS location so we can match you with people nearby.\n\n"
            "📍 *How to share:*\n"
            "1️⃣ Make sure Location Services are *ON* in your phone settings\n"
            "2️⃣ Tap the *📍 Share My Real Location* button below\n"
            "3️⃣ Allow location access when prompted — your real GPS location will be sent securely\n\n"
            "⚠️ Only GPS-verified locations are accepted. Manual map pins are a Premium feature."
        )
        if edit:
            await update_or_query.message.reply_text(text, parse_mode="Markdown", reply_markup=location_request_keyboard())
        else:
            await update_or_query.message.reply_text(text, parse_mode="Markdown", reply_markup=location_request_keyboard())

async def require_onboarding_or_prompt(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    Call at the top of any protected action. Returns True if the user is fully
    onboarded. Otherwise sends the next required step and returns False.
    """
    user = update.effective_user
    db_user = get_user(user.id)
    if not db_user:
        upsert_user(user.id, user.username, user.first_name, user.language_code or "en")
        db_user = get_user(user.id)

    if is_onboarded(db_user):
        return True

    step = db_user["onboard_step"] or STEP_GENDER
    await update.message.reply_text(
        "⚠️ Please complete your profile setup first — this is required before you can use the bot."
    )
    await prompt_for_step(update, step, context)
    return False


# ─── COMMANDS ────────────────────────────────────────────────────────────────

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    lang = user.language_code or "en"
    upsert_user(user.id, user.username, user.first_name, lang)

    if context.args:
        try:
            referrer_id = int(context.args[0])
            if referrer_id != user.id:
                context.user_data["pending_referrer"] = referrer_id
                set_referrer(user.id, referrer_id)
        except ValueError:
            pass

    db_user = get_user(user.id)
    if is_onboarded(db_user):
        await update.message.reply_text(
            f"Welcome back, {user.first_name}! 👋\n\nUse the menu below or /begin to find a chat partner.",
            reply_markup=main_menu_keyboard()
        )
    else:
        step = db_user["onboard_step"] or STEP_GENDER
        await prompt_for_step(update, step, context)

async def begin(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await require_onboarding_or_prompt(update, context):
        return

    user_id = update.effective_user.id
    db_user = get_user(user_id)

    if get_active_match(user_id):
        await update.message.reply_text("You're already in a chat! Use /end or the 🚫 End chat button to leave first.")
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

        pending_ref = context.user_data.pop("pending_referrer", None)
        if pending_ref:
            bonus = 3 if db_user["gender"] == "Female" else 1
            add_bonus(pending_ref, bonus)
            try:
                await context.bot.send_message(
                    chat_id=pending_ref,
                    text=f"🎉 Your referral joined! You earned {bonus} bonus{'es' if bonus > 1 else ''}."
                )
            except Exception:
                pass
    else:
        bonuses = db_user["bonuses"] if db_user else 0
        add_to_queue(user_id, bonuses)
        looking = db_user["looking_for"]
        await update.message.reply_text(
            f"You are looking for a partner who is: *{looking}*",
            parse_mode="Markdown",
            reply_markup=main_menu_keyboard()
        )
        await update.message.reply_text("Rand Talk: Looking for a stranger for you 🤔")

        bot_info = await context.bot.get_me()
        ref_link = f"https://t.me/{bot_info.username}?start={user_id}"
        await update.message.reply_text(
            f"Chat lacks *females*! Send the link to your friends and earn 3 bonuses "
            f"for every invited female and 1 bonus for each male "
            f"(the more bonuses you have → the faster partner's search will be!)\n\n{ref_link}",
            parse_mode="Markdown"
        )

async def end_chat(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    partner_id = get_partner_id(user_id)
    remove_from_queue(user_id)

    if partner_id:
        end_match(user_id)
        await update.message.reply_text("Chat ended. Use /begin to find a new partner!", reply_markup=main_menu_keyboard())
        await context.bot.send_message(
            chat_id=partner_id,
            text="😾 Rand Talk: Your partner has left chat. Feel free to /begin a new one."
        )
    else:
        await update.message.reply_text("You're not in a chat right now.", reply_markup=main_menu_keyboard())

async def setup(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await require_onboarding_or_prompt(update, context):
        return
    await update.message.reply_text(
        "Ok, Let's get you setup. What do you like to configure?",
        reply_markup=setup_inline_keyboard()
    )

async def profile(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await require_onboarding_or_prompt(update, context):
        return
    user_id = update.effective_user.id
    u = get_user(user_id)
    await update.message.reply_text(
        f"👤 *Your Profile*\n\n"
        f"👤 Gender: {u['gender']}\n"
        f"❤️ Looking For: {u['looking_for']}\n"
        f"📍 City: {u['city'] or 'Not set'}\n"
        f"🔍 Search Scope: {u['search_scope']}\n"
        f"⭐ Bonuses: {u['bonuses']}",
        parse_mode="Markdown",
        reply_markup=main_menu_keyboard()
    )

async def exchange_username(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await require_onboarding_or_prompt(update, context):
        return
    user_id = update.effective_user.id
    partner_id = get_partner_id(user_id)
    if not partner_id:
        await update.message.reply_text("You're not in a chat. Use /begin first.")
        return

    context.bot_data.setdefault("exchange_requests", {})
    requests_map = context.bot_data["exchange_requests"]

    if requests_map.get(partner_id) == user_id:
        del requests_map[partner_id]
        user = get_user(user_id)
        partner = get_user(partner_id)
        u_name = f"@{user['username']}" if user["username"] else user["first_name"]
        p_name = f"@{partner['username']}" if partner["username"] else partner["first_name"]
        await update.message.reply_text(f"Your partner's username: {p_name}")
        await context.bot.send_message(chat_id=partner_id, text=f"Your partner's username: {u_name}")
    else:
        requests_map[user_id] = partner_id
        await update.message.reply_text("Username exchange request sent! Waiting for your partner to agree.")
        await context.bot.send_message(
            chat_id=partner_id,
            text="Your partner wants to exchange usernames. Use /exchange_username to accept."
        )

async def referral(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await require_onboarding_or_prompt(update, context):
        return
    user_id = update.effective_user.id
    bot_info = await context.bot.get_me()
    link = f"https://t.me/{bot_info.username}?start={user_id}"
    u = get_user(user_id)
    await update.message.reply_text(
        f"🔗 *Your Referral Link:*\n{link}\n\n"
        f"Earn *3 bonuses* for every female you invite and *1 bonus* for each male.\n"
        f"More bonuses = faster match!\n\n"
        f"⭐ Your current bonuses: {u['bonuses']}",
        parse_mode="Markdown",
        reply_markup=main_menu_keyboard()
    )

async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "🤖 *Rand Talk Help*\n\n"
        "/begin — Start looking for a chat partner\n"
        "/end — End the current chat\n"
        "/setup — Configure your profile\n"
        "/profile — Show your current profile\n"
        "/exchange\\_username — Exchange usernames with your partner\n"
        "/referral — Get your referral link\n"
        "/help — Show this help message\n\n"
        "💡 *Tip:* Earn bonuses by inviting friends to get matched faster!\n\n"
        "📍 *Matching:* By default you're matched with people near your shared location "
        "(within 50km). Change this in /setup → Search Scope.",
        parse_mode="Markdown",
        reply_markup=main_menu_keyboard()
    )


# ─── LOCATION HANDLER (WebApp) ───────────────────────────────────────────────
#
# Location now comes via a Telegram WebApp (location.html) that uses
# navigator.geolocation with enableHighAccuracy:true.
# Data arrives as update.message.web_app_data — this update type can ONLY
# originate from a WebApp button tap inside Telegram, so it cannot be
# spoofed via the attachment menu, manual pin drop, or any other Telegram
# location-sharing flow. No flag needed; the update type itself is the proof.

async def handle_webapp_location(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    raw = update.message.web_app_data.data

    try:
        data = __import__('json').loads(raw)
        lat = float(data["latitude"])
        lon = float(data["longitude"])
        acc = data.get("accuracy")
    except Exception as e:
        logger.warning(f"WebApp data parse error for user {user_id}: {e}")
        await update.message.reply_text(
            "❌ Something went wrong reading your location. Please try again.",
            reply_markup=location_request_keyboard()
        )
        return

    logger.info(f"WebApp location | user={user_id} | lat={lat} lon={lon} | accuracy={acc}m")

    city = reverse_geocode_city(lat, lon)
    update_user_location(user_id, lat, lon, city)

    db_user = get_user(user_id)

    if db_user["onboard_step"] != STEP_DONE:
        update_user_field(user_id, "onboard_step", STEP_DONE)
        await update.message.reply_text(
            f"✅ Location verified: *{city}*\n\n"
            f"🎉 Your profile is complete! Use the menu below or /begin to find a chat partner.",
            parse_mode="Markdown",
            reply_markup=main_menu_keyboard()
        )
    else:
        await update.message.reply_text(
            f"✅ Location updated: *{city}*",
            parse_mode="Markdown",
            reply_markup=main_menu_keyboard()
        )

async def handle_location_fallback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    Catches any location sent via the old Telegram location flows
    (attachment menu, manual pin, request_location button) and rejects them.
    """
    await update.message.reply_text(
        "🔒 Manual location entry is reserved for *Premium* users.\n\n"
        "Please tap the *📍 Share My Real Location* button below to share "
        "your GPS location securely through our verified location system.",
        parse_mode="Markdown",
        reply_markup=location_request_keyboard()
    )


# ─── TEXT BUTTON HANDLER (bottom menu) ───────────────────────────────────────

async def handle_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text

    if text == "🔍 Start looking":
        await begin(update, context)
    elif text == "🚫 End chat":
        await end_chat(update, context)
    elif text == "⚙️ Setup":
        await setup(update, context)
    elif text == "👤 Profile":
        await profile(update, context)
    elif text == "🔗 Referral":
        await referral(update, context)
    elif text == "❓ Help":
        await help_command(update, context)
    else:
        if not await require_onboarding_or_prompt(update, context):
            return
        user_id = update.effective_user.id
        partner_id = get_partner_id(user_id)
        if partner_id:
            try:
                await context.bot.send_message(chat_id=partner_id, text=text)
            except Exception as e:
                logger.error(f"Relay error: {e}")
                await update.message.reply_text("Could not deliver your message. Your partner may have left.")
        else:
            await update.message.reply_text(
                "You're not in a chat. Use the menu or /begin to find a partner.",
                reply_markup=main_menu_keyboard()
            )


# ─── MEDIA RELAY ─────────────────────────────────────────────────────────────

async def relay_media(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await require_onboarding_or_prompt(update, context):
        return
    user_id = update.effective_user.id
    partner_id = get_partner_id(user_id)

    if not partner_id:
        await update.message.reply_text(
            "You're not in a chat. Use /begin to find a partner.",
            reply_markup=main_menu_keyboard()
        )
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
        await update.message.reply_text("Could not deliver your message. Your partner may have left.")


# ─── CALLBACK HANDLER ────────────────────────────────────────────────────────

async def callback_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    user_id = query.from_user.id
    data = query.data

    # ── Onboarding: step 1 — gender ──
    if data.startswith("onb_gender_"):
        gender = data.replace("onb_gender_", "")
        update_user_field(user_id, "gender", gender)
        update_user_field(user_id, "onboard_step", STEP_LOOKING)
        await query.edit_message_text(f"Got it — you are *{gender}*.", parse_mode="Markdown")
        await context.bot.send_message(
            chat_id=user_id,
            text="-> Who are you looking for?",
            reply_markup=InlineKeyboardMarkup([[
                InlineKeyboardButton("👩 Female", callback_data="onb_looking_Female"),
                InlineKeyboardButton("👨 Male",   callback_data="onb_looking_Male"),
            ]])
        )
        return

    # ── Onboarding: step 2 — looking for ──
    if data.startswith("onb_looking_"):
        looking = data.replace("onb_looking_", "")
        update_user_field(user_id, "looking_for", looking)
        update_user_field(user_id, "onboard_step", STEP_LOCATION)
        await query.edit_message_text(f"Got it — looking for *{looking}*.", parse_mode="Markdown")
        # Send instructions as plain text first
        await context.bot.send_message(
            chat_id=user_id,
            text=(
                "-> Last step! Share your verified GPS location so we can match you with people nearby.\n\n"
                "📍 How to share:\n"
                "1️⃣ Make sure Location Services are ON in your phone settings\n"
                "2️⃣ Tap the Share My Real Location button below\n"
                "3️⃣ Allow location access when prompted\n\n"
                "⚠️ Only GPS-verified locations are accepted."
            )
        )
        # Send the WebApp button as a separate message
        await context.bot.send_message(
            chat_id=user_id,
            text="Tap the button below to share your location 👇",
            reply_markup=location_request_keyboard()
        )
        return

    # ── Setup menu ──
    if data == "setup_scope":
        buttons = [[InlineKeyboardButton(s, callback_data=f"set_scope_{s}")] for s in SEARCH_SCOPES]
        buttons.append([InlineKeyboardButton("⬅️ Back", callback_data="setup_back")])
        await query.edit_message_text("🔍 Choose your search scope:", reply_markup=InlineKeyboardMarkup(buttons))

    elif data == "setup_gender":
        buttons = [[InlineKeyboardButton(f"{'👩' if g == 'Female' else '👨'} {g}", callback_data=f"set_gender_{g}")] for g in GENDERS]
        buttons.append([InlineKeyboardButton("⬅️ Back", callback_data="setup_back")])
        await query.edit_message_text("👤 Choose your gender:", reply_markup=InlineKeyboardMarkup(buttons))

    elif data == "setup_looking":
        buttons = [[InlineKeyboardButton(f"{'👩' if g == 'Female' else '👨'} {g}", callback_data=f"set_looking_{g}")] for g in LOOKING_FOR]
        buttons.append([InlineKeyboardButton("⬅️ Back", callback_data="setup_back")])
        await query.edit_message_text("❤️ Who are you looking for?", reply_markup=InlineKeyboardMarkup(buttons))

    elif data == "setup_location":
        await query.edit_message_text(
            "📍 Tap the button below to verify and update your location.",
            parse_mode="Markdown"
        )
        await context.bot.send_message(
            chat_id=user_id,
            text="Share your updated location:",
            reply_markup=location_request_keyboard()
        )

    elif data == "setup_back":
        await query.edit_message_text(
            "Ok, Let's get you setup. What do you like to configure?",
            reply_markup=setup_inline_keyboard()
        )

    elif data == "setup_close":
        await query.edit_message_text("✅ Setup closed. Use /profile to view your settings.")

    # ── Apply settings ──
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
        await query.edit_message_text(f"✅ Search scope set to *{val}*", parse_mode="Markdown", reply_markup=setup_inline_keyboard())


# ─── SET BOT COMMANDS (slash menu) ───────────────────────────────────────────

async def post_init(app):
    await app.bot.set_my_commands([
        BotCommand("begin",            "Start looking for a chat partner"),
        BotCommand("end",              "End the current chat"),
        BotCommand("setup",            "Configure your profile"),
        BotCommand("profile",          "Show your current profile"),
        BotCommand("exchange_username","Exchange usernames with your partner"),
        BotCommand("referral",         "Get your referral link"),
        BotCommand("help",             "Show help"),
    ])


# ─── MAIN ────────────────────────────────────────────────────────────────────

def main():
    init_db()
    app = (
        Application.builder()
        .token(BOT_TOKEN)
        .post_init(post_init)
        .build()
    )

    app.add_handler(CommandHandler("start",            start))
    app.add_handler(CommandHandler("begin",            begin))
    app.add_handler(CommandHandler("end",              end_chat))
    app.add_handler(CommandHandler("setup",            setup))
    app.add_handler(CommandHandler("profile",          profile))
    app.add_handler(CommandHandler("exchange_username",exchange_username))
    app.add_handler(CommandHandler("referral",         referral))
    app.add_handler(CommandHandler("help",             help_command))
    app.add_handler(CallbackQueryHandler(callback_handler))

    # WebApp location data — the only accepted location source
    app.add_handler(MessageHandler(filters.StatusUpdate.WEB_APP_DATA, handle_webapp_location))

    # Reject any location sent via old Telegram flows (attachment menu, manual pin)
    app.add_handler(MessageHandler(filters.LOCATION, handle_location_fallback))

    # Text messages: menu buttons first, then relay
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text))

    # Media relay
    app.add_handler(MessageHandler(
        (filters.PHOTO | filters.Sticker.ALL | filters.VOICE | filters.VIDEO |
         filters.Document.ALL | filters.AUDIO | filters.VIDEO_NOTE) & ~filters.COMMAND,
        relay_media
    ))

    logger.info("Bot starting...")
    app.run_polling(drop_pending_updates=True)

if __name__ == "__main__":
    main()
