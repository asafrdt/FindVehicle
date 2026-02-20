import os
from dotenv import load_dotenv

load_dotenv()

YAD2_URL = "https://www.yad2.co.il/vehicles/cars"
YAD2_PARAMS = {
    "manufacturer": "21",
    "model": "10279",
    "year": "2021-2026",
    "price": "-1-120000",
    "km": "-1-50000",
    "hand": "0-1",
    "subModel": "104856",
    "priceOnly": "1",
    "imgOnly": "1",
    "ownerID": "1",
}

CHECK_INTERVAL_SECONDS = 20

DISPLAY_NAMES = {
    "manufacturer": {"21": "יונדאי"},
    "model": {"10279": "איוניק"},
    "subModel": {"104856": "Premium FL היברידי אוט׳ 1.6 (141 כ״ס)"},
}

AUTO_START = os.getenv("AUTO_START", "0") == "1"

GMAIL_ADDRESS = os.getenv("GMAIL_ADDRESS", "")
GMAIL_APP_PASSWORD = os.getenv("GMAIL_APP_PASSWORD", "")
NOTIFY_EMAIL = os.getenv("NOTIFY_EMAIL", "")

TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "")

SEEN_FILE = "seen_listings.json"
FOUND_FILE = "found_listings.json"
PROFILES_FILE = "profiles.json"
LOG_FILE = "monitor.log"
LOG_MAX_BYTES = 5 * 1024 * 1024  # 5 MB
LOG_BACKUP_COUNT = 3

SEEN_TTL_DAYS = 30

MAX_PAGES = 5
PAGE_DELAY_SECONDS = 3

FETCH_MAX_RETRIES = 3
FETCH_RETRY_DELAY = 5

CAPTCHA_BACKOFF_MULTIPLIER = 2
CAPTCHA_BACKOFF_MAX = 3600  # 1 hour cap

REQUEST_HEADERS = {
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,image/apng,*/*;q=0.8,application/signed-exchange;v=b3;q=0.7",
    "Accept-Language": "he-IL,he;q=0.9,en-US;q=0.8,en;q=0.7",
    "Cache-Control": "max-age=0",
    "Connection": "keep-alive",
    "DNT": "1",
    "Referer": "https://www.yad2.co.il/",
    "Sec-Fetch-Dest": "document",
    "Sec-Fetch-Mode": "navigate",
    "Sec-Fetch-Site": "same-origin",
    "Sec-Fetch-User": "?1",
    "Upgrade-Insecure-Requests": "1",
    "sec-ch-ua-mobile": "?0",
    "sec-ch-ua-platform": '"Linux"',
}

REQUEST_COOKIES = {
    "__ssds": "3",
    "y2018-2-cohort": "88",
    "use_elastic_search": "1",
    "abTestKey": "2",
    "cohortGroup": "D",
}
