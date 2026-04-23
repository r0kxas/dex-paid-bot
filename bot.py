import os
import time
import json
import logging
import requests
from datetime import datetime

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)
logger = logging.getLogger(__name__)

TELEGRAM_BOT_TOKEN = os.environ["TELEGRAM_BOT_TOKEN"]
TELEGRAM_CHANNEL_ID = os.environ["TELEGRAM_CHANNEL_ID"]
CHECK_INTERVAL = int(os.getenv("CHECK_INTERVAL_SECONDS", "60"))

DEXSCREENER_PAID_URL = "https://api.dexscreener.com/token-boosts/latest/v1"

seen_tokens: set[str] = set()


def get_paid_eth_tokens() -> list[dict]:
    """Fetch latest paid/boosted tokens and filter for ETH chain."""
    try:
        resp = requests.get(DEXSCREENER_PAID_URL, timeout=15)
        resp.raise_for_status()
        data = resp.json()

        # API returns a list of token boost objects
        if not isinstance(data, list):
            logger.warning("Unexpected API response format: %s", type(data))
            return []

        eth_tokens = [
            token for token in data
            if token.get("chainId", "").lower() == "ethereum"
        ]
        logger.info("Found %d ETH paid tokens out of %d total", len(eth_tokens), len(data))
        return eth_tokens

    except requests.RequestException as e:
        logger.error("Failed to fetch DexScreener data: %s", e)
        return []
    except json.JSONDecodeError as e:
        logger.error("Failed to parse DexScreener response: %s", e)
        return []


def format_alert(token: dict) -> str:
    """Format a token alert message for Telegram."""
    token_address = token.get("tokenAddress", "N/A")
    url = token.get("url", f"https://dexscreener.com/ethereum/{token_address}")
    description = token.get("description", "")
    links = token.get("links", [])
    amount = token.get("amount", 0)
    total_amount = token.get("totalAmount", 0)

    # Build social links
    socials = []
    for link in links:
        link_type = link.get("type", "").lower()
        link_url = link.get("url", "")
        label = link.get("label", link_type).capitalize()
        if link_url:
            if link_type == "twitter":
                socials.append(f"[𝕏 Twitter]({link_url})")
            elif link_type == "telegram":
                socials.append(f"[✈️ Telegram]({link_url})")
            elif link_type == "website":
                socials.append(f"[🌐 Website]({link_url})")
            else:
                socials.append(f"[{label}]({link_url})")

    socials_str = "  |  ".join(socials) if socials else "—"

    short_addr = f"{token_address[:6]}...{token_address[-4:]}" if len(token_address) > 12 else token_address

    lines = [
        "🔥 *New Paid Listing on Ethereum*",
        "",
        f"📋 *Contract:* `{token_address}`",
        f"💰 *Boost Amount:* {amount:,} pts",
        f"📊 *Total Boost:* {total_amount:,} pts",
    ]

    if description:
        lines.append(f"📝 *Description:* {description[:200]}")

    lines += [
        "",
        f"🔗 [View on DexScreener]({url})",
        "",
        f"🌐 *Socials:* {socials_str}",
        "",
        f"⏰ {datetime.utcnow().strftime('%Y-%m-%d %H:%M UTC')}",
    ]

    return "\n".join(lines)


def send_telegram_message(text: str) -> bool:
    """Send a message to the Telegram channel."""
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    payload = {
        "chat_id": TELEGRAM_CHANNEL_ID,
        "text": text,
        "parse_mode": "Markdown",
        "disable_web_page_preview": False,
    }
    try:
        resp = requests.post(url, json=payload, timeout=15)
        resp.raise_for_status()
        return True
    except requests.RequestException as e:
        logger.error("Failed to send Telegram message: %s", e)
        return False


def check_and_alert():
    """Main check loop iteration."""
    tokens = get_paid_eth_tokens()

    new_count = 0
    for token in tokens:
        token_address = token.get("tokenAddress", "")
        if not token_address:
            continue

        if token_address in seen_tokens:
            continue

        seen_tokens.add(token_address)
        message = format_alert(token)
        success = send_telegram_message(message)

        if success:
            logger.info("Alert sent for token: %s", token_address)
            new_count += 1
        else:
            logger.warning("Failed to send alert for token: %s", token_address)

        time.sleep(1)  # Small delay between messages to avoid Telegram rate limits

    if new_count == 0:
        logger.info("No new ETH paid tokens found.")
    else:
        logger.info("Sent %d new alerts.", new_count)


def send_startup_message():
    """Send a startup notification to the channel."""
    msg = (
        "🤖 *DexScreener ETH Paid Alerts Bot Started*\n\n"
        "Monitoring for new paid listings on Ethereum chain...\n"
        f"Check interval: every {CHECK_INTERVAL}s"
    )
    send_telegram_message(msg)


def main():
    logger.info("Starting DexScreener ETH Paid Alerts Bot...")
    logger.info("Channel: %s | Interval: %ds", TELEGRAM_CHANNEL_ID, CHECK_INTERVAL)

    send_startup_message()

    # On first run, seed seen_tokens without alerting (avoid spamming existing listings)
    logger.info("Seeding existing paid tokens (no alerts for these)...")
    existing = get_paid_eth_tokens()
    for token in existing:
        addr = token.get("tokenAddress", "")
        if addr:
            seen_tokens.add(addr)
    logger.info("Seeded %d existing ETH paid tokens.", len(seen_tokens))

    logger.info("Now watching for NEW paid listings. Bot is live!")

    while True:
        try:
            check_and_alert()
        except Exception as e:
            logger.exception("Unexpected error in check loop: %s", e)

        time.sleep(CHECK_INTERVAL)


if __name__ == "__main__":
    main()
