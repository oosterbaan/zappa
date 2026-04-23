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
import random
import re
import sys
import time
import urllib.request
import urllib.parse
import urllib.error
from datetime import datetime

from playwright.sync_api import sync_playwright
from playwright_stealth import Stealth
from bs4 import BeautifulSoup

# === CONFIGURATIE ===
VESTEDA_URL = "https://www.vesteda.com/nl/woning-zoeken?placeType=1&sortType=1&radius=20&s=Amersfoort&sc=woning&latitude=52.156113&longitude=5.3878264&priceFrom=500&priceTo=9999"
GOVAERT_URL = "https://govaert.nl/woning-huren/actueel-huuraanbod/?_plaatsen=amersfoort"
PARARIUS_URL = "https://www.pararius.nl/huurwoningen/amersfoort"
DOMICA_URL = "https://www.domica.nl/woningaanbod?offer=rent&location=Amersfoort"
WONEN123_URL = "https://www.123wonen.nl/huurwoningen/in/amersfoort"
INTERHOUSE_URL = "https://interhouse.nl/aanbod/?offer=huur&search_terms=Amersfoort&search_type=city"
NEDERWOON_URL = "https://nederwoon.nl/search?city=Amersfoort"
HUURPORTAAL_URL = "https://huurwoningportaal.nl/huurwoningen?view=1&property_search%5Bgroup_ids%5D=2600&property_search%5Bsort%5D=popularity"

# Telegram instellingen
TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "").strip()

# Wisselende user agents
USER_AGENTS = [
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/121.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.2 Safari/605.1.15",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:122.0) Gecko/20100101 Firefox/122.0",
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/119.0.0.0 Safari/537.36",
]

def random_ua():
    return random.choice(USER_AGENTS)

# Bestand om bekende woningen op te slaan
BEKENDE_WONINGEN_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "bekende_woningen.json")


MIN_PRIJS = 1500


def parse_prijs(prijs_str):
    """Extract prijs als integer uit diverse formaten. Retourneert None als onbekend."""
    if not prijs_str:
        return None
    # Verwijder EUR, €, spaties, "per maand", "p/mnd", ",-"
    cleaned = re.sub(r"[€EUR]|per maand|p/mnd|,-|kale huur", "", prijs_str, flags=re.IGNORECASE).strip()
    # Match getal (met eventueel punt als duizendtal en ,00 als decimaal)
    match = re.search(r"(\d[\d.]*)", cleaned)
    if not match:
        return None
    num = match.group(1).replace(".", "")
    try:
        return int(num)
    except ValueError:
        return None


def normalize_adres(adres):
    """Normaliseer adres voor vergelijking: lowercase, alleen letters+cijfers."""
    if not adres:
        return ""
    # Lowercase, strip whitespace
    a = adres.lower().strip()
    # Verwijder Gereserveerd/Nieuw prefixen
    a = re.sub(r"^(gereserveerd|nieuw|te huur|verhuurd)\s+", "", a)
    # Houd alleen letters, cijfers en spaties
    a = re.sub(r"[^a-z0-9\s]", " ", a)
    # Normaliseer whitespace
    a = re.sub(r"\s+", " ", a).strip()
    return a


def normalize_url(url):
    """Normaliseer URL (verwijder trailing slash, lowercase host, query params)."""
    if not url:
        return ""
    # Strip trailing slash en fragment
    url = url.rstrip("/").split("#")[0]
    # Strip query parameters die we niet nodig hebben
    url = url.split("?")[0]
    return url


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
    """Scrape vesteda.com (geen login nodig, Playwright voor JS-rendering)."""
    woningen = []

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        context = browser.new_context(
            user_agent=random_ua(),
            viewport={"width": 1440, "height": 900},
        )
        page = context.new_page()
        Stealth().apply_stealth_sync(page)
        page.goto(VESTEDA_URL, wait_until="networkidle", timeout=30000)
        page.wait_for_timeout(3000)

        items = page.evaluate("""
            const seen = new Set();
            const results = [];
            document.querySelectorAll('a[href*="vesteda.com/nl/"]').forEach(a => {
                if (seen.has(a.href) || a.href.includes('woning-zoeken')) return;
                if (!a.href.includes('amersfoort') && !a.href.includes('huurwoning')) return;
                seen.add(a.href);
                const text = a.textContent.replace(/\\s+/g, ' ').trim();
                const priceMatch = text.match(/(\\d[\\d.]+),-/);
                const adresMatch = text.match(/^(?:Gereserveerd\\s+)?(.+?)\\s*EUR/i) || text.match(/^(?:Gereserveerd\\s+)?(.+?)\\s*per maand/i);
                // Get first line as address
                const lines = text.split(/EUR|per maand|Woonoppervlakte|Amersfoort/);
                let adres = lines[0].replace('Gereserveerd', '').replace(/^\\s+|\\s+$/g, '');
                results.push({
                    adres: adres,
                    prijs: priceMatch ? priceMatch[1] + ',-' : '',
                    url: a.href
                });
            });
            results;
        """)
        browser.close()

    for item in items:
        if item["adres"]:
            woningen.append({
                "bron": "Vesteda",
                "adres": item["adres"],
                "stad": "Amersfoort",
                "prijs": item["prijs"],
                "url": item["url"],
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
            "User-Agent": random_ua()
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
        if not href:
            continue
        if not href.startswith("http"):
            href = "https://govaert.nl" + href
        norm_href = normalize_url(href)
        if norm_href in seen_urls:
            continue
        seen_urls.add(norm_href)

        street_el = link.select_one(".object-street")
        number_el = link.select_one(".object-housenumber")
        city_el = link.select_one(".object-city")

        if not street_el:
            continue

        adres = street_el.get_text(strip=True)
        if number_el:
            adres += " " + number_el.get_text(strip=True)

        stad = city_el.get_text(strip=True) if city_el else "Amersfoort"

        # Filter verhuurd/onder optie (status zit in een badge in de link)
        tekst_lower = link.get_text().lower()
        if "verhuurd" in tekst_lower or "onder optie" in tekst_lower:
            continue

        # Prijs
        tekst = link.get_text()
        prijs_match = re.search(r"([\d.]+)\s*(?:\n|\t)*\s*per maand", tekst)
        prijs = prijs_match.group(1) + " per maand" if prijs_match else ""

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
            user_agent=random_ua(),
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
                status: (item.textContent || '').toLowerCase(),
                url: item.href || ''
            }))
        """)
        browser.close()

    for item in items:
        if not item["adres"]:
            continue
        # Filter verhuurde woningen
        status = item.get("status", "")
        if "verhuurd" in status or "onder optie" in status or "gereserveerd" in status:
            continue
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
            "User-Agent": random_ua()
        })
        response = urllib.request.urlopen(req, timeout=15)
        html = response.read().decode()
    except Exception as e:
        print(f"  FOUT bij ophalen 123Wonen: {e}")
        return woningen

    soup = BeautifulSoup(html, "html.parser")
    links = soup.select('a[href*="/huur/amersfoort/"]')
    seen = set()

    for a in links:
        href = a.get("href", "")
        if not href:
            continue
        if not href.startswith("http"):
            href = "https://www.123wonen.nl" + href
        norm = normalize_url(href)
        if norm in seen:
            continue
        seen.add(norm)

        # Ga omhoog naar de kaart
        card = a.parent
        for _ in range(5):
            if card.get_text() and "p/mnd" in card.get_text():
                break
            card = card.parent

        text = " ".join(card.get_text().split())

        # Filter verhuurd
        if "verhuurd" in text.lower() or "onder optie" in text.lower():
            continue

        # Adres
        adres_match = re.search(r"Amersfoort,\s*([A-Za-z\s]+(?:\d[\w\s-]*)?)", text)
        adres = adres_match.group(1).strip() if adres_match else "Onbekend"

        # Prijs
        prijs_match = re.search(r"([\d.]+,-)\s*p/mnd", text)
        prijs = prijs_match.group(1) + " p/mnd" if prijs_match else ""

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
            user_agent=random_ua(),
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
            user_agent=random_ua(),
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
            "User-Agent": random_ua()
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
            user_agent=random_ua(),
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
    """Stuur een bericht via Telegram bot naar alle chat IDs (komma-gescheiden)."""
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        print("Telegram niet geconfigureerd.")
        print(tekst)
        return False

    chat_ids = [c.strip() for c in TELEGRAM_CHAT_ID.split(",") if c.strip()]
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    any_success = False

    for chat_id in chat_ids:
        payload = json.dumps({
            "chat_id": chat_id,
            "text": tekst,
            "parse_mode": "HTML",
            "disable_web_page_preview": False,
        })
        try:
            req = urllib.request.Request(url, data=payload.encode(), headers={"Content-Type": "application/json"})
            urllib.request.urlopen(req)
            print(f"  Telegram bericht verzonden naar {chat_id}!")
            any_success = True
        except urllib.error.HTTPError as e:
            body = e.read().decode()
            print(f"  FOUT bij Telegram ({chat_id}): {e} - {body}")
        except Exception as e:
            print(f"  FOUT bij Telegram ({chat_id}): {e}")

    return any_success


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

    # Scrape alle bronnen in willekeurige volgorde (minder voorspelbaar)
    scrapers = [
        ("Vesteda", scrape_vesteda),
        ("Govaert", scrape_govaert),
        ("Pararius", scrape_pararius),
        ("Domica", scrape_domica),
        ("123Wonen", scrape_123wonen),
        ("Interhouse", scrape_interhouse),
        ("NederWoon", scrape_nederwoon),
        ("Huurportaal", scrape_huurportaal),
    ]
    random.shuffle(scrapers)

    for naam, scraper in scrapers:
        print(f"\n  [{naam}]")
        try:
            resultaten = scraper()
            alle_woningen += resultaten
            print(f"  {len(resultaten)} woningen via {naam}.")
        except Exception as e:
            print(f"  FOUT bij {naam}: {e}")
        # Wacht 5-15 seconden tussen sites
        pauze = random.randint(5, 15)
        print(f"  (pauze {pauze}s)")
        time.sleep(pauze)

    print(f"\n  Totaal: {len(alle_woningen)} woningen.")

    # Laad bekende woningen
    bekende = laad_bekende_woningen()

    # Vergelijk
    nieuwe_woningen = []
    huidige_dict = {}

    # Migreer oude bekende woningen naar genormaliseerde keys + bouw adres-index
    bekende_genormaliseerd = {}
    bekende_adressen = set()
    for old_key, val in bekende.items():
        new_key = normalize_url(old_key) if old_key.startswith("http") else old_key
        bekende_genormaliseerd[new_key] = val
        # Bouw adres-lookup
        if isinstance(val, dict):
            adres_key = normalize_adres(val.get("adres", ""))
            if adres_key:
                bekende_adressen.add(adres_key)

    gefilterd_te_goedkoop = 0
    gefilterd_duplicaat_adres = 0
    gezien_adressen_deze_run = set()

    for w in alle_woningen:
        # Filter op minimale prijs
        prijs_num = parse_prijs(w.get("prijs", ""))
        if prijs_num is not None and prijs_num < MIN_PRIJS:
            gefilterd_te_goedkoop += 1
            continue

        raw_key = w["url"] or w["adres"]
        key = normalize_url(raw_key) if raw_key.startswith("http") else raw_key
        if key in huidige_dict:
            continue  # Skip duplicaten in dezelfde scrape

        # Adres-filter: zelfde straat+nummer al bekend? Sla over
        adres_key = normalize_adres(w.get("adres", ""))
        if adres_key and adres_key in bekende_adressen:
            huidige_dict[key] = w  # Wel opslaan zodat verdwijnen correct werkt
            continue
        if adres_key and adres_key in gezien_adressen_deze_run:
            gefilterd_duplicaat_adres += 1
            huidige_dict[key] = w
            continue

        huidige_dict[key] = w
        if key not in bekende_genormaliseerd:
            nieuwe_woningen.append(w)
            if adres_key:
                gezien_adressen_deze_run.add(adres_key)

    if gefilterd_te_goedkoop:
        print(f"  {gefilterd_te_goedkoop} woning(en) gefilterd (prijs < EUR {MIN_PRIJS}).")
    if gefilterd_duplicaat_adres:
        print(f"  {gefilterd_duplicaat_adres} woning(en) gefilterd (duplicaat adres).")

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
