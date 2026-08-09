# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

A Flask real estate listings site for Meleven Consultancy (Egyptian property advisory). Public pages showcase
"compounds" (developments) and their "units" (individual listings); a password-gated `/admin` area lets staff
manage compounds, units, developers, and leads, including bulk CSV import. Deployed on Render.

## Running locally

```bash
pip install -r requirements.txt
python app.py          # runs on debug mode, http://127.0.0.1:5000
```

No `.env` is required to start — `config.py` falls back to a local SQLite file (`meleveneg.db`) and default
dev secrets when `DATABASE_URL`, `SECRET_KEY`, `ADMIN_PASSWORD`, etc. aren't set as env vars.

Seed sample data (one compound with a few units) once the DB exists:

```bash
python seed.py
```

There is no test suite, linter, or build step in this repo — don't invent commands for these.

## Deployment

Runs on Render via `gunicorn` (see `requirements.txt`) against Python 3.12.7 (`runtime.txt`). Render provides
`DATABASE_URL` (Postgres) once a DB is attached; locally/without one it falls back to SQLite. Uploaded photos
are expected to live on a Render persistent disk mounted at `UPLOAD_FOLDER` (default `/var/uploads`) so they
survive redeploys — don't assume local filesystem uploads persist in production.

## Architecture

**Everything lives in three top-level files**, not a package:

- `app.py` — the entire Flask app. `create_app()` builds the app, registers all routes as inline closures, and
  returns it; `app = create_app()` at module bottom is what gunicorn/`flask run` imports. There are no
  blueprints — all ~35 routes are defined inline inside `create_app()`.
- `models.py` — four SQLAlchemy models: `Compound`, `Unit` (FK to Compound), `Lead` (inquiry form submissions,
  optionally FK'd to a Compound), `Developer` (display logos, matched to `Compound.developer` by name string,
  not a FK — intentionally, so existing compound data never needs backfilling to get a logo).
- `config.py` — env-driven `Config` class. Normalizes `postgres://`/`postgresql://` URLs to
  `postgresql+psycopg://` (psycopg3) since Render's `DATABASE_URL` doesn't include a driver.

**Self-healing schema migrations**: there's no migration framework (no Alembic/Flask-Migrate). Instead,
`create_app()` runs `db.create_all()` then manually checks `inspector.get_columns(...)` for a few
columns (`compounds.location`, `compounds.is_launch`, `units.is_launch`) and runs raw `ALTER TABLE` if missing.
This pattern exists because there's no direct DB shell access in the deploy workflow — if you add a new
column to an existing table, follow the same pattern (check-then-`ALTER TABLE`) rather than assuming
`db.create_all()` covers it. This block also backfills `Compound.location` from `Compound.area` via a static
`AREA_TO_LOCATION` dict, on every startup, for any row where `location` is still empty.

**Location model**: two-level geography. `Compound.location` is the top-level region shown in nav/footers
(e.g. "New Cairo", "North Coast"); `Compound.area` is the sub-area (e.g. "Mostakbal City", "Sidi Heneish").
The `/locations` page groups by `location` (falling back to `area` via `COALESCE` for rows not yet backfilled)
and lists each location's sub-areas. The homepage's "Explore Areas" cards group by `area` directly. Don't
conflate the two columns when adding features — check which one a given page already uses.

**"Launch" flagging**: a compound-wide `is_launch` boolean is a shortcut for flagging every unit at once;
a unit can also be flagged individually via `Unit.is_launch`. The homepage's "New Launches" section OR's both
together, then falls back to soonest-`delivery_year` sort if nothing is flagged, so the section is never empty.

**Investment calculator** (homepage "Plan Your Investment" panel): server-rendered initial figures (computed
in `home()`) plus a live AJAX endpoint, `GET /api/properties-count`, that re-runs the same filtered query
(budget + area/type/developer/bedrooms/delivery-year) and returns JSON (`count`, `projects`, `min_price`,
`suggested_areas`). Keep the filter logic in both places in sync if you change one.

**Admin auth**: a single shared password (`Config.ADMIN_PASSWORD`), no per-user accounts. `login_required` is
a local decorator (defined inside `create_app()`, not importable) checking `session["admin_logged_in"]`. There
is no CSRF protection despite `Flask-WTF` being installed — it's not currently wired into the admin forms.

**CSV bulk import** (`/admin/compounds/import`, `/admin/units/import`): compounds import generates a unique
slug per row (via `slugify()`, disambiguated with a numeric suffix on collision) and prints
`slug` back to server logs so admins can line up a matching units CSV afterwards; units import matches rows to
an existing compound by `compound_slug` and skips + reports any unmatched slugs by name rather than failing
the whole import.

**Image uploads**: `save_uploaded_image()` validates extension (`ALLOWED_IMAGE_EXTENSIONS`), writes to
`Config.UPLOAD_FOLDER` under a `uuid4`-based filename (collision-proof, discards the original name), and is
served back via the catch-all `/uploads/<path:filename>` route (not directly by a webserver).

**Templates**: Jinja2, no template inheritance beyond one shared `templates/base.html` (nav, footer, mobile
menu JS, all inlined `<style>` — there's no separate layout CSS file for header/footer chrome). Admin templates
inherit from their own `templates/admin/base.html`. `templates/index_v2.html` and `templates/base_v2.html` are
unused/orphaned — not referenced anywhere in `app.py`; don't assume they're live just because they exist.

**Styling**: two plain CSS files, no build step/preprocessor/bundler — `static/css/style.css` (public site,
uses CSS custom properties like `--color-navy`, `--color-gold`) and `static/css/admin.css` (admin area). Editing
either takes effect on refresh with no compile step.
