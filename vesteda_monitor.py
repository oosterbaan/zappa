#!/usr/bin/env python3
"""
Huurwoning Monitor - Amersfoort
Scraped meerdere huursites en stuurt een Telegram-bericht bij nieuwe woningen.
Draait op GitHub Actions elke 15 minuten.

Ondersteunde sites:
- Vesteda (hurenbij.vesteda.com)
- Govaert (govaert.nl)
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
GOVAERT_URL = "https://govaert.nl/woning-huren/actueel-huuraanbod/?_plaatsen=amersfoort"
PARARIUS_URL = "https://www.pararius.nl/huurwoningen/amersfoort"
DOMICA_URL = "https://www.domica.nl/woningaanbod?offer=rent&location=Amersfoort"
WONEN123_URL = "https://www.123wonen.nl/huurwoningen/in/amersfoort"
INTERHOUSE_URL = "https://interhouse.nl/aanbod/?offer=huur&search_terms=Amersfoort&search_type=city"
NEDERWOON_URL = "https://nederwoon.nl/search?city=Amersfoort"
HUURPORTAAL_URL = "https://huurwoningportaal.nl/huurwoningen?view=1&property_search%5Bgroup_ids%5D=2600&property_search%5Bsort%5D=popularity"

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


# =============================================================================
# VESTEDA SCRAPER
# =============================================================================

def scrape_vesteda():
    """Log in op Vesteda, scrape de zoekpagina en retourneer lijst met woningen."""
    if not VESTEDA_EMAIL or not VESTEDA_WACHTWOORD:
        print("  Vesteda overgeslagen (geen inloggegevens).")
        return []

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

        # Pagina herladen voor vers CSRF-token
        page.goto(LOGIN_URL, wait_until="networkidle", timeout=30000)
        page.wait_for_timeout(1000)

        # Inlogformulier invullen en submitten
        page.fill("#txtEmail", VESTEDA_EMAIL)
        page.fill("#txtWachtwoord", VESTEDA_WACHTWOORD)
        page.evaluate("document.querySelector('#frmLogin').submit()")
        page.wait_for_load_state("networkidle", timeout=15000)
        page.wait_for_timeout(2000)

        if "/login" in page.url:
            print("  FOUT: Vesteda login mislukt.")
            browser.close()
            return woningen

        print("  Succesvol ingelogd op Vesteda.")

        # Stap 2: Naar zoekpagina
        page.goto(VESTEDA_URL, wait_until="networkidle", timeout=30000)

        try:
            page.wait_for_selector(".card.card-result-list", timeout=15000)
        except Exception:
            print("  Geen woningen gevonden op Vesteda.")
            browser.close()
            return woningen

        html = page.content()
        browser.close()

    soup = BeautifulSoup(html, "html.parser")
    cards = soup.select(".card.card-result-list")

    for card in cards:
        link_el = card.select_one("a.stretched-link, a")
        href = ""
        if link_el and link_el.get("href"):
            href = link_el["href"]
            if not href.startswith("http"):
                href = "https://hurenbij.vesteda.com" + href

        title_el = card.select_one("h5.card-title, h2, h3")
        adres = title_el.get_text(strip=True) if title_el else "Onbekend"

        stad_el = card.select_one(".card-text")
        stad = stad_el.get_text(strip=True) if stad_el else ""

        prijs_tekst = card.get_text()
        prijs_match = re.search(r"[\d.]+,-", prijs_tekst)
        prijs = prijs_match.group() if prijs_match else ""

        woningen.append({
            "bron": "Vesteda",
            "adres": adres,
            "stad": stad,
            "prijs": prijs,
            "url": href,
        })

    return woningen


# =============================================================================
# GOVAERT SCRAPER
# =============================================================================

def scrape_govaert():
    """Scrape Govaert makelaardij (geen login nodig)."""
    woningen = []

    try:
        req = urllib.request.Request(GOVAERT_URL, headers={
            "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36"
        })
        response = urllib.request.urlopen(req, timeout=15)
        html = response.read().decode()
    except Exception as e:
        print(f"  FOUT bij ophalen Govaert: {e}")
        return woningen

    soup = BeautifulSoup(html, "html.parser")

    # Vind alle woninglinks
    links = soup.select('a[href*="/woningen/"]')
    seen_urls = set()

    for link in links:
        href = link.get("href", "")
        if href in seen_urls or not href:
            continue
        seen_urls.add(href)

        street_el = link.select_one(".object-street")
        number_el = link.select_one(".object-housenumber")
        city_el = link.select_one(".object-city")

        if not street_el:
            continue

        adres = street_el.get_text(strip=True)
        if number_el:
            adres += " " + number_el.get_text(strip=True)

        stad = city_el.get_text(strip=True) if city_el else "Amersfoort"

        # Prijs
        tekst = link.get_text()
        prijs_match = re.search(r"([\d.]+)\s*(?:\n|\t)*\s*per maand", tekst)
        prijs = prijs_match.group(1) + " per maand" if prijs_match else ""

        if not href.startswith("http"):
            href = "https://govaert.nl" + href

        woningen.append({
            "bron": "Govaert",
            "adres": adres,
            "stad": stad,
            "prijs": prijs,
            "url": href,
        })

    return woningen


# =============================================================================
# DOMICA SCRAPER
# =============================================================================

def scrape_domica():
    """Scrape Domica (Playwright nodig, dynamisch geladen)."""
    woningen = []

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        context = browser.new_context(
            user_agent="Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
            viewport={"width": 1440, "height": 900},
        )
        page = context.new_page()
        Stealth().apply_stealth_sync(page)
        page.goto(DOMICA_URL, wait_until="networkidle", timeout=30000)

        try:
            page.wait_for_selector("a.eazlee_object", timeout=15000)
        except Exception:
            print("  Geen woningen gevonden op Domica.")
            browser.close()
            return woningen

        items = page.evaluate("""
            Array.from(document.querySelectorAll('a.eazlee_object')).map(item => ({
                adres: (item.querySelector('.eazlee_object_bottom_street_nummer') || {}).textContent?.trim() || '',
                stad: (item.querySelector('.eazlee_object_bottom_postcode_city') || {}).textContent?.trim() || '',
                prijs: (item.querySelector('.eazlee_object_bottom_price') || {}).textContent?.trim() || '',
                url: item.href || ''
            }))
        """)
        browser.close()

    for item in items:
        if item["adres"]:
            woningen.append({
                "bron": "Domica",
                "adres": item["adres"],
                "stad": item["stad"],
                "prijs": item["prijs"],
                "url": item["url"],
            })

    return woningen


# =============================================================================
# 123WONEN SCRAPER
# =============================================================================

def scrape_123wonen():
    """Scrape 123Wonen (geen login of Playwright nodig)."""
    woningen = []

    try:
        req = urllib.request.Request(WONEN123_URL, headers={
            "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36"
        })
        response = urllib.request.urlopen(req, timeout=15)
        html = response.read().decode()
    except Exception as e:
        print(f"  FOUT bij ophalen 123Wonen: {e}")
        return woningen

    soup = BeautifulSoup(html, "html.parser")
    links = soup.select('a[href*="/huur/amersfoort/"]')

    for a in links:
        href = a.get("href", "")
        if not href:
            continue

        # Ga omhoog naar de kaart
        card = a.parent
        for _ in range(5):
            if card.get_text() and "p/mnd" in card.get_text():
                break
            card = card.parent

        text = " ".join(card.get_text().split())

        # Adres
        adres_match = re.search(r"Amersfoort,\s*([A-Za-z\s]+(?:\d[\w\s-]*)?)", text)
        adres = adres_match.group(1).strip() if adres_match else "Onbekend"

        # Prijs
        prijs_match = re.search(r"([\d.]+,-)\s*p/mnd", text)
        prijs = prijs_match.group(1) + " p/mnd" if prijs_match else ""

        if not href.startswith("http"):
            href = "https://www.123wonen.nl" + href

        woningen.append({
            "bron": "123Wonen",
            "adres": adres,
            "stad": "Amersfoort",
            "prijs": prijs,
            "url": href,
        })

    return woningen


# =============================================================================
# INTERHOUSE SCRAPER
# =============================================================================

def scrape_interhouse():
    """Scrape Interhouse (Playwright nodig, client-side rendered)."""
    woningen = []

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        context = browser.new_context(
            user_agent="Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
            viewport={"width": 1440, "height": 900},
        )
        page = context.new_page()
        Stealth().apply_stealth_sync(page)
        page.goto(INTERHOUSE_URL, wait_until="networkidle", timeout=30000)
        page.wait_for_timeout(3000)

        items = page.evaluate("""
            Array.from(document.querySelectorAll('a[href*="/vastgoed/huur/"]')).map(a => {
                const text = a.textContent.replace(/\\s+/g, ' ').trim();
                const streetMatch = text.match(/(?:Beschikbaar|Verhuurd)\\s+(.+?)\\s+AMERSFOORT/i);
                return {
                    adres: streetMatch ? streetMatch[1] : '',
                    verhuurd: text.toLowerCase().includes('verhuurd'),
                    url: a.href
                };
            }).filter(item => item.adres)
        """)
        browser.close()

    seen = set()
    for item in items:
        if item["url"] in seen or item["verhuurd"]:
            continue
        seen.add(item["url"])
        woningen.append({
            "bron": "Interhouse",
            "adres": item["adres"],
            "stad": "Amersfoort",
            "prijs": "",
            "url": item["url"],
        })

    return woningen


# =============================================================================
# NEDERWOON SCRAPER
# =============================================================================

def scrape_nederwoon():
    """Scrape NederWoon (Playwright nodig, JS-rendered)."""
    woningen = []

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        context = browser.new_context(
            user_agent="Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
            viewport={"width": 1440, "height": 900},
        )
        page = context.new_page()
        Stealth().apply_stealth_sync(page)
        page.goto(NEDERWOON_URL, wait_until="networkidle", timeout=30000)
        page.wait_for_timeout(3000)

        items = page.evaluate("""
            const seen = new Set();
            const results = [];
            document.querySelectorAll('a[href*="/huurwoning/amersfoort/"]').forEach(a => {
                if (seen.has(a.href)) return;
                seen.add(a.href);
                const card = a.closest('.row') || a.parentElement.parentElement;
                const h = a.querySelector('h2, h3, h4');
                const adres = h ? h.textContent.trim() : a.textContent.trim();
                const allText = card ? card.textContent : '';
                const priceMatch = allText.match(/([\\d.,]+),00/);
                results.push({
                    adres: adres.substring(0, 50),
                    prijs: priceMatch ? priceMatch[0] : '',
                    url: a.href
                });
            });
            results;
        """)
        browser.close()

    for item in items:
        if item["adres"]:
            woningen.append({
                "bron": "NederWoon",
                "adres": item["adres"],
                "stad": "Amersfoort",
                "prijs": item["prijs"],
                "url": item["url"],
            })

    return woningen


# =============================================================================
# HUURWONINGPORTAAL SCRAPER
# =============================================================================

def scrape_huurportaal():
    """Scrape Huurwoningportaal (geen login of Playwright nodig)."""
    woningen = []

    try:
        req = urllib.request.Request(HUURPORTAAL_URL, headers={
            "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36"
        })
        response = urllib.request.urlopen(req, timeout=15)
        html = response.read().decode()
    except Exception as e:
        print(f"  FOUT bij ophalen Huurwoningportaal: {e}")
        return woningen

    soup = BeautifulSoup(html, "html.parser")
    links = soup.select('a[href*="/huurwoning/"][href*="amersfoort"]')
    seen = set()

    for a in links:
        href = a.get("href", "")
        if href in seen or not href or "abonnementen" in href:
            continue
        seen.add(href)

        if not href.startswith("http"):
            href = "https://huurwoningportaal.nl" + href

        # Adres uit URL halen (bijv. "4-kamer-appartement-in-amersfoort-05a5d8")
        slug = href.rstrip("/").split("/")[-1]
        adres_parts = slug.rsplit("-", 1)[0]  # verwijder hash
        adres = adres_parts.replace("-", " ").replace(" in amersfoort", "").title()

        # Prijs uit de card
        prijs = ""
        parent = a.parent
        for _ in range(5):
            if parent is None:
                break
            price_el = parent.select_one('[class*="price"], [class*="rate"]')
            if price_el:
                prijs = price_el.get_text(strip=True)
                break
            parent = parent.parent

        woningen.append({
            "bron": "Huurportaal",
            "adres": adres,
            "stad": "Amersfoort",
            "prijs": prijs,
            "url": href,
        })

    return woningen


# =============================================================================
# PARARIUS SCRAPER
# =============================================================================

def scrape_pararius():
    """Scrape Pararius (Playwright nodig, blokkeert curl)."""
    woningen = []

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        context = browser.new_context(
            user_agent="Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
            viewport={"width": 1440, "height": 900},
        )
        page = context.new_page()
        Stealth().apply_stealth_sync(page)
        page.goto(PARARIUS_URL, wait_until="networkidle", timeout=30000)

        try:
            page.wait_for_selector(".search-list__item--listing", timeout=10000)
        except Exception:
            print("  Geen woningen gevonden op Pararius.")
            browser.close()
            return woningen

        html = page.content()
        browser.close()

    soup = BeautifulSoup(html, "html.parser")
    items = soup.select(".search-list__item--listing")

    for item in items:
        link_el = item.select_one("a")
        href = link_el.get("href", "") if link_el else ""
        if href and not href.startswith("http"):
            href = "https://www.pararius.nl" + href

        title_el = item.select_one(".listing-search-item__link--title")
        adres = title_el.get_text(strip=True) if title_el else "Onbekend"

        sub_el = item.select_one(".listing-search-item__sub-title")
        stad = sub_el.get_text(strip=True) if sub_el else "Amersfoort"

        price_el = item.select_one(".listing-search-item__price")
        prijs = price_el.get_text(strip=True) if price_el else ""

        woningen.append({
            "bron": "Pararius",
            "adres": adres,
            "stad": stad,
            "prijs": prijs,
            "url": href,
        })

    return woningen


# =============================================================================
# TELEGRAM & MAIN
# =============================================================================

def stuur_telegram(tekst):
    """Stuur een bericht via Telegram bot."""
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        print("Telegram niet geconfigureerd.")
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
            f"<b>Nieuw huurhuis via {w['bron']}!</b>\n\n"
            f"<b>{w['adres']}</b>\n"
            f"{w['stad']}\n\n"
            f"Huurprijs: EUR {w['prijs']}\n\n"
            f"<a href=\"{w['url']}\">Bekijk woning</a>"
        )
        stuur_telegram(bericht)


def main():
    print(f"[{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] Huurwoning monitor gestart...")

    alle_woningen = []

    # Scrape alle bronnen
    print("\n  [Vesteda]")
    alle_woningen += scrape_vesteda()
    print(f"  {len([w for w in alle_woningen if w['bron'] == 'Vesteda'])} woningen via Vesteda.")

    print("\n  [Govaert]")
    govaert = scrape_govaert()
    alle_woningen += govaert
    print(f"  {len(govaert)} woningen via Govaert.")

    print("\n  [Pararius]")
    pararius = scrape_pararius()
    alle_woningen += pararius
    print(f"  {len(pararius)} woningen via Pararius.")

    print("\n  [Domica]")
    domica = scrape_domica()
    alle_woningen += domica
    print(f"  {len(domica)} woningen via Domica.")

    print("\n  [123Wonen]")
    wonen123 = scrape_123wonen()
    alle_woningen += wonen123
    print(f"  {len(wonen123)} woningen via 123Wonen.")

    print("\n  [Interhouse]")
    interhouse = scrape_interhouse()
    alle_woningen += interhouse
    print(f"  {len(interhouse)} woningen via Interhouse.")

    print("\n  [NederWoon]")
    nederwoon = scrape_nederwoon()
    alle_woningen += nederwoon
    print(f"  {len(nederwoon)} woningen via NederWoon.")

    print("\n  [Huurportaal]")
    huurportaal = scrape_huurportaal()
    alle_woningen += huurportaal
    print(f"  {len(huurportaal)} woningen via Huurportaal.")

    print(f"\n  Totaal: {len(alle_woningen)} woningen.")

    # Laad bekende woningen
    bekende = laad_bekende_woningen()

    # Vergelijk
    nieuwe_woningen = []
    huidige_dict = {}

    for w in alle_woningen:
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
            print(f"    - {info.get('adres', key)} ({info.get('bron', '?')})")


if __name__ == "__main__":
    main()
