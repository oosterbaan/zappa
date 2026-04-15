#!/usr/bin/env python3
"""
Vesteda Huurwoning Monitor
Scraped de Vesteda website en stuurt een Telegram-bericht bij nieuwe woningen.
Draait op GitHub Actions elke 15 minuten.
"""

import json
import os
import re
import sys
import urllib.request
import urllib.parse
import urllib.error
from datetime import datetime

from playwright.sync_api import sync_playwright
from playwright_stealth import Stealth
from bs4 import BeautifulSoup

# === CONFIGURATIE ===
VESTEDA_URL = "https://hurenbij.vesteda.com/zoekopdracht/"
LOGIN_URL = "https://hurenbij.vesteda.com/login/"

# Vesteda inloggegevens
VESTEDA_EMAIL = os.environ.get("VESTEDA_EMAIL", "")
VESTEDA_WACHTWOORD = os.environ.get("VESTEDA_WACHTWOORD", "")

# Telegram instellingen
TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "").strip()

# Bestand om bekende woningen op te slaan
BEKENDE_WONINGEN_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "bekende_woningen.json")


def laad_bekende_woningen():
    """Laad eerder geziene woningen uit JSON bestand."""
    if os.path.exists(BEKENDE_WONINGEN_FILE):
        with open(BEKENDE_WONINGEN_FILE, "r") as f:
            return json.load(f)
    return {}


def sla_bekende_woningen_op(woningen):
    """Sla bekende woningen op naar JSON bestand."""
    with open(BEKENDE_WONINGEN_FILE, "w") as f:
        json.dump(woningen, f, indent=2, ensure_ascii=False)


def scrape_vesteda():
    """Log in op Vesteda, scrape de zoekpagina en retourneer lijst met woningen."""
    if not VESTEDA_EMAIL or not VESTEDA_WACHTWOORD:
        print("FOUT: Stel VESTEDA_EMAIL en VESTEDA_WACHTWOORD in als environment variabelen.")
        sys.exit(1)

    woningen = []

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        context = browser.new_context(
            user_agent="Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
            viewport={"width": 1440, "height": 900},
        )
        stealth = Stealth()
        page = context.new_page()
        stealth.apply_stealth_sync(page)

        # Stap 1: Inloggen - cookies accepteren
        page.goto(LOGIN_URL, wait_until="networkidle", timeout=30000)
        try:
            page.click("#CybotCookiebotDialogBodyLevelButtonLevelOptinAllowAll", timeout=5000)
            page.wait_for_timeout(1500)
        except Exception:
            pass

        # Pagina herladen voor vers CSRF-token (cookie-consent reset de sessie)
        page.goto(LOGIN_URL, wait_until="networkidle", timeout=30000)
        page.wait_for_timeout(1000)

        # Inlogformulier invullen en submitten
        page.fill("#txtEmail", VESTEDA_EMAIL)
        page.fill("#txtWachtwoord", VESTEDA_WACHTWOORD)
        page.evaluate("document.querySelector('#frmLogin').submit()")
        page.wait_for_load_state("networkidle", timeout=15000)
        page.wait_for_timeout(2000)

        # Controleer of login gelukt is
        if "/login" in page.url:
            try:
                error_el = page.query_selector(".alert-danger, .alert-warning")
                error_msg = error_el.text_content().strip() if error_el else "Onbekende fout"
            except Exception:
                error_msg = "Onbekende fout"
            print(f"FOUT: Login mislukt: {error_msg}")
            browser.close()
            sys.exit(1)

        print("  Succesvol ingelogd op Vesteda.")

        # Stap 2: Naar zoekpagina
        page.goto(VESTEDA_URL, wait_until="networkidle", timeout=30000)

        # Wacht op woningkaarten
        try:
            page.wait_for_selector(".card.card-result-list", timeout=15000)
        except Exception:
            print("  Waarschuwing: Geen woningkaarten gevonden op de pagina.")
            browser.close()
            return woningen

        html = page.content()
        browser.close()

    # Parse de HTML
    soup = BeautifulSoup(html, "html.parser")
    cards = soup.select(".card.card-result-list")

    for card in cards:
        link_el = card.select_one("a.stretched-link, a")
        href = ""
        if link_el and link_el.get("href"):
            href = link_el["href"]
            if not href.startswith("http"):
                href = "https://hurenbij.vesteda.com" + href

        # Adres
        title_el = card.select_one("h5.card-title, h2, h3")
        adres = title_el.get_text(strip=True) if title_el else "Onbekend"

        # Stad
        stad_el = card.select_one(".card-text")
        stad = stad_el.get_text(strip=True) if stad_el else ""

        # Prijs
        prijs_tekst = card.get_text()
        prijs_match = re.search(r"[\d.]+,-", prijs_tekst)
        prijs = prijs_match.group() if prijs_match else ""

        # Slaapkamers
        slaapkamers = ""
        sk_match = re.search(r"slaapkamers:\s*(\d+)", prijs_tekst, re.IGNORECASE)
        if sk_match:
            slaapkamers = sk_match.group(1)

        # Oppervlakte
        oppervlakte = ""
        opp_match = re.search(r"(\d+)\s*m2", prijs_tekst)
        if opp_match:
            oppervlakte = opp_match.group(1) + " m2"

        woningen.append({
            "adres": adres,
            "stad": stad,
            "prijs": prijs,
            "slaapkamers": slaapkamers,
            "oppervlakte": oppervlakte,
            "url": href,
        })

    return woningen


def stuur_telegram(tekst):
    """Stuur een bericht via Telegram bot."""
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        print("Telegram niet geconfigureerd. Stel TELEGRAM_BOT_TOKEN en TELEGRAM_CHAT_ID in.")
        print(tekst)
        return False

    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    payload = json.dumps({
        "chat_id": TELEGRAM_CHAT_ID,
        "text": tekst,
        "parse_mode": "HTML",
        "disable_web_page_preview": False,
    })

    try:
        req = urllib.request.Request(url, data=payload.encode(), headers={"Content-Type": "application/json"})
        urllib.request.urlopen(req)
        print("  Telegram bericht verzonden!")
        return True
    except urllib.error.HTTPError as e:
        body = e.read().decode()
        print(f"  FOUT bij Telegram: {e} - {body}")
    except Exception as e:
        print(f"  FOUT bij Telegram: {e}")
        return False


def meld_nieuwe_woningen(nieuwe_woningen):
    """Stuur Telegram-bericht(en) voor nieuwe woningen."""
    for w in nieuwe_woningen:
        bericht = (
            f"<b>Nieuw huurhuis op Vesteda!</b>\n\n"
            f"<b>{w['adres']}</b>\n"
            f"{w['stad']}\n\n"
            f"Huurprijs: EUR {w['prijs']}\n"
            f"Slaapkamers: {w['slaapkamers']}\n"
            f"Oppervlakte: {w['oppervlakte']}\n\n"
            f"<a href=\"{w['url']}\">Bekijk op Vesteda</a>"
        )
        stuur_telegram(bericht)


def main():
    print(f"[{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] Vesteda monitor gestart...")

    # Scrape huidige woningen
    huidige_woningen = scrape_vesteda()
    print(f"  {len(huidige_woningen)} woningen gevonden op Vesteda.")

    # Laad bekende woningen
    bekende = laad_bekende_woningen()

    # Vergelijk
    nieuwe_woningen = []
    huidige_dict = {}

    for w in huidige_woningen:
        key = w["url"] or w["adres"]
        huidige_dict[key] = w
        if key not in bekende:
            nieuwe_woningen.append(w)

    if nieuwe_woningen:
        print(f"  {len(nieuwe_woningen)} NIEUWE woning(en) gevonden!")
        meld_nieuwe_woningen(nieuwe_woningen)
    else:
        print("  Geen nieuwe woningen.")

    # Sla huidige stand op
    sla_bekende_woningen_op(huidige_dict)

    # Toon verdwenen woningen
    verdwenen = set(bekende.keys()) - set(huidige_dict.keys())
    if verdwenen:
        print(f"  {len(verdwenen)} woning(en) niet meer beschikbaar:")
        for key in verdwenen:
            info = bekende[key]
            print(f"    - {info.get('adres', key)}")


if __name__ == "__main__":
    main()
