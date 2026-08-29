#!/usr/bin/env python3
"""
Scrapes nawy.com compound page(s) via the Firecrawl API and writes
compounds.csv / units.csv in exactly the column order that
/admin/compounds/import and /admin/units/import (see app.py) read.

Why this crawls more than just the one URL you give it:
  On nawy.com, a compound's top-level page (e.g. /compound/530-silversands)
  almost never lists individually-priced units directly — it shows phase
  cards and aggregate counts like "77 Chalet for sale". The actual priced
  unit cards usually live one or two clicks deeper (a phase page, or a
  "<type> for sale in <compound>" page), and those in turn are often
  paginated. So for every URL you pass in, this script:
    1. Asks Firecrawl to extract compound-level fields AND any unit cards
       AND any "drill-in" links / pagination it can see on that page.
    2. If no priced unit cards are on the page but drill-in links are,
       it follows them (and their pagination) automatically.
    3. Stops once nothing new is found, or --max-pages requests have been
       spent on that compound (whichever comes first).
  This is driven entirely by what Firecrawl actually reports back on each
  page — it does not hardcode nawy's URL scheme — so it adapts if a given
  page already shows priced units directly.

Usage:
    pip install -r scripts/requirements.txt
    cp scripts/.env.example .env        # then fill in FIRECRAWL_API_KEY
    python scripts/nawy_to_csv.py https://www.nawy.com/compound/530-silversands
    python scripts/nawy_to_csv.py <url1> <url2> --max-pages 40 --out-dir scripts/output

Output:
    scripts/output/compounds.csv
    scripts/output/units.csv
  (both ready to feed straight into /admin/import, Step 1 then Step 2)
"""

import argparse
import csv
import os
import re
import sys
import time
from collections import Counter, deque
from pathlib import Path
from urllib.parse import urljoin

import requests
from dotenv import load_dotenv

PROJECT_ROOT = Path(__file__).resolve().parent.parent
FIRECRAWL_SCRAPE_URL = "https://api.firecrawl.dev/v2/scrape"

# ---------------------------------------------------------------------------
# CSV column order — copied from how app.py's admin_compounds_import /
# admin_units_import actually construct Compound(...)/Unit(...), NOT from the
# (stale) help text on the /admin/import page, which is missing `location`
# and `is_launch`. This order is what determines correctness for import.
# ---------------------------------------------------------------------------
COMPOUND_FIELDS = [
    "name", "slug", "developer", "location", "area", "location_detail",
    "short_description", "full_description", "min_price", "max_price", "currency",
    "land_area_acres", "delivery_year", "cover_image_url", "contact_phone",
    "contact_whatsapp", "is_featured", "is_launch", "is_published",
]

# short_description/full_description are deliberately written as a placeholder, never nawy's own
# text verbatim (or a light paraphrase of it) — copied competitor copy is a duplicate-content and
# originality risk for Meleven's own site. about_text_raw carries the source material for a human
# (or Claude) to write fresh copy from; it's a STAGING-ONLY column, stripped before import.
COMPOUND_STAGING_FIELDS = COMPOUND_FIELDS + ["about_text_raw"]
REWRITE_PLACEHOLDER = "[REWRITE NEEDED]"

UNIT_FIELDS = [
    "compound_slug", "unit_type", "phase", "delivery_year", "bedrooms",
    "bathrooms", "area_sqm", "price", "currency", "payment_plan", "image_url",
    "is_available", "is_launch",
]

# Same shape as UNIT_FIELDS plus listing_type — used for the excluded_units.csv report of
# units explicitly identified as Resale/Nawy Now (not for import; just for visibility).
EXCLUDED_UNIT_FIELDS = UNIT_FIELDS + ["listing_type"]

COMPOUND_LEVEL_KEYS = [
    "compound_name", "developer", "location", "area", "location_detail",
    "short_description", "full_description", "land_area_acres",
    "delivery_year", "cover_image_url", "is_launch",
]

# ---------------------------------------------------------------------------
# Extraction schema / prompt sent to Firecrawl. Every "explicit only" rule
# below exists specifically so delivery_year (and everything else) never
# gets guessed — Firecrawl's json format runs an LLM extraction pass, and
# LLMs happily infer/round unless told not to.
# ---------------------------------------------------------------------------
COMPOUND_SCHEMA = {
    "type": "object",
    "properties": {
        "compound_name": {"type": ["string", "null"]},
        "developer": {"type": ["string", "null"]},
        "location": {
            "type": ["string", "null"],
            "description": "Top-level region, e.g. 'New Cairo', 'North Coast', 'Ain Sokhna'.",
        },
        "area": {
            "type": ["string", "null"],
            "description": "Specific sub-area/neighborhood, e.g. 'Sidi Heneish', 'Mostakbal City'.",
        },
        "location_detail": {
            "type": ["string", "null"],
            "description": "Precise address line, e.g. 'Kilo 247, International Coastal Road'.",
        },
        "short_description": {"type": ["string", "null"]},
        "full_description": {"type": ["string", "null"]},
        "land_area_acres": {"type": ["number", "null"]},
        "delivery_year": {
            "type": ["integer", "null"],
            "description": (
                "A specific 4-digit handover/delivery year EXPLICITLY printed on the page "
                "(e.g. 2028). Phrases like 'ready to move' or a phase's marketing name do NOT "
                "count as a year. If no explicit year is shown, return null — never guess."
            ),
        },
        "cover_image_url": {"type": ["string", "null"]},
        "is_launch": {
            "type": "boolean",
            "description": "true ONLY if the page explicitly shows a 'New Launch'/'Launch' badge or label for this compound as a whole. Default false.",
        },
        "total_units_advertised": {
            "type": ["integer", "null"],
            "description": "A total results count explicitly printed on the page (e.g. '124 results'). null if none shown.",
        },
        "has_next_page": {"type": "boolean"},
        "next_page_url": {
            "type": ["string", "null"],
            "description": "The real href/URL of the next results page, if this page is paginated and is not the last page. null if there is no next page or no real URL for it.",
        },
        "sub_listing_links": {
            "type": "array",
            "description": (
                "Links on this page that drill into a filtered listing of individual units "
                "(by property type or by phase), each with its own advertised count. Only "
                "populate this when `units` below is empty — i.e. this page shows aggregate "
                "counts/phase cards rather than individually-priced units."
            ),
            "items": {
                "type": "object",
                "properties": {
                    "label": {"type": ["string", "null"]},
                    "url": {"type": "string"},
                    "count": {"type": ["integer", "null"]},
                },
                "required": ["url"],
            },
        },
        "units": {
            "type": "array",
            "description": "Individual for-sale unit/property cards visible on THIS page, each with its own price. Empty array if this page does not show individually-priced units.",
            "items": {
                "type": "object",
                "properties": {
                    "unit_type": {
                        "type": ["string", "null"],
                        "description": "Only if a visible text label states it (e.g. 'Chalet', 'Villa'). null if not labeled — a fallback is derived from the card's own detail_url afterwards, don't guess one yourself.",
                    },
                    "detail_url": {
                        "type": ["string", "null"],
                        "description": "The URL the whole card links to (its own property detail page), if any.",
                    },
                    "phase": {"type": ["string", "null"]},
                    "delivery_year": {
                        "type": ["integer", "null"],
                        "description": "ONLY if explicitly stated for this specific unit/phase. null otherwise — never guess.",
                    },
                    "bedrooms": {"type": ["integer", "null"]},
                    "bathrooms": {"type": ["integer", "null"]},
                    "area_sqm": {"type": ["number", "null"]},
                    "price": {"type": ["number", "null"]},
                    "currency": {
                        "type": ["string", "null"],
                        "description": (
                            "The currency symbol/label shown right next to this specific unit's "
                            "price (e.g. '$'/'USD' -> \"USD\", 'EGP' -> \"EGP\"). nawy.com prices "
                            "some compounds (several El Gouna/Red Sea Orascom projects especially) "
                            "in USD, sometimes mixed with EGP-priced units on the very same page -- "
                            "read it per-card, never assume/carry over from another card. null only "
                            "if truly no currency indicator is visible for this card."
                        ),
                    },
                    "payment_plan": {"type": ["string", "null"]},
                    "image_url": {"type": ["string", "null"]},
                    "is_available": {
                        "type": "boolean",
                        "description": "false ONLY if explicitly marked sold/reserved/unavailable. Default true.",
                    },
                    "is_launch": {
                        "type": "boolean",
                        "description": "true ONLY if this specific unit/card explicitly shows a 'Launch' badge. Default false.",
                    },
                    "listing_type": {
                        "type": "string",
                        "enum": ["Primary", "Resale", "Nawy Now", "Unknown"],
                        "description": (
                            "nawy.com sorts every unit into one of 'Developer Sale' (=Primary), "
                            "'Resale', or 'Nawy Now' — shown as filter tabs above the listing grid. "
                            "On the card itself, this shows up as a small colored tag/badge with the "
                            "literal text 'Resale' or 'Nawy Now' near the image. A card with NO such "
                            "tag/badge at all is a Developer Sale card — output 'Primary' for those. "
                            "Output 'Resale' or 'Nawy Now' only when that exact tag text is visible on "
                            "the card. If you truly cannot tell, output 'Unknown' — never guess between "
                            "Primary and Resale/Nawy Now without seeing (or clearly not seeing) the tag."
                        ),
                    },
                },
            },
        },
    },
    "required": ["units"],
}

PROMPT = (
    "Extract real-estate listing data from this nawy.com page for a database import. "
    "Extract ONLY what is explicitly written on the page — never invent, estimate, round, or infer "
    "a value that isn't shown, especially delivery_year. If a field isn't stated, return null. "
    "If this page shows aggregate counts or phase/category cards instead of individually-priced "
    "units, leave `units` empty and list the drill-in links in `sub_listing_links` instead. "
    "IMPORTANT: individual property cards on nawy.com often have a BROKEN image alt-text "
    "placeholder like 'of 0 m² with 0 bedrooms' baked into the card markup — that literal '0' is "
    "not real data, ignore it completely. The card's real area/bedrooms/price/delivery year appear "
    "as separate text lines within the same card (below the image), and that is where the actual "
    "values come from — extract those, not the alt-text placeholder. ALSO IMPORTANT: classify every "
    "unit's listing_type by whether its card shows a small colored 'Resale' or 'Nawy Now' tag/badge "
    "near the image — present means that type, absent entirely means 'Primary' (Developer Sale). "
    "Use 'Unknown' only if you genuinely cannot tell. ALSO IMPORTANT: read each card's currency "
    "from the symbol/label printed right next to ITS OWN price ('$' or 'USD' -> \"USD\", 'EGP' -> "
    "\"EGP\") — nawy.com prices some compounds in USD, and a single page can even mix USD and EGP "
    "cards, so check every card individually rather than assuming the whole page shares one currency."
)


def slugify(text):
    """Mirrors app.py's slugify() exactly, so generated slugs match what the site would produce."""
    text = (text or "").lower().strip()
    text = re.sub(r"[^a-z0-9]+", "-", text)
    return text.strip("-") or "compound"


def unique_slug(base, used):
    slug = base
    n = 1
    while slug in used:
        n += 1
        slug = f"{base}-{n}"
    used.add(slug)
    return slug


# Matches nawy.com property detail URLs, e.g.:
#   .../property/131711-administrative-office-unit-for-sale-in-viehalo-in-newcairo-by-vie-communities
# Group 1 is the human-readable type slug ("administrative-office-unit").
DETAIL_URL_TYPE_RE = re.compile(r"/property/\d+-(.+?)-for-sale-in-")


def derive_unit_type_from_url(detail_url):
    """Nawy's unit cards frequently have no visible 'unit type' text label — the type only
    shows up as a slug in the card's own detail_url (e.g. '...-administrative-office-unit-for-sale-in-...').
    This reformats that slug into a readable label. It's not a guess about facts the page doesn't
    state — it's the page's own text, just relocated from a URL into a display string."""
    if not detail_url:
        return None
    m = DETAIL_URL_TYPE_RE.search(detail_url)
    if not m:
        return None
    slug = m.group(1)
    if slug.endswith("-unit"):
        slug = slug[: -len("-unit")]
    words = [w for w in slug.split("-") if w]
    return " ".join(w.capitalize() for w in words) or None


CARD_ANCHOR_PREFIX = '<a href="https://www.nawy.com/compound/'
CARD_TAG_RE = re.compile(r'<div class="tag"[^>]*><p>([^<]*)</p></div>')


def classify_listing_type_from_html(html, detail_url):
    """Ground truth for listing_type, read directly from the DOM instead of trusted to the LLM.
    Confirmed by inspecting nawy.com's real markup: a unit card renders a colored
    <div class="tag"><p>Resale</p></div> (or 'Nawy Now') ONLY when it isn't a Developer Sale
    listing — a card with no such tag at all is Primary. Returns None if `detail_url`'s own card
    couldn't be located in `html` (missing detail_url, html wasn't fetched, or markup didn't
    match) — callers should treat None as "couldn't verify", not as any particular type."""
    if not html or not detail_url:
        return None
    href_marker = f'href="{detail_url}"'
    start = html.find(href_marker)
    if start == -1:
        return None
    next_card = html.find(CARD_ANCHOR_PREFIX, start + len(href_marker))
    block = html[start:next_card] if next_card != -1 else html[start:start + 6000]

    m = CARD_TAG_RE.search(block)
    if not m:
        return "Primary"
    tag_text = m.group(1).strip()
    if tag_text in ("Resale", "Nawy Now"):
        return tag_text
    return "Unknown"  # some other/unexpected tag text on the card — don't guess


def firecrawl_scrape(url, api_key, schema=None, prompt=None, timeout=60, retries=3):
    """Shared Firecrawl /v2/scrape caller (with retry/backoff). Defaults to the compound/unit
    extraction schema+prompt; nawy_by_developer.py reuses this with its own schema+prompt for
    developer pages instead of duplicating the HTTP/retry logic."""
    headers = {"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"}
    body = {
        "url": url,
        "onlyMainContent": False,
        # "html" is fetched alongside markdown/json so crawl_compound can deterministically
        # verify each unit's listing_type straight from the DOM (see classify_listing_type_from_html)
        # instead of trusting the LLM's json extraction for that one field — proven unreliable in
        # testing: it cannot reliably reason from the ABSENCE of a "Resale"/"Nawy Now" tag/badge.
        "formats": ["markdown", "html", {"type": "json", "schema": schema or COMPOUND_SCHEMA, "prompt": prompt or PROMPT}],
    }
    last_err = None
    for attempt in range(1, retries + 1):
        try:
            resp = requests.post(FIRECRAWL_SCRAPE_URL, headers=headers, json=body, timeout=timeout)
        except requests.RequestException as e:
            last_err = e
            time.sleep(2 * attempt)
            continue
        if resp.status_code == 200:
            payload = resp.json()
            if not payload.get("success"):
                raise RuntimeError(f"Firecrawl reported failure for {url}: {payload}")
            return payload.get("data") or {}
        if resp.status_code in (429, 500, 502, 503, 504) and attempt < retries:
            time.sleep(2 * attempt)
            continue
        raise RuntimeError(f"Firecrawl request failed ({resp.status_code}) for {url}: {resp.text[:500]}")
    raise RuntimeError(f"Firecrawl request failed after {retries} attempt(s) for {url}: {last_err}")


# Extracts the "/compound/<id>-<slug>" prefix nawy.com uses to namespace everything that
# belongs to one compound (its property pages, its /<Type> filters, its ?page=N pagination).
COMPOUND_PREFIX_RE = re.compile(r"^(/compound/\d+-[^/?#]+)")


def compound_prefix(url):
    from urllib.parse import urlparse
    m = COMPOUND_PREFIX_RE.match(urlparse(url).path)
    return m.group(1) if m else None


def is_same_compound(url, prefix):
    """True only if `url`'s path is the compound's own page, or something namespaced under it
    (a property detail page, a /<Type> filter, pagination, etc). False for links to a DIFFERENT
    compound — nawy.com pages commonly surface "similar/nearby compounds" recommendation links,
    and without this check those get mistaken for legitimate drill-in links, silently pulling in
    another project's units under the wrong compound_slug."""
    if not prefix:
        return False
    from urllib.parse import urlparse
    path = urlparse(url).path
    return path == prefix or path.startswith(prefix + "/")


def unit_dedup_key(u):
    # detail_url is each unit's own property-page link — a far more reliable dedup key than
    # a tuple of specs, since two distinct units can otherwise share identical specs. Fall back
    # to the spec tuple only when a card has no detail_url.
    if u.get("detail_url"):
        return ("url", u["detail_url"])
    return (
        "specs", u.get("unit_type"), u.get("phase"), u.get("bedrooms"),
        u.get("bathrooms"), u.get("area_sqm"), u.get("price"),
        u.get("payment_plan"), u.get("image_url"),
    )


def crawl_compound(entry_url, api_key, max_pages, sleep_s, log):
    """Fetches entry_url plus whatever pagination/drill-in links it leads to,
    bounded by max_pages Firecrawl requests. Returns
    (compound_fields, units, total_advertised, warnings, pages_fetched)."""
    budget = max_pages
    prefix = compound_prefix(entry_url)
    queue = deque([entry_url])
    seen_urls = set()
    units, unit_keys = [], set()
    compound_fields = {}
    total_advertised = None
    branch_counts = []
    warnings = []
    pages_fetched = 0
    rejected_foreign_links = []

    while queue and budget > 0:
        url = queue.popleft()
        if url in seen_urls:
            continue
        seen_urls.add(url)
        log(f"  fetching: {url}")
        data = firecrawl_scrape(url, api_key)
        pages_fetched += 1
        budget -= 1
        j = data.get("json") or {}

        for f in COMPOUND_LEVEL_KEYS:
            if f == "is_launch":
                if j.get("is_launch"):
                    compound_fields["is_launch"] = True
            elif not compound_fields.get(f) and j.get(f):
                compound_fields[f] = j[f]

        if total_advertised is None and j.get("total_units_advertised"):
            total_advertised = j["total_units_advertised"]

        html = data.get("html") or ""
        page_units = j.get("units") or []
        for u in page_units:
            # Same guard as the sub_listing_links/pagination check below, but applied to units
            # extracted directly off a single page's own `units` array -- confirmed by inspection
            # that compound pages (e.g. Makadi Heights, Ras Soma) render "similar/nearby projects"
            # unit cards inline alongside the page's own units, with no visual distinction the LLM
            # schema was told to look for. Without this, those foreign units get silently imported
            # under the WRONG compound (wrong price, wrong specs, wrong everything but the page they
            # happened to appear on). A unit with no detail_url can't be verified either way -- kept
            # rather than dropped, since the vast majority of a real page's OWN units do carry one.
            detail_url = u.get("detail_url") or ""
            if detail_url and not is_same_compound(urljoin(url, detail_url), prefix):
                rejected_foreign_links.append(detail_url)
                continue
            # Override the LLM's listing_type guess with the deterministic DOM check whenever
            # possible — testing showed the LLM reliably notices a "Resale"/"Nawy Now" tag when
            # present, but is NOT reliable at concluding "no tag -> Primary" on its own. Force
            # Unknown (never trust the LLM's own guess alone) when the card can't be verified.
            u["listing_type"] = classify_listing_type_from_html(html, u.get("detail_url")) or "Unknown"
            key = unit_dedup_key(u)
            if key in unit_keys:
                continue
            unit_keys.add(key)
            units.append(u)

        if j.get("has_next_page") and j.get("next_page_url"):
            nxt = urljoin(url, j["next_page_url"])
            if nxt in seen_urls:
                pass
            elif is_same_compound(nxt, prefix):
                queue.append(nxt)
            else:
                rejected_foreign_links.append(nxt)

        if not page_units:
            for link in (j.get("sub_listing_links") or []):
                link_url = link.get("url")
                if not link_url:
                    continue
                abs_url = urljoin(url, link_url)
                if abs_url in seen_urls:
                    continue
                if is_same_compound(abs_url, prefix):
                    queue.append(abs_url)
                    branch_counts.append((link.get("label"), link.get("count")))
                else:
                    rejected_foreign_links.append(abs_url)

        if queue:
            time.sleep(sleep_s)

    if budget <= 0 and queue:
        warnings.append(
            f"Stopped after the --max-pages budget ({max_pages} request(s)) with "
            f"{len(queue)} more page(s)/link(s) still queued — raise --max-pages to cover the rest."
        )

    if rejected_foreign_links:
        sample = ", ".join(rejected_foreign_links[:3])
        warnings.append(
            f"Ignored {len(rejected_foreign_links)} link(s) pointing to a DIFFERENT compound "
            f"(likely a 'similar/nearby compounds' recommendation block) — not crawled: {sample}"
            + (", ..." if len(rejected_foreign_links) > 3 else "")
        )

    if total_advertised is None and branch_counts:
        counted = sum(c for _, c in branch_counts if isinstance(c, int))
        if counted:
            total_advertised = counted

    if total_advertised is not None and len(units) < total_advertised:
        warnings.append(
            f"Page(s) advertise {total_advertised} unit(s) but only {len(units)} were extracted "
            f"— likely more pagination or drill-in branches than were crawled. Re-run with a "
            f"higher --max-pages, or check the URL(s) manually."
        )

    return compound_fields, units, total_advertised, warnings, pages_fetched


def filter_primary_units(units):
    """Splits extracted units into (primary, excluded) by listing_type. Only units explicitly
    tagged 'Resale' or 'Nawy Now' are excluded — 'Primary' and 'Unknown' are both kept, since we
    only want to drop what's EXPLICITLY confirmed non-Primary, never speculatively."""
    primary, excluded = [], []
    for u in units:
        if (u.get("listing_type") or "") in ("Resale", "Nawy Now"):
            excluded.append(u)
        else:
            primary.append(u)
    return primary, excluded


def _unit_base_row(u, slug):
    """The fields shared by both a kept unit row (UNIT_FIELDS) and an excluded-unit report row
    (EXCLUDED_UNIT_FIELDS) — factored out so the two never drift apart."""
    unit_type = u.get("unit_type") or derive_unit_type_from_url(u.get("detail_url")) or ""
    return {
        "compound_slug": slug,
        "unit_type": unit_type,
        "phase": u.get("phase") or "",
        "delivery_year": u.get("delivery_year") or "",
        "bedrooms": u.get("bedrooms") or "",
        "bathrooms": u.get("bathrooms") or "",
        "area_sqm": u.get("area_sqm") or "",
        "price": u.get("price") or "",
        "currency": (u.get("currency") or "EGP").strip().upper() or "EGP",
        "payment_plan": u.get("payment_plan") or "",
        "image_url": u.get("image_url") or "",
        "is_available": "false" if u.get("is_available") is False else "true",
        "is_launch": "true" if u.get("is_launch") else "false",
    }


def build_excluded_rows(units, slug):
    """units here should already be filtered to non-Primary (Resale/Nawy Now) by filter_primary_units."""
    rows = []
    for u in units:
        row = _unit_base_row(u, slug)
        row["listing_type"] = u.get("listing_type") or "Unknown"
        assert set(row.keys()) == set(EXCLUDED_UNIT_FIELDS), "excluded-unit row columns drifted from EXCLUDED_UNIT_FIELDS"
        assert len(row) == len(EXCLUDED_UNIT_FIELDS), "excluded-unit row column count != header column count"
        rows.append(row)
    return rows


def build_rows(fields, units, used_slugs):
    """`units` should already be filtered to Primary-only (via filter_primary_units) — min/max
    price and every unit row here come only from what's passed in. Returns
    (compound_row, unit_rows, about_text_raw) — about_text_raw is the source material for a
    human/Claude-written short_description/full_description, kept OUT of compound_row itself so
    COMPOUND_FIELDS' contract with app.py's import route never drifts."""
    name = (fields.get("compound_name") or "").strip()
    slug = unique_slug(slugify(name), used_slugs)

    prices = [u["price"] for u in units if isinstance(u.get("price"), (int, float))]
    min_price = min(prices) if prices else ""
    max_price = max(prices) if prices else ""

    about_text_raw = (fields.get("full_description") or fields.get("short_description") or "").strip()

    unit_rows = []
    for u in units:
        row = _unit_base_row(u, slug)
        assert set(row.keys()) == set(UNIT_FIELDS), "unit row columns drifted from UNIT_FIELDS"
        assert len(row) == len(UNIT_FIELDS), "unit row column count != header column count"
        unit_rows.append(row)

    # Compound.currency is one value, but units are extracted per-card (see the
    # PROMPT/schema note on currency above) and a page can genuinely mix EGP and
    # USD cards (seen on real nawy.com pages, e.g. Kamaran in El Gouna). Take the
    # majority currency among this compound's own Primary units for the
    # compound-level field (used by price_range_display() etc) and report any
    # minority currency back to the caller so it can be surfaced as a warning
    # rather than silently dropped.
    currency_counts = Counter(row["currency"] for row in unit_rows)
    compound_currency = currency_counts.most_common(1)[0][0] if currency_counts else "EGP"
    mixed_currencies = sorted(currency_counts) if len(currency_counts) > 1 else []

    compound_row = {
        "name": name,
        "slug": slug,
        "developer": fields.get("developer") or "",
        "location": fields.get("location") or "",
        "area": fields.get("area") or "",
        "location_detail": fields.get("location_detail") or "",
        # Never nawy's own text verbatim (or a light paraphrase) — see REWRITE_PLACEHOLDER comment above.
        "short_description": REWRITE_PLACEHOLDER,
        "full_description": REWRITE_PLACEHOLDER,
        "min_price": min_price,
        "max_price": max_price,
        "currency": compound_currency,
        "land_area_acres": fields.get("land_area_acres") or "",
        "delivery_year": fields.get("delivery_year") or "",
        "cover_image_url": fields.get("cover_image_url") or "",
        # Meleven's own contact numbers for this listing — not scraped from a
        # competitor site on purpose. Fill in via the admin form or the CSV.
        "contact_phone": "",
        "contact_whatsapp": "",
        "is_featured": "false",
        "is_launch": "true" if fields.get("is_launch") else "false",
        "is_published": "true",
    }
    assert set(compound_row.keys()) == set(COMPOUND_FIELDS), "compound row columns drifted from COMPOUND_FIELDS"
    assert len(compound_row) == len(COMPOUND_FIELDS), "compound row column count != header column count"

    return compound_row, unit_rows, about_text_raw, mixed_currencies


def write_csv(path, fieldnames, rows, append=False):
    mode = "a" if append and path.exists() else "w"
    write_header = mode == "w"
    with open(path, mode, newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        if write_header:
            writer.writeheader()
        for row in rows:
            if len(row) != len(fieldnames):
                raise ValueError(f"Row has {len(row)} columns, header has {len(fieldnames)}: {row}")
            writer.writerow(row)


def write_compound_output(out_dir, compound_row, unit_rows, about_text_raw="", excluded_rows=None):
    """Each compound gets its own isolated set of files at <out_dir>/<slug>/ — compound.csv,
    units.csv, and (only if there's anything to report) excluded_units.csv — always freshly
    overwritten (re-scraping the same compound just refreshes its own folder; it doesn't append
    onto a shared file). compound.csv is written with COMPOUND_STAGING_FIELDS (COMPOUND_FIELDS
    plus about_text_raw) — strip that extra column and replace the REWRITE_PLACEHOLDER values
    with real copy before this is ready for /admin/compounds/import. Returns (compound_path,
    units_path, excluded_path_or_None)."""
    compound_dir = Path(out_dir) / compound_row["slug"]
    compound_dir.mkdir(parents=True, exist_ok=True)
    compound_path = compound_dir / "compound.csv"
    units_path = compound_dir / "units.csv"
    staging_row = {**compound_row, "about_text_raw": about_text_raw}
    write_csv(compound_path, COMPOUND_STAGING_FIELDS, [staging_row], append=False)
    write_csv(units_path, UNIT_FIELDS, unit_rows, append=False)

    excluded_path = None
    if excluded_rows:
        excluded_path = compound_dir / "excluded_units.csv"
        write_csv(excluded_path, EXCLUDED_UNIT_FIELDS, excluded_rows, append=False)

    return compound_path, units_path, excluded_path


def main():
    # Without this, stdout is fully-buffered (not line-buffered) whenever it isn't a TTY — e.g.
    # piped to a file or captured by a background task runner — so every progress print() below
    # sits invisible in the buffer until the process exits, making a long crawl look hung even
    # while it's working normally. Line-buffering flushes each print as it happens instead.
    sys.stdout.reconfigure(line_buffering=True)

    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("urls", nargs="+", help="One or more nawy.com compound URLs")
    parser.add_argument("--out-dir", default=str(PROJECT_ROOT / "scripts" / "output"),
                         help="Base directory — each compound gets its own <out-dir>/<slug>/compound.csv + units.csv (default: scripts/output)")
    parser.add_argument("--max-pages", type=int, default=25,
                         help="Max Firecrawl requests PER compound URL, across pagination + drill-in branches (default: 25)")
    parser.add_argument("--sleep", type=float, default=1.0, help="Seconds to wait between Firecrawl requests (default: 1.0)")
    args = parser.parse_args()

    load_dotenv(PROJECT_ROOT / ".env")
    load_dotenv()
    api_key = os.environ.get("FIRECRAWL_API_KEY")
    if not api_key:
        sys.exit(
            "FIRECRAWL_API_KEY not found. Copy scripts/.env.example to .env in the project root "
            "and fill in your key (or export FIRECRAWL_API_KEY in your shell)."
        )

    out_dir = Path(args.out_dir)
    used_slugs = set()
    written, all_warnings = [], []

    for url in args.urls:
        print(f"\n=== {url} ===")
        fields, units, total_advertised, warnings, pages_fetched = crawl_compound(
            url, api_key, args.max_pages, args.sleep, log=print
        )
        all_warnings.extend(f"[{url}] {w}" for w in warnings)

        if not (fields.get("compound_name") or "").strip():
            print("  ⚠️  could not extract a compound name from this URL — skipping.")
            all_warnings.append(f"[{url}] no compound name extracted — nothing written for this URL.")
            continue

        primary_units, excluded_units = filter_primary_units(units)
        unknown_kept = sum(1 for u in primary_units if (u.get("listing_type") or "Unknown") == "Unknown")

        crow, urows, about_text_raw, mixed_currencies = build_rows(fields, primary_units, used_slugs)
        excluded_rows = build_excluded_rows(excluded_units, crow["slug"])
        compound_path, units_path, excluded_path = write_compound_output(out_dir, crow, urows, about_text_raw, excluded_rows)
        written.append((
            crow["name"], crow["slug"], compound_path, units_path, excluded_path,
            len(urows), len(excluded_rows), unknown_kept, total_advertised, pages_fetched,
        ))

        if mixed_currencies:
            all_warnings.append(
                f"[{url}] '{crow['name']}' has Primary units in more than one currency "
                f"({', '.join(mixed_currencies)}) — compound_row.currency was set to the majority "
                f"one ('{crow['currency']}'); check units.csv's own currency column per row before "
                f"importing, a minority-currency unit needs its price double-checked."
            )

        summary = f"  -> '{crow['name']}' (slug={crow['slug']}): {len(urows)} Primary unit(s)"
        if excluded_rows:
            summary += f", {len(excluded_rows)} excluded (Resale/Nawy Now)"
        if total_advertised:
            summary += f", {total_advertised} advertised total"
        summary += f", {pages_fetched} page(s) fetched"
        print(summary)
        if unknown_kept:
            print(f"     note: {unknown_kept} unit(s) had an undetermined listing_type — kept in units.csv, not excluded.")

    print(f"\nWrote {len(written)} compound(s):")
    for name, slug, compound_path, units_path, excluded_path, n, n_excluded, n_unknown, total_adv, pages in written:
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

    if all_warnings:
        print("\n⚠️  Warnings:")
        for w in all_warnings:
            print(f"  - {w}")

    print(
        "\nReminder: slugs above are generated locally with the same slugify() app.py uses. If a "
        "compound with the same slug already exists in the live DB, Step 1 of /admin/import will "
        "auto-suffix it (e.g. '-2') — check the resulting slug in the admin dashboard and fix "
        "compound_slug in units.csv before running Step 2 if it changed."
    )
    print(
        "Reminder: contact_phone / contact_whatsapp / is_featured were left blank/false on purpose — "
        "those are Meleven's own business fields, not something to pull from a competitor listing site."
    )
    print(
        "Reminder: short_description/full_description in each compound.csv are the literal "
        f"placeholder '{REWRITE_PLACEHOLDER}' — never nawy's own text. Write original copy from "
        "the about_text_raw column, then delete that column before importing."
    )


if __name__ == "__main__":
    main()
