# FindVehicle - Yad2 Vehicle Monitor

A Python script that checks Yad2 for new vehicle listings at a configurable interval and sends email notifications when new **private seller** listings appear (dealerships are excluded).

## Installation

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

## Email Setup

The script sends notifications via Gmail SMTP. You need a **Gmail App Password** (not your regular Gmail password).

### Creating an App Password

1. Go to [Google Account Security](https://myaccount.google.com/security)
2. Make sure **2-Step Verification** is enabled
3. Go to [App Passwords](https://myaccount.google.com/apppasswords)
4. Create a new app password (name it `FindVehicle` for example)
5. Copy the 16-character code that is generated

### Configure `.env`

```
GMAIL_ADDRESS=your_email@gmail.com
GMAIL_APP_PASSWORD=abcd efgh ijkl mnop
NOTIFY_EMAIL=recipient@gmail.com
```

- `GMAIL_ADDRESS` — the Gmail account that sends the notifications
- `GMAIL_APP_PASSWORD` — the App Password from the previous step
- `NOTIFY_EMAIL` — the email address that receives notifications (can be the same address)

## Usage

```bash
source .venv/bin/activate
python monitor.py
```

The script runs continuously, checks at the configured interval, and writes logs to both stdout and `monitor.log`.

On the first run it saves all current listings without sending notifications to avoid flooding your inbox.

## Changing the Search

Search parameters are defined in `config.py` under `YAD2_PARAMS`. You can change manufacturer, model, year range, price, km, and hand.

The check interval is also in `config.py` (`CHECK_INTERVAL_SECONDS`).
