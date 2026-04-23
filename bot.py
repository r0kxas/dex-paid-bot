import os
import time
import json
import logging
import requests
from datetime import datetime, timezone

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)
logger = logging.getLogger(__name__)

TELEGRAM_BOT_TOKEN = os.environ["TELEGRAM_BOT_TOKEN"]
TELEGRAM_CHANNEL_ID = os.environ["TELEGRAM_CHANNEL_ID"]
CHECK_INTERVAL = int(os.getenv("CHECK_INTERVAL_SECONDS", "30"))

BOOSTS_URL = "https://api.dexscreener.com/token-boosts/latest/v1"
PAIRS_URL = "https://api.dexscreener.com/latest/dex/tokens/{}"

ETH_CHAIN_IDS = {"ethereum", "eth", "1"}

seen_tokens: set[str] = set()


def get_token_market_data(token_address: str) -> dict:
    try:
        url = PAIRS_URL.format(token_address)
        resp = requests.get(url, timeout=10, headers={"User-Agent": "Mozilla/5.0"})
        resp.raise_for_status()
        data = resp.json()
        pairs = data.get("pairs") or []
        if not pairs:
            return {}
        best = sorted(pairs, key=lambda p: p.get("liquidity", {}).get("usd", 0), reverse=True)[0]
        return best
    except Exception as e:
        logger.warning("Could not fetch market data for %s: %s", token_address, e)
        return {}


def format_number(value) -> str:
    try:
        value = float(value)
    except (TypeError, ValueError):
        return "N/A"
    if value >= 1_000_000:
        return f"${value/1_000_000:.2f}M"
    elif value >= 1_000:
        return f"${value/1_000:.2f}K"
    else:
        return f"${value:.2f}"


def format_price(value) -> str:
    try:
        value = float(value)
        if value < 0.000001:
            return f"${value:.10f}"
        elif value < 0.01:
            return f"${value:.6f}"
        else:
            return f"${value:.4f}"
    except (TypeError, ValueError):
        return "N/A"


def time_ago(unix_ms: int) -> str:
    try:
        now = datetime.now(timezone.utc).timestamp()
        diff = now - (unix_ms / 1000)
        if diff < 60:
            return f"{int(diff)}s ago"
        elif diff < 3600:
            m = int(diff // 60)
            s = int(diff % 60)
            return f"{m}' {s}s ago"
        else:
            h = int(diff // 3600)
            m = int((diff % 3600) // 60)
            return f"{h}h {m}m ago"
    except Exception:
        return "recently"


def format_alert(token: dict, market: dict) -> str:
    token_address = token.get("tokenAddress", "")
    chain_id = token.get("chainId", "ethereum")
    ds_url = token.get("url", f"https://dexscreener.com/{chain_id}/{token_address}")
    description = token.get("description", "")
    links = token.get("links", [])

    base_token = market.get("baseToken", {})
    name = base_token.get("name", description[:30] if description else "Unknown")
    symbol = base_token.get("symbol", "???")

    price_usd = format_price(market.get("priceUsd"))
    mcap = format_number(market.get("marketCap") or market.get("fdv"))
    volume_24h = format_number((market.get("volume") or {}).get("h24"))
    liquidity = format_number((market.get("liquidity") or {}).get("usd"))

    price_change = market.get("priceChange") or {}
    change_5m = price_change.get("m5")
    change_1h = price_change.get("h1")
    change_24h = price_change.get("h24")

    def pct(v):
        if v is None:
            return "N/A"
        try:
            v = float(v)
            arrow = "🟢" if v >= 0 else "🔴"
            return f"{arrow} {v:+.1f}%"
        except Exception:
            return "N/A"

    dex_id = market.get("dexId", "").capitalize() or "DEX"
    pair_created_at = market.get("pairCreatedAt")
    listed_ago = time_ago(pair_created_at) if pair_created_at else "N/A"

    website = telegram_link = twitter_link = None
    for link in links:
        lt = link.get("type", "").lower()
        lu = link.get("url", "")
        if lt == "website" and not website:
            website = lu
        elif lt == "twitter" and not twitter_link:
            twitter_link = lu
        elif lt == "telegram" and not telegram_link:
            telegram_link = lu

    lines = [
        f"🔥 *{name} | {symbol}* (ethereum)",
        f"`{token_address}`",
        "",
        f"⏰ *DEX Time*",
        f"└ {dex_id} • {listed_ago}",
        f"└ [DexScreener]({ds_url}) 🟢 *PAID*",
        "",
    ]

    social_lines = []
    if telegram_link:
        social_lines.append(f"└ [Telegram]({telegram_link})")
    if twitter_link:
        social_lines.append(f"└ [Twitter]({twitter_link})")
    if website:
        social_lines.append(f"└ [Website]({website})")

    if social_lines:
        lines.append("🔗 *Links*")
        lines.extend(social_lines)
        lines.append("")

    lines += [
        f"📊 *Market*",
        f"└ Cap: {mcap}",
        f"└ Price: {price_usd}",
        f"└ Liq: {liquidity}",
        f"└ Vol 24h: {volume_24h}",
        "",
        f"📈 *Price Change*",
        f"└ 5m: {pct(change_5m)}  1h: {pct(change_1h)}  24h: {pct(change_24h)}",
    ]

    if description:
        lines += ["", f"📝 _{description[:200]}_"]

    return "\n".join(lines)


def send_telegram_message(text: str) -> bool:
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    payload = {
        "chat_id": TELEGRAM_CHANNEL_ID,
        "text": text,
        "parse_mode": "Markdown",
        "disable_web_page_preview": True,
    }
    try:
        resp = requests.post(url, json=payload, timeout=15)
        if not resp.ok:
            logger.error("Telegram error %s: %s", resp.status_code, resp.text)
            return False
        return True
    except requests.RequestException as e:
        logger.error("Failed to send Telegram message: %s", e)
        return False


def get_paid_eth_tokens() -> list[dict]:
    try:
        headers = {"User-Agent": "Mozilla/5.0", "Accept": "application/json"}
        resp = requests.get(BOOSTS_URL, timeout=15, headers=headers)
        resp.raise_for_status()
        data = resp.json()

        if isinstance(data, list):
            token_list = data
        elif isinstance(data, dict):
            token_list = data.get("data") or data.get("tokens") or []
        else:
            logger.warning("Unexpected API format: %s", type(data))
            return []

        chain_ids = {t.get("chainId", "") for t in token_list}
        logger.info("Total tokens: %d | Chains: %s", len(token_list), chain_ids)

        eth_tokens = [
            t for t in token_list
            if str(t.get("chainId", "")).lower() in ETH_CHAIN_IDS
        ]
        logger.info("ETH paid tokens: %d", len(eth_tokens))
        return eth_tokens

    except Exception as e:
        logger.error("Error fetching DexScreener: %s", e)
        return []


def check_and_alert():
    tokens = get_paid_eth_tokens()
    new_count = 0

    for token in tokens:
        addr = token.get("tokenAddress", "")
        if not addr or addr in seen_tokens:
            continue

        seen_tokens.add(addr)
        market = get_token_market_data(addr)
        message = format_alert(token, market)
        success = send_telegram_message(message)

        if success:
            logger.info("✅ Alert sent: %s", addr)
            new_count += 1
        else:
            logger.warning("❌ Failed alert: %s", addr)

        time.sleep(1.5)

    if new_count == 0:
        logger.info("No new ETH paid tokens this cycle.")
    else:
        logger.info("Sent %d new alerts.", new_count)


def send_startup_message():
    msg = (
        "🤖 *DexScreener ETH Paid Alerts — Live*\n\n"
        f"Monitoring Ethereum paid listings every {CHECK_INTERVAL}s\n"
        "Alerts will appear when new tokens get a paid listing."
    )
    send_telegram_message(msg)


def main():
    logger.info("Bot starting... Channel: %s | Interval: %ds", TELEGRAM_CHANNEL_ID, CHECK_INTERVAL)
    send_startup_message()

    logger.info("Seeding existing paid tokens (no alerts for these)...")
    existing = get_paid_eth_tokens()
    for t in existing:
        addr = t.get("tokenAddress", "")
        if addr:
            seen_tokens.add(addr)
    logger.info("Seeded %d tokens. Watching for NEW listings...", len(seen_tokens))

    while True:
        try:
            check_and_alert()
        except Exception as e:
            logger.exception("Unexpected error: %s", e)
        time.sleep(CHECK_INTERVAL)


if __name__ == "__main__":
    main()
