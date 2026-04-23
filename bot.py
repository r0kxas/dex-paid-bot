import os
import time
import logging
import requests
from datetime import datetime, timezone
from collections import deque

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)
logger = logging.getLogger(__name__)

TELEGRAM_BOT_TOKEN = os.environ["TELEGRAM_BOT_TOKEN"]
TELEGRAM_CHANNEL_ID = os.environ["TELEGRAM_CHANNEL_ID"]
CHECK_INTERVAL = int(os.getenv("CHECK_INTERVAL_SECONDS", "30"))

BOOSTS_URL = "https://api.dexscreener.com/token-boosts/latest/v1"
PAIRS_URL  = "https://api.dexscreener.com/latest/dex/tokens/{}"

ETH_CHAIN_IDS = {"ethereum", "eth", "1"}

seen_tokens: set[str] = set()

# --- Rate limiter ---
class RateLimiter:
    """Sliding window rate limiter."""
    def __init__(self, max_calls: int, period: float):
        self.max_calls = max_calls
        self.period = period      # seconds
        self.calls: deque = deque()

    def wait(self):
        now = time.monotonic()
        # Drop calls outside the window
        while self.calls and now - self.calls[0] >= self.period:
            self.calls.popleft()
        if len(self.calls) >= self.max_calls:
            sleep_for = self.period - (now - self.calls[0])
            if sleep_for > 0:
                logger.debug("Rate limit: sleeping %.2fs", sleep_for)
                time.sleep(sleep_for)
        self.calls.append(time.monotonic())

# 60 req/min for boosts, 300 req/min for pairs — use 80% of limit to be safe
boosts_limiter = RateLimiter(max_calls=48, period=60)   # 80% of 60
pairs_limiter  = RateLimiter(max_calls=240, period=60)  # 80% of 300


def get_token_market_data(token_address: str) -> dict:
    pairs_limiter.wait()
    try:
        resp = requests.get(
            PAIRS_URL.format(token_address),
            timeout=10,
            headers={"User-Agent": "Mozilla/5.0"},
        )
        resp.raise_for_status()
        pairs = resp.json().get("pairs") or []
        if not pairs:
            return {}
        return sorted(pairs, key=lambda p: p.get("liquidity", {}).get("usd", 0), reverse=True)[0]
    except Exception as e:
        logger.warning("Market data fetch failed for %s: %s", token_address, e)
        return {}


def fmt_number(value) -> str:
    try:
        v = float(value)
    except (TypeError, ValueError):
        return "N/A"
    if v >= 1_000_000_000:
        return f"${v/1_000_000_000:.2f}B"
    if v >= 1_000_000:
        return f"${v/1_000_000:.2f}M"
    if v >= 1_000:
        return f"${v/1_000:.2f}K"
    return f"${v:.2f}"


def fmt_price(value) -> str:
    try:
        v = float(value)
        if v < 0.000001:
            return f"${v:.10f}"
        if v < 0.01:
            return f"${v:.6f}"
        return f"${v:.4f}"
    except (TypeError, ValueError):
        return "N/A"


def fmt_pct(v) -> str:
    if v is None:
        return "N/A"
    try:
        v = float(v)
        icon = "🟢" if v >= 0 else "🔴"
        return f"{icon} {v:+.1f}%"
    except Exception:
        return "N/A"


def time_ago(unix_ms) -> str:
    try:
        diff = datetime.now(timezone.utc).timestamp() - (unix_ms / 1000)
        if diff < 60:
            return f"{int(diff)}s ago"
        if diff < 3600:
            return f"{int(diff//60)}' {int(diff%60)}s ago"
        return f"{int(diff//3600)}h {int((diff%3600)//60)}m ago"
    except Exception:
        return "recently"


def format_alert(token: dict, market: dict) -> str:
    addr      = token.get("tokenAddress", "")
    chain     = token.get("chainId", "ethereum")
    ds_url    = token.get("url", f"https://dexscreener.com/{chain}/{addr}")
    desc      = token.get("description", "")
    links     = token.get("links", [])

    base  = market.get("baseToken", {})
    name  = base.get("name") or desc[:30] or "Unknown"
    sym   = base.get("symbol", "???")

    dex        = (market.get("dexId") or "DEX").capitalize()
    created_at = market.get("pairCreatedAt")
    listed     = time_ago(created_at) if created_at else "N/A"

    pc = market.get("priceChange") or {}

    # Socials
    site = tg = tw = None
    for lnk in links:
        lt, lu = lnk.get("type","").lower(), lnk.get("url","")
        if lt == "website"  and not site: site = lu
        if lt == "twitter"  and not tw:   tw   = lu
        if lt == "telegram" and not tg:   tg   = lu

    lines = [
        f"🔥 *{name} | {sym}* (ethereum)",
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
    except requests.RequestException as e:
        logger.error("Telegram send failed: %s", e)
        return False


def get_paid_eth_tokens() -> list[dict]:
    boosts_limiter.wait()
    try:
        resp = requests.get(
            BOOSTS_URL,
            timeout=15,
            headers={"User-Agent": "Mozilla/5.0", "Accept": "application/json"},
        )
        resp.raise_for_status()
        data = resp.json()

        if isinstance(data, list):
            all_tokens = data
        elif isinstance(data, dict):
            all_tokens = data.get("data") or data.get("tokens") or []
        else:
            logger.warning("Unexpected API format: %s", type(data))
            return []

        chains = {t.get("chainId","") for t in all_tokens}
        logger.info("API returned %d tokens across chains: %s", len(all_tokens), chains)

        eth = [t for t in all_tokens if str(t.get("chainId","")).lower() in ETH_CHAIN_IDS]
        logger.info("ETH paid tokens: %d", len(eth))
        return eth

    except Exception as e:
        logger.error("DexScreener fetch error: %s", e)
        return []


def check_and_alert():
    tokens = get_paid_eth_tokens()
    new_count = 0

    for token in tokens:
        addr = token.get("tokenAddress", "")
        if not addr or addr in seen_tokens:
            continue

        seen_tokens.add(addr)
        market  = get_token_market_data(addr)
        message = format_alert(token, market)

        if send_telegram(message):
            logger.info("✅ Sent alert: %s", addr)
            new_count += 1
        else:
            logger.warning("❌ Failed alert: %s", addr)

        time.sleep(1)  # small buffer between Telegram messages

    logger.info("Cycle done — %d new alerts.", new_count)


def main():
    logger.info("Starting | channel=%s interval=%ds", TELEGRAM_CHANNEL_ID, CHECK_INTERVAL)

    send_telegram(
        f"🤖 *DexScreener ETH Paid Alerts — Live*\n\n"
        f"Monitoring Ethereum paid listings every {CHECK_INTERVAL}s\n"
        f"Rate limits: 48 req/min (boosts) · 240 req/min (pairs)"
    )

    # Seed without alerting
    logger.info("Seeding existing tokens (silent)...")
    for t in get_paid_eth_tokens():
        if addr := t.get("tokenAddress"):
            seen_tokens.add(addr)
    logger.info("Seeded %d tokens. Watching for new ones...", len(seen_tokens))

    while True:
        try:
            check_and_alert()
        except Exception as e:
            logger.exception("Loop error: %s", e)
        time.sleep(CHECK_INTERVAL)


if __name__ == "__main__":
    main()
