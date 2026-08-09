#!/usr/bin/env python3
"""
Scrapes EVERY compound belonging to one developer on nawy.com in one run. Each
compound gets its own isolated pair of files, same as nawy_to_csv.py:
    scripts/output/<compound_slug>/compound.csv
    scripts/output/<compound_slug>/units.csv

Discovery isn't perfectly reliable on nawy.com — a developer's page shows only
a partial carousel of compounds (~12) plus whatever else is mentioned in its
"About <Developer>" text, with no real "view all" pagination. So this script:
  1. Resolves the developer (by name, via Firecrawl map) to their nawy.com
     /developer/<id>-<slug> page, unless you already pass that URL directly.
  2. Extracts every compound link that page attributes to this developer
     (explicitly excluding nawy's generic sitewide footer widget, which lists
     unrelated "Compounds / Areas / Developers / Top Searches" on every page —
     confirmed by comparing it across multiple unrelated pages).
  3. For each discovered compound, reuses nawy_to_csv.py's crawl_compound()
     UNCHANGED — same same-compound URL guard, same placeholder-alt-text fix,
     same everything — then VERIFIES the compound's own extracted `developer`
     field actually matches the requested developer before accepting it. Any
     mismatch is rejected and reported, never silently written.
  4. Prints a final report: discovered vs accepted vs rejected vs errored,
     total units, and a shortfall warning against the site's own stated
     project count if the numbers don't line up.

Usage:
    python scripts/nawy_by_developer.py "Palm Hills"
    python scripts/nawy_by_developer.py https://www.nawy.com/developer/85-ora-developers
    python scripts/nawy_by_developer.py "Ora Developers" --max-pages 15
"""

import argparse
import os
import re
import sys
from pathlib import Path
from urllib.parse import urljoin

from dotenv import load_dotenv

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(Path(__file__).resolve().parent))

from nawy_to_csv import (  # noqa: E402 — reuse, don't duplicate, the tested compound-scraping logic
    REWRITE_PLACEHOLDER,
    build_excluded_rows,
    build_rows,
    compound_prefix,
    crawl_compound,
    filter_primary_units,
    firecrawl_scrape,
    write_compound_output,
)
from nawy_map_compounds import firecrawl_map  # noqa: E402 — reuse the same map-endpoint caller

DEVELOPER_SCHEMA = {
    "type": "object",
    "properties": {
        "developer_name": {"type": ["string", "null"]},
        "stated_project_count": {
            "type": ["integer", "null"],
            "description": (
                "A total count of DISTINCT PROJECTS/COMPOUNDS explicitly printed on the page "
                "(e.g. '35+ projects' -> 35). This is NOT the same as a 'Properties Available' or "
                "'units for sale' figure, which counts individual apartments/units across all "
                "compounds and is typically a much larger number — do not use that one. null if no "
                "distinct project/compound count is stated."
            ),
        },
        "compounds": {
            "type": "array",
            "description": (
                "Links to compounds that are part of THIS developer's own portfolio — found "
                "either in the 'Compounds In <Developer>' carousel/section, or named within the "
                "descriptive 'About <Developer>' text about this developer's own history/projects. "
                "Do NOT include the generic sitewide footer block near the very bottom of the page, "
                "structured like 'Areas [...] / Compounds [...] / Developers [...] / Top Searches "
                "[...]' — that block is unrelated boilerplate shown on every nawy.com page "
                "regardless of developer, not specific to this one."
            ),
            "items": {
                "type": "object",
                "properties": {
                    "name": {"type": ["string", "null"]},
                    "url": {"type": "string"},
                },
                "required": ["url"],
            },
        },
    },
    "required": ["compounds"],
}

DEVELOPER_PROMPT = (
    "This is a real-estate developer profile page on nawy.com. Extract ONLY: the developer's own "
    "name; a stated total project/compound count if explicitly written (e.g. '35+ projects'); and "
    "every compound link that belongs to THIS developer's own portfolio — from the 'Compounds In "
    "<Developer>' carousel and from the 'About <Developer>' descriptive text about their own "
    "history and projects. Do NOT include the generic sitewide footer block near the bottom of the "
    "page structured as 'Areas [...] / Compounds [...] / Developers [...] / Top Searches [...]' — "
    "that is unrelated boilerplate repeated on every nawy.com page, not specific to this developer."
)

GENERIC_DEVELOPER_WORDS = {
    "development", "developments", "group", "real", "estate", "for", "the",
    "company", "co", "properties", "holding", "holdings", "egypt",
}


def normalize_name(name):
    return re.sub(r"[^a-z0-9]+", " ", (name or "").lower()).strip()


def developer_matches(candidate, requested):
    """True if `candidate` (a compound's own extracted developer field) plausibly refers to the
    same company as `requested` (what the user asked for). Deliberately permissive on shared
    brand words, since this is a safety net against clearly WRONG developers slipping through —
    not the primary filter (discovery already targeted this developer's own page)."""
    c, r = normalize_name(candidate), normalize_name(requested)
    if not c or not r:
        return False
    if r in c or c in r:
        return True
    c_words = set(c.split()) - GENERIC_DEVELOPER_WORDS
    r_words = set(r.split()) - GENERIC_DEVELOPER_WORDS
    return bool(c_words & r_words)


def resolve_developer_url(name_or_url, api_key):
    """Returns (url, match_score) — match_score is None when name_or_url was already a URL."""
    if name_or_url.startswith("http://") or name_or_url.startswith("https://"):
        return name_or_url, None

    links = firecrawl_map("https://www.nawy.com", api_key, 50000)
    target_words = set(normalize_name(name_or_url).split()) - GENERIC_DEVELOPER_WORDS
    if not target_words:
        return None, 0

    best_url, best_score = None, 0
    for l in links:
        u = l.get("url") or ""
        if "/developer/" not in u or "/ar/" in u:
            continue
        m = re.search(r"/developer/\d+-([^/?#]+)", u)
        if not m:
            continue
        slug_words = set(m.group(1).replace("-", " ").split()) - GENERIC_DEVELOPER_WORDS
        score = len(target_words & slug_words)
        if score > best_score:
            best_score, best_url = score, u
    return best_url, best_score


def discover_compound_urls(dev_url, api_key):
    data = firecrawl_scrape(dev_url, api_key, schema=DEVELOPER_SCHEMA, prompt=DEVELOPER_PROMPT)
    j = data.get("json") or {}

    prefix_to_url = {}
    for c in (j.get("compounds") or []):
        u = c.get("url")
        if not u:
            continue
        abs_u = urljoin(dev_url, u)
        p = compound_prefix(abs_u)
        if p and p not in prefix_to_url:
            prefix_to_url[p] = abs_u

    return sorted(prefix_to_url.values()), j.get("developer_name"), j.get("stated_project_count")


def main():
    # See the matching comment in nawy_to_csv.py's main() — without this, stdout is fully
    # buffered whenever it's not a TTY, so a long batch run's progress prints stay invisible
    # until the process exits, looking hung even while it's working normally.
    sys.stdout.reconfigure(line_buffering=True)

    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("developer", help="Developer name (e.g. 'Palm Hills') or a nawy.com /developer/... URL")
    parser.add_argument("--out-dir", default=str(PROJECT_ROOT / "scripts" / "output"),
                         help="Base directory — each compound gets its own <out-dir>/<slug>/compound.csv + units.csv (default: scripts/output)")
    parser.add_argument("--max-pages", type=int, default=25,
                         help="Max Firecrawl requests PER compound (pagination + drill-in branches), same as nawy_to_csv.py (default: 25)")
    parser.add_argument("--sleep", type=float, default=1.0, help="Seconds between Firecrawl requests (default: 1.0)")
    args = parser.parse_args()

    load_dotenv(PROJECT_ROOT / ".env")
    load_dotenv()
    api_key = os.environ.get("FIRECRAWL_API_KEY")
    if not api_key:
        sys.exit("FIRECRAWL_API_KEY not found — see scripts/.env.example.")

    print(f"Resolving developer: {args.developer}")
    dev_url, score = resolve_developer_url(args.developer, api_key)
    if not dev_url:
        sys.exit(
            f"Could not find a nawy.com developer page matching '{args.developer}'. "
            f"Pass the exact /developer/... URL instead."
        )
    print(f"-> {dev_url}" + (f" (matched on {score} shared word(s))" if score is not None else " (given directly)"))

    print("\nDiscovering this developer's compounds...")
    compound_urls, page_developer_name, stated_count = discover_compound_urls(dev_url, api_key)
    developer_label = page_developer_name or args.developer
    print(f"Discovered {len(compound_urls)} distinct compound link(s) for '{developer_label}'"
          + (f" (site states {stated_count}+ project(s))" if stated_count else " (no total stated on the page)"))
    if not compound_urls:
        sys.exit("No compound links discovered — nothing to scrape. Check the developer page manually.")

    out_dir = Path(args.out_dir)
    used_slugs = set()

    accepted, rejected_mismatch, errored = [], [], []
    total_units = 0
    total_excluded = 0

    for url in compound_urls:
        print(f"\n=== {url} ===")
        try:
            fields, units, total_advertised, warnings, pages_fetched = crawl_compound(
                url, api_key, args.max_pages, args.sleep, log=print
            )
        except Exception as e:
            print(f"  ERROR: {e}")
            errored.append((url, str(e)))
            continue

        name = (fields.get("compound_name") or "").strip()
        if not name:
            print("  could not extract a compound name — skipping.")
            errored.append((url, "no compound name extracted"))
            continue

        extracted_dev = fields.get("developer") or ""
        if not developer_matches(extracted_dev, developer_label):
            print(f"  ⚠️  REJECTED — page says developer='{extracted_dev}', expected something matching '{developer_label}'")
            rejected_mismatch.append((url, name, extracted_dev))
            continue

        primary_units, excluded_units = filter_primary_units(units)
        unknown_kept = sum(1 for u in primary_units if (u.get("listing_type") or "Unknown") == "Unknown")

        crow, urows, about_text_raw = build_rows(fields, primary_units, used_slugs)
        excluded_rows = build_excluded_rows(excluded_units, crow["slug"])
        compound_path, units_path, excluded_path = write_compound_output(out_dir, crow, urows, about_text_raw, excluded_rows)
        total_units += len(urows)
        total_excluded += len(excluded_rows)
        accepted.append((
            crow["name"], crow["slug"], compound_path, units_path, excluded_path,
            len(urows), len(excluded_rows), unknown_kept, total_advertised,
        ))

        summary = f"  -> accepted '{crow['name']}' (slug={crow['slug']}): {len(urows)} Primary unit(s)"
        if excluded_rows:
            summary += f", {len(excluded_rows)} excluded (Resale/Nawy Now)"
        if total_advertised:
            summary += f", {total_advertised} advertised total"
        print(summary)
        if unknown_kept:
            print(f"     note: {unknown_kept} unit(s) had an undetermined listing_type — kept in units.csv, not excluded.")
        for w in warnings:
            print(f"     warning: {w}")

    print("\n" + "=" * 70)
    print(f"Developer: {developer_label}  ({dev_url})")
    if stated_count:
        print(f"Site states {stated_count}+ project(s).")
    print(f"Discovered {len(compound_urls)} candidate compound link(s).")
    print(f"Accepted & written: {len(accepted)} compound(s), {total_units} Primary unit(s) total"
          + (f", {total_excluded} excluded (Resale/Nawy Now)" if total_excluded else ""))
    for name, slug, compound_path, units_path, excluded_path, n, n_excluded, n_unknown, total_adv in accepted:
        line = f"  - {name} (slug={slug}): {n} Primary unit(s)"
        if n_excluded:
            line += f", {n_excluded} excluded"
        if total_adv:
            line += f" of {total_adv} advertised total"
        print(line)
        print(f"      {compound_path}")
        print(f"      {units_path}")
        if excluded_path:
            print(f"      {excluded_path}  ({n_excluded} Resale/Nawy Now unit(s), not imported)")
        if n_unknown:
            print(f"      ({n_unknown} unit(s) in units.csv have an undetermined listing_type — worth a manual check)")

    if rejected_mismatch:
        print(f"\nRejected — developer mismatch ({len(rejected_mismatch)}):")
        for url, name, extracted_dev in rejected_mismatch:
            print(f"  - {name or url}: page says developer='{extracted_dev}' — {url}")

    if errored:
        print(f"\nErrored / needs manual review ({len(errored)}):")
        for url, err in errored:
            print(f"  - {url}: {err}")

    if stated_count and len(accepted) < stated_count:
        print(
            f"\n⚠️  Accepted {len(accepted)} of the {stated_count}+ project(s) this developer's page "
            f"claims to have — the carousel + description text on that one page likely doesn't "
            f"mention all of them. Check the developer page manually for anything still missing."
        )

    if accepted:
        print(
            "\nReminder: short_description/full_description in each compound.csv are the literal "
            f"placeholder '{REWRITE_PLACEHOLDER}' — never nawy's own text. Write original copy from "
            "the about_text_raw column, then delete that column before importing."
        )


if __name__ == "__main__":
    main()
