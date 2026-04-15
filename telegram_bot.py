#!/usr/bin/env python3
"""
Telegram Bot Poller - luistert naar /scan commando en triggert GitHub Actions.
Draait als aparte GitHub Actions workflow elke 2 minuten.
"""

import json
import os
import urllib.request
import urllib.error

TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "").strip()
GITHUB_TOKEN = os.environ.get("GH_PAT", "").strip()
GITHUB_REPO = "oosterbaan/huur"

OFFSET_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "telegram_offset.txt")


def get_offset():
    if os.path.exists(OFFSET_FILE):
        with open(OFFSET_FILE) as f:
            return int(f.read().strip())
    return 0


def save_offset(offset):
    with open(OFFSET_FILE, "w") as f:
        f.write(str(offset))


def get_updates(offset):
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/getUpdates?offset={offset}&timeout=0"
    try:
        req = urllib.request.Request(url)
        resp = urllib.request.urlopen(req, timeout=10)
        return json.loads(resp.read().decode())
    except Exception as e:
        print(f"Fout bij getUpdates: {e}")
        return {"ok": False, "result": []}


def send_message(text):
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    payload = json.dumps({
        "chat_id": TELEGRAM_CHAT_ID,
        "text": text,
        "parse_mode": "HTML",
    })
    try:
        req = urllib.request.Request(url, data=payload.encode(), headers={"Content-Type": "application/json"})
        urllib.request.urlopen(req)
    except Exception as e:
        print(f"Fout bij sendMessage: {e}")


def trigger_scan():
    """Trigger de monitor workflow via GitHub API."""
    url = f"https://api.github.com/repos/{GITHUB_REPO}/actions/workflows/monitor.yml/dispatches"
    payload = json.dumps({"ref": "main"})
    try:
        req = urllib.request.Request(url, data=payload.encode(), headers={
            "Authorization": f"token {GITHUB_TOKEN}",
            "Accept": "application/vnd.github.v3+json",
            "Content-Type": "application/json",
        })
        urllib.request.urlopen(req)
        return True
    except urllib.error.HTTPError as e:
        print(f"Fout bij trigger: {e} - {e.read().decode()}")
        return False
    except Exception as e:
        print(f"Fout bij trigger: {e}")
        return False


def main():
    if not TELEGRAM_BOT_TOKEN or not GITHUB_TOKEN:
        print("TELEGRAM_BOT_TOKEN en GH_PAT moeten gezet zijn.")
        return

    offset = get_offset()
    data = get_updates(offset)

    if not data.get("ok"):
        return

    for update in data["result"]:
        update_id = update["update_id"]
        message = update.get("message", {})
        text = message.get("text", "")
        chat_id = str(message.get("chat", {}).get("id", ""))

        # Alleen reageren op berichten van jouw chat
        if chat_id != TELEGRAM_CHAT_ID:
            save_offset(update_id + 1)
            continue

        if text.strip().lower() == "/scan":
            print("Scan commando ontvangen!")
            send_message("🔍 Scan gestart! Resultaten komen over ~3 minuten...")
            if trigger_scan():
                print("Workflow getriggerd.")
            else:
                send_message("❌ Kon scan niet starten. Check GitHub Actions.")

        save_offset(update_id + 1)


if __name__ == "__main__":
    main()
