#!/usr/bin/env python3
"""Scheduled task runner."""

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
TARGETS = ["amersfoort", "leusden"]

# Site config geladen uit GitHub Secret SITES_JSON. Format:
# { "a": {"name": "...", "url": "..."},
#   "b": {"name": "...", "url_tpl": "https://.../{city}"},
#   "h": {"name": "...", "url_tpl": "...", "groups": {"amersfoort": "2600", ...}},
#   ... }
try:
    SITES = json.loads(os.environ.get("SITES_JSON", "{}"))
except Exception:
    SITES = {}

if not SITES:
    print("WAARSCHUWING: SITES_JSON env var leeg. Voeg secret toe in GitHub.")


def site_url(key, city=None):
    """Bouw URL voor een site (met optionele stad)."""
    cfg = SITES.get(key, {})
    if cfg.get("url"):
        return cfg["url"]
    tpl = cfg.get("url_tpl", "")
    if not tpl or not city:
        return tpl
    gid = cfg.get("groups", {}).get(city, "")
    return tpl.format(city=city, city_cap=city.capitalize(), gid=gid)


def site_name(key):
    """Display naam voor een site (uit secret config)."""
    return SITES.get(key, {}).get("name", key.upper())

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




def base_url(full_url):
    """Extract scheme://host from a URL."""
    from urllib.parse import urlparse
    p = urlparse(full_url)
    return f"{p.scheme}://{p.netloc}"

def sla_bekende_woningen_op(woningen):
    """Sla bekende woningen op naar JSON bestand."""
    with open(BEKENDE_WONINGEN_FILE, "w") as f:
        json.dump(woningen, f, indent=2, ensure_ascii=False)


# =============================================================================
# SOURCE A
# =============================================================================

def scrape_a(url=None):
    """Scrape source A."""
    if url is None:
        url = site_url("a")
    woningen = []

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        context = browser.new_context(
            user_agent=random_ua(),
            viewport={"width": 1440, "height": 900},
        )
        page = context.new_page()
        Stealth().apply_stealth_sync(page)
        # Slow site, use domcontentloaded
        page.goto(url, wait_until="domcontentloaded", timeout=60000)
        # Cookie consent
        try:
            page.click("button:has-text('Akkoord')", timeout=5000)
            page.wait_for_timeout(1000)
        except Exception:
            pass
        # Wacht tot de woninglinks zijn geladen (lazy loaded)
        for _ in range(5):
            try:
                page.wait_for_selector('a[href*="huurwoningen-"]', timeout=8000)
                break
            except Exception:
                # Scroll om lazy loading te triggeren
                page.evaluate("window.scrollBy(0, 500)")
                page.wait_for_timeout(1500)
        page.wait_for_timeout(2000)

        host = base_url(url)
        items = page.evaluate("""(host) => {
            const seen = new Set();
            const results = [];
            const pathHint = SITES_HINT_A;
            document.querySelectorAll('a').forEach(a => {
                if (!a.href.startsWith(host)) return;
                if (!a.href.includes(pathHint)) return;
                if (seen.has(a.href)) return;
                seen.add(a.href);
                let card = a;
                for (let i = 0; i < 6; i++) {
                    if (card.parentElement) card = card.parentElement;
                    if (card.textContent && card.textContent.match(/per maand|EUR/i)) break;
                }
                const text = (card.textContent || '').replace(/\\s+/g, ' ').trim();
                if (text.toLowerCase().includes('verhuurd') || text.toLowerCase().includes('gereserveerd')) return;
                const priceMatch = text.match(/(\\d[\\d.]+),-/) || text.match(/EUR\\s*(\\d[\\d.]+)/i);
                const urlMatch = a.href.match(/-([a-z]+)\\/[^\\/]+\\/([^\\/]+?)-([a-z]+)-\\d+\\/?$/i);
                let adres = '', stad = '';
                if (urlMatch) {
                    adres = urlMatch[2].replace(/-/g, ' ').replace(/\\b\\w/g, c => c.toUpperCase());
                    stad = urlMatch[1].charAt(0).toUpperCase() + urlMatch[1].slice(1);
                }
                results.push({
                    adres: adres,
                    stad: stad,
                    prijs: priceMatch ? priceMatch[1] + ',-' : '',
                    url: a.href
                });
            });
            return results;
        }""".replace("SITES_HINT_A", repr(SITES.get("a", {}).get("path_hint", "huurwoningen-"))), host)
        browser.close()

    for item in items:
        if item["adres"]:
            woningen.append({
                "bron": "a",
                "adres": item["adres"],
                "stad": item.get("stad") or "Amersfoort",
                "prijs": item["prijs"],
                "url": item["url"],
            })

    return woningen


# =============================================================================
# SOURCE B
# =============================================================================

def scrape_b(url):
    """Scrape source B."""
    woningen = []

    try:
        req = urllib.request.Request(url, headers={
            "User-Agent": random_ua()
        })
        response = urllib.request.urlopen(req, timeout=15)
        html = response.read().decode()
    except Exception as e:
        print(f"  FOUT: {e}")
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
            href = base_url(url) + href
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
            "bron": "b",
            "adres": adres,
            "stad": stad,
            "prijs": prijs,
            "url": href,
        })

    return woningen


# =============================================================================
# SOURCE D
# =============================================================================

def scrape_d(url):
    """Scrape source D."""
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
            print("  Geen resultaten.")
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
            "bron": "d",
            "adres": item["adres"],
            "stad": item["stad"],
            "prijs": item["prijs"],
            "url": item["url"],
        })

    return woningen


# =============================================================================
# SOURCE E
# =============================================================================

def scrape_e(url):
    """Scrape source E."""
    woningen = []

    try:
        req = urllib.request.Request(url, headers={
            "User-Agent": random_ua()
        })
        response = urllib.request.urlopen(req, timeout=15)
        html = response.read().decode()
    except Exception as e:
        print(f"  FOUT: {e}")
        return woningen

    soup = BeautifulSoup(html, "html.parser")
    links = soup.select('a[href*="/huur/amersfoort/"]')
    seen = set()

    for a in links:
        href = a.get("href", "")
        if not href:
            continue
        if not href.startswith("http"):
            href = base_url(url) + href
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
            "bron": "e",
            "adres": adres,
            "stad": "Amersfoort",
            "prijs": prijs,
            "url": href,
        })

    return woningen


# =============================================================================
# SOURCE F
# =============================================================================

def scrape_f(url):
    """Scrape source F."""
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
            "bron": "f",
            "adres": item["adres"],
            "stad": "Amersfoort",
            "prijs": "",
            "url": item["url"],
        })

    return woningen


# =============================================================================
# SOURCE G
# =============================================================================

def scrape_g(url):
    """Scrape source G."""
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
                "bron": "g",
                "adres": item["adres"],
                "stad": "Amersfoort",
                "prijs": item["prijs"],
                "url": item["url"],
            })

    return woningen


# =============================================================================
# SOURCE H
# =============================================================================

def scrape_h(url):
    """Scrape source H."""
    woningen = []

    try:
        req = urllib.request.Request(url, headers={
            "User-Agent": random_ua()
        })
        response = urllib.request.urlopen(req, timeout=15)
        html = response.read().decode()
    except Exception as e:
        print(f"  FOUT: {e}")
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
            href = base_url(url) + href

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
            "bron": "h",
            "adres": adres,
            "stad": "Amersfoort",
            "prijs": prijs,
            "url": href,
        })

    return woningen


# =============================================================================
# SOURCE C
# =============================================================================

def scrape_c(url):
    """Scrape source C."""
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
            print("  Geen resultaten.")
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
            href = base_url(url) + href

        title_el = item.select_one(".listing-search-item__link--title")
        adres = title_el.get_text(strip=True) if title_el else "Onbekend"

        sub_el = item.select_one(".listing-search-item__sub-title")
        stad = sub_el.get_text(strip=True) if sub_el else "Amersfoort"

        price_el = item.select_one(".listing-search-item__price")
        prijs = price_el.get_text(strip=True) if price_el else ""

        woningen.append({
            "bron": "c",
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
            f"<b>Nieuw huurhuis via {site_name(w.get('bron', ''))}!</b>\n\n"
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

    # Bouw lijst van (key, scraper, url). Site 'a' gebruikt vaste URL.
    scrape_jobs = [("a", scrape_a, site_url("a"))]

    city_scrapers = [
        ("b", scrape_b), ("c", scrape_c), ("d", scrape_d),
        ("e", scrape_e), ("f", scrape_f), ("g", scrape_g), ("h", scrape_h),
    ]
    for target in TARGETS:
        for key, fn in city_scrapers:
            url = site_url(key, target)
            if url:
                scrape_jobs.append((f"{key}-{target}", fn, url))

    random.shuffle(scrape_jobs)

    for label, scraper, url in scrape_jobs:
        print(f"\n  [{label}]")
        try:
            resultaten = scraper(url)
            alle_woningen += resultaten
            print(f"  {len(resultaten)} via {label}.")
        except Exception as e:
            print(f"  FOUT bij {label}: {e}")
            mislukte_scrapers.append((label, str(e)[:100]))
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
    gefilterd_buiten_target = 0
    gefilterd_duplicaat_adres = 0
    gezien_adressen_deze_run = set()

    for w in alle_woningen:
        prijs_str = w.get("prijs", "")
        adres = w.get("adres", "")
        stad = w.get("stad", "")

        # City-filter: alleen TARGETS toelaten
        combined = (stad + " " + adres).lower()
        if not any(target in combined for target in TARGETS):
            gefilterd_buiten_target += 1
            continue

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
    if gefilterd_buiten_target:
        print(f"  {gefilterd_buiten_target} woning(en) gefilterd (buiten {TARGETS}).")
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
