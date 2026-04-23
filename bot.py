import os
import asyncio
import logging
import json
import time
import requests
import websockets
from datetime import datetime, timezone

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)
logger = logging.getLogger(__name__)

TELEGRAM_BOT_TOKEN = os.environ["TELEGRAM_BOT_TOKEN"]
TELEGRAM_CHANNEL_ID = os.environ["TELEGRAM_CHANNEL_ID"]

# Official DexScreener WebSocket — real-time token profiles (DEX PAID)
WS_URL   = "wss://api.dexscreener.com/token-profiles/latest/v1"
PAIRS_URL = "https://api.dexscreener.com/latest/dex/tokens/{}"

ETH_CHAIN_IDS = {"ethereum", "eth", "1"}

seen_tokens: set[str] = set()


# ── Market data ────────────────────────────────────────────────────────────────

def get_token_market_data(token_address: str) -> dict:
    try:
        resp = requests.get(
            PAIRS_URL.format(token_address), timeout=10,
            headers={"User-Agent": "Mozilla/5.0"}
        )
        resp.raise_for_status()
        pairs = resp.json().get("pairs") or []
        if not pairs:
            return {}
        return sorted(pairs, key=lambda p: p.get("liquidity", {}).get("usd", 0), reverse=True)[0]
    except Exception as e:
        logger.warning("Market data failed for %s: %s", token_address, e)
        return {}


# ── Formatters ─────────────────────────────────────────────────────────────────

def fmt_number(value) -> str:
    try:
        v = float(value)
    except (TypeError, ValueError):
        return "N/A"
    if v >= 1_000_000_000: return f"${v/1_000_000_000:.2f}B"
    if v >= 1_000_000:     return f"${v/1_000_000:.2f}M"
    if v >= 1_000:         return f"${v/1_000:.2f}K"
    return f"${v:.2f}"


def fmt_price(value) -> str:
    try:
        v = float(value)
        if v < 0.000001: return f"${v:.10f}"
        if v < 0.01:     return f"${v:.6f}"
        return f"${v:.4f}"
    except (TypeError, ValueError):
        return "N/A"


def fmt_pct(v) -> str:
    if v is None: return "N/A"
    try:
        v = float(v)
        return f"{'🟢' if v >= 0 else '🔴'} {v:+.1f}%"
    except Exception:
        return "N/A"


def time_ago(unix_ms) -> str:
    try:
        diff = datetime.now(timezone.utc).timestamp() - (unix_ms / 1000)
        if diff < 60:   return f"{int(diff)}s ago"
        if diff < 3600: return f"{int(diff//60)}' {int(diff%60)}s ago"
        h = int(diff // 3600)
        if h < 24*30:   return f"{h}h {int((diff%3600)//60)}m ago"
        days = int(diff // 86400)
        if days < 365:  return f"{days}d ago"
        return f"{int(days//365)}y ago"
    except Exception:
        return "recently"


# ── Alert formatter ────────────────────────────────────────────────────────────

def format_alert(token: dict, market: dict) -> str:
    addr   = token.get("tokenAddress", "")
    chain  = token.get("chainId", "ethereum")
    ds_url = token.get("url", f"https://dexscreener.com/{chain}/{addr}")
    desc   = token.get("description", "")
    links  = token.get("links", [])

    base = market.get("baseToken", {})
    name = base.get("name") or desc[:30] or "Unknown"
    sym  = base.get("symbol", "???")

    dex        = (market.get("dexId") or "DEX").capitalize()
    created_at = market.get("pairCreatedAt")
    listed     = time_ago(created_at) if created_at else "N/A"
    pc         = market.get("priceChange") or {}

    site = tg = tw = None
    for lnk in links:
        lt, lu = lnk.get("type", "").lower(), lnk.get("url", "")
        if lt == "website"  and not site: site = lu
        if lt == "twitter"  and not tw:   tw   = lu
        if lt == "telegram" and not tg:   tg   = lu

    lines = [
        f"✅ *{name} | {sym}* (ethereum)",
        f"`{addr}`",
        "",
        "⏰ *DEX Time*",
        f"└ {dex} • {listed}",
        f"└ [DexScreener]({ds_url}) 🟢 *PAID*",
        "",
    ]

    social_rows = []
    if tg:   social_rows.append(f"└ [Telegram]({tg})")
    if tw:   social_rows.append(f"└ [Twitter]({tw})")
    if site: social_rows.append(f"└ [Website]({site})")
    if social_rows:
        lines += ["🔗 *Links*"] + social_rows + [""]

    lines += [
        "📊 *Market*",
        f"└ Cap: {fmt_number(market.get('marketCap') or market.get('fdv'))}",
        f"└ Price: {fmt_price(market.get('priceUsd'))}",
        f"└ Liq: {fmt_number((market.get('liquidity') or {}).get('usd'))}",
        f"└ Vol 24h: {fmt_number((market.get('volume') or {}).get('h24'))}",
        "",
        "📈 *Price Change*",
        f"└ 5m: {fmt_pct(pc.get('m5'))}  1h: {fmt_pct(pc.get('h1'))}  24h: {fmt_pct(pc.get('h24'))}",
    ]

    if desc:
        lines += ["", f"📝 _{desc[:200]}_"]

    return "\n".join(lines)


# ── Telegram ───────────────────────────────────────────────────────────────────

def send_telegram(text: str) -> bool:
    try:
        resp = requests.post(
            f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage",
            json={
                "chat_id": TELEGRAM_CHANNEL_ID,
                "text": text,
                "parse_mode": "Markdown",
                "disable_web_page_preview": True,
            },
            timeout=15,
        )
        if not resp.ok:
            logger.error("Telegram %s: %s", resp.status_code, resp.text[:200])
            return False
        return True
    except Exception as e:
        logger.error("Telegram send failed: %s", e)
        return False


# ── Token handler ──────────────────────────────────────────────────────────────

def handle_token(token: dict):
    addr  = token.get("tokenAddress", "")
    chain = str(token.get("chainId", "")).lower()

    if not addr or chain not in ETH_CHAIN_IDS:
        return

    if addr in seen_tokens:
        return

    seen_tokens.add(addr)
    logger.info("🆕 New ETH DEX PAID: %s", addr)

    market  = get_token_market_data(addr)
    message = format_alert(token, market)

    if send_telegram(message):
        logger.info("✅ Alert sent: %s", addr)
    else:
        logger.warning("❌ Failed to send alert: %s", addr)


# ── WebSocket listener ─────────────────────────────────────────────────────────

async def listen():
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/120.0.0.0 Safari/537.36",
        "Origin": "https://dexscreener.com",
    }

    reconnect_delay = 5

    while True:
        try:
            logger.info("Connecting to DexScreener WebSocket...")
            async with websockets.connect(WS_URL, additional_headers=headers, ping_interval=30, ping_timeout=10) as ws:
                logger.info("✅ WebSocket connected — streaming DEX PAID in real time!")
                reconnect_delay = 5  # reset on success

                async for raw_msg in ws:
                    try:
                        msg = json.loads(raw_msg)

                        # WebSocket sends: {"limit": 90, "data": [...tokens]}
                        data = msg.get("data") or []
                        if not isinstance(data, list):
                            # Might be a direct list
                            data = [msg] if isinstance(msg, dict) and "tokenAddress" in msg else []

                        for token in data:
                            handle_token(token)

                    except json.JSONDecodeError:
                        logger.warning("Non-JSON message: %s", raw_msg[:100])
                    except Exception as e:
                        logger.error("Error handling message: %s", e)

        except websockets.exceptions.ConnectionClosed as e:
            logger.warning("WebSocket closed: %s — reconnecting in %ds", e, reconnect_delay)
        except Exception as e:
            logger.error("WebSocket error: %s — reconnecting in %ds", e, reconnect_delay)

        await asyncio.sleep(reconnect_delay)
        reconnect_delay = min(reconnect_delay * 2, 60)  # exponential backoff, max 60s


# ── Seed existing tokens (no alerts) ──────────────────────────────────────────

def seed_existing_tokens():
    logger.info("Seeding existing DEX PAID tokens silently...")
    try:
        resp = requests.get(
            "https://api.dexscreener.com/token-profiles/latest/v1",
            timeout=15,
            headers={"User-Agent": "Mozilla/5.0"}
        )
        resp.raise_for_status()
        data = resp.json()
        tokens = data if isinstance(data, list) else data.get("data", [])
        for t in tokens:
            addr = t.get("tokenAddress", "")
            if addr:
                seen_tokens.add(addr)
        logger.info("Seeded %d tokens. Only NEW ones will trigger alerts.", len(seen_tokens))
    except Exception as e:
        logger.error("Seed failed: %s", e)


# ── Main ───────────────────────────────────────────────────────────────────────

async def main():
    logger.info("Starting DexScreener ETH DEX PAID WebSocket Bot")
    logger.info("Channel: %s", TELEGRAM_CHANNEL_ID)

    send_telegram(
        "🤖 *DexScreener ETH DEX PAID — WebSocket Mode*\n\n"
        "Connected to DexScreener real-time stream.\n"
        "Alerts fire instantly when new ETH tokens get DEX PAID ✅"
    )

    seed_existing_tokens()
    await listen()


if __name__ == "__main__":
    asyncio.run(main())
