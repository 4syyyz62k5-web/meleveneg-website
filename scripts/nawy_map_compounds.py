#!/usr/bin/env python3
"""
Uses Firecrawl's `map` endpoint (URL discovery only — no page scraping, no
per-compound data extraction) to find every nawy.com URL containing
'/compound/', counts them, and writes them to a file. Meant to run BEFORE
any real scraping with nawy_to_csv.py, so you know the actual scale first.

Note on coverage: `map` returns whatever Firecrawl can discover from the
site's sitemap plus its own link graph — it is not guaranteed to be 100%
of every compound nawy.com has, only what's actually linked/indexed.

Usage:
    python scripts/nawy_map_compounds.py
    python scripts/nawy_map_compounds.py --start-url https://www.nawy.com --limit 50000
"""

import argparse
import os
import sys
from pathlib import Path

import requests
from dotenv import load_dotenv

PROJECT_ROOT = Path(__file__).resolve().parent.parent
FIRECRAWL_MAP_URL = "https://api.firecrawl.dev/v2/map"

sys.path.insert(0, str(Path(__file__).resolve().parent))
from nawy_to_csv import compound_prefix  # noqa: E402 — reuse the same /compound/<id>-<slug> parser


def firecrawl_map(start_url, api_key, limit, timeout=120):
    headers = {"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"}
    body = {"url": start_url, "limit": limit, "sitemap": "include"}
    resp = requests.post(FIRECRAWL_MAP_URL, headers=headers, json=body, timeout=timeout)
    if resp.status_code != 200:
        raise RuntimeError(f"Firecrawl map failed ({resp.status_code}): {resp.text[:500]}")
    payload = resp.json()
    if not payload.get("success"):
        raise RuntimeError(f"Firecrawl map reported failure: {payload}")
    return payload.get("links") or []


def main():
    # See the matching comment in nawy_to_csv.py's main() — line-buffer stdout so progress
    # prints show up as they happen instead of sitting invisible until the process exits.
    sys.stdout.reconfigure(line_buffering=True)

    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--start-url", default="https://www.nawy.com")
    parser.add_argument("--limit", type=int, default=50000,
                         help="Max links Firecrawl map returns (default 50000; Firecrawl's own cap is 100000)")
    parser.add_argument("--out", default=str(PROJECT_ROOT / "scripts" / "output" / "all_compound_urls.txt"))
    args = parser.parse_args()

    load_dotenv(PROJECT_ROOT / ".env")
    load_dotenv()
    api_key = os.environ.get("FIRECRAWL_API_KEY")
    if not api_key:
        sys.exit("FIRECRAWL_API_KEY not found — see scripts/.env.example.")

    print(f"Mapping {args.start_url} (limit={args.limit}) ...")
    links = firecrawl_map(args.start_url, api_key, args.limit)
    print(f"Firecrawl map returned {len(links)} total link(s) on the site.")

    compound_urls = sorted({l["url"] for l in links if l.get("url") and "/compound/" in l["url"]})
    print(f"{len(compound_urls)} link(s) contain '/compound/'.")

    # De-dupe down to one URL per distinct compound (same /compound/<id>-<slug> prefix logic
    # nawy_to_csv.py uses) — the raw count above double-counts every compound's own property
    # pages, /<Type> filters, ?page=N, /resale, etc., all of which also contain '/compound/'.
    prefix_to_url = {}
    for u in compound_urls:
        p = compound_prefix(u)
        if p and p not in prefix_to_url:
            prefix_to_url[p] = u
    top_level_urls = sorted(prefix_to_url.values())
    print(f"{len(top_level_urls)} of those are DISTINCT compounds — the rest are sub-pages "
          f"(property listings, type filters, pagination) under those same compounds.")

    out_path = Path(args.out)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text("\n".join(compound_urls) + ("\n" if compound_urls else ""), encoding="utf-8")
    print(f"\nWrote {len(compound_urls)} raw '/compound/' URL(s) -> {out_path}")

    top_level_path = out_path.with_name(out_path.stem + "_top_level.txt")
    top_level_path.write_text("\n".join(top_level_urls) + ("\n" if top_level_urls else ""), encoding="utf-8")
    print(f"Wrote {len(top_level_urls)} distinct compound URL(s) -> {top_level_path}")

    print(
        "\nNo pages were scraped and no compound/unit data was extracted — this step only "
        "discovered and counted URLs. Coverage depends on nawy.com's sitemap + Firecrawl's own "
        "link graph, so treat this count as 'at least this many', not a guaranteed exact total."
    )


if __name__ == "__main__":
    main()
