#!/usr/bin/env python3
"""
Yad2 vehicle monitor - sends email alerts when new private listings appear.
"""

import json
import logging
import signal
import smtplib
import sys
import threading
import time
from datetime import datetime, timezone
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from logging.handlers import RotatingFileHandler
from pathlib import Path

import requests
from bs4 import BeautifulSoup
from fake_useragent import UserAgent

import config

BASE_DIR = Path(__file__).parent
SEEN_PATH = BASE_DIR / config.SEEN_FILE
LOG_PATH = BASE_DIR / config.LOG_FILE

shutdown_event = threading.Event()


def setup_logging() -> logging.Logger:
    logger = logging.getLogger("yad2_monitor")
    logger.setLevel(logging.INFO)

    formatter = logging.Formatter(
        "%(asctime)s | %(levelname)s | %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )

    console = logging.StreamHandler(sys.stdout)
    console.setFormatter(formatter)
    logger.addHandler(console)

    file_handler = RotatingFileHandler(
        LOG_PATH,
        maxBytes=config.LOG_MAX_BYTES,
        backupCount=config.LOG_BACKUP_COUNT,
        encoding="utf-8",
    )
    file_handler.setFormatter(formatter)
    logger.addHandler(file_handler)

    return logger


log = setup_logging()
ua = UserAgent(platforms="pc")


# ---------------------------------------------------------------------------
# Persistent HTTP session
# ---------------------------------------------------------------------------

def create_session() -> requests.Session:
    session = requests.Session()
    session.headers.update(config.REQUEST_HEADERS)
    session.cookies.update(config.REQUEST_COOKIES)
    _rotate_ua(session)
    return session


def _rotate_ua(session: requests.Session) -> None:
    agent = ua.random
    session.headers["User-Agent"] = agent
    chrome_match = ""
    if "Chrome/" in agent:
        ver = agent.split("Chrome/")[1].split(" ")[0].split(".")[0]
        chrome_match = f'"Chromium";v="{ver}", "Not_A Brand";v="24"'
    session.headers["sec-ch-ua"] = chrome_match


# ---------------------------------------------------------------------------
# Fetch listings from Yad2
# ---------------------------------------------------------------------------

def build_url(page: int = 1) -> str:
    params = "&".join(f"{k}={v}" for k, v in config.YAD2_PARAMS.items())
    url = f"{config.YAD2_URL}?{params}"
    if page > 1:
        url += f"&page={page}"
    return url


def fetch_page(session: requests.Session, url: str) -> str | None:
    for attempt in range(1, config.FETCH_MAX_RETRIES + 1):
        try:
            resp = session.get(url, timeout=30)
            resp.raise_for_status()
            return resp.text
        except requests.RequestException as exc:
            log.warning("Fetch attempt %d/%d failed: %s", attempt, config.FETCH_MAX_RETRIES, exc)
            if attempt < config.FETCH_MAX_RETRIES:
                time.sleep(config.FETCH_RETRY_DELAY)
    log.error("All %d fetch attempts failed for %s", config.FETCH_MAX_RETRIES, url)
    return None


def _is_private_seller(item: dict) -> bool:
    customer = item.get("customer", {})
    return "agencyName" not in customer


class CaptchaDetected(Exception):
    pass


def parse_listings(html: str) -> tuple[list[dict], int]:
    """Returns (private_listings, total_pages). Raises CaptchaDetected on bot block."""
    soup = BeautifulSoup(html, "html.parser")
    script = soup.find("script", id="__NEXT_DATA__")
    if script is None:
        if "ShieldSquare" in html or "Captcha" in html:
            raise CaptchaDetected("CAPTCHA detected in response")
        log.warning("__NEXT_DATA__ script tag not found in page")
        return [], 0

    try:
        data = json.loads(script.string)
        listings_data = (
            data["props"]["pageProps"]["dehydratedState"]["queries"][0]["state"]["data"]
        )
    except (json.JSONDecodeError, KeyError, IndexError) as exc:
        log.error("Failed to parse JSON data: %s", exc)
        return [], 0

    total_pages = listings_data.get("pagination", {}).get("pages", 1)

    all_listings: list[dict] = []
    for tier in ("private", "commercial", "solo", "platinum", "boost"):
        all_listings.extend(listings_data.get(tier, []))

    private_listings = [item for item in all_listings if _is_private_seller(item)]
    return private_listings, total_pages


def extract_listing_info(item: dict) -> dict:
    year = item.get("vehicleDates", {}).get("yearOfProduction", "")

    address = item.get("address", {})
    area_obj = address.get("city", address.get("area", {}))
    area = area_obj.get("text", "") if isinstance(area_obj, dict) else str(area_obj)

    hand_obj = item.get("hand", {})
    hand = hand_obj.get("text", "") if isinstance(hand_obj, dict) else str(hand_obj)

    km = item.get("km")

    token = item.get("token", "")
    link = f"https://www.yad2.co.il/vehicles/item/{token}" if token else ""

    return {
        "token": token,
        "model": item.get("model", {}).get("text", ""),
        "sub_model": item.get("subModel", {}).get("text", ""),
        "manufacturer": item.get("manufacturer", {}).get("text", ""),
        "price": item.get("price", ""),
        "year": year,
        "km": km,
        "hand": hand,
        "area": area,
        "link": link,
    }


def fetch_listings(session: requests.Session) -> list[dict] | None:
    """Fetches all pages. Returns None on CAPTCHA (caller handles backoff)."""
    _rotate_ua(session)
    all_results: list[dict] = []

    url = build_url(page=1)
    log.info("Fetching listings from: %s", url)

    html = fetch_page(session, url)
    if html is None:
        return []

    raw_listings, total_pages = parse_listings(html)  # may raise CaptchaDetected
    all_results.extend(extract_listing_info(item) for item in raw_listings)

    pages_to_fetch = min(total_pages, config.MAX_PAGES)
    for page in range(2, pages_to_fetch + 1):
        if shutdown_event.is_set():
            break
        time.sleep(config.PAGE_DELAY_SECONDS)
        url = build_url(page=page)
        log.info("Fetching page %d/%d: %s", page, pages_to_fetch, url)
        html = fetch_page(session, url)
        if html is None:
            break
        raw_listings, _ = parse_listings(html)
        all_results.extend(extract_listing_info(item) for item in raw_listings)

    seen_tokens = set()
    unique: list[dict] = []
    for item in all_results:
        if item["token"] not in seen_tokens:
            seen_tokens.add(item["token"])
            unique.append(item)

    log.info("Found %d private listings across %d page(s)", len(unique), pages_to_fetch)
    return unique


# ---------------------------------------------------------------------------
# Seen listings persistence (with TTL pruning)
# ---------------------------------------------------------------------------

def load_seen() -> dict[str, str]:
    """Returns {token: iso_timestamp}."""
    if not SEEN_PATH.exists():
        return {}
    try:
        data = json.loads(SEEN_PATH.read_text(encoding="utf-8"))
        if isinstance(data, list):
            now = datetime.now(timezone.utc).isoformat()
            return {token: now for token in data}
        return data
    except (json.JSONDecodeError, TypeError):
        return {}


def save_seen(seen: dict[str, str]) -> None:
    SEEN_PATH.write_text(json.dumps(seen, ensure_ascii=False, indent=2), encoding="utf-8")


def prune_seen(seen: dict[str, str]) -> dict[str, str]:
    cutoff = datetime.now(timezone.utc).timestamp() - config.SEEN_TTL_DAYS * 86400
    pruned: dict[str, str] = {}
    for token, ts in seen.items():
        try:
            entry_time = datetime.fromisoformat(ts).timestamp()
        except (ValueError, TypeError):
            entry_time = 0
        if entry_time > cutoff:
            pruned[token] = ts
    removed = len(seen) - len(pruned)
    if removed > 0:
        log.info("Pruned %d old entries from seen listings (older than %d days)", removed, config.SEEN_TTL_DAYS)
    return pruned


# ---------------------------------------------------------------------------
# Email notification (content in Hebrew)
# ---------------------------------------------------------------------------

def format_listing_text(info: dict) -> str:
    price = info["price"]
    price_str = f"{price:,} ש\"ח" if isinstance(price, (int, float)) and price else "לא צוין"

    km = info["km"]
    km_str = f"{km:,} ק\"מ" if isinstance(km, (int, float)) and km else "לא צוין"

    return (
        f'--- רכב חדש ---\n'
        f'יצרן: {info["manufacturer"]}\n'
        f'דגם: {info["model"]} {info["sub_model"]}\n'
        f'מחיר: {price_str}\n'
        f'שנה: {info["year"]}\n'
        f'קילומטראז\': {km_str}\n'
        f'יד: {info["hand"]}\n'
        f'אזור: {info["area"]}\n'
        f'קישור: {info["link"]}\n'
    )


def send_email(new_listings: list[dict]) -> bool:
    if not config.GMAIL_ADDRESS or not config.GMAIL_APP_PASSWORD or not config.NOTIFY_EMAIL:
        log.error("Missing email credentials in .env — cannot send notification")
        return False

    count = len(new_listings)
    if count == 1:
        subject = "רכב חדש נמצא ביד2!"
    else:
        subject = f"{count} רכבים חדשים נמצאו ביד2!"

    body_parts = [format_listing_text(lst) for lst in new_listings]
    body = "\n".join(body_parts)
    body += f"\n\nקישור לחיפוש המלא:\n{build_url()}\n"

    msg = MIMEMultipart("alternative")
    msg["Subject"] = subject
    msg["From"] = config.GMAIL_ADDRESS
    msg["To"] = config.NOTIFY_EMAIL
    msg.attach(MIMEText(body, "plain", "utf-8"))

    try:
        with smtplib.SMTP_SSL("smtp.gmail.com", 465, timeout=30) as server:
            server.login(config.GMAIL_ADDRESS, config.GMAIL_APP_PASSWORD)
            server.sendmail(config.GMAIL_ADDRESS, config.NOTIFY_EMAIL, msg.as_string())
        log.info("Email sent successfully to %s", config.NOTIFY_EMAIL)
        return True
    except smtplib.SMTPException as exc:
        log.error("Failed to send email: %s", exc)
        return False


# ---------------------------------------------------------------------------
# Main loop
# ---------------------------------------------------------------------------

def check_once(session: requests.Session, seen: dict[str, str]) -> dict[str, str]:
    listings = fetch_listings(session)  # raises CaptchaDetected on CAPTCHA
    if not listings:
        return seen

    new_listings = [lst for lst in listings if lst["token"] not in seen]

    if not new_listings:
        log.info("No new listings found")
        return seen

    log.info("Found %d new listing(s)!", len(new_listings))

    for lst in new_listings:
        log.info(
            "New listing: token=%s | %s %s | price=%s | year=%s | km=%s | %s | %s",
            lst["token"],
            lst["manufacturer"],
            lst["model"],
            lst["price"],
            lst["year"],
            lst["km"],
            lst["hand"],
            lst["area"],
        )

    email_sent = send_email(new_listings)

    if email_sent:
        now_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        log.info("--- Notification sent at %s ---", now_str)
        for lst in new_listings:
            log.info(
                "  Notified: %s %s %s | price=%s | %s",
                lst["manufacturer"],
                lst["model"],
                lst["sub_model"],
                lst["price"],
                lst["link"],
            )
        log.info("--- End of notification details ---")

    now = datetime.now(timezone.utc).isoformat()
    for lst in new_listings:
        seen[lst["token"]] = now
    save_seen(seen)
    return seen


def main() -> None:
    def _handle_signal(signum, _frame):
        sig_name = signal.Signals(signum).name
        log.info("Received %s — shutting down gracefully...", sig_name)
        shutdown_event.set()

    signal.signal(signal.SIGINT, _handle_signal)
    signal.signal(signal.SIGTERM, _handle_signal)

    log.info("=" * 60)
    log.info("Starting Yad2 vehicle monitor")
    log.info("Check interval: %d seconds", config.CHECK_INTERVAL_SECONDS)
    log.info("=" * 60)

    session = create_session()
    seen = load_seen()
    seen = prune_seen(seen)

    captcha_backoff = 0

    while not shutdown_event.is_set():
        try:
            seen = check_once(session, seen)
            captcha_backoff = 0
        except CaptchaDetected:
            if captcha_backoff == 0:
                captcha_backoff = config.CHECK_INTERVAL_SECONDS
            else:
                captcha_backoff = min(
                    captcha_backoff * config.CAPTCHA_BACKOFF_MULTIPLIER,
                    config.CAPTCHA_BACKOFF_MAX,
                )
            log.warning("CAPTCHA detected — backing off for %d seconds", captcha_backoff)
            session = create_session()
            shutdown_event.wait(captcha_backoff)
            continue
        except Exception:
            log.exception("Unexpected error during check cycle")

        log.info("Waiting %d seconds until next check...", config.CHECK_INTERVAL_SECONDS)
        shutdown_event.wait(config.CHECK_INTERVAL_SECONDS)

    log.info("Monitor stopped.")


if __name__ == "__main__":
    main()
