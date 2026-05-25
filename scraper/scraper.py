#!/usr/bin/env python3
"""
Syttende Vinkort Monitor — Scraper Agent (100% gratis, ingen API-noegler)
Kraever env vars: SUPABASE_URL, SUPABASE_SERVICE_KEY
"""

import os, re, json, hashlib, time
import httpx
import pdfplumber
from datetime import datetime, timezone
from io import BytesIO
from typing import Optional

SUPABASE_URL = os.environ["SUPABASE_URL"]
SUPABASE_KEY = os.environ["SUPABASE_SERVICE_KEY"]
WINE_PAGE_URL = "https://www.syttende.dk/vinen"
HEADERS = {"User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 Chrome/124.0 Safari/537.36"}

PDF_SOURCES = {
    "bobler":  "https://cdn.prod.website-files.com/68e4e0d05e784db5b02931a0/698f5696066729ad6f5d5bd4_77e5403ce19c0082f527b3a7ca1b86c7_Vinkort%20BOBLER%20%26%20S%C3%98DT.pdf",
    "hvidvin": "https://cdn.prod.website-files.com/68e4e0d05e784db5b02931a0/698f5696d37acaa6f98eed15_db0c5c76c52868dc16dc5d110d8cf687_Vinkort%20HVIDVIN.pdf",
    "roserod": "https://cdn.prod.website-files.com/68e4e0d05e784db5b02931a0/698f56967915ca645f55ab51_3d3d5376de879ad7ba5d8413cfe1f6d4_Vinkort%20ROS%C3%88%20%26%20R%C3%98DVIN.pdf",
    "avec":    "https://cdn.prod.website-files.com/68e4e0d05e784db5b02931a0/69bbf8cf10f6a1d7d706adbd_04ef0f7aac2b42b057991bb7a68656e3_Avec.pdf",
}

PRICE_RE   = re.compile(r'\b(\d{1,2}\.\d{3}|\d{3,5})(?:,-|\.-)?\b')
VINTAGE_RE = re.compile(r'\b(19[5-9]\d|20[0-2]\d)\b')
SKIP_RE    = re.compile(
    r'^(side|page|\d+$|vinkort|menu|restaurant|syttende|alsik|tel|www|mail|'
    r'hvid|roed|rose|bobler|champagne|italien|frankrig|spanien|tyskland|ostrig|'
    r'mousserende|dessert|hedvin|avec|digestif|aperitif|glas|flaske|pris|kr\.?)$',
    re.IGNORECASE
)

class Supabase:
    def __init__(self, url, key):
        self.base = url.rstrip("/") + "/rest/v1"
        self.h = {"apikey": key, "Authorization": f"Bearer {key}",
                  "Content-Type": "application/json", "Prefer": "return=representation"}

    def select(self, table, q=""):
        r = httpx.get(f"{self.base}/{table}?{q}", headers=self.h, timeout=30)
        r.raise_for_status(); return r.json()

    def upsert(self, table, data, on_conflict="id"):
        r = httpx.post(f"{self.base}/{table}?on_conflict={on_conflict}",
            headers={**self.h, "Prefer": "return=representation,resolution=merge-duplicates"},
            json=data, timeout=30)
        r.raise_for_status(); return r.json()

    def insert(self, table, data):
        r = httpx.post(f"{self.base}/{table}", headers=self.h, json=data, timeout=30)
        r.raise_for_status(); return r.json()

    def update(self, table, match, data):
        r = httpx.patch(f"{self.base}/{table}?{match}", headers=self.h, json=data, timeout=30)
        r.raise_for_status(); return r.json()

db = Supabase(SUPABASE_URL, SUPABASE_KEY)

def discover_pdf_urls():
    print("Opdager PDF-URLs fra syttende.dk/vinen ...")
    try:
        html = httpx.get(WINE_PAGE_URL, headers=HEADERS, timeout=20, follow_redirects=True).text
        patterns = {
            "bobler":  r'(https://cdn\.prod\.website-files\.com[^"\']*(?:BOBLER|bobler)[^"\']*\.pdf)',
            "hvidvin": r'(https://cdn\.prod\.website-files\.com[^"\']*(?:HVIDVIN|hvidvin)[^"\']*\.pdf)',
            "roserod": r'(https://cdn\.prod\.website-files\.com[^"\']*(?:ROS)[^"\']*\.pdf)',
            "avec":    r'(https://cdn\.prod\.website-files\.com[^"\']*(?:Avec|avec)[^"\']*\.pdf)',
        }
        urls = {}
        for key, pat in patterns.items():
            m = re.search(pat, html, re.IGNORECASE)
            urls[key] = m.group(1) if m else PDF_SOURCES[key]
            print(f"  {'OK' if m else 'fallback'}: {key}")
        return urls
    except Exception as e:
        print(f"  Fejl: {e} -- bruger hardkodede URLs")
        return PDF_SOURCES

def download_pdf(url):
    resp = httpx.get(url, headers=HEADERS, timeout=60, follow_redirects=True)
    resp.raise_for_status()
    return resp.content, hashlib.sha256(resp.content).hexdigest()

def parse_pdf(pdf_bytes, category):
    wines, seen = [], set()
    with pdfplumber.open(BytesIO(pdf_bytes)) as pdf:
        for page in pdf.pages:
            text = page.extract_text(x_tolerance=2, y_tolerance=2) or ""
            lines = [l.strip() for l in text.split("\n") if l.strip()]
            i = 0
            while i < len(lines):
                line = lines[i]
                if len(line) < 4 or SKIP_RE.match(line):
                    i += 1; continue
                context = line + (" " + lines[i+1] if i+1 < len(lines) else "")
                prices = [int(p.replace(".", "")) for p in PRICE_RE.findall(context)
                          if 50 <= int(p.replace(".", "")) <= 99999]
                if not prices:
                    i += 1; continue
                vintage = (VINTAGE_RE.search(line) or type('', (), {'group': lambda s, n: None})()).group(1)
                clean = re.sub(r'\s{2,}', ' ', re.sub(r'[,.\-/|]+$', '', VINTAGE_RE.sub("", PRICE_RE.sub("", line)))).strip()
                if len(clean) < 3:
                    i += 1; continue
                producer, name = None, clean
                for sep in [" / ", " - ", " – ", ", "]:
                    if sep in clean:
                        parts = clean.split(sep, 1)
                        if len(parts[0]) > 2 and len(parts[1]) > 2:
                            producer, name = parts[0].strip(), parts[1].strip()
                            break
                key = f"{producer or ''}|{name}".lower()
                if key in seen or len(name) < 3:
                    i += 1; continue
                seen.add(key)
                wines.append({
                    "name": name, "producer": producer, "region": None,
                    "vintage": vintage, "category": category,
                    "glass_price":  prices[0] if len(prices) >= 2 else None,
                    "bottle_price": prices[-1],
                    "grape": None, "notes": None,
                })
                i += 1
    print(f"  Parsede {len(wines)} vine fra {category}")
    return wines

def search_wine_searcher(name, producer):
    query = f"{producer} {name}".strip() if producer else name
    url = f"https://www.wine-searcher.com/find/{query.replace(' ', '+')}/1/dk"
    try:
        html = httpx.get(url, headers=HEADERS, timeout=15, follow_redirects=True).text
        matches = re.findall(r'"price"\s*:\s*"?([\d.]+)"?\s*,\s*"currency"\s*:\s*"(EUR|DKK)"', html)
        if matches:
            prices = [int(float(v)*7.46) if c=="EUR" else int(float(v)) for v,c in matches]
            return min(prices), url
    except Exception as e:
        print(f"    Wine-Searcher fejl for '{name}': {e}")
    return None, url

def make_key(w):
    return f"{(w.get('producer') or '').lower().strip()}|{(w.get('name') or '').lower().strip()}"

def run_diff(category, new_wines, pdf_hash):
    print(f"\nDiffer {category} ...")
    now = datetime.now(timezone.utc).isoformat()
    existing = db.select("wines", f"category=eq.{category}&is_active=eq.true")
    ex_map  = {make_key(w): w for w in existing}
    new_map = {make_key(w): w for w in new_wines}
    changes = []

    for key, wine in new_map.items():
        if key not in ex_map:
            print(f"  + {wine['name']}")
            saved = db.upsert("wines", {**wine, "is_active": True,
                "first_seen_at": now, "last_seen_at": now, "pdf_hash": pdf_hash},
                on_conflict="category,name,producer")
            wine_id = saved[0]["id"] if saved else None
            ws_price, ws_url = None, None
            if wine.get("name"):
                ws_price, ws_url = search_wine_searcher(wine["name"], wine.get("producer"))
                time.sleep(2)
            bottle_note = f" - flaske {wine.get('bottle_price')} kr." if wine.get('bottle_price') else ""
            changes.append({"wine_id": wine_id, "change_type": "added", "detected_at": now,
                "notes": f"Tilfojet{bottle_note}",
                "wine_searcher_price": ws_price, "wine_searcher_url": ws_url,
                "old_values": None, "new_values": json.dumps(wine, ensure_ascii=False)})

    for key, wine in ex_map.items():
        if key not in new_map:
            print(f"  - {wine['name']}")
            db.update("wines", f"id=eq.{wine['id']}", {"is_active": False})
            changes.append({"wine_id": wine["id"], "change_type": "removed", "detected_at": now,
                "notes": "Fjernet fra vinkortet", "wine_searcher_price": None,
                "wine_searcher_url": None,
                "old_values": json.dumps(wine, ensure_ascii=False), "new_values": None})

    for key, new_w in new_map.items():
        if key in ex_map:
            old_w = ex_map[key]
            notes = []
            if old_w.get("glass_price") != new_w.get("glass_price") and new_w.get("glass_price"):
                notes.append(f"Glas: {old_w.get('glass_price') or '-'} kr. -> {new_w['glass_price']} kr.")
            if old_w.get("bottle_price") != new_w.get("bottle_price") and new_w.get("bottle_price"):
                notes.append(f"Flaske: {old_w.get('bottle_price') or '-'} kr. -> {new_w['bottle_price']} kr.")
            if notes:
                print(f"  $ {new_w['name']}")
                db.update("wines", f"id=eq.{old_w['id']}", {
                    "glass_price": new_w.get("glass_price"),
                    "bottle_price": new_w.get("bottle_price"),
                    "last_seen_at": now, "pdf_hash": pdf_hash})
                changes.append({"wine_id": old_w["id"], "change_type": "price_changed",
                    "detected_at": now, "notes": " | ".join(notes),
                    "wine_searcher_price": None, "wine_searcher_url": None,
                    "old_values": json.dumps({"glass_price": old_w.get("glass_price"), "bottle_price": old_w.get("bottle_price")}),
                    "new_values":  json.dumps({"glass_price": new_w.get("glass_price"),  "bottle_price": new_w.get("bottle_price")})})
            else:
                db.update("wines", f"id=eq.{old_w['id']}", {"last_seen_at": now, "pdf_hash": pdf_hash})

    if changes:
        db.insert("wine_changes", changes)
        print(f"  {len(changes)} aendringer gemt")
    else:
        print("  Ingen aendringer")
    return len(changes)

def get_stored_hash(category):
    rows = db.select("pdf_snapshots", f"category=eq.{category}&order=created_at.desc&limit=1")
    return rows[0]["pdf_hash"] if rows else None

def store_hash(category, pdf_hash, url):
    db.insert("pdf_snapshots", {"category": category, "pdf_hash": pdf_hash,
        "pdf_url": url, "created_at": datetime.now(timezone.utc).isoformat()})

def main():
    print("=" * 55)
    print("Syttende Vinkort Monitor (gratis mode)")
    print(datetime.now().strftime('%Y-%m-%d %H:%M:%S'))
    print("=" * 55)
    total = 0
    for category, url in discover_pdf_urls().items():
        print(f"\n--- {category.upper()} ---")
        try:
            pdf_bytes, pdf_hash = download_pdf(url)
            print(f"  Download OK ({len(pdf_bytes)//1024} KB)")
            if get_stored_hash(category) == pdf_hash:
                print("  PDF uaendret -- springer over"); continue
            wines = parse_pdf(pdf_bytes, category)
            if not wines:
                print("  Ingen vine parsede (PDF evt. billedbaseret)"); continue
            total += run_diff(category, wines, pdf_hash)
            store_hash(category, pdf_hash, url)
        except Exception as e:
            print(f"  FEJL: {e}")
            import traceback; traceback.print_exc()
        time.sleep(2)
    print(f"\n{'='*55}\nFaerdig -- {total} aendringer i alt\n{'='*55}")

if __name__ == "__main__":
    main()
