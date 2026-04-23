import os
import asyncio
import logging
import json
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

# Both WebSocket endpoints — run simultaneously
WS_LATEST_URL  = "wss://api.dexscreener.com/token-profiles/latest/v1"         # new DEX PAIDs
WS_UPDATES_URL = "wss://api.dexscreener.com/token-profiles/recent-updates/v1"  # social updates
PAIRS_URL      = "https://api.dexscreener.com/latest/dex/tokens/{}"

ETH_CHAIN_IDS = {"ethereum", "eth", "1"}

# seen_tokens: addr -> last alert timestamp (allows re-alerting if socials update)
alerted_tokens: dict[str, float] = {}

WS_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/120.0.0.0 Safari/537.36",
    "Origin": "https://dexscreener.com",
}


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
        if diff < 60:    return f"{int(diff)}s ago"
        if diff < 3600:  return f"{int(diff//60)}' {int(diff%60)}s ago"
        h = int(diff // 3600)
        if h < 24*30:   return f"{h}h {int((diff%3600)//60)}m ago"
        days = int(diff // 86400)
        if days < 365:  return f"{days}d ago"
        return f"{int(days//365)}y {int((days%365)//30)}mo ago"
    except Exception:
        return "recently"


# ── Alert formatter ────────────────────────────────────────────────────────────

def format_alert(token: dict, market: dict, is_update: bool = False) -> str:
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

    header = "🔄 *DEX PAID — Social Update*" if is_update else "✅ *New DEX PAID Listing*"

    lines = [
        header,
        f"*{name} | {sym}* (ethereum)",
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
    else:
        lines += ["🔗 *Links* — none yet\n"]

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

def handle_token(token: dict, is_update: bool = False):
    addr  = token.get("tokenAddress", "")
    chain = str(token.get("chainId", "")).lower()

    if not addr or chain not in ETH_CHAIN_IDS:
        return

    now = __import__("time").time()

    # For updates: only re-alert if we haven't alerted this token in last 10 mins
    if addr in alerted_tokens:
        if not is_update:
            return  # already alerted for new listing
        if now - alerted_tokens[addr] < 600:
            return  # too soon for another update alert

    alerted_tokens[addr] = now
    source = "UPDATE" if is_update else "NEW"
    logger.info("🆕 ETH DEX PAID [%s]: %s", source, addr)

    market  = get_token_market_data(addr)
    message = format_alert(token, market, is_update=is_update)

    if send_telegram(message):
        logger.info("✅ Alert sent [%s]: %s", source, addr)
    else:
        logger.warning("❌ Failed [%s]: %s", source, addr)


# ── WebSocket listener ─────────────────────────────────────────────────────────

async def ws_listener(url: str, label: str, is_update: bool, seed_on_connect: bool = False):
    delay = 3
    while True:
        try:
            logger.info("[%s] Connecting to %s ...", label, url)
            async with websockets.connect(
                url,
                additional_headers=WS_HEADERS,
                ping_interval=20,
                ping_timeout=10,
                max_size=10 * 1024 * 1024,
            ) as ws:
                logger.info("[%s] ✅ Connected!", label)
                delay = 3  # reset backoff

                first_msg = True
                async for raw_msg in ws:
                    try:
                        msg = json.loads(raw_msg)
                        tokens = msg.get("data") or []

                        if first_msg and seed_on_connect:
                            # Seed existing tokens silently on first message
                            first_msg = False
                            for t in tokens:
                                addr = t.get("tokenAddress", "")
                                if addr:
                                    alerted_tokens[addr] = __import__("time").time()
                            logger.info("[%s] Seeded %d existing tokens silently", label, len(tokens))
                            continue

                        first_msg = False
                        for token in tokens:
                            handle_token(token, is_update=is_update)

                    except json.JSONDecodeError:
                        logger.warning("[%s] Non-JSON: %s", label, str(raw_msg)[:80])
                    except Exception as e:
                        logger.error("[%s] Message error: %s", label, e)

        except websockets.exceptions.ConnectionClosed as e:
            logger.warning("[%s] Closed: %s — retry in %ds", label, e, delay)
        except Exception as e:
            logger.error("[%s] Error: %s — retry in %ds", label, e, delay)

        await asyncio.sleep(delay)
        delay = min(delay * 2, 60)


# ── Main ───────────────────────────────────────────────────────────────────────

async def main():
    logger.info("Starting DexScreener ETH DEX PAID Bot — Dual WebSocket Mode")
    logger.info("Channel: %s", TELEGRAM_CHANNEL_ID)

    send_telegram(
        "🤖 *DexScreener ETH DEX PAID — Live*\n\n"
        "📡 Connected to TWO real-time streams:\n"
        "• `latest/v1` — new DEX PAID listings\n"
        "• `recent-updates/v1` — social info updates\n\n"
        "Alerts fire instantly ✅"
    )

    # Run both WebSocket listeners concurrently
    await asyncio.gather(
        ws_listener(WS_LATEST_URL,  label="LATEST",  is_update=False, seed_on_connect=True),
        ws_listener(WS_UPDATES_URL, label="UPDATES", is_update=True,  seed_on_connect=True),
    )


if __name__ == "__main__":
    asyncio.run(main())
