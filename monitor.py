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
from datetime import datetime, timedelta, timezone
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from logging.handlers import RotatingFileHandler
from pathlib import Path

import requests
from bs4 import BeautifulSoup
from fake_useragent import UserAgent

import config

BASE_DIR = Path(__file__).parent
LOG_PATH = BASE_DIR / config.LOG_FILE
FOUND_PATH = BASE_DIR / config.FOUND_FILE

shutdown_event = threading.Event()

# ---------------------------------------------------------------------------
# Found listings persistence (Phase 1a)
# ---------------------------------------------------------------------------

_found_listings: list[dict] = []
_found_lock = threading.Lock()
_FOUND_MAX = 500


def _load_found() -> None:
    global _found_listings
    if not FOUND_PATH.exists():
        return
    try:
        data = json.loads(FOUND_PATH.read_text(encoding="utf-8"))
        if isinstance(data, list):
            _found_listings = data[-_FOUND_MAX:]
    except (json.JSONDecodeError, TypeError, OSError):
        pass


def _save_found() -> None:
    try:
        FOUND_PATH.write_text(
            json.dumps(_found_listings, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
    except OSError:
        pass


_load_found()


def get_found_listings() -> list[dict]:
    """Returns non-dismissed listings for the UI."""
    with _found_lock:
        return [l for l in _found_listings if not l.get("dismissed")]


def _get_seen_tokens() -> set[str]:
    """All tokens (including dismissed) for dedup tracking."""
    with _found_lock:
        return {l["token"] for l in _found_listings}


def _append_found(listings: list[dict]) -> None:
    with _found_lock:
        _found_listings.extend(listings)
        if len(_found_listings) > _FOUND_MAX:
            del _found_listings[: len(_found_listings) - _FOUND_MAX]
        _save_found()


def remove_found(token: str) -> bool:
    """Mark a listing as dismissed (hidden from UI but still tracked)."""
    with _found_lock:
        for entry in _found_listings:
            if entry.get("token") == token and not entry.get("dismissed"):
                entry["dismissed"] = True
                _save_found()
                return True
        return False


def clear_found() -> None:
    with _found_lock:
        _found_listings.clear()
        _save_found()


# ---------------------------------------------------------------------------
# Monitor state dict (Phase 1b + Phase 2)
# ---------------------------------------------------------------------------

_state: dict = {
    "next_check_at": None,
    "last_check_at": None,
    "checks_count": 0,
    "found_total": 0,
    "captcha_active": False,
    "captcha_backoff_until": None,
}
_state_lock = threading.Lock()


def get_state() -> dict:
    with _state_lock:
        return dict(_state)


def _update_state(**kwargs) -> None:
    with _state_lock:
        _state.update(kwargs)


def _reset_state() -> None:
    with _state_lock:
        _state.update(
            next_check_at=None,
            last_check_at=None,
            checks_count=0,
            found_total=0,
            captcha_active=False,
            captcha_backoff_until=None,
        )


# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------

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

    img = ""
    images = item.get("images")
    if isinstance(images, list) and images:
        first = images[0]
        if isinstance(first, dict):
            img = first.get("src", first.get("url", ""))
        elif isinstance(first, str):
            img = first

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
        "img": img,
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


def _prune_found() -> None:
    """Remove entries older than SEEN_TTL_DAYS from the found store."""
    cutoff = (datetime.now(timezone.utc) - timedelta(days=config.SEEN_TTL_DAYS)).strftime("%Y-%m-%d %H:%M:%S")
    with _found_lock:
        before = len(_found_listings)
        _found_listings[:] = [
            l for l in _found_listings if (l.get("found_at", "") >= cutoff)
        ]
        removed = before - len(_found_listings)
        if removed > 0:
            _save_found()
    if removed > 0:
        log.info("Pruned %d old entries from found listings (older than %d days)", removed, config.SEEN_TTL_DAYS)


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
# Telegram notification (Phase 5b)
# ---------------------------------------------------------------------------

def send_telegram(new_listings: list[dict]) -> bool:
    token = config.TELEGRAM_BOT_TOKEN
    chat_id = config.TELEGRAM_CHAT_ID
    if not token or not chat_id:
        return False

    lines = []
    for lst in new_listings:
        price = lst["price"]
        price_s = f"{price:,}₪" if isinstance(price, (int, float)) and price else "?"
        km = lst.get("km")
        km_s = f"{km:,} ק\"מ" if isinstance(km, (int, float)) and km else "לא צוין"
        lines.append(
            f"🚗 {lst['manufacturer']} {lst['model']}\n"
            f"💰 {price_s} | {lst['year']} | {lst['hand']}\n"
            f"🛣 {km_s}\n"
            f"📍 {lst['area']}\n"
            f"🔗 {lst['link']}"
        )
    text = "\n\n".join(lines)

    url = f"https://api.telegram.org/bot{token}/sendMessage"
    try:
        resp = requests.post(url, json={"chat_id": chat_id, "text": text}, timeout=10)
        if resp.ok:
            log.info("Telegram message sent successfully")
        else:
            log.warning("Telegram API returned %d: %s", resp.status_code, resp.text[:200])
        return resp.ok
    except requests.RequestException as exc:
        log.error("Failed to send Telegram message: %s", exc)
        return False


# ---------------------------------------------------------------------------
# Main loop
# ---------------------------------------------------------------------------

def check_once(session: requests.Session) -> None:
    listings = fetch_listings(session)  # raises CaptchaDetected on CAPTCHA
    if not listings:
        return

    seen_tokens = _get_seen_tokens()
    new_listings = [lst for lst in listings if lst["token"] not in seen_tokens]

    if not new_listings:
        log.info("No new listings found")
        return

    log.info("Found %d new listing(s)!", len(new_listings))

    _update_state(found_total=get_state()["found_total"] + len(new_listings))

    stamped = []
    for lst in new_listings:
        entry = dict(lst)
        entry["found_at"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        stamped.append(entry)
    _append_found(stamped)

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

    send_telegram(new_listings)


def run_loop() -> None:
    """Run the monitor loop. Can be called from a background thread or from main()."""
    shutdown_event.clear()
    _reset_state()
    _prune_found()

    log.info("=" * 60)
    log.info("Starting Yad2 vehicle monitor")
    log.info("Check interval: %d seconds", config.CHECK_INTERVAL_SECONDS)
    log.info("=" * 60)

    session = create_session()

    captcha_backoff = 0

    while not shutdown_event.is_set():
        try:
            check_once(session)
            captcha_backoff = 0
            _update_state(
                last_check_at=datetime.now(timezone.utc).isoformat(),
                checks_count=get_state()["checks_count"] + 1,
                captcha_active=False,
                captcha_backoff_until=None,
            )
        except CaptchaDetected:
            if captcha_backoff == 0:
                captcha_backoff = config.CHECK_INTERVAL_SECONDS
            else:
                captcha_backoff = min(
                    captcha_backoff * config.CAPTCHA_BACKOFF_MULTIPLIER,
                    config.CAPTCHA_BACKOFF_MAX,
                )
            backoff_until = (datetime.now(timezone.utc) + timedelta(seconds=captcha_backoff)).isoformat()
            _update_state(captcha_active=True, captcha_backoff_until=backoff_until)
            log.warning("CAPTCHA detected — backing off for %d seconds", captcha_backoff)
            session = create_session()
            shutdown_event.wait(captcha_backoff)
            _update_state(captcha_active=False, captcha_backoff_until=None)
            continue
        except Exception:
            log.exception("Unexpected error during check cycle")

        interval = config.CHECK_INTERVAL_SECONDS
        next_at = (datetime.now(timezone.utc) + timedelta(seconds=interval)).isoformat()
        _update_state(next_check_at=next_at)
        log.info("Waiting %d seconds until next check...", interval)
        shutdown_event.wait(interval)

    _update_state(next_check_at=None)
    log.info("Monitor stopped.")


def main() -> None:
    def _handle_signal(signum, _frame):
        sig_name = signal.Signals(signum).name
        log.info("Received %s — shutting down gracefully...", sig_name)
        shutdown_event.set()

    signal.signal(signal.SIGINT, _handle_signal)
    signal.signal(signal.SIGTERM, _handle_signal)

    run_loop()


if __name__ == "__main__":
    main()
