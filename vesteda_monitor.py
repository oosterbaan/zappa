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
# Steden om te scrapen (URL-vriendelijke namen, lowercase)
STEDEN = ["amersfoort", "leusden"]

# URL-templates per site (gebruik {city} als placeholder)
GOVAERT_URL = "https://govaert.nl/woning-huren/actueel-huuraanbod/?_plaatsen={city}"
PARARIUS_URL = "https://www.pararius.nl/huurwoningen/{city}"
DOMICA_URL = "https://www.domica.nl/woningaanbod?offer=rent&location={city_cap}"
WONEN123_URL = "https://www.123wonen.nl/huurwoningen/in/{city}"
INTERHOUSE_URL = "https://interhouse.nl/aanbod/?offer=huur&search_terms={city_cap}&search_type=city"
NEDERWOON_URL = "https://nederwoon.nl/search?city={city_cap}"
# Huurportaal werkt met group_ids per regio
HUURPORTAAL_GROUP_IDS = {"amersfoort": "2600", "leusden": "2620"}
HUURPORTAAL_URL = "https://huurwoningportaal.nl/huurwoningen?view=1&property_search%5Bgroup_ids%5D={gid}&property_search%5Bsort%5D=popularity"

# Vesteda: 20km radius rond Amersfoort dekt ook Leusden, dus 1 call
VESTEDA_URL = "https://www.vesteda.com/nl/woning-zoeken?placeType=1&sortType=1&radius=20&s=Amersfoort&sc=woning&latitude=52.156113&longitude=5.3878264&priceFrom=500&priceTo=9999"


def url_voor(template, city):
    """Vul stad in URL template."""
    return template.format(city=city, city_cap=city.capitalize())

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


# === FILTER INSTELLINGEN ===
MIN_PRIJS = 1500           # Minimaal in euro (None = geen minimum)
MAX_PRIJS = 2500           # Maximaal in euro (None = geen maximum)
MIN_OPPERVLAKTE = 60       # Minimaal m² (alleen filteren als oppervlakte bekend is)
MIN_SLAAPKAMERS = 2        # Minimaal aantal slaapkamers (alleen filteren als bekend)
VERGEET_NA_DAGEN = 180     # Woningen 6 maanden onthouden


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


POSTCODE_RE = re.compile(r"\b(\d{4}\s*[A-Z]{2})\b")


def extract_postcode(*texts):
    """Vind een Nederlandse postcode in een of meer tekstvelden."""
    for t in texts:
        if not t:
            continue
        m = POSTCODE_RE.search(t.upper())
        if m:
            return m.group(1).replace(" ", "")
    return ""


def parse_oppervlakte(*texts):
    """Vind oppervlakte in m² in tekstvelden. Retourneert None als onbekend."""
    for t in texts:
        if not t:
            continue
        m = re.search(r"(\d{2,4})\s*m(?:²|2|\\u00b2)?", t)
        if m:
            try:
                return int(m.group(1))
            except ValueError:
                pass
    return None


def parse_slaapkamers(*texts):
    """Vind aantal slaapkamers. 'X slaapkamers' = X. 'X kamers' = X-1 (1 woonkamer).
    Retourneert None als onbekend."""
    for t in texts:
        if not t:
            continue
        low = t.lower()
        # Eerst expliciet 'slaapkamers' (zowel 'X slaapkamers' als 'slaapkamer(s) X')
        m = re.search(r"(\d+)\s*slaapkamer", low) or re.search(r"slaapkamer[\(\)s]*\s*(\d+)", low)
        if m:
            try:
                return int(m.group(1))
            except ValueError:
                pass
        # Anders 'kamers' (totaal kamers, -1 voor woonkamer)
        m = re.search(r"(\d+)\s*kamer", low)
        if m:
            try:
                return max(0, int(m.group(1)) - 1)
            except ValueError:
                pass
    return None


def is_verhuurd(*texts):
    """Check of een woning verhuurd/onder optie/gereserveerd is."""
    for t in texts:
        if not t:
            continue
        low = t.lower()
        if "verhuurd" in low or "onder optie" in low or "gereserveerd" in low:
            return True
    return False


def normalize_adres(adres):
    """Normaliseer adres voor vergelijking: lowercase, alleen letters+cijfers,
    woningtype-prefixen verwijderd."""
    if not adres:
        return ""
    a = adres.lower().strip()
    # Verwijder status prefixen
    a = re.sub(r"^(gereserveerd|nieuw|te huur|verhuurd|onder optie)\s+", "", a)
    # Verwijder woningtype prefixen (Appartement, Woning, Studio, etc.)
    a = re.sub(
        r"^(appartement|woning|studio|huis|eengezinswoning|tussenwoning|"
        r"hoekwoning|kamer|2-onder-1-kap|vrijstaand|maisonnette|penthouse|"
        r"loft|bovenwoning|benedenwoning|herenhuis|villa)\s+",
        "",
        a,
    )
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

def scrape_vesteda(url=None):
    if url is None:
        url = VESTEDA_URL
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
        page.goto(url, wait_until="networkidle", timeout=30000)
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

def scrape_govaert(url):
    """Scrape Govaert makelaardij (geen login nodig)."""
    woningen = []

    try:
        req = urllib.request.Request(url, headers={
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

def scrape_domica(url):
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
        page.goto(url, wait_until="networkidle", timeout=30000)

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

def scrape_123wonen(url):
    """Scrape 123Wonen (geen login of Playwright nodig)."""
    woningen = []

    try:
        req = urllib.request.Request(url, headers={
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

def scrape_interhouse(url):
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
        page.goto(url, wait_until="networkidle", timeout=30000)
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

def scrape_nederwoon(url):
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
        page.goto(url, wait_until="networkidle", timeout=30000)
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

def scrape_huurportaal(url):
    """Scrape Huurwoningportaal (geen login of Playwright nodig)."""
    woningen = []

    try:
        req = urllib.request.Request(url, headers={
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

def scrape_pararius(url):
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
        page.goto(url, wait_until="networkidle", timeout=30000)

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
    mislukte_scrapers = []  # Voor crash-notifier

    # Bouw lijst van (naam, scraper-functie, url) per stad
    # Vesteda: 1x (radius dekt beide steden)
    scrape_jobs = [("Vesteda", scrape_vesteda, VESTEDA_URL)]

    for stad in STEDEN:
        scrape_jobs.append((f"Govaert {stad.capitalize()}", scrape_govaert, url_voor(GOVAERT_URL, stad)))
        scrape_jobs.append((f"Pararius {stad.capitalize()}", scrape_pararius, url_voor(PARARIUS_URL, stad)))
        scrape_jobs.append((f"Domica {stad.capitalize()}", scrape_domica, url_voor(DOMICA_URL, stad)))
        scrape_jobs.append((f"123Wonen {stad.capitalize()}", scrape_123wonen, url_voor(WONEN123_URL, stad)))
        scrape_jobs.append((f"Interhouse {stad.capitalize()}", scrape_interhouse, url_voor(INTERHOUSE_URL, stad)))
        scrape_jobs.append((f"NederWoon {stad.capitalize()}", scrape_nederwoon, url_voor(NEDERWOON_URL, stad)))
        gid = HUURPORTAAL_GROUP_IDS.get(stad)
        if gid:
            scrape_jobs.append((f"Huurportaal {stad.capitalize()}", scrape_huurportaal, HUURPORTAAL_URL.format(gid=gid)))

    random.shuffle(scrape_jobs)

    for naam, scraper, url in scrape_jobs:
        print(f"\n  [{naam}]")
        try:
            resultaten = scraper(url)
            if not resultaten:
                mislukte_scrapers.append((naam, "0 resultaten"))
            alle_woningen += resultaten
            print(f"  {len(resultaten)} woningen via {naam}.")
        except Exception as e:
            print(f"  FOUT bij {naam}: {e}")
            mislukte_scrapers.append((naam, str(e)[:100]))
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
    def make_adres_key(woning):
        """Bouw een unique key uit adres + postcode (of adres alleen)."""
        adres = normalize_adres(woning.get("adres", ""))
        postcode = extract_postcode(
            woning.get("stad", ""),
            woning.get("adres", ""),
        )
        if adres and postcode:
            return f"{adres}|{postcode}"
        return adres

    bekende_genormaliseerd = {}
    bekende_adressen = set()
    for old_key, val in bekende.items():
        new_key = normalize_url(old_key) if old_key.startswith("http") else old_key
        bekende_genormaliseerd[new_key] = val
        if isinstance(val, dict):
            ak = make_adres_key(val)
            if ak:
                bekende_adressen.add(ak)

    gefilterd_te_goedkoop = 0
    gefilterd_te_duur = 0
    gefilterd_te_klein = 0
    gefilterd_verhuurd = 0
    gefilterd_te_weinig_kamers = 0
    gefilterd_duplicaat_adres = 0
    gezien_adressen_deze_run = set()

    for w in alle_woningen:
        prijs_str = w.get("prijs", "")
        adres = w.get("adres", "")
        stad = w.get("stad", "")

        # Filter verhuurd op alle sites (extra check)
        if is_verhuurd(prijs_str, adres, stad):
            gefilterd_verhuurd += 1
            continue

        # Prijs-filters
        prijs_num = parse_prijs(prijs_str)
        if prijs_num is not None:
            if MIN_PRIJS is not None and prijs_num < MIN_PRIJS:
                gefilterd_te_goedkoop += 1
                continue
            if MAX_PRIJS is not None and prijs_num > MAX_PRIJS:
                gefilterd_te_duur += 1
                continue

        # Oppervlakte-filter (alleen als bekend)
        opp = parse_oppervlakte(prijs_str, adres, stad)
        if opp is not None and MIN_OPPERVLAKTE is not None and opp < MIN_OPPERVLAKTE:
            gefilterd_te_klein += 1
            continue

        # Slaapkamers-filter (alleen als bekend)
        sk = parse_slaapkamers(prijs_str, adres, stad)
        if sk is not None and MIN_SLAAPKAMERS is not None and sk < MIN_SLAAPKAMERS:
            gefilterd_te_weinig_kamers += 1
            continue

        raw_key = w["url"] or adres
        key = normalize_url(raw_key) if raw_key.startswith("http") else raw_key
        if key in huidige_dict:
            continue

        # Adres-filter (met postcode indien beschikbaar)
        adres_key = make_adres_key(w)
        if adres_key and adres_key in bekende_adressen:
            huidige_dict[key] = w
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
    if gefilterd_te_duur:
        print(f"  {gefilterd_te_duur} woning(en) gefilterd (prijs > EUR {MAX_PRIJS}).")
    if gefilterd_te_klein:
        print(f"  {gefilterd_te_klein} woning(en) gefilterd (oppervlakte < {MIN_OPPERVLAKTE} m²).")
    if gefilterd_verhuurd:
        print(f"  {gefilterd_verhuurd} woning(en) gefilterd (verhuurd/onder optie).")
    if gefilterd_te_weinig_kamers:
        print(f"  {gefilterd_te_weinig_kamers} woning(en) gefilterd (< {MIN_SLAAPKAMERS} slaapkamers).")
    if gefilterd_duplicaat_adres:
        print(f"  {gefilterd_duplicaat_adres} woning(en) gefilterd (duplicaat adres).")

    if nieuwe_woningen:
        print(f"  {len(nieuwe_woningen)} NIEUWE woning(en) gevonden!")
        meld_nieuwe_woningen(nieuwe_woningen)
    else:
        print("  Geen nieuwe woningen.")

    # Sla huidige stand op + behoud oude entries (max VERGEET_NA_DAGEN dagen)
    nu_iso = datetime.now().isoformat()
    cutoff_ts = (datetime.now().timestamp() - VERGEET_NA_DAGEN * 86400)
    samengevoegd = {}

    # Begin met oude entries die nog niet "verlopen" zijn
    for old_key, val in bekende.items():
        new_key = normalize_url(old_key) if old_key.startswith("http") else old_key
        if isinstance(val, dict):
            last_seen = val.get("last_seen")
            try:
                last_seen_ts = datetime.fromisoformat(last_seen).timestamp() if last_seen else cutoff_ts
            except Exception:
                last_seen_ts = cutoff_ts
            if last_seen_ts >= cutoff_ts:
                samengevoegd[new_key] = val
        else:
            samengevoegd[new_key] = {"last_seen": nu_iso}

    # Voeg/overschrijf met huidige run
    for key, w in huidige_dict.items():
        w_met_ts = dict(w)
        w_met_ts["last_seen"] = nu_iso
        samengevoegd[key] = w_met_ts

    sla_bekende_woningen_op(samengevoegd)
    print(f"  {len(samengevoegd)} woningen onthouden (oud + huidig).")

    # Crash-notifier: stuur Telegram als de helft of meer scrapers faalt
    if len(mislukte_scrapers) >= len(scrape_jobs) // 2:
        details = "\n".join(f"- <b>{naam}</b>: {fout}" for naam, fout in mislukte_scrapers)
        stuur_telegram(
            f"⚠️ <b>Huurmonitor: scrapers falen!</b>\n\n"
            f"{len(mislukte_scrapers)}/{len(scrape_jobs)} scrapers faalden:\n{details}\n\n"
            f"Check de GitHub Actions logs."
        )


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        import traceback
        tb = traceback.format_exc()
        print(tb)
        try:
            stuur_telegram(
                f"❌ <b>Huurmonitor crashed!</b>\n\n"
                f"<code>{str(exc)[:500]}</code>\n\n"
                f"Check GitHub Actions logs voor details."
            )
        except Exception:
            pass
        # Niet falen voor de Action — anders krijg je weer foutmails
        sys.exit(0)
