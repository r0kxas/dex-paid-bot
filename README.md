# DexScreener ETH Paid Alerts — Telegram Bot

Monitors DexScreener for **new paid listings on Ethereum** and sends instant alerts to your Telegram channel.

---

## How It Works

- Polls `https://api.dexscreener.com/token-boosts/latest/v1` every 60 seconds
- Filters for `chainId == "ethereum"`
- On first start, seeds existing listings silently (no spam)
- Sends a formatted Telegram message for every **new** paid listing

---

## Setup Guide

### Step 1 — Create a Telegram Bot

1. Open Telegram and message [@BotFather](https://t.me/BotFather)
2. Send `/newbot` and follow the prompts
3. Copy the **bot token** (looks like `123456:ABC-DEF...`)

### Step 2 — Create a Telegram Channel

1. Create a new Telegram channel (public or private)
2. Add your bot as an **Administrator** with permission to post messages
3. Get your channel ID:
   - **Public channel:** use `@yourchannel` (with the @)
   - **Private channel:** forward a message from it to [@userinfobot](https://t.me/userinfobot) to get the numeric ID (e.g. `-1001234567890`)

### Step 3 — Deploy to Railway

1. Push this repo to GitHub
2. Go to [railway.app](https://railway.app) → **New Project** → **Deploy from GitHub repo**
3. Select your repo
4. Go to **Variables** tab and add:

| Variable | Value |
|---|---|
| `TELEGRAM_BOT_TOKEN` | Your bot token from BotFather |
| `TELEGRAM_CHANNEL_ID` | `@yourchannel` or `-1001234567890` |
| `CHECK_INTERVAL_SECONDS` | `60` (or lower for faster alerts) |

5. Railway will auto-detect the `Procfile` and deploy as a **worker** (no web server needed)
6. Click **Deploy** — done!

---

## Alert Format

```
🔥 New Paid Listing on Ethereum

📋 Contract: 0xabc...1234
💰 Boost Amount: 500 pts
📊 Total Boost: 1,200 pts
📝 Description: ...

🔗 View on DexScreener

🌐 Socials: 𝕏 Twitter | ✈️ Telegram | 🌐 Website

⏰ 2026-04-23 14:00 UTC
```

---

## Local Testing

```bash
pip install -r requirements.txt
cp .env.example .env
# Edit .env with your values
export $(cat .env | xargs)
python bot.py
```

---

## Configuration

| Env Var | Default | Description |
|---|---|---|
| `TELEGRAM_BOT_TOKEN` | required | Bot token from BotFather |
| `TELEGRAM_CHANNEL_ID` | required | Channel username or numeric ID |
| `CHECK_INTERVAL_SECONDS` | `60` | Polling interval in seconds |

---

## Notes

- Railway's free tier gives 500 hours/month — enough for 24/7 if you only have one worker
- The bot uses Railway's **worker** type (not a web service) — no sleep/wake issues
- DexScreener's public API has no auth required
