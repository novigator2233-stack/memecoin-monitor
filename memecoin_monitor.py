#!/usr/bin/env python3
"""
Memecoin Monitor Bot
---------------------
Monitors:
  1. Pump.fun — brand-new token launches on Solana
  2. Dexscreener — trending tokens with moderate liquidity/volume filters
  3. Crypto news — RSS feeds (CoinDesk, Cointelegraph, Decrypt)

Sends alerts to a Telegram chat. This bot NEVER trades automatically —
it only monitors and notifies. You decide what to do with each alert.

Usage:
    Either set environment variables:
        export TELEGRAM_BOT_TOKEN="123456:ABC..."
        export TELEGRAM_CHAT_ID="123456789"
    Or put them in config.json next to this script:
        {"TELEGRAM_BOT_TOKEN": "...", "TELEGRAM_CHAT_ID": "..."}
    Then:
        python3 memecoin_monitor.py
"""

import os
import sys
import time
import json
import logging
import requests
import feedparser
from datetime import datetime, timezone

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

_CONFIG_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "config.json")
_file_config = {}
if os.path.exists(_CONFIG_PATH):
    try:
        with open(_CONFIG_PATH, "r") as _f:
            _file_config = json.load(_f)
    except Exception:
        _file_config = {}

TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN") or _file_config.get("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID") or _file_config.get("TELEGRAM_CHAT_ID", "")

POLL_INTERVAL_SECONDS = 5 * 60  # every 5 minutes, as requested

# "Moderate filter" thresholds, as requested
MIN_LIQUIDITY_USD = 5_000
MIN_VOLUME_24H_USD = 10_000
MAX_TOKEN_AGE_HOURS = 24

DEXSCREENER_SEARCH_URL = "https://api.dexscreener.com/latest/dex/search"
DEXSCREENER_TOKEN_PROFILES_URL = "https://api.dexscreener.com/token-profiles/latest/v1"
PUMPFUN_NEW_URL = "https://frontend-api.pump.fun/coins?offset=0&limit=30&sort=created_timestamp&order=DESC"

NEWS_FEEDS = [
    ("CoinDesk", "https://www.coindesk.com/arc/outboundfeeds/rss/"),
    ("Cointelegraph", "https://cointelegraph.com/rss"),
    ("Decrypt", "https://decrypt.co/feed"),
]

STATE_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "monitor_state.json")

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[logging.StreamHandler(sys.stdout)],
)
log = logging.getLogger("memecoin_monitor")


# ---------------------------------------------------------------------------
# State persistence (so we don't re-alert on the same items after a restart)
# ---------------------------------------------------------------------------

def load_state():
    if os.path.exists(STATE_FILE):
        try:
            with open(STATE_FILE, "r") as f:
                return json.load(f)
        except Exception:
            pass
    return {"seen_tokens": [], "seen_news": []}


def save_state(state):
    # Keep the seen-lists from growing forever
    state["seen_tokens"] = state["seen_tokens"][-500:]
    state["seen_news"] = state["seen_news"][-200:]
    with open(STATE_FILE, "w") as f:
        json.dump(state, f)


# ---------------------------------------------------------------------------
# Telegram
# ---------------------------------------------------------------------------

def send_telegram(message: str):
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        log.warning("Telegram not configured — printing alert instead:\n%s", message)
        return
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    try:
        resp = requests.post(
            url,
            data={
                "chat_id": TELEGRAM_CHAT_ID,
                "text": message,
                "parse_mode": "HTML",
                "disable_web_page_preview": False,
            },
            timeout=15,
        )
        if resp.status_code != 200:
            log.error("Telegram send failed: %s %s", resp.status_code, resp.text)
    except Exception as e:
        log.error("Telegram send error: %s", e)


# ---------------------------------------------------------------------------
# Pump.fun — newly created tokens
# ---------------------------------------------------------------------------

def check_pumpfun(state):
    try:
        resp = requests.get(PUMPFUN_NEW_URL, timeout=15, headers={"User-Agent": "Mozilla/5.0"})
        if resp.status_code != 200:
            log.warning("Pump.fun API returned %s", resp.status_code)
            return
        coins = resp.json()
    except Exception as e:
        log.error("Pump.fun fetch error: %s", e)
        return

    for coin in coins:
        mint = coin.get("mint")
        if not mint or mint in state["seen_tokens"]:
            continue

        name = coin.get("name", "?")
        symbol = coin.get("symbol", "?")
        market_cap = coin.get("usd_market_cap", 0) or 0
        created_ts = coin.get("created_timestamp", 0) or 0

        # Only alert on tokens with at least some initial traction,
        # to avoid flooding you with every single brand-new token.
        if market_cap < 3_000:
            state["seen_tokens"].append(mint)
            continue

        age_min = "?"
        if created_ts:
            age_sec = time.time() - (created_ts / 1000 if created_ts > 1e12 else created_ts)
            age_min = f"{age_sec/60:.0f} мин назад"

        msg = (
            f"🆕 <b>Новый токен на Pump.fun</b>\n"
            f"<b>{name}</b> (${symbol})\n"
            f"Market cap: ${market_cap:,.0f}\n"
            f"Создан: {age_min}\n"
            f"Mint: <code>{mint}</code>\n"
            f"🔗 https://pump.fun/{mint}\n"
            f"📊 https://dexscreener.com/solana/{mint}"
        )
        send_telegram(msg)
        state["seen_tokens"].append(mint)
        log.info("Alert sent: new pump.fun token %s (%s)", name, symbol)


# ---------------------------------------------------------------------------
# Dexscreener — trending tokens with moderate liquidity/volume filter
# ---------------------------------------------------------------------------

def check_dexscreener_trending(state):
    try:
        resp = requests.get(
            DEXSCREENER_TOKEN_PROFILES_URL, timeout=15, headers={"User-Agent": "Mozilla/5.0"}
        )
        if resp.status_code != 200:
            log.warning("Dexscreener profiles API returned %s", resp.status_code)
            return
        profiles = resp.json()
    except Exception as e:
        log.error("Dexscreener fetch error: %s", e)
        return

    if not isinstance(profiles, list):
        return

    for profile in profiles[:30]:
        token_address = profile.get("tokenAddress")
        chain_id = profile.get("chainId", "")
        if not token_address or token_address in state["seen_tokens"]:
            continue

        # Pull pair data for this token to check liquidity/volume
        try:
            pair_resp = requests.get(
                f"https://api.dexscreener.com/token-pairs/v1/{chain_id}/{token_address}",
                timeout=15,
                headers={"User-Agent": "Mozilla/5.0"},
            )
            pairs = pair_resp.json() if pair_resp.status_code == 200 else []
        except Exception:
            pairs = []

        if not pairs:
            continue

        best = max(pairs, key=lambda p: (p.get("liquidity") or {}).get("usd", 0) or 0)
        liquidity = (best.get("liquidity") or {}).get("usd", 0) or 0
        volume24h = (best.get("volume") or {}).get("h24", 0) or 0
        price_change_24h = (best.get("priceChange") or {}).get("h24", 0) or 0
        pair_created = best.get("pairCreatedAt", 0) or 0

        age_hours = None
        if pair_created:
            age_hours = (time.time() * 1000 - pair_created) / 3_600_000

        # Moderate filter, as requested
        if liquidity < MIN_LIQUIDITY_USD or volume24h < MIN_VOLUME_24H_USD:
            state["seen_tokens"].append(token_address)
            continue
        if age_hours is not None and age_hours > MAX_TOKEN_AGE_HOURS:
            state["seen_tokens"].append(token_address)
            continue

        base = best.get("baseToken", {})
        name = base.get("name", "?")
        symbol = base.get("symbol", "?")
        url = best.get("url", "")

        msg = (
            f"📈 <b>Трендовый токен (Dexscreener)</b>\n"
            f"<b>{name}</b> (${symbol}) — {chain_id}\n"
            f"Ликвидность: ${liquidity:,.0f}\n"
            f"Объём 24ч: ${volume24h:,.0f}\n"
            f"Изменение цены 24ч: {price_change_24h:+.1f}%\n"
            f"{'Возраст: %.1f ч' % age_hours if age_hours is not None else ''}\n"
            f"🔗 {url}"
        )
        send_telegram(msg)
        state["seen_tokens"].append(token_address)
        log.info("Alert sent: trending token %s (%s)", name, symbol)


# ---------------------------------------------------------------------------
# News — RSS feeds
# ---------------------------------------------------------------------------

NEWS_KEYWORDS = [
    "memecoin", "meme coin", "solana", "pump.fun", "dogecoin", "shiba",
    "pepe", "token", "crypto", "bitcoin", "ethereum", "sec", "regulation",
]


def check_news(state):
    for source_name, feed_url in NEWS_FEEDS:
        try:
            feed = feedparser.parse(feed_url)
        except Exception as e:
            log.error("News feed error (%s): %s", source_name, e)
            continue

        for entry in feed.entries[:15]:
            entry_id = entry.get("id") or entry.get("link")
            if not entry_id or entry_id in state["seen_news"]:
                continue

            title = entry.get("title", "")
            title_lower = title.lower()
            if not any(kw in title_lower for kw in NEWS_KEYWORDS):
                state["seen_news"].append(entry_id)
                continue

            link = entry.get("link", "")
            msg = f"📰 <b>{source_name}</b>\n{title}\n🔗 {link}"
            send_telegram(msg)
            state["seen_news"].append(entry_id)
            log.info("Alert sent: news from %s — %s", source_name, title[:60])


# ---------------------------------------------------------------------------
# One monitoring cycle (used by both the loop mode and single-run mode)
# ---------------------------------------------------------------------------

def run_cycle(state):
    cycle_start = datetime.now(timezone.utc)
    log.info("--- Cycle start: %s ---", cycle_start.isoformat())

    try:
        check_pumpfun(state)
    except Exception as e:
        log.error("check_pumpfun crashed: %s", e)

    try:
        check_dexscreener_trending(state)
    except Exception as e:
        log.error("check_dexscreener_trending crashed: %s", e)

    try:
        check_news(state)
    except Exception as e:
        log.error("check_news crashed: %s", e)

    save_state(state)
    log.info("Cycle done.")


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------
#
# Two modes:
#   - RUN_ONCE=1 (or running under GitHub Actions, which sets GITHUB_ACTIONS=true):
#     run a single cycle and exit. The scheduler (e.g. a GitHub Actions cron)
#     is what provides the repeated 5-minute polling.
#   - Otherwise: loop forever, sleeping POLL_INTERVAL_SECONDS between cycles.
#     Use this when running on your own always-on machine/VPS/Termux.

def main():
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        log.warning(
            "TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID not set — alerts will only print to console."
        )

    run_once = os.environ.get("RUN_ONCE") == "1" or os.environ.get("GITHUB_ACTIONS") == "true"

    state = load_state()

    if run_once:
        log.info("Memecoin monitor — single-run mode (e.g. GitHub Actions).")
        run_cycle(state)
        return

    log.info("Memecoin monitor — loop mode. Poll interval: %ss", POLL_INTERVAL_SECONDS)
    while True:
        run_cycle(state)
        log.info("Sleeping %ss...", POLL_INTERVAL_SECONDS)
        time.sleep(POLL_INTERVAL_SECONDS)


if __name__ == "__main__":
    main()
