#!/usr/bin/env python3
"""
Fetches ONLY the cover image URL for one or more nawy.com compound pages —
a much lighter cousin of nawy_to_csv.py for when all you need is the
compound-level cover_image_url (e.g. backfilling it for compounds that were
imported without one), not a full compound+units scrape. One single-page
Firecrawl request per URL — no pagination/drill-in following, no units.

Usage:
    python scripts/nawy_fetch_cover_images.py <url1> <url2> ...

Prints "<url> -> <cover_image_url or MISSING>" per line, and writes
scripts/output/cover_images.csv (columns: url, cover_image_url).
"""
import csv
import os
import sys
import time
from pathlib import Path

from dotenv import load_dotenv

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(Path(__file__).resolve().parent))

from nawy_to_csv import firecrawl_scrape  # noqa: E402 — reuse the tested HTTP/retry logic

MINIMAL_SCHEMA = {
    "type": "object",
    "properties": {
        "cover_image_url": {
            "type": ["string", "null"],
            "description": "The compound's main cover/hero photo URL shown at the top of the page.",
        },
    },
    "required": ["cover_image_url"],
}
MINIMAL_PROMPT = "Extract only the main cover/hero image URL shown for this compound."


def main():
    sys.stdout.reconfigure(line_buffering=True)
    urls = sys.argv[1:]
    if not urls:
        sys.exit("Usage: python scripts/nawy_fetch_cover_images.py <url1> <url2> ...")

    load_dotenv(PROJECT_ROOT / ".env")
    load_dotenv()
    api_key = os.environ.get("FIRECRAWL_API_KEY")
    if not api_key:
        sys.exit("FIRECRAWL_API_KEY not found — see scripts/.env.example.")

    out_dir = PROJECT_ROOT / "scripts" / "output"
    out_dir.mkdir(parents=True, exist_ok=True)
    out_path = out_dir / "cover_images.csv"

    rows = []
    for url in urls:
        try:
            data = firecrawl_scrape(url, api_key, schema=MINIMAL_SCHEMA, prompt=MINIMAL_PROMPT)
            j = data.get("json") or {}
            image = (j.get("cover_image_url") or "").strip()
        except Exception as e:
            print(f"{url} -> ERROR: {e}")
            rows.append({"url": url, "cover_image_url": ""})
            time.sleep(1.0)
            continue
        print(f"{url} -> {image or 'MISSING'}")
        rows.append({"url": url, "cover_image_url": image})
        time.sleep(1.0)

    with open(out_path, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=["url", "cover_image_url"])
        writer.writeheader()
        writer.writerows(rows)

    found = sum(1 for r in rows if r["cover_image_url"])
    print(f"\n{found}/{len(rows)} cover image(s) found -> {out_path}")


if __name__ == "__main__":
    main()
