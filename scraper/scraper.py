#!/usr/bin/env python3
"""
Syttende Vinkort Monitor — Scraper Agent
========================================
Kører dagligt (GitHub Actions cron).
1. Henter de 4 PDF-vinkort fra syttende.dk
2. Parser vinnavn, producent, region, priser via Claude AI
3. Differ mod Supabase — finder tilføjede/fjernede/prisændrede vine
4. For nye vine: søger på Wine-Searcher.com efter EU lavpris
5. Gemmer alt i Supabase

Kræver env vars:
  SUPABASE_URL
  SUPABASE_SERVICE_KEY   (service role — ikke anon)
  ANTHROPIC_API_KEY
  WINE_SEARCHER_API_KEY  (valgfrit — falder tilbage til web scraping)
"""

import os
import re
import json
import hashlib
import time
import httpx
import pdfplumber
import anthropic
from datetime import datetime, timezone
from io import BytesIO
from typing import Optional

# ─── CONFIG ───────────────────────────────────────────────────────────────────
SUPABASE_URL = os.environ["SUPABASE_URL"]
SUPABASE_KEY = os.environ["SUPABASE_SERVICE_KEY"]
ANTHROPIC_API_KEY = os.environ["ANTHROPIC_API_KEY"]
WINE_SEARCHER_KEY = os.environ.get("WINE_SEARCHER_API_KEY")  # valgfrit

WINE_PAGE_URL = "https://www.syttende.dk/vinen"

PDF_SOURCES = {
    "bobler": "https://cdn.prod.website-files.com/68e4e0d05e784db5b02931a0/698f5696066729ad6f5d5bd4_77e5403ce19c0082f527b3a7ca1b86c7_Vinkort%20BOBLER%20%26%20S%C3%98DT.pdf",
    "hvidvin": "https://cdn.prod.website-files.com/68e4e0d05e784db5b02931a0/698f5696d37acaa6f98eed15_db0c5c76c52868dc16dc5d110d8cf687_Vinkort%20HVIDVIN.pdf",
    "roserod": "https://cdn.prod.website-files.com/68e4e0d05e784db5b02931a0/698f56967915ca645f55ab51_3d3d5376de879ad7ba5d8413cfe1f6d4_Vinkort%20ROS%C3%88%20%26%20R%C3%98DVIN.pdf",
    "avec": "https://cdn.prod.website-files.com/68e4e0d05e784db5b02931a0/69bbf8cf10f6a1d7d706adbd_04ef0f7aac2b42b057991bb7a68656e3_Avec.pdf",
}

# ─── SUPABASE CLIENT ──────────────────────────────────────────────────────────
class Supabase:
    def __init__(self, url: str, key: str):
        self.base = url.rstrip("/") + "/rest/v1"
        self.headers = {
            "apikey": key,
            "Authorization": f"Bearer {key}",
            "Content-Type": "application/json",
            "Prefer": "return=representation",
        }

    def select(self, table: str, query: str = "") -> list:
        r = httpx.get(f"{self.base}/{table}?{query}", headers=self.headers, timeout=30)
        r.raise_for_status()
        return r.json()

    def upsert(self, table: str, data: dict | list, on_conflict: str = "id") -> list:
        r = httpx.post(
            f"{self.base}/{table}?on_conflict={on_conflict}",
            headers={**self.headers, "Prefer": "return=representation,resolution=merge-duplicates"},
            json=data, timeout=30,
        )
        r.raise_for_status()
        return r.json()

    def insert(self, table: str, data: dict | list) -> list:
        r = httpx.post(f"{self.base}/{table}", headers=self.headers, json=data, timeout=30)
        r.raise_for_status()
        return r.json()

    def update(self, table: str, match: str, data: dict) -> list:
        r = httpx.patch(f"{self.base}/{table}?{match}", headers=self.headers, json=data, timeout=30)
        r.raise_for_status()
        return r.json()


db = Supabase(SUPABASE_URL, SUPABASE_KEY)
claude = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)


# ─── STEP 1: DISCOVER PDF URLS ────────────────────────────────────────────────
def discover_pdf_urls() -> dict[str, str]:
    """
    Henter vinkort-siden og finder de aktuelle PDF-links dynamisk.
    Returnerer dict med samme keys som PDF_SOURCES som fallback.
    """
    print("🔍 Opdager PDF-URLs fra syttende.dk/vinen …")
    try:
        resp = httpx.get(WINE_PAGE_URL, timeout=20, follow_redirects=True)
        resp.raise_for_status()
        html = resp.text

        urls = {}
        patterns = {
            "bobler": r'(https://cdn\.prod\.website-files\.com[^"\']*Vinkort[^"\']*BOBLER[^"\']*\.pdf)',
            "hvidvin": r'(https://cdn\.prod\.website-files\.com[^"\']*Vinkort[^"\']*HVIDVIN[^"\']*\.pdf)',
            "roserod": r'(https://cdn\.prod\.website-files\.com[^"\']*Vinkort[^"\']*ROS[^"\']*\.pdf)',
            "avec": r'(https://cdn\.prod\.website-files\.com[^"\']*Avec[^"\']*\.pdf)',
        }
        for key, pat in patterns.items():
            m = re.search(pat, html, re.IGNORECASE)
            if m:
                urls[key] = m.group(1)
                print(f"  ✓ {key}: {m.group(1)[:80]}…")
            else:
                urls[key] = PDF_SOURCES[key]
                print(f"  ⚠ {key}: bruger fallback URL")
        return urls
    except Exception as e:
        print(f"  ⚠ Kunne ikke hente side: {e}. Bruger hardkodede URLs.")
        return PDF_SOURCES


# ─── STEP 2: DOWNLOAD & HASH PDFS ────────────────────────────────────────────
def download_pdf(url: str) -> tuple[bytes, str]:
    """Downloader en PDF og returnerer (bytes, sha256)."""
    resp = httpx.get(url, timeout=60, follow_redirects=True)
    resp.raise_for_status()
    data = resp.content
    sha = hashlib.sha256(data).hexdigest()
    return data, sha


def extract_text_from_pdf(pdf_bytes: bytes) -> str:
    """Udtrækker råtekst fra PDF via pdfplumber."""
    text_parts = []
    with pdfplumber.open(BytesIO(pdf_bytes)) as pdf:
        for page in pdf.pages:
            t = page.extract_text()
            if t:
                text_parts.append(t)
    return "\n".join(text_parts)


# ─── STEP 3: PARSE VINKORT MED CLAUDE ────────────────────────────────────────
PARSE_SYSTEM = """Du er en ekspert i at parse restaurantvinkort fra PDF-tekst til struktureret JSON.
Returner KUN et JSON-array — ingen forklaring, ingen markdown, ingen backticks.

Hvert element skal have disse felter (brug null hvis ukendt):
{
  "name": "vinens fulde navn",
  "producer": "producentens navn",
  "region": "region og land, f.eks. Bourgogne, Frankrig",
  "vintage": "årstal som string eller null",
  "glass_price": heltal i DKK eller null,
  "bottle_price": heltal i DKK eller null,
  "grape": "druesort(er) eller null",
  "notes": "evt. beskrivelse fra kortet eller null"
}

Regler:
- Ekskluder sektionsoverskrifter, sidehoveder, beskrivende tekst der ikke er vine
- Priser er altid i DKK (danske kroner)
- Brug aldrig estimerede priser — kun det der står direkte i teksten
- Hvis en vin kun har flaske-pris, sæt glass_price til null (og omvendt)
"""

def parse_wines_with_claude(raw_text: str, category: str) -> list[dict]:
    """Bruger Claude til at parse vinkort-tekst til struktureret JSON."""
    print(f"  🤖 Parser {category} med Claude AI …")
    
    prompt = f"""Her er teksten fra Syttende restaurant's vinkort — kategori: {category}

---
{raw_text[:12000]}
---

Parse alle vine og returner JSON-array som instrueret."""

    msg = claude.messages.create(
        model="claude-sonnet-4-20250514",
        max_tokens=4000,
        system=PARSE_SYSTEM,
        messages=[{"role": "user", "content": prompt}],
    )
    
    raw = msg.content[0].text.strip()
    # Fjern evt. markdown-fences
    raw = re.sub(r"^```(?:json)?\s*", "", raw)
    raw = re.sub(r"\s*```$", "", raw)
    
    wines = json.loads(raw)
    for w in wines:
        w["category"] = category
    print(f"  ✓ Fandt {len(wines)} vine i {category}")
    return wines


# ─── STEP 4: WINE-SEARCHER LOOKUP ────────────────────────────────────────────
def search_wine_searcher(wine_name: str, producer: Optional[str]) -> tuple[Optional[int], Optional[str]]:
    """
    Søger på Wine-Searcher efter EU lavpris.
    Returnerer (pris_i_dkk, url) eller (None, None).
    
    Bruger Wine-Searcher API v3.3 hvis WINE_SEARCHER_KEY er sat,
    ellers web-scraping som fallback.
    """
    query = f"{producer} {wine_name}" if producer else wine_name
    
    if WINE_SEARCHER_KEY:
        # Wine-Searcher Pro API
        try:
            url = "https://api.wine-searcher.com/api/default/v1/wine"
            params = {"api_key": WINE_SEARCHER_KEY, "name": query, "location": "eu"}
            r = httpx.get(url, params=params, timeout=15)
            if r.status_code == 200:
                data = r.json()
                # Find første EU-pris
                for result in data.get("search_results", [])[:5]:
                    for price_data in result.get("prices", []):
                        if price_data.get("currency") in ("EUR", "DKK"):
                            price_eur = float(price_data.get("price", 0))
                            # Omregn EUR → DKK (ca. 7.46)
                            price_dkk = int(price_eur * 7.46) if price_data.get("currency") == "EUR" else int(price_eur)
                            wine_url = f"https://www.wine-searcher.com/find/{query.replace(' ', '+')}"
                            return price_dkk, wine_url
        except Exception as e:
            print(f"    ⚠ Wine-Searcher API fejl: {e}")
    
    # Fallback: konstruer søge-URL (manuel søgning)
    search_url = f"https://www.wine-searcher.com/find/{query.replace(' ', '+')}/1/dk"
    try:
        headers = {"User-Agent": "Mozilla/5.0 (compatible; WineMonitor/1.0)"}
        r = httpx.get(search_url, headers=headers, timeout=15, follow_redirects=True)
        if r.status_code == 200:
            # Simpel regex efter pris på siden
            m = re.search(r'data-price="([\d.]+)".*?data-currency="(EUR|DKK)"', r.text)
            if m:
                price_val = float(m.group(1))
                currency = m.group(2)
                price_dkk = int(price_val * 7.46) if currency == "EUR" else int(price_val)
                return price_dkk, search_url
    except Exception as e:
        print(f"    ⚠ Wine-Searcher scrape fejl: {e}")
    
    # Returner kun URL hvis vi ikke kan finde pris
    return None, f"https://www.wine-searcher.com/find/{query.replace(' ', '+')}"


# ─── STEP 5: DIFF OG GEM ─────────────────────────────────────────────────────
def make_wine_key(wine: dict) -> str:
    """Laver en stabil nøgle til at matche vine på tværs af scrapes."""
    name = (wine.get("name") or "").lower().strip()
    producer = (wine.get("producer") or "").lower().strip()
    return f"{producer}|{name}"


def run_diff_and_save(category: str, new_wines: list[dict], pdf_hash: str):
    """
    Differ nye vine mod database.
    Gemmer ændringer og opdaterer wine-tabellen.
    """
    print(f"\n📊 Differ {category} mod database …")
    now = datetime.now(timezone.utc).isoformat()

    # Hent eksisterende vine fra DB for denne kategori
    existing = db.select("wines", f"category=eq.{category}&is_active=eq.true")
    existing_map = {make_wine_key(w): w for w in existing}
    new_map = {make_wine_key(w): w for w in new_wines}

    changes = []

    # ── Tilføjede vine ──────────────────────────────────────────────────────
    for key, wine in new_map.items():
        if key not in existing_map:
            print(f"  ➕ Ny vin: {wine['name']}")
            
            # Gem vin i DB
            wine_record = {
                **wine,
                "is_active": True,
                "first_seen_at": now,
                "last_seen_at": now,
                "pdf_hash": pdf_hash,
            }
            saved = db.upsert("wines", wine_record, on_conflict="category,name,producer")
            wine_id = saved[0]["id"] if saved else None

            # Wine-Searcher lookup
            ws_price, ws_url = None, None
            if wine.get("name"):
                print(f"    🍷 Søger Wine-Searcher: {wine['name']} …")
                ws_price, ws_url = search_wine_searcher(wine.get("name"), wine.get("producer"))
                time.sleep(1.5)  # Respekter rate limits

            changes.append({
                "wine_id": wine_id,
                "change_type": "added",
                "detected_at": now,
                "notes": f"Tilføjet til vinkortet",
                "wine_searcher_price": ws_price,
                "wine_searcher_url": ws_url,
                "old_values": None,
                "new_values": json.dumps(wine, ensure_ascii=False),
            })

    # ── Fjernede vine ───────────────────────────────────────────────────────
    for key, wine in existing_map.items():
        if key not in new_map:
            print(f"  ➖ Fjernet: {wine['name']}")
            db.update("wines", f"id=eq.{wine['id']}", {"is_active": False})
            changes.append({
                "wine_id": wine["id"],
                "change_type": "removed",
                "detected_at": now,
                "notes": "Fjernet fra vinkortet",
                "wine_searcher_price": None,
                "wine_searcher_url": None,
                "old_values": json.dumps(wine, ensure_ascii=False),
                "new_values": None,
            })

    # ── Prisændringer ───────────────────────────────────────────────────────
    for key, new_w in new_map.items():
        if key in existing_map:
            old_w = existing_map[key]
            price_changes = []
            if old_w.get("glass_price") != new_w.get("glass_price") and new_w.get("glass_price"):
                price_changes.append(f"Glas: {old_w.get('glass_price') or '–'}kr → {new_w['glass_price']}kr")
            if old_w.get("bottle_price") != new_w.get("bottle_price") and new_w.get("bottle_price"):
                price_changes.append(f"Flaske: {old_w.get('bottle_price') or '–'}kr → {new_w['bottle_price']}kr")
            
            if price_changes:
                print(f"  💰 Prisændring: {new_w['name']} — {', '.join(price_changes)}")
                db.update("wines", f"id=eq.{old_w['id']}", {
                    "glass_price": new_w.get("glass_price"),
                    "bottle_price": new_w.get("bottle_price"),
                    "last_seen_at": now,
                    "pdf_hash": pdf_hash,
                })
                changes.append({
                    "wine_id": old_w["id"],
                    "change_type": "price_changed",
                    "detected_at": now,
                    "notes": ". ".join(price_changes),
                    "wine_searcher_price": None,
                    "wine_searcher_url": None,
                    "old_values": json.dumps({"glass_price": old_w.get("glass_price"), "bottle_price": old_w.get("bottle_price")}),
                    "new_values": json.dumps({"glass_price": new_w.get("glass_price"), "bottle_price": new_w.get("bottle_price")}),
                })
            else:
                # Opdater last_seen
                db.update("wines", f"id=eq.{old_w['id']}", {"last_seen_at": now, "pdf_hash": pdf_hash})

    # Gem alle ændringer
    if changes:
        db.insert("wine_changes", changes)
        print(f"  ✓ Gemt {len(changes)} ændringer for {category}")
    else:
        print(f"  ✓ Ingen ændringer i {category}")

    return len(changes)


# ─── STEP 6: CHECK PDF HASH ───────────────────────────────────────────────────
def get_stored_hash(category: str) -> Optional[str]:
    """Henter senest gemte hash for en kategori."""
    rows = db.select("pdf_snapshots", f"category=eq.{category}&order=created_at.desc&limit=1")
    return rows[0]["pdf_hash"] if rows else None


def store_hash(category: str, pdf_hash: str, url: str):
    db.insert("pdf_snapshots", {
        "category": category,
        "pdf_hash": pdf_hash,
        "pdf_url": url,
        "created_at": datetime.now(timezone.utc).isoformat(),
    })


# ─── MAIN ─────────────────────────────────────────────────────────────────────
def main():
    print("=" * 60)
    print("🍷 Syttende Vinkort Monitor — starter")
    print(f"   {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    print("=" * 60)

    total_changes = 0
    pdf_urls = discover_pdf_urls()

    for category, url in pdf_urls.items():
        print(f"\n{'─'*50}")
        print(f"📄 Behandler: {category.upper()}")
        print(f"   URL: {url[:80]}…")

        try:
            # Download
            pdf_bytes, pdf_hash = download_pdf(url)
            print(f"  ✓ Download OK — SHA256: {pdf_hash[:16]}…")

            # Check om PDF er ændret
            stored_hash = get_stored_hash(category)
            if stored_hash == pdf_hash:
                print(f"  ⏭ Ingen ændringer i PDF (hash uændret) — springer over")
                continue

            # Parse
            raw_text = extract_text_from_pdf(pdf_bytes)
            if not raw_text.strip():
                print(f"  ⚠ Ingen tekst udtrukket fra PDF — muligvis billedbaseret")
                continue

            wines = parse_wines_with_claude(raw_text, category)

            if not wines:
                print(f"  ⚠ Ingen vine fundet i {category}")
                continue

            # Diff og gem
            n_changes = run_diff_and_save(category, wines, pdf_hash)
            total_changes += n_changes

            # Gem ny hash
            store_hash(category, pdf_hash, url)

        except Exception as e:
            print(f"  ❌ FEJL i {category}: {e}")
            import traceback; traceback.print_exc()

        time.sleep(2)  # Pause mellem kategorier

    print(f"\n{'='*60}")
    print(f"✅ Færdig — {total_changes} ændringer registreret i alt")
    print("=" * 60)


if __name__ == "__main__":
    main()
