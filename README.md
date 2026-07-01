# RandTalk Bot 🎲

A Telegram bot that matches random users for anonymous chats based on gender preference and city proximity.

## Features

- 🔀 Random stranger matching by gender preference and city (~50km radius)
- 🔒 Mandatory onboarding: gender → looking for → verified GPS location
- 📍 WebApp-based location verification (hardware GPS only, no manual pins)
- ⌨️ Persistent bottom menu + slash-command menu
- 🔗 Referral system with bonus-based priority matching
- 💬 Full message relay (text, photos, stickers, voice, video, documents)
- 🔄 Username exchange with mutual consent
- ⭐ Bonus-based priority queue

---

## Setup (full guide: GitHub + Railway)

### Step 1 — Create a Telegram Bot

1. Message [@BotFather](https://t.me/BotFather) on Telegram
2. Send `/newbot`, follow the prompts
3. Copy the token — you'll need it in Step 4

---

### Step 2 — Put everything on GitHub

1. Go to [github.com](https://github.com) → create a free account
2. Click **New repository** → name it `randtalk` → set **Public** → **Create**
3. Upload all files from this folder:
   - `bot.py`
   - `location.html`
   - `requirements.txt`
   - `Procfile`
   - `runtime.txt`
   - `.gitignore`
   - `README.md`

   The easiest way: click **Add file → Upload files** and drag everything in at once.

4. Click **Commit changes**

---

### Step 3 — Host `location.html` on GitHub Pages (free HTTPS)

1. In your repo, go to **Settings → Pages**
2. Under **Source**, select **Deploy from a branch**
3. Choose **main** branch, folder **/ (root)** → **Save**
4. After ~60 seconds, your page will be live at:
   ```
   https://YOUR_GITHUB_USERNAME.github.io/randtalk/location.html
   ```
   Copy this URL — you'll need it in Step 4.

---

### Step 4 — Deploy the bot on Railway (free, 24/7)

1. Go to [railway.app](https://railway.app) → sign up with your GitHub account
2. Click **New Project → Deploy from GitHub repo**
3. Select your `randtalk` repository
4. Railway will detect the `Procfile` and start deploying automatically

**Set environment variables** (Railway → your project → Variables tab):

| Variable | Value |
|---|---|
| `BOT_TOKEN` | Your token from BotFather |
| `WEBAPP_URL` | `https://YOUR_USERNAME.github.io/randtalk/location.html` |

5. Click **Deploy** — Railway will install dependencies and start the bot

The bot will now run 24/7. Every time you push an update to GitHub, Railway redeploys automatically.

---

### Step 5 — Persist the database (important!)

Railway's filesystem resets on every redeploy, which means `randtalk.db` gets wiped and all users lose their profiles. To fix this:

1. In Railway, go to your project → **New** → **Database → PostgreSQL**
   (or use Railway's built-in **Volume** feature to persist the SQLite file)
2. The simplest option: in Railway → your service → **Volumes** → add a volume mounted at `/app` — this keeps `randtalk.db` alive across deploys

---

## Commands

| Command | Description |
|---|---|
| `/start` | Register and onboard |
| `/begin` | Find a random chat partner |
| `/end` | End the current chat |
| `/setup` | Configure your profile |
| `/profile` | View your current profile |
| `/exchange_username` | Exchange usernames with your partner |
| `/referral` | Get your referral link |
| `/help` | Show help |

---

## How matching works

- Users share their GPS location via an in-app WebApp (hardware GPS, not manual pin)
- Location is reverse-geocoded to a city using OpenStreetMap Nominatim (free, no API key)
- Matching pairs users within ~50km using the Haversine distance formula
- Change `max_distance_km` in `find_match_in_queue()` in `bot.py` to adjust the radius
- Users with more referral bonuses get priority in the queue

## How location verification works

The "📍 Share My Real Location" button opens a Telegram WebApp (location.html).
Inside it, `navigator.geolocation` with `enableHighAccuracy: true` forces the
phone's hardware GPS. Data is returned via `tg.sendData()` which arrives as
`update.message.web_app_data` — an update type that is structurally impossible
to send from the attachment menu or any manual Telegram location flow.
This is the only accepted location source; all other location messages are rejected.
