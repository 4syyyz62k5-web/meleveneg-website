# nawy_to_csv.py / nawy_by_developer.py

Scrapes nawy.com compound page(s) via the [Firecrawl](https://www.firecrawl.dev) API and writes, for
each compound, a `compound.csv` + `units.csv` pair in exactly the column order
`/admin/compounds/import` and `/admin/units/import` read (see `app.py`).

## Setup

```bash
pip install -r scripts/requirements.txt
cp scripts/.env.example .env      # then paste your real key into .env
```

`.env` lives in the project root (same place the Flask app already loads config from) and is
git-ignored — never commit it.

## Usage

```bash
python scripts/nawy_to_csv.py https://www.nawy.com/compound/530-silversands
python scripts/nawy_to_csv.py <url1> <url2> --max-pages 40

python scripts/nawy_by_developer.py "Palm Hills"
python scripts/nawy_by_developer.py https://www.nawy.com/developer/85-ora-developers
```

Each compound gets its own isolated set of files:

```
scripts/output/<compound_slug>/compound.csv
scripts/output/<compound_slug>/units.csv            # Primary (Developer Sale) units only
scripts/output/<compound_slug>/excluded_units.csv   # Resale / Nawy Now units — written only if any were found
```

Re-scraping the same compound later just refreshes its own folder — it doesn't append onto a shared
file. `nawy_by_developer.py` writes every compound it finds for that developer into this same
`scripts/output/` tree, one folder per compound, right alongside anything `nawy_to_csv.py` already
wrote there. Both scripts print every file path they wrote in their final report. Upload each
compound's pair into `/admin/import` — Step 1 (`compound.csv`) then Step 2 (`units.csv`).

## How it decides what counts as a "unit"

nawy's top-level compound page usually shows phase cards and aggregate counts (e.g. "77 Chalet for
sale"), not individually-priced unit cards — those tend to live one or two clicks deeper. So for each
URL you give it, the script asks Firecrawl to extract, from whatever page it's actually looking at:

- compound-level fields (name, developer, location/area, description, land area, delivery year, cover image)
- any individually-priced unit cards on *that* page
- if there are none: any "drill-in" links (by property type / by phase) it can see, plus their
  advertised counts
- pagination info (a real next-page URL), if present

It then follows drill-in links and pagination automatically, up to `--max-pages` Firecrawl requests
per compound URL, merging and de-duplicating units as it goes. This is driven entirely by what
Firecrawl reports on each page — nothing about nawy's URL scheme is hardcoded — so it adapts whether
the URL you pass in is a compound overview page or an already-deep listing page.

## Guardrails baked in

- **`delivery_year` is never guessed** — compound-level and per-unit — it's only filled in when a
  specific year is explicitly printed on the page; otherwise it's left blank.
- **Column count is enforced**: every CSV row is built as a dict with exactly the header's keys, and
  both `build_rows()` (assertions) and `write_csv()` (explicit length check) verify the column count
  matches the header before anything is written.
- **Under-extraction is flagged**: if a page states a total unit count higher than what was actually
  extracted (because of pagination or drill-in branches not fully crawled, or the `--max-pages`
  budget running out), the script prints an explicit `⚠️ Warning` naming the shortfall — it never
  silently under-reports.
- **`contact_phone` / `contact_whatsapp` / `is_featured` are left blank/false on purpose** — those are
  Meleven's own business fields, not something to copy from a competitor's listing. Fill them in via
  the CSV or the admin form.
- **Slugs use the same `slugify()` as `app.py`.** If a compound with the same slug already exists in
  the live DB, Step 1 of `/admin/import` will auto-suffix it (e.g. `-2`) — check the resulting slug in
  the admin dashboard and fix `compound_slug` in `units.csv` before running Step 2 if it changed.
- **Only Primary (Developer Sale) units land in `units.csv`.** nawy.com mixes Primary, Resale, and
  "Nawy Now" listings on the same compound page. Each unit card's real type is read deterministically
  from the page's own HTML (a colored tag/badge reading exactly "Resale" or "Nawy Now" — a card with
  no such tag at all is Primary) rather than trusted to the LLM extraction, which testing showed
  reliably notices a tag when present but is NOT reliable at concluding "no tag → Primary" on its
  own. Anything confirmed Resale/Nawy Now is excluded and written to that compound's own
  `excluded_units.csv` instead — never silently dropped. A unit whose type genuinely can't be
  determined is left in `units.csv` as-is (kept, not excluded) rather than guessed either way.
- **`nawy_by_developer.py` never writes a compound under the wrong developer.** A developer's own
  nawy.com page only surfaces a partial carousel (~12 compounds) plus whatever's mentioned in its
  "About" text — no real "view all" listing — so discovery there is best-effort. As a safety net, once
  a candidate compound is actually scraped, its own extracted `developer` field is checked against the
  developer you asked for; any mismatch is rejected and reported, never silently written. The final
  report also flags when the accepted count falls short of the site's own stated project count.

## nawy_map_compounds.py

Discovery-only: uses Firecrawl's `map` endpoint to find every nawy.com URL containing `/compound/`,
counts the distinct compounds, and writes `all_compound_urls.txt` (raw) + `all_compound_urls_top_level.txt`
(deduped, one per compound) — no scraping, no data extraction. Useful for sizing a job before running
either scraper above.

```bash
python scripts/nawy_map_compounds.py
```

## Note on the on-page import help text

`/admin/import`'s own help text (`templates/admin/import.html`) is stale — it's missing the
`location` and `is_launch` columns that `app.py`'s import routes actually read. This script's column
order matches what the code actually does, not that help text.
