import re
import csv
import io
import json
import os
import uuid
import xml.etree.ElementTree as ET
from datetime import datetime
from functools import wraps
from flask import Flask, render_template, request, redirect, url_for, flash, session, send_from_directory, jsonify, Response, abort
from werkzeug.utils import secure_filename
from dotenv import load_dotenv

# Loads .env into os.environ for local development (scripts/ already does this
# for the scraper tools; the main app never did). Real environment variables
# set on the host (e.g. Render) always win — load_dotenv() does not override
# a variable that's already set, it only fills in what's missing. This MUST
# run before `from config import Config` below -- Config's class attributes
# (e.g. CLAUDE_API_KEY) read os.environ at class-definition time, so if
# load_dotenv() ran after that import, .env's values would never make it in.
load_dotenv()

import anthropic
from flask_limiter import Limiter
from flask_limiter.util import get_remote_address
from config import Config
from models import db, Compound, Unit, Lead, Developer, Listing

# Rate-limits the public API (see /api/public/* below) -- in-memory storage,
# per-process. Fine at the current scale/single-service deployment; would
# need a shared backend (e.g. Redis) if this ever runs as multiple
# instances/dynos, since each process would then enforce its own separate
# budget instead of one shared one.
limiter = Limiter(key_func=get_remote_address)

ALLOWED_IMAGE_EXTENSIONS = {"png", "jpg", "jpeg", "webp", "gif"}

# Unit types that are commercial/administrative rather than residential. A
# compound can be genuinely residential overall (and kept during the
# nawy.com import, which only excluded a compound if ALL of its units were
# non-residential) while still having a handful of these mixed in -- e.g. a
# small retail strip inside an otherwise-residential gated community. The
# homepage Investment Calculator is explicitly residential-focused ("Plan
# Your Investment"), so these are filtered out of both its Type dropdown
# (all_unit_types) and its actual matching/counting queries -- a visitor
# should never be able to select or match against "Retail"/"Office"/etc.
# there. Deliberately NOT applied to /compounds, a compound's own unit
# listing, or the chatbot -- those are general property browsing, not the
# residential-investment calculator, so a genuine retail unit should still
# be visible/searchable there.
NON_RESIDENTIAL_UNIT_TYPES = {
    "Retail", "Administrative", "Administrative Office", "Commercial",
    "Medical", "Office", "Clinic", "Shop", "Pharmacy", "Mall",
    "Hotel Room", "Hotel Unit",
}

# ---------------------------------------------------------------------------
# /api/chat — property-search chatbot (Claude API + tool use, no RAG). See
# _run_query_properties_tool() in create_app() for what the tool actually
# queries; these two constants are the static parts of the Claude request.
# ---------------------------------------------------------------------------
CLAUDE_CHAT_MODEL = "claude-haiku-4-5-20251001"

QUERY_PROPERTIES_TOOL = {
    "name": "query_properties",
    "description": (
        "Search Meleven's real, live database of published real estate compounds "
        "and available units. Returns ONLY actual matching rows -- never invent or "
        "guess a property that isn't in the results. Call this whenever the visitor "
        "asks about available properties, prices, locations, developers, or unit "
        "specs, even if you think you already know the answer."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "area": {
                "type": "string",
                "description": "A location or sub-area name, e.g. 'North Coast', 'New Cairo', 'Sidi Heneish'. Matched against both the compound's top-level location and its sub-area (partial match).",
            },
            "developer": {
                "type": "string",
                "description": "Developer name, e.g. 'Mountain View', 'SODIC' (partial match).",
            },
            "unit_type": {
                "type": "string",
                "description": "e.g. 'Chalet', 'Villa', 'Townhouse', 'Apartment' (partial match).",
            },
            "min_price": {"type": "number", "description": "Minimum unit price in EGP."},
            "max_price": {"type": "number", "description": "Maximum unit price in EGP."},
            "bedrooms": {"type": "integer", "description": "Minimum number of bedrooms -- matches units with this many bedrooms OR MORE, not an exact count."},
            "delivery_year": {"type": "integer", "description": "Exact handover/delivery year."},
        },
        "required": [],
    },
}

# Shared by CHATBOT_SYSTEM_PROMPT and SMART_SEARCH_SYSTEM_PROMPT. Compound.area/
# location are stored in English (see AREA_TO_LOCATION in create_app()) even
# when a visitor writes in Arabic -- without this, area="التجمع الخامس" gets
# passed straight through and the ILIKE match against "New Cairo" silently
# finds nothing. Keep this list in sync with AREA_TO_LOCATION's target values
# if that mapping ever changes.
AREA_NORMALIZATION_GUIDANCE = """AREA NAMES: the database stores the top-level region in English even when the
visitor writes in Arabic. Always translate a colloquial area name to the
matching English value below before using it as the area filter -- never pass
the visitor's raw Arabic area text straight through, it will not match
anything in the database:
  التجمع / التجمع الخامس / القاهرة الجديدة -> "New Cairo"
  الساحل / الساحل الشمالي -> "North Coast"
  الشيخ زايد / زايد / أكتوبر -> "West Cairo"
  العاصمة الإدارية / العاصمة الجديدة -> "New Capital"
  العين السخنة -> "Ain Sokhna"
  الإسكندرية -> "Alexandria"
If the visitor names a specific neighborhood that isn't in this list (e.g.
"Mostakbal City", "Sidi Heneish"), pass that neighborhood name through as-is
instead -- the area filter matches sub-areas too, just not Arabic ones."""

CHATBOT_SYSTEM_PROMPT = f"""You are Meleven Consultancy's property assistant on meleveneg.com, a boutique
Egyptian real estate advisory.

STRICT RULES -- these override anything else:
1. You must NEVER state a property name, price, location, developer, unit spec,
   or availability fact unless it came directly from a query_properties tool
   result in THIS conversation. Do not use prior knowledge, training data, or
   general assumptions about Egyptian real estate.
2. Whenever the visitor's message is about available properties, prices,
   locations, developers, unit types, bedrooms, or delivery timing -- call
   query_properties BEFORE answering, even if you think you already know.
   See AREA NAMES below before filling in the area filter.
3. If query_properties returns zero results, say so plainly and invite the
   visitor to loosen their criteria. Example: "مفيش نتيجة مطابقة لمعايير
   البحث دي. جرب تغيّر السعر أو المنطقة أو نوع الوحدة." (or the English
   equivalent). Do NOT suggest a specific alternative property, area, or
   developer that wasn't itself returned by a tool call -- do not soften this
   by guessing what "might" be close.
4. Never invent a payment plan, discount, delivery date, or availability status.
   If the visitor asks something the tool results don't cover, say you don't
   have that information and offer to connect them with the team instead.
5. Keep answers concise and scannable -- short paragraphs or a compact list,
   not walls of text. Always include each property's real /compound/<slug>
   link when citing it.

FORMATTING -- these are as strict as the rules above, visitors are reading
this on a small chat bubble, not a results page. Before sending any reply,
check it against every line below -- a reply that violates one of these is
a failed reply, not a stylistic nitpick:
- If the visitor's message is just a greeting or too vague to search on
  (e.g. "hi", "helloo", "عايز اسأل"), reply in ONE OR TWO short sentences
  only -- what you can help with -- and ask what they're looking for. Never
  open with a menu/list of every filter or capability you have.
- When query_properties returns results, NEVER start with a preamble
  sentence like "Great! There are 416 available units in New Cairo" or
  "Here's a sample:" -- go straight into the results themselves as your
  first line. No throat-clearing, no restating the visitor's question back
  to them, no announcing the count before showing anything.
- Show AT MOST 3 results, never more, even if more came back. Each result
  line is EXACTLY: name, a separator, the price, a separator, the
  /compound/<slug> link. Nothing else goes on that line -- no unit type, no
  bedroom count, no phase, no area, no developer, period. This holds even
  when several results are the same compound with different unit types --
  do NOT add "(Chalet, 2BR)" or similar to tell them apart; the price and
  the link already do that. If the visitor's question genuinely can't be
  answered without a second attribute per line (rare -- e.g. they explicitly
  asked to compare bedroom counts across options), that ONE extra attribute
  may be added, but area/developer/delivery-year never belong on a result
  line under any circumstance. Correct shape, follow this exactly:
    Silversands North Coast -- 6,500,000 EGP -- /compound/silversands
    Silversands North Coast -- 14,500,000 EGP -- /compound/silversands
  Wrong (do not do this):
    Silversands North Coast -- 6,500,000 EGP (Chalet, 2BR) -- /compound/silversands
- After the result lines, add exactly ONE closing sentence -- a single
  sentence, ending in one period or question mark, nothing after it. Never
  two sentences, never a sentence plus a second question. Pick exactly one
  of: (a) if total_matching_units is bigger than what you showed, name the
  remaining count and ask widen-or-narrow -- "وفيه 39 نتيجة تانية، عايز
  أوسع البحث ولا أضيّقه أكتر؟"; or (b) if you showed everything there is,
  a short consultation nudge instead. Never both in the same reply --
  delivery year, developer name, or any other extra fact does NOT belong
  in the closing sentence either, only in a result line if truly needed
  (see above), or not at all.
- No markdown subheadings, no bold section titles, no more than one emoji
  in a whole reply (plain text is preferred). A whole reply -- opening,
  results, and closing line together -- should be readable in a few
  seconds, not require scrolling a small chat bubble.

{AREA_NORMALIZATION_GUIDANCE}

LANGUAGE: Reply in the same language and register the visitor used (Egyptian
Arabic if they wrote Arabic, English if they wrote English). Don't switch
languages mid-conversation unless they do.

TONE: Helpful, direct, no hard-sell pressure. You represent a 12-year-old
boutique advisory, not a pushy sales bot.

WHEN YOU HAVE A REAL ANSWER (matches found or a clear "no match"): close with
one short, natural line encouraging the visitor to book a free consultation
or leave their contact details for a callback, pointing to the site's contact
page -- but only ever OFFER this, never insist on collecting their phone/email
yourself in the chat. Do not repeat this nudge on every single message if the
conversation is still in the middle of narrowing down criteria.

You have no knowledge beyond what query_properties returns and general
conversational ability. You are not a general real estate encyclopedia -- if
asked something unrelated to Meleven's own listings (mortgage law, other
countries, etc.), say that's outside what you can help with here."""

# ---------------------------------------------------------------------------
# /api/smart-search — natural-language search box on /compounds. Unlike
# /api/chat, this never generates a reply Claude's model never sees or states
# search RESULTS at all; its only job is translating free text into the same
# filter shape /compounds' own filter form already uses (area/developer/
# unit_type/min_price/max_price/bedrooms/delivery_year), forced via
# tool_choice so there's no back-and-forth and no risk of the model composing
# any user-facing text. The real query still runs entirely in the existing,
# deterministic compounds() route below -- Claude never touches a DB row.
# ---------------------------------------------------------------------------
EXTRACT_SEARCH_FILTERS_TOOL = {
    "name": "extract_search_filters",
    "description": (
        "Extract structured real-estate search filters from the visitor's free-text "
        "search query. Only include a field if the query actually specifies or clearly "
        "implies it -- never guess or default a field that wasn't mentioned. This is "
        "the only thing you do; you are not answering the visitor or holding a "
        "conversation."
    ),
    "input_schema": QUERY_PROPERTIES_TOOL["input_schema"],
}

SMART_SEARCH_SYSTEM_PROMPT = f"""You translate a real estate visitor's free-text search (Arabic or English) on
meleveneg.com into structured filters by calling extract_search_filters. You
never answer questions, never chat, never explain -- your only output is that
one tool call.

Only extract a field if the query actually states or clearly implies it.
Never invent or default a value for anything the visitor didn't mention --
leaving a field out entirely is always correct when it's genuinely absent
from the query, even if that means calling the tool with very few fields
filled in (or none at all for a query with no concrete criteria).

{AREA_NORMALIZATION_GUIDANCE}
"""


def slugify(text):
    text = (text or "").lower().strip()
    text = re.sub(r"[^a-z0-9]+", "-", text)
    return text.strip("-") or "compound"


def allowed_image(filename):
    return "." in filename and filename.rsplit(".", 1)[1].lower() in ALLOWED_IMAGE_EXTENSIONS


def save_uploaded_image(file_storage, upload_folder):
    """Saves an uploaded image with a unique filename. Returns the filename, or None if no valid file was given."""
    if not file_storage or file_storage.filename == "":
        return None
    if not allowed_image(file_storage.filename):
        return None
    os.makedirs(upload_folder, exist_ok=True)
    ext = secure_filename(file_storage.filename).rsplit(".", 1)[1].lower()
    unique_name = f"{uuid.uuid4().hex}.{ext}"
    file_storage.save(os.path.join(upload_folder, unique_name))
    return unique_name


def create_app():
    app = Flask(__name__)
    app.config.from_object(Config)
    db.init_app(app)
    limiter.init_app(app)
    app.jinja_env.globals["slugify"] = slugify  # so templates can build /developer/<slug> links

    with app.app_context():
        db.create_all()

        # ---------------------------------------------------------------
        # One-time, self-healing migration: `db.create_all()` only creates
        # brand-new tables, it never adds a column to a table that already
        # exists. Since there's no direct database shell access in this
        # workflow, this checks whether the `location` column is already on
        # the `compounds` table and adds it automatically if it's missing.
        # Safe to leave in permanently — once the column exists, this is a
        # no-op on every future restart.
        # ---------------------------------------------------------------
        inspector = db.inspect(db.engine)
        existing_columns = [col["name"] for col in inspector.get_columns("compounds")]
        if "location" not in existing_columns:
            with db.engine.connect() as connection:
                connection.execute(db.text("ALTER TABLE compounds ADD COLUMN location VARCHAR(150)"))
                connection.commit()

        if "is_launch" not in existing_columns:
            with db.engine.connect() as connection:
                connection.execute(db.text("ALTER TABLE compounds ADD COLUMN is_launch BOOLEAN DEFAULT FALSE"))
                connection.commit()

        existing_unit_columns = [col["name"] for col in inspector.get_columns("units")]
        if "is_launch" not in existing_unit_columns:
            with db.engine.connect() as connection:
                connection.execute(db.text("ALTER TABLE units ADD COLUMN is_launch BOOLEAN DEFAULT FALSE"))
                connection.commit()

        # `listings` itself is a brand-new table, so db.create_all() above
        # already creates it — only `leads.listing_id` needs the manual
        # check-then-ALTER TABLE treatment, since `leads` already existed.
        existing_lead_columns = [col["name"] for col in inspector.get_columns("leads")]
        if "listing_id" not in existing_lead_columns:
            with db.engine.connect() as connection:
                connection.execute(db.text("ALTER TABLE leads ADD COLUMN listing_id INTEGER"))
                connection.commit()

        # `listings` itself was new in the previous deploy, but it's already
        # live now, so a further field addition (owner_email) needs the same
        # check-then-ALTER TABLE treatment as everything else here.
        existing_listing_columns = [col["name"] for col in inspector.get_columns("listings")]
        if "owner_email" not in existing_listing_columns:
            with db.engine.connect() as connection:
                connection.execute(db.text("ALTER TABLE listings ADD COLUMN owner_email VARCHAR(150)"))
                connection.commit()

        # ---------------------------------------------------------------
        # Backfill: any existing compound that doesn't have `location` set
        # yet gets one assigned automatically based on its current `area`,
        # grouping sub-areas under the top-level regions the site uses
        # (New Cairo, North Coast, West Cairo, Ain Sokhna, New Capital,
        # Alexandria). This runs on every startup but only touches rows
        # where location is still empty, so it's safe to leave in place —
        # it will pick up newly imported compounds automatically too.
        # ---------------------------------------------------------------
        AREA_TO_LOCATION = {
            "New Cairo": "New Cairo",
            "Mostakbal City": "New Cairo",
            "New Heliopolis": "New Cairo",
            "North Coast": "North Coast",
            "North Coast-Sahel": "North Coast",
            "Ras El Hekma": "North Coast",
            "Sidi Abdel Rahman": "North Coast",
            "Al Dabaa": "North Coast",
            "New Zayed": "West Cairo",
            "El Sheikh Zayed": "West Cairo",
            "6th of October City": "West Cairo",
            "6th Settlement": "West Cairo",
            "October Gardens": "West Cairo",
            "Ain Sokhna": "Ain Sokhna",
            "New Capital City": "New Capital",
            "Alexandria": "Alexandria",
        }
        unmatched_compounds = Compound.query.filter(
            db.or_(Compound.location.is_(None), Compound.location == "")
        ).all()
        for c in unmatched_compounds:
            mapped = AREA_TO_LOCATION.get((c.area or "").strip())
            if mapped:
                c.location = mapped
        if unmatched_compounds:
            db.session.commit()

    @app.context_processor
    def inject_footer_areas():
        rows = db.session.query(Compound.area, db.func.count(Compound.id)).filter(
            Compound.is_published == True, Compound.area.isnot(None)
        ).group_by(Compound.area).order_by(Compound.area.asc()).all()

        dev_rows = db.session.query(Compound.developer, db.func.count(Compound.id)).filter(
            Compound.is_published == True, Compound.developer.isnot(None)
        ).group_by(Compound.developer).order_by(Compound.developer.asc()).all()

        return {
            "footer_areas": [{"name": r[0], "count": r[1]} for r in rows],
            "footer_developers": [{"name": r[0], "count": r[1]} for r in dev_rows],
        }

    # ---------- Uploaded file serving ----------

    @app.route("/uploads/<path:filename>")
    def uploaded_file(filename):
        return send_from_directory(app.config["UPLOAD_FOLDER"], filename)

    # Safari (macOS/iOS) requests /favicon.ico directly at the domain root as
    # a fallback, independent of the <link rel="icon"> tags in base.html --
    # Chrome doesn't need this and worked fine without it, which is why the
    # favicon showed correctly in Chrome but not Safari. static/img/favicon.ico
    # is a multi-resolution ICO (16/32/48/64px) built from the same brand-mark
    # crop as favicon-32.png/favicon-16.png.
    @app.route("/favicon.ico")
    def favicon_ico():
        return send_from_directory(
            os.path.join(app.root_path, "static", "img"), "favicon.ico",
            mimetype="image/vnd.microsoft.icon",
        )

    # ---------- Sitemap (for Google Search Console) ----------

    @app.route("/sitemap.xml")
    def sitemap():
        # Absolute URLs are built from Config.SITE_URL, not the incoming
        # request's Host header, so this always points at the real domain
        # regardless of how the app was reached.
        base_url = app.config["SITE_URL"].rstrip("/")
        urlset = ET.Element("urlset", xmlns="http://www.sitemaps.org/schemas/sitemap/0.9")

        def add_url(path, lastmod=None, changefreq=None, priority=None):
            el = ET.SubElement(urlset, "url")
            ET.SubElement(el, "loc").text = base_url + path
            if lastmod:
                ET.SubElement(el, "lastmod").text = lastmod.strftime("%Y-%m-%d")
            if changefreq:
                ET.SubElement(el, "changefreq").text = changefreq
            if priority:
                ET.SubElement(el, "priority").text = priority

        # Static pages — no lastmod, there's no real "last modified" signal
        # for a template-driven page (a fabricated one would be worse than none).
        for endpoint, changefreq, priority in [
            ("home", "daily", "1.0"),
            ("compounds", "daily", "0.8"),
            ("resale_listings", "weekly", "0.6"),
            ("rent_listings", "weekly", "0.6"),
            ("sell_property", "monthly", "0.5"),
            ("about", "monthly", "0.4"),
            ("contact", "monthly", "0.4"),
        ]:
            add_url(url_for(endpoint), changefreq=changefreq, priority=priority)

        # Every published compound's own page.
        for c in Compound.query.filter_by(is_published=True).all():
            add_url(
                url_for("compound_detail", slug=c.slug),
                lastmod=c.created_at, changefreq="weekly", priority="0.7",
            )

        # Every approved Resale/Rent listing's own page.
        for l in Listing.query.filter_by(status="approved").all():
            add_url(
                url_for("listing_detail", listing_id=l.id),
                lastmod=l.reviewed_at or l.submitted_at, changefreq="weekly", priority="0.5",
            )

        xml_bytes = ET.tostring(urlset, encoding="utf-8", xml_declaration=True)
        return Response(xml_bytes, mimetype="application/xml")

    # ---------- Public pages ----------

    @app.route("/")
    def home():
        featured = Compound.query.filter_by(is_featured=True, is_published=True).limit(6).all()
        latest = Compound.query.filter_by(is_published=True).order_by(Compound.created_at.desc()).limit(8).all()

        # Grouped by the normalized top-level `location` (New Cairo, North
        # Coast, ...) rather than the free-text `area` sub-area column —
        # `area` is scraped/entered per-compound with no normalization, so
        # grouping by it directly produced near-duplicates ("5th Settlement"
        # vs "Fifth Settlement", "North Coast" vs "North Coast-Sahel") and
        # garbage placeholder values ("N/A", "-", even blank) showing up as
        # if they were real areas. `location` is the column meant to already
        # be normalized (see the AREA_TO_LOCATION backfill above), so this
        # also excludes blank/placeholder-looking values defensively in case
        # a row's location was ever set directly to something like that.
        JUNK_LOCATION_VALUES = {"", "n/a", "-", "none", "null"}
        location_rows = (
            db.session.query(Compound.location, db.func.count(Compound.id))
            .filter(Compound.is_published == True, Compound.location.isnot(None))
            .group_by(Compound.location)
            .order_by(Compound.location)
            .all()
        )

        top_areas = []
        for location_name, count in location_rows:
            if (location_name or "").strip().lower() in JUNK_LOCATION_VALUES:
                continue
            # Grab one compound in this location that has a cover image, to represent it visually
            sample = (
                Compound.query
                .filter(
                    Compound.location == location_name,
                    Compound.is_published == True,
                    Compound.cover_image_url.isnot(None),
                    Compound.cover_image_url != "",
                )
                .order_by(Compound.is_featured.desc(), Compound.created_at.desc())
                .first()
            )
            top_areas.append({
                "name": location_name,
                "count": count,
                "cover_image_url": sample.cover_image_url if sample else None,
            })

        # Show at most this many as folder cards; anything beyond that is
        # only reachable via the "View all areas" dropdown so the two lists
        # never repeat each other.
        CARD_LIMIT = 5
        all_locations_sorted = top_areas
        top_areas = all_locations_sorted[:CARD_LIMIT]

        # "New Launches" — compounds flagged directly (Compound.is_launch) OR
        # with at least one unit explicitly flagged (Unit.is_launch), most
        # recently added first. Falls back to the old "soonest delivery
        # year" sort if nothing has been flagged yet, so the section never
        # sits empty while launch flags are still being set in the admin.
        new_launches = (
            Compound.query
            .outerjoin(Unit, Unit.compound_id == Compound.id)
            .filter(
                Compound.is_published == True,
                db.or_(Compound.is_launch == True, Unit.is_launch == True),
            )
            .distinct()
            .order_by(Compound.created_at.desc())
            .limit(8)
            .all()
        )
        if not new_launches:
            new_launches = (
                Compound.query
                .filter(Compound.is_published == True)
                .order_by(Compound.delivery_year.asc().nullslast(), Compound.created_at.desc())
                .limit(8)
                .all()
            )

        # "Recommended Units" — most recently added available units across published compounds
        # (Unit has no created_at column, so we use id as a proxy for insertion order)
        recommended_units = (
            Unit.query
            .join(Compound, Unit.compound_id == Compound.id)
            .filter(Unit.is_available == True, Compound.is_published == True)
            .order_by(Unit.id.desc())
            .limit(10)
            .all()
        )

        # Names for the hero search bar's compound autocomplete (datalist)
        all_compound_names = [
            row[0] for row in
            db.session.query(Compound.name).filter(Compound.is_published == True).order_by(Compound.name.asc()).all()
        ]

        # Always pulled fresh from the DB, so a newly added area shows up
        # in the hero search dropdown without any code changes. Distinct
        # `location` values are folded in too, so typing a top-level region
        # like "New Cairo" is offered right alongside its sub-areas.
        all_location_names = {
            row[0] for row in
            db.session.query(Compound.location).filter(Compound.is_published == True).distinct().all()
            if row[0] and row[0].strip().lower() not in JUNK_LOCATION_VALUES
        }
        all_area_names = sorted({a["name"] for a in all_locations_sorted} | all_location_names)

        # Developer names get the same predictive-search treatment
        all_developer_names = sorted({
            row[0] for row in
            db.session.query(Compound.developer).filter(Compound.is_published == True).distinct().all()
            if row[0]
        })

        # Only the locations NOT already shown as a folder card above — the
        # dropdown exists purely to reach the "rest" of them, so it never
        # repeats what's already visible in the scroll row.
        locations_menu = [
            {"name": a["name"], "count": a["count"], "cover_image_url": a["cover_image_url"]}
            for a in all_locations_sorted[CARD_LIMIT:]
        ]

        # Developer name -> logo URL, so property cards can show a real logo
        # instead of just the developer's name when one has been uploaded.
        developer_logos = {
            d.name: d.logo_url
            for d in Developer.query.filter(Developer.logo_url.isnot(None), Developer.logo_url != "").all()
        }

        # "Partnering With Industry Leaders" showcase — deliberately NOT
        # all_developer_names below (that list is for the search autocomplete
        # and is meant to be exhaustive; after the nawy.com import it grew to
        # 100+ names, only a handful of which have a real uploaded logo). A
        # logo showcase should only ever show real logos, never a bare-text
        # fallback pill, so this is the subset of all_developer_names that
        # has one, ranked by published-compound count and capped so the
        # section stays a curated strip rather than a directory dump.
        PARTNER_LOGO_LIMIT = 20
        top_partner_developers = [
            row[0] for row in
            db.session.query(Compound.developer, db.func.count(Compound.id))
            .filter(Compound.is_published == True, Compound.developer.in_(developer_logos.keys()))
            .group_by(Compound.developer)
            .order_by(db.func.count(Compound.id).desc())
            .limit(PARTNER_LOGO_LIMIT)
            .all()
        ]

        # Distinct unit types across published, available units — powers the
        # "Type" dropdown on the homepage Investment Calculator.
        all_unit_types = sorted({
            row[0] for row in
            db.session.query(Unit.unit_type)
            .join(Compound, Unit.compound_id == Compound.id)
            .filter(Compound.is_published == True, Unit.unit_type.isnot(None), Unit.unit_type != "",
                    Unit.unit_type.notin_(NON_RESIDENTIAL_UNIT_TYPES))
            .distinct()
            .all()
        })

        # Distinct delivery years — power the "Delivery" dropdown on the
        # Investment Calculator.
        all_delivery_years = sorted({
            row[0] for row in
            db.session.query(Compound.delivery_year)
            .filter(Compound.is_published == True, Compound.delivery_year.isnot(None))
            .distinct()
            .all()
        })

        # Initial calculator figures shown before the JS has run its first
        # live lookup — computed from the same defaults the down-payment
        # slider / installment field / duration select start at, via the
        # same "price <= down_payment + installment x periods" rule the
        # /api/properties-count endpoint uses, so the panel is already
        # correct on first paint instead of flashing from a placeholder.
        INITIAL_DOWN_PAYMENT = 2000000
        INITIAL_INSTALLMENT = 50000
        INITIAL_DURATION_YEARS = 8
        initial_periods = INITIAL_DURATION_YEARS * 12  # monthly is the default cadence
        initial_max_price = INITIAL_DOWN_PAYMENT + INITIAL_INSTALLMENT * initial_periods

        initial_calc_base_query = (
            Unit.query
            .join(Compound, Unit.compound_id == Compound.id)
            .filter(Unit.is_available == True, Compound.is_published == True, Unit.price.isnot(None),
                    Unit.price <= initial_max_price, Unit.unit_type.notin_(NON_RESIDENTIAL_UNIT_TYPES))
        )
        initial_calc_count = initial_calc_base_query.count()
        initial_calc_projects = initial_calc_base_query.with_entities(Compound.id).distinct().count()
        initial_calc_min_price = initial_calc_base_query.with_entities(db.func.min(Unit.price)).scalar()
        # When the down payment alone already covers the cheapest matching unit
        # (common now that budget resort units start well under a typical down
        # payment), there's no meaningful installment to show — surfaced via
        # initial_calc_covered_by_down_payment instead of a misleading near-zero
        # figure. See the identical logic in api_properties_count() below.
        initial_calc_min_installment = None
        initial_calc_covered_by_down_payment = False
        if initial_calc_min_price is not None:
            initial_calc_price_gap = float(initial_calc_min_price) - INITIAL_DOWN_PAYMENT
            if initial_calc_price_gap <= 0:
                initial_calc_covered_by_down_payment = True
            else:
                initial_calc_min_installment = initial_calc_price_gap / initial_periods
        initial_sample_units = _serialize_sample_units(initial_calc_base_query)

        return render_template(
            "index.html",
            featured=featured,
            latest=latest,
            top_areas=top_areas,
            new_launches=new_launches,
            recommended_units=recommended_units,
            all_compound_names=all_compound_names,
            all_area_names=all_area_names,
            all_developer_names=all_developer_names,
            locations_menu=locations_menu,
            developer_logos=developer_logos,
            top_partner_developers=top_partner_developers,
            all_unit_types=all_unit_types,
            all_delivery_years=all_delivery_years,
            initial_down_payment=INITIAL_DOWN_PAYMENT,
            initial_installment=INITIAL_INSTALLMENT,
            initial_duration_years=INITIAL_DURATION_YEARS,
            initial_calc_count=initial_calc_count,
            initial_calc_projects=initial_calc_projects,
            initial_calc_min_price=initial_calc_min_price,
            initial_calc_min_installment=initial_calc_min_installment,
            initial_calc_covered_by_down_payment=initial_calc_covered_by_down_payment,
            initial_sample_units=initial_sample_units,
        )

    # ---------- Investment calculator: live matching-properties count ----------

    # Units don't store a down-payment/installment plan as separate columns —
    # payment_plan is free text scraped from listings (e.g. "140,625
    # Quarterly / 8 Years") and isn't reliable enough to parse for filtering
    # (a sample of real scraped values found cadences beyond monthly/quarterly
    # and a handful of rows that don't match any "amount cadence / N Years"
    # shape at all). So instead of reading an installment plan off the unit,
    # this derives the price a visitor's own terms can reach and filters
    # Unit.price against that — the same math as the old max_price filter,
    # just computed from three inputs instead of taken directly.
    CALC_DURATIONS_YEARS = {5, 6, 7, 8, 10, 12}

    def _calc_periods_and_ceiling(down_payment, installment, cadence, duration_years):
        """Returns (periods, max_price) for the "down payment + installment x
        periods" rule, or (None, None) if installment/duration aren't usable —
        a unit with price P fits when P <= down_payment + installment x periods,
        i.e. the down payment plus every remaining installment covers it."""
        if not installment or duration_years not in CALC_DURATIONS_YEARS:
            return None, None
        periods_per_year = 4 if cadence == "quarterly" else 12
        periods = duration_years * periods_per_year
        return periods, down_payment + installment * periods

    def _serialize_sample_units(query, limit=4):
        """A cheap slice of an already-filtered Unit query — reuses the exact
        same WHERE clause built by the caller (no duplicated filter logic),
        just orders by price and caps the row count, so this is one small
        extra SELECT rather than a second heavy query."""
        units = query.order_by(Unit.price.asc()).limit(limit).all()
        return [
            {
                "compound_name": u.compound.name,
                "compound_slug": u.compound.slug,
                "unit_type": u.unit_type,
                "bedrooms": u.bedrooms,
                "bathrooms": u.bathrooms,
                "area_sqm": float(u.area_sqm) if u.area_sqm is not None else None,
                "price": float(u.price) if u.price is not None else None,
                "image_url": u.image_url or u.compound.cover_image_url,
            }
            for u in units
        ]

    @app.route("/api/properties-count")
    def api_properties_count():
        """
        Returns how many published, available units actually fit a visitor's
        own down payment + affordable installment + installment duration
        (plus optional Area / Type / Delivery Year filters) — powers the
        homepage "Plan Your Investment" panel so every figure shown
        (projects, units, starting price, starting installment) reflects the
        real, filtered inventory instead of a static estimate.
        """
        down_payment = request.args.get("down_payment", type=int) or 0
        installment = request.args.get("installment", type=int)
        cadence = request.args.get("cadence", "monthly").strip().lower()
        duration_years = request.args.get("duration_years", type=int)
        area = request.args.get("area", "").strip()
        unit_type = request.args.get("unit_type", "").strip()
        delivery_year = request.args.get("delivery_year", "").strip()

        periods, max_price = _calc_periods_and_ceiling(down_payment, installment, cadence, duration_years)

        query = (
            Unit.query
            .join(Compound, Unit.compound_id == Compound.id)
            .filter(Unit.is_available == True, Compound.is_published == True, Unit.price.isnot(None),
                    Unit.unit_type.notin_(NON_RESIDENTIAL_UNIT_TYPES))
        )
        if max_price is not None:
            query = query.filter(Unit.price <= max_price)
        if area:
            query = query.filter(db.or_(Compound.area.ilike(f"%{area}%"), Compound.location.ilike(f"%{area}%")))
        if unit_type:
            query = query.filter(Unit.unit_type.ilike(f"%{unit_type}%"))
        if delivery_year:
            query = query.filter(Compound.delivery_year == delivery_year)

        count = query.count()
        projects = query.with_entities(Compound.id).distinct().count()
        min_price = query.with_entities(db.func.min(Unit.price)).scalar()

        # The actual installment the cheapest matching unit would need on
        # these same terms — usually lower than what the visitor said they
        # could afford, which is worth surfacing rather than just echoing
        # their own input back at them. When the down payment alone already
        # covers the cheapest matching unit (common now that budget resort
        # units start well under a typical down payment), there's no
        # meaningful installment to show — surfaced via covered_by_down_payment
        # instead of a misleading near-zero figure like "EGP 0/month".
        min_installment = None
        covered_by_down_payment = False
        if min_price is not None:
            price_gap = float(min_price) - down_payment
            if price_gap <= 0:
                covered_by_down_payment = True
            elif periods:
                min_installment = price_gap / periods

        # Consultancy angle: when no area is picked yet, surface the areas
        # that actually have inventory within reach, so the panel can
        # suggest "Areas within your budget" instead of making the person
        # guess an area before seeing anything.
        suggested_areas = []
        if not area and max_price is not None:
            area_query = (
                Unit.query
                .join(Compound, Unit.compound_id == Compound.id)
                .filter(Unit.is_available == True, Compound.is_published == True,
                        Unit.price.isnot(None), Unit.price <= max_price,
                        Unit.unit_type.notin_(NON_RESIDENTIAL_UNIT_TYPES))
            )
            if unit_type:
                area_query = area_query.filter(Unit.unit_type.ilike(f"%{unit_type}%"))
            rows = (
                area_query.with_entities(Compound.area, db.func.count(Unit.id))
                .group_by(Compound.area)
                .order_by(db.func.count(Unit.id).desc())
                .limit(3)
                .all()
            )
            suggested_areas = [r[0] for r in rows if r[0]]

        sample_units = _serialize_sample_units(query)

        return jsonify({
            "count": count,
            "projects": projects,
            "min_price": min_price,
            "min_installment": min_installment,
            "covered_by_down_payment": covered_by_down_payment,
            "max_price": max_price,
            "suggested_areas": suggested_areas,
            "sample_units": sample_units,
        })

    # ---------- Chatbot: property search via Claude tool use ----------

    CHAT_RESULT_LIMIT = 8

    def _run_query_properties_tool(filters):
        """Executes the query_properties tool call against real inventory --
        same join/is_published/is_available shape as api_properties_count(),
        just filtered by the tool's own filter set instead of the calculator's
        derived price ceiling. Returns a JSON-serializable dict: capped result
        rows (each with a real /compound/<slug> link) plus the true total
        count, so the model can say "showing 8 of 23" instead of implying
        these are the only matches."""
        query = (
            Unit.query
            .join(Compound, Unit.compound_id == Compound.id)
            .filter(Unit.is_available == True, Compound.is_published == True)
        )

        area = (filters.get("area") or "").strip()
        if area:
            query = query.filter(db.or_(Compound.area.ilike(f"%{area}%"), Compound.location.ilike(f"%{area}%")))

        developer = (filters.get("developer") or "").strip()
        if developer:
            query = query.filter(Compound.developer.ilike(f"%{developer}%"))

        unit_type = (filters.get("unit_type") or "").strip()
        if unit_type:
            query = query.filter(Unit.unit_type.ilike(f"%{unit_type}%"))

        min_price = filters.get("min_price")
        if isinstance(min_price, (int, float)):
            query = query.filter(Unit.price >= min_price)

        max_price = filters.get("max_price")
        if isinstance(max_price, (int, float)):
            query = query.filter(Unit.price <= max_price)

        # "At least N bedrooms", not an exact match -- a visitor asking for
        # "3 bedrooms" almost always also wants to see 4- and 5-bedroom units.
        bedrooms = filters.get("bedrooms")
        if isinstance(bedrooms, int):
            query = query.filter(Unit.bedrooms >= bedrooms)

        delivery_year = filters.get("delivery_year")
        if isinstance(delivery_year, int):
            query = query.filter(Compound.delivery_year == delivery_year)

        total_matching = query.count()
        rows = query.order_by(Unit.price.asc()).limit(CHAT_RESULT_LIMIT).all()

        results = [
            {
                "compound_name": u.compound.name,
                "url": url_for("compound_detail", slug=u.compound.slug, _external=False),
                "location": u.compound.location,
                "area": u.compound.area,
                "developer": u.compound.developer,
                "unit_type": u.unit_type,
                "bedrooms": u.bedrooms,
                "bathrooms": u.bathrooms,
                "area_sqm": float(u.area_sqm) if u.area_sqm is not None else None,
                "price": float(u.price) if u.price is not None else None,
                "currency": u.currency,
                "delivery_year": u.compound.delivery_year,
            }
            for u in rows
        ]

        return {
            "total_matching_units": total_matching,
            "showing": len(results),
            "results": results,
        }

    @app.route("/api/chat", methods=["POST"])
    def api_chat():
        """Stateless chat turn: the client sends the full conversation history
        (this endpoint keeps nothing server-side -- see the plan discussion:
        no ChatLog table, conversation lives only in the browser tab). Runs at
        most one query_properties round-trip per visitor message -- Claude
        either answers directly or calls the tool once and we feed the real
        result back for a final grounded reply."""
        if not Config.CLAUDE_API_KEY:
            return jsonify({"error": "Chat is not configured."}), 503

        data = request.get_json(silent=True) or {}
        user_message = (data.get("message") or "").strip()
        history = data.get("history") or []

        if not user_message:
            return jsonify({"error": "message is required"}), 400
        if len(user_message) > 2000:
            return jsonify({"error": "message is too long"}), 400
        # Bound how much prior context we forward -- a chat widget conversation
        # shouldn't need more than this to stay coherent, and it caps token
        # spend per request regardless of what the client sends.
        if isinstance(history, list):
            history = history[-20:]
        else:
            history = []

        messages = []
        for turn in history:
            role = turn.get("role")
            content = turn.get("content")
            if role in ("user", "assistant") and isinstance(content, str) and content.strip():
                messages.append({"role": role, "content": content})
        messages.append({"role": "user", "content": user_message})

        client = anthropic.Anthropic(api_key=Config.CLAUDE_API_KEY)

        try:
            response = client.messages.create(
                model=CLAUDE_CHAT_MODEL,
                max_tokens=1024,
                system=CHATBOT_SYSTEM_PROMPT,
                tools=[QUERY_PROPERTIES_TOOL],
                messages=messages,
            )

            if response.stop_reason == "tool_use":
                tool_use_block = next(b for b in response.content if b.type == "tool_use")
                tool_result = _run_query_properties_tool(tool_use_block.input or {})

                messages.append({"role": "assistant", "content": response.content})
                messages.append({
                    "role": "user",
                    "content": [{
                        "type": "tool_result",
                        "tool_use_id": tool_use_block.id,
                        "content": json.dumps(tool_result),
                    }],
                })

                response = client.messages.create(
                    model=CLAUDE_CHAT_MODEL,
                    max_tokens=1024,
                    system=CHATBOT_SYSTEM_PROMPT,
                    tools=[QUERY_PROPERTIES_TOOL],
                    messages=messages,
                )

            reply_text = "".join(b.text for b in response.content if b.type == "text").strip()
            if not reply_text:
                reply_text = "معلش، حصلت مشكلة في صياغة الرد. جرب تاني أو تواصل معانا مباشرة."

            return jsonify({"reply": reply_text})

        except anthropic.APIError as e:
            app.logger.error(f"Claude API error in /api/chat: {e}")
            return jsonify({"error": "Chat is temporarily unavailable. Please try again shortly."}), 502

    # ---------- Smart search: natural-language query -> /compounds filters ----------

    @app.route("/api/smart-search", methods=["POST"])
    def api_smart_search():
        """Translates one free-text search query into the same filter shape
        compounds() already reads from the querystring. Never returns search
        results itself -- just the extracted filters, so the client can
        navigate to /compounds?<filters> and let the existing, deterministic
        route/template do the actual (safe, non-LLM) lookup and rendering."""
        if not Config.CLAUDE_API_KEY:
            return jsonify({"error": "Smart search is not configured."}), 503

        data = request.get_json(silent=True) or {}
        query_text = (data.get("query") or "").strip()
        if not query_text:
            return jsonify({"error": "query is required"}), 400
        if len(query_text) > 300:
            return jsonify({"error": "query is too long"}), 400

        client = anthropic.Anthropic(api_key=Config.CLAUDE_API_KEY)

        try:
            response = client.messages.create(
                model=CLAUDE_CHAT_MODEL,
                max_tokens=512,
                system=SMART_SEARCH_SYSTEM_PROMPT,
                tools=[EXTRACT_SEARCH_FILTERS_TOOL],
                tool_choice={"type": "tool", "name": "extract_search_filters"},
                messages=[{"role": "user", "content": query_text}],
            )
            tool_use_block = next(b for b in response.content if b.type == "tool_use")
            extracted = tool_use_block.input or {}
        except anthropic.APIError as e:
            app.logger.error(f"Claude API error in /api/smart-search: {e}")
            return jsonify({"error": "Smart search is temporarily unavailable. Please try again shortly."}), 502

        # Only pass through fields compounds() actually reads, with the same
        # types it expects from a querystring (everything ends up a string).
        filters = {}
        if isinstance(extracted.get("area"), str) and extracted["area"].strip():
            filters["area"] = extracted["area"].strip()
        if isinstance(extracted.get("developer"), str) and extracted["developer"].strip():
            filters["developer"] = extracted["developer"].strip()
        if isinstance(extracted.get("unit_type"), str) and extracted["unit_type"].strip():
            filters["unit_type"] = extracted["unit_type"].strip()
        if isinstance(extracted.get("min_price"), (int, float)):
            filters["min_price"] = str(int(extracted["min_price"]))
        if isinstance(extracted.get("max_price"), (int, float)):
            filters["max_price"] = str(int(extracted["max_price"]))
        if isinstance(extracted.get("bedrooms"), int):
            filters["bedrooms"] = str(extracted["bedrooms"])
        if isinstance(extracted.get("delivery_year"), int):
            filters["delivery_year"] = str(extracted["delivery_year"])

        return jsonify({"filters": filters})

    # ---------- Public read-only API (for external consumers, e.g. Circles) ----------
    #
    # GET-only, no login. Every route below does nothing but SELECT already-
    # public data (is_published compounds / is_available units, the same
    # rows the public site itself shows) -- there is no write path through
    # this section at all, so it can't be used to modify or delete anything
    # regardless of what a caller sends. Rate-limited per IP (see `limiter`
    # near the top of this file) since it's keyless/open to anyone.

    PUBLIC_API_RATE_LIMIT = "60 per minute"

    def _serialize_public_compound(c):
        return {
            "slug": c.slug,
            "name": c.name,
            "developer": c.developer,
            "location": c.location,
            "area": c.area,
            "min_price": float(c.min_price) if c.min_price is not None else None,
            "max_price": float(c.max_price) if c.max_price is not None else None,
            "currency": c.currency,
            "delivery_year": c.delivery_year,
            "cover_image_url": c.cover_image_url,
            "is_launch": bool(c.is_launch),
            "url": url_for("compound_detail", slug=c.slug, _external=True),
        }

    @app.route("/api/public/compounds")
    @limiter.limit(PUBLIC_API_RATE_LIMIT)
    def api_public_compounds():
        try:
            page = max(1, request.args.get("page", 1, type=int) or 1)
        except (TypeError, ValueError):
            page = 1
        per_page = request.args.get("per_page", 50, type=int) or 50
        per_page = max(1, min(per_page, 100))  # hard cap -- no unbounded response size regardless of what's requested

        query = Compound.query.filter_by(is_published=True).order_by(Compound.id.asc())
        total = query.count()
        rows = query.offset((page - 1) * per_page).limit(per_page).all()

        return jsonify({
            "compounds": [_serialize_public_compound(c) for c in rows],
            "page": page,
            "per_page": per_page,
            "total": total,
        })

    @app.route("/api/public/compounds/<slug>/units")
    @limiter.limit(PUBLIC_API_RATE_LIMIT)
    def api_public_compound_units(slug):
        # 404s the same way for "doesn't exist" and "exists but unpublished"
        # -- never confirms an unpublished compound's existence to a caller.
        compound = Compound.query.filter_by(slug=slug, is_published=True).first()
        if not compound:
            return jsonify({"error": "compound not found"}), 404

        units = (
            Unit.query
            .filter_by(compound_id=compound.id, is_available=True)
            .order_by(Unit.price.asc())
            .all()
        )
        return jsonify({
            "compound_slug": compound.slug,
            "units": [
                {
                    "unit_type": u.unit_type,
                    "phase": u.phase,
                    "delivery_year": u.delivery_year,
                    "bedrooms": u.bedrooms,
                    "bathrooms": u.bathrooms,
                    "area_sqm": float(u.area_sqm) if u.area_sqm is not None else None,
                    "price": float(u.price) if u.price is not None else None,
                    "currency": u.currency,
                    "payment_plan": u.payment_plan,
                    "image_url": u.image_url or compound.cover_image_url,
                    "is_launch": bool(u.is_launch),
                }
                for u in units
            ],
        })

    @app.route("/locations")
    def locations():
        # Group by the top-level `location` column (e.g. "New Cairo", "North
        # Coast"). Compounds that don't have `location` set yet fall back to
        # their `area` value, so nothing silently disappears from this page
        # while any stragglers are still being backfilled.
        location_expr = db.func.coalesce(Compound.location, Compound.area)

        rows = (
            db.session.query(location_expr.label("location_name"), db.func.count(Compound.id))
            .filter(Compound.is_published == True, location_expr.isnot(None))
            .group_by("location_name")
            .order_by("location_name")
            .all()
        )

        location_list = []
        for location_name, count in rows:
            sample = (
                Compound.query
                .filter(
                    location_expr == location_name,
                    Compound.is_published == True,
                    Compound.cover_image_url.isnot(None),
                    Compound.cover_image_url != "",
                )
                .order_by(Compound.is_featured.desc(), Compound.created_at.desc())
                .first()
            )
            # Sub-areas within this location (e.g. New Cairo -> Mostakbal City, New Heliopolis)
            sub_area_rows = (
                db.session.query(Compound.area, db.func.count(Compound.id))
                .filter(location_expr == location_name, Compound.is_published == True, Compound.area.isnot(None))
                .group_by(Compound.area)
                .order_by(Compound.area.asc())
                .all()
            )
            location_list.append({
                "name": location_name,
                "count": count,
                "cover_image_url": sample.cover_image_url if sample else None,
                "sub_areas": [{"name": r[0], "count": r[1]} for r in sub_area_rows],
            })

        return render_template("locations.html", locations=location_list)

    @app.route("/compounds")
    def compounds():
        # Filters can be multi-select (areas) or single-value (developer, delivery_year, price range)
        selected_areas = request.args.getlist("area")
        selected_developer = request.args.get("developer", "").strip()
        selected_unit_type = request.args.get("unit_type", "").strip()
        selected_bedrooms = request.args.get("bedrooms", "").strip()
        min_price = request.args.get("min_price", "").strip()
        max_price = request.args.get("max_price", "").strip()
        delivery_year = request.args.get("delivery_year", "").strip()
        search_query = request.args.get("q", "").strip()
        # Display-only: the original free-text smart-search query, carried
        # through purely so the page can show "Results for: '...'" -- it does
        # not participate in filtering itself (the extracted filters already
        # arrived as normal area/developer/... params by the time we get here).
        nl_query = request.args.get("nl_q", "").strip()

        query = Compound.query.filter_by(is_published=True)

        if search_query:
            query = query.filter(Compound.name.ilike(f"%{search_query}%"))

        if selected_areas:
            area_filters = []
            for a in selected_areas:
                if not a:
                    continue
                # Matching against both `area` and `location` means typing a
                # top-level region (e.g. "New Cairo") also returns compounds
                # whose sub-area is "Mostakbal City" or "New Heliopolis",
                # without needing a separate locations page.
                area_filters.append(Compound.area.ilike(f"%{a}%"))
                area_filters.append(Compound.location.ilike(f"%{a}%"))
            if area_filters:
                query = query.filter(db.or_(*area_filters))

        if selected_developer:
            query = query.filter(Compound.developer.ilike(f"%{selected_developer}%"))

        # unit_type and bedrooms both live on Unit, not Compound -- joined
        # (and distinct()'d) once here if either is present, rather than each
        # doing its own .join(), which would join Unit twice and error out.
        if selected_unit_type or selected_bedrooms:
            query = query.join(Unit, Unit.compound_id == Compound.id)
            if selected_unit_type:
                query = query.filter(Unit.unit_type.ilike(f"%{selected_unit_type}%"))
            if selected_bedrooms:
                try:
                    # "At least N bedrooms" -- matches query_properties' chatbot
                    # tool semantics (see QUERY_PROPERTIES_TOOL), not an exact count.
                    query = query.filter(Unit.bedrooms >= int(selected_bedrooms))
                except ValueError:
                    pass
            query = query.distinct()

        if delivery_year:
            query = query.filter(Compound.delivery_year == delivery_year)

        if min_price:
            try:
                query = query.filter(Compound.max_price >= int(min_price))
            except ValueError:
                pass

        if max_price:
            try:
                query = query.filter(Compound.min_price <= int(max_price))
            except ValueError:
                pass

        all_compounds = query.order_by(Compound.name.asc()).all()

        # Options for the filter sidebar
        areas = sorted({row[0] for row in db.session.query(Compound.area).distinct() if row[0]})
        developers = sorted({row[0] for row in db.session.query(Compound.developer).distinct() if row[0]})
        delivery_years = sorted(
            {row[0] for row in db.session.query(Compound.delivery_year).distinct() if row[0]}
        )

        return render_template(
            "compounds.html",
            compounds=all_compounds,
            areas=areas,
            developers=developers,
            delivery_years=delivery_years,
            selected_areas=selected_areas,
            selected_developer=selected_developer,
            selected_unit_type=selected_unit_type,
            selected_bedrooms=selected_bedrooms,
            min_price=min_price,
            max_price=max_price,
            selected_delivery_year=delivery_year,
            search_query=search_query,
            nl_query=nl_query,
        )

    @app.route("/compound/<slug>")
    def compound_detail(slug):
        compound = Compound.query.filter_by(slug=slug, is_published=True).first_or_404()
        return render_template("compound_detail.html", compound=compound)

    @app.route("/developer/<slug>")
    def developer_detail(slug):
        # Compound.developer is free text (no FK to Developer — see models.py),
        # so this resolves the slug against whatever developer names actually
        # appear on published compounds, not against the Developer table
        # (which only exists to optionally attach a logo and doesn't have to
        # have a row for every developer name in use).
        all_names = {
            row[0] for row in
            db.session.query(Compound.developer)
            .filter(Compound.is_published == True, Compound.developer.isnot(None))
            .distinct().all()
            if row[0]
        }
        developer_name = next((n for n in all_names if slugify(n) == slug), None)
        if not developer_name:
            abort(404)

        compounds = (
            Compound.query
            .filter(Compound.developer == developer_name, Compound.is_published == True)
            .order_by(Compound.name.asc())
            .all()
        )
        developer = Developer.query.filter(db.func.lower(Developer.name) == developer_name.lower()).first()

        return render_template(
            "developer_detail.html",
            developer_name=developer_name, developer=developer, compounds=compounds,
        )

    @app.route("/about")
    def about():
        return render_template("about.html")

    @app.route("/sell", methods=["GET", "POST"])
    def sell_property():
        if request.method == "POST":
            name = request.form.get("name", "").strip()
            phone = request.form.get("phone", "").strip()
            email = request.form.get("email", "").strip()
            location = request.form.get("location", "").strip()
            compound_name = request.form.get("compound_name", "").strip()
            property_type = request.form.get("property_type", "").strip()

            message_parts = []
            if property_type:
                message_parts.append(f"Property Type: {property_type}")
            if location:
                message_parts.append(f"Location: {location}")
            if compound_name:
                message_parts.append(f"Compound: {compound_name}")
            message = " | ".join(message_parts)

            lead = Lead(
                name=name,
                phone=phone,
                email=email,
                message=message,
                source_page="sell_property",
            )
            db.session.add(lead)
            db.session.commit()

            # Step 1 of 2: this Lead is a safety net so nothing is lost if the
            # visitor never finishes step 2 — the fields are also carried into
            # the fuller /list-your-property form via the session so they
            # don't have to retype them. Read once and cleared there.
            session["sell_prefill"] = {
                "name": name,
                "phone": phone,
                "email": email,
                "location": location,
                "compound_name": compound_name,
                "property_type": property_type,
            }
            return redirect(url_for("list_property"))

        return render_template("sell.html")

    @app.route("/contact", methods=["GET", "POST"])
    def contact():
        if request.method == "POST":
            lead = Lead(
                name=request.form.get("name", "").strip(),
                phone=request.form.get("phone", "").strip(),
                email=request.form.get("email", "").strip(),
                message=request.form.get("message", "").strip(),
                source_page="contact_page",
            )
            db.session.add(lead)
            db.session.commit()
            flash("Thanks for reaching out! Our team at Meleven will contact you shortly.", "success")
            return redirect(url_for("contact"))

        return render_template("contact.html")

    @app.route("/compound/<slug>/interested", methods=["POST"])
    def compound_interested(slug):
        compound = Compound.query.filter_by(slug=slug).first_or_404()
        lead = Lead(
            name=request.form.get("name", "").strip(),
            phone=request.form.get("phone", "").strip(),
            email=request.form.get("email", "").strip(),
            message=request.form.get("message", "").strip(),
            compound_id=compound.id,
            source_page=f"compound:{slug}",
        )
        db.session.add(lead)
        db.session.commit()
        flash("Thanks for your interest! Our team at Meleven will contact you shortly about this project.", "success")
        return redirect(url_for("compound_detail", slug=slug))

    # ---------- Resale/Rent listings (public) ----------
    # Entirely separate from the Compound/Unit developer-sale inventory —
    # these are individually-owned units submitted by their owner or an
    # agent. Every submission lands as status="pending" and is invisible on
    # every public route below until an admin approves it under /admin.

    ALLOWED_LISTING_TYPES = {"resale", "rent_annual", "rent_seasonal"}

    @app.route("/list-your-property", methods=["GET", "POST"])
    def list_property():
        if request.method == "POST":
            listing_type = request.form.get("listing_type", "").strip()
            if listing_type not in ALLOWED_LISTING_TYPES:
                flash("Please choose a valid listing type.", "error")
                return redirect(url_for("list_property"))

            title = request.form.get("title", "").strip()
            owner_name = request.form.get("owner_name", "").strip()
            owner_phone = request.form.get("owner_phone", "").strip()
            if not title or not owner_name or not owner_phone:
                flash("Title, your name, and phone number are required.", "error")
                return redirect(url_for("list_property"))

            image_url = request.form.get("image_url", "").strip()
            uploaded_name = save_uploaded_image(request.files.get("image_file"), app.config["UPLOAD_FOLDER"])
            if uploaded_name:
                image_url = url_for("uploaded_file", filename=uploaded_name)

            listing = Listing(
                listing_type=listing_type,
                status="pending",
                compound_id=request.form.get("compound_id") or None,
                title=title,
                area=request.form.get("area", "").strip(),
                location=request.form.get("location", "").strip(),
                unit_type=request.form.get("unit_type", "").strip(),
                bedrooms=request.form.get("bedrooms") or None,
                bathrooms=request.form.get("bathrooms") or None,
                area_sqm=request.form.get("area_sqm") or None,
                price=request.form.get("price") or None,
                rent_amount=request.form.get("rent_amount") or None,
                rent_cadence=request.form.get("rent_cadence", "").strip(),
                price_per_week=request.form.get("price_per_week") or None,
                high_season_multiplier=request.form.get("high_season_multiplier") or None,
                furnishing=request.form.get("furnishing", "").strip(),
                condition=request.form.get("condition", "").strip(),
                legal_status=request.form.get("legal_status", "").strip(),
                seller_type=request.form.get("seller_type", "").strip(),
                negotiable=bool(request.form.get("negotiable")),
                owner_name=owner_name,
                owner_phone=owner_phone,
                owner_email=request.form.get("owner_email", "").strip(),
                image_url=image_url,
            )
            db.session.add(listing)
            db.session.commit()
            flash("Thanks! Your listing has been submitted and is pending review — we'll publish it once approved.", "success")
            return redirect(url_for("list_property"))

        # Step 2 of 2 when arriving from /sell: read the step-1 fields once
        # and clear them immediately, so a stale value never lingers into an
        # unrelated later visit. Absent entirely for anyone who lands here
        # directly (e.g. the footer link) — the template only shows the
        # "step 2" framing when this is actually set.
        prefill = session.pop("sell_prefill", None)
        prefilled_compound_id = None
        suggested_title = ""
        if prefill and prefill.get("compound_name"):
            match = Compound.query.filter(
                db.func.lower(Compound.name) == prefill["compound_name"].strip().lower(),
                Compound.is_published == True,
            ).first()
            if match:
                prefilled_compound_id = match.id
            else:
                # No matching compound on file — don't lose what they typed,
                # just carry it forward as a starting point for the title.
                suggested_title = prefill["compound_name"]

        compounds_for_link = Compound.query.filter_by(is_published=True).order_by(Compound.name.asc()).all()
        return render_template(
            "list_your_property.html",
            compounds_for_link=compounds_for_link,
            prefill=prefill,
            prefilled_compound_id=prefilled_compound_id,
            suggested_title=suggested_title,
        )

    def _listings_browse(listing_types, page_title, endpoint_name):
        """Shared query + render for /resale and /rent — only status="approved"
        listings of the given type(s) are ever visible here."""
        area = request.args.get("area", "").strip()
        unit_type = request.args.get("unit_type", "").strip()

        base_filter = db.and_(Listing.status == "approved", Listing.listing_type.in_(listing_types))

        query = Listing.query.filter(base_filter)
        if area:
            query = query.filter(db.or_(Listing.area.ilike(f"%{area}%"), Listing.location.ilike(f"%{area}%")))
        if unit_type:
            query = query.filter(Listing.unit_type.ilike(f"%{unit_type}%"))
        all_listings = query.order_by(Listing.submitted_at.desc()).all()

        areas = sorted({
            row[0] for row in
            db.session.query(Listing.area)
            .filter(base_filter, Listing.area.isnot(None), Listing.area != "")
            .distinct().all()
            if row[0]
        })
        unit_types = sorted({
            row[0] for row in
            db.session.query(Listing.unit_type)
            .filter(base_filter, Listing.unit_type.isnot(None), Listing.unit_type != "")
            .distinct().all()
            if row[0]
        })

        return render_template(
            "listings.html",
            listings=all_listings,
            page_title=page_title,
            areas=areas,
            unit_types=unit_types,
            selected_area=area,
            selected_unit_type=unit_type,
            endpoint_name=endpoint_name,
        )

    @app.route("/resale")
    def resale_listings():
        return _listings_browse(("resale",), "Resale Properties", "resale_listings")

    @app.route("/rent")
    def rent_listings():
        return _listings_browse(("rent_annual", "rent_seasonal"), "Properties for Rent", "rent_listings")

    @app.route("/listing/<int:listing_id>")
    def listing_detail(listing_id):
        listing = Listing.query.filter_by(id=listing_id, status="approved").first_or_404()
        return render_template("listing_detail.html", listing=listing)

    @app.route("/listing/<int:listing_id>/interested", methods=["POST"])
    def listing_interested(listing_id):
        listing = Listing.query.filter_by(id=listing_id, status="approved").first_or_404()
        lead = Lead(
            name=request.form.get("name", "").strip(),
            phone=request.form.get("phone", "").strip(),
            email=request.form.get("email", "").strip(),
            message=request.form.get("message", "").strip(),
            listing_id=listing.id,
            source_page=f"listing:{listing.id}",
        )
        db.session.add(lead)
        db.session.commit()
        flash("Thanks for your interest! Our team at Meleven will contact you shortly about this property.", "success")
        return redirect(url_for("listing_detail", listing_id=listing.id))

    # ---------- Admin auth ----------

    def login_required(f):
        @wraps(f)
        def wrapper(*args, **kwargs):
            if not session.get("admin_logged_in"):
                return redirect(url_for("admin_login", next=request.path))
            return f(*args, **kwargs)
        return wrapper

    @app.route("/admin/login", methods=["GET", "POST"])
    def admin_login():
        if request.method == "POST":
            password = request.form.get("password", "")
            if password == app.config["ADMIN_PASSWORD"]:
                session["admin_logged_in"] = True
                next_url = request.args.get("next") or url_for("admin_dashboard")
                return redirect(next_url)
            flash("Incorrect password.", "error")
        return render_template("admin/login.html")

    @app.route("/admin/logout")
    def admin_logout():
        session.pop("admin_logged_in", None)
        return redirect(url_for("admin_login"))

    # ---------- Admin: compounds ----------

    @app.route("/admin")
    @login_required
    def admin_dashboard():
        all_compounds = Compound.query.order_by(Compound.created_at.desc()).all()
        return render_template("admin/dashboard.html", compounds=all_compounds)

    @app.route("/admin/leads")
    @login_required
    def admin_leads():
        source_filter = request.args.get("source", "all")

        query = Lead.query
        if source_filter == "sell":
            query = query.filter(Lead.source_page == "sell_property")
        elif source_filter == "other":
            query = query.filter(Lead.source_page != "sell_property")
        # "all" -> no filter

        all_leads = query.order_by(Lead.created_at.desc()).all()

        sell_count = Lead.query.filter(Lead.source_page == "sell_property").count()
        other_count = Lead.query.filter(Lead.source_page != "sell_property").count()

        return render_template(
            "admin/leads.html",
            leads=all_leads,
            source_filter=source_filter,
            sell_count=sell_count,
            other_count=other_count,
        )

    @app.route("/admin/leads/<int:lead_id>/delete", methods=["POST"])
    @login_required
    def admin_lead_delete(lead_id):
        l = Lead.query.get_or_404(lead_id)
        db.session.delete(l)
        db.session.commit()
        flash("Lead deleted.", "success")
        return redirect(url_for("admin_leads"))

    @app.route("/admin/compounds/new", methods=["GET", "POST"])
    @login_required
    def admin_compound_new():
        if request.method == "POST":
            name = request.form.get("name", "").strip()
            slug = request.form.get("slug", "").strip() or slugify(name)

            # ensure slug uniqueness
            base_slug, n = slug, 1
            while Compound.query.filter_by(slug=slug).first():
                n += 1
                slug = f"{base_slug}-{n}"

            cover_image_url = request.form.get("cover_image_url", "").strip()
            uploaded_name = save_uploaded_image(request.files.get("cover_image_file"), app.config["UPLOAD_FOLDER"])
            if uploaded_name:
                cover_image_url = url_for("uploaded_file", filename=uploaded_name)

            c = Compound(
                name=name,
                slug=slug,
                developer=request.form.get("developer", "").strip(),
                location=request.form.get("location", "").strip(),
                area=request.form.get("area", "").strip(),
                location_detail=request.form.get("location_detail", "").strip(),
                short_description=request.form.get("short_description", "").strip(),
                full_description=request.form.get("full_description", "").strip(),
                min_price=request.form.get("min_price") or None,
                max_price=request.form.get("max_price") or None,
                land_area_acres=request.form.get("land_area_acres") or None,
                delivery_year=request.form.get("delivery_year") or None,
                cover_image_url=cover_image_url,
                contact_phone=request.form.get("contact_phone", "").strip(),
                contact_whatsapp=request.form.get("contact_whatsapp", "").strip(),
                is_featured=bool(request.form.get("is_featured")),
                is_launch=bool(request.form.get("is_launch")),
                is_published=bool(request.form.get("is_published")),
            )
            db.session.add(c)
            db.session.commit()
            flash("Compound created.", "success")
            return redirect(url_for("admin_dashboard"))

        return render_template("admin/compound_form.html", compound=None)

    @app.route("/admin/compounds/<int:compound_id>/edit", methods=["GET", "POST"])
    @login_required
    def admin_compound_edit(compound_id):
        c = Compound.query.get_or_404(compound_id)
        if request.method == "POST":
            c.name = request.form.get("name", "").strip()

            # Keep slug in sync when it's explicitly provided; otherwise leave the
            # existing slug untouched so published links never break silently.
            new_slug = request.form.get("slug", "").strip()
            if new_slug and new_slug != c.slug:
                base_slug, n = new_slug, 1
                candidate = new_slug
                while Compound.query.filter(Compound.slug == candidate, Compound.id != c.id).first():
                    n += 1
                    candidate = f"{base_slug}-{n}"
                c.slug = candidate

            c.developer = request.form.get("developer", "").strip()
            c.location = request.form.get("location", "").strip()
            c.area = request.form.get("area", "").strip()
            c.location_detail = request.form.get("location_detail", "").strip()
            c.short_description = request.form.get("short_description", "").strip()
            c.full_description = request.form.get("full_description", "").strip()
            c.min_price = request.form.get("min_price") or None
            c.max_price = request.form.get("max_price") or None
            c.land_area_acres = request.form.get("land_area_acres") or None
            c.delivery_year = request.form.get("delivery_year") or None

            cover_image_url = request.form.get("cover_image_url", "").strip()
            uploaded_name = save_uploaded_image(request.files.get("cover_image_file"), app.config["UPLOAD_FOLDER"])
            if uploaded_name:
                cover_image_url = url_for("uploaded_file", filename=uploaded_name)
            c.cover_image_url = cover_image_url

            c.contact_phone = request.form.get("contact_phone", "").strip()
            c.contact_whatsapp = request.form.get("contact_whatsapp", "").strip()
            c.is_featured = bool(request.form.get("is_featured"))
            c.is_launch = bool(request.form.get("is_launch"))
            c.is_published = bool(request.form.get("is_published"))
            db.session.commit()
            flash("Compound updated.", "success")
            return redirect(url_for("admin_dashboard"))

        return render_template("admin/compound_form.html", compound=c)

    @app.route("/admin/compounds/<int:compound_id>/delete", methods=["POST"])
    @login_required
    def admin_compound_delete(compound_id):
        c = Compound.query.get_or_404(compound_id)
        db.session.delete(c)
        db.session.commit()
        flash("Compound deleted.", "success")
        return redirect(url_for("admin_dashboard"))

    # ---------- Admin: developer logos ----------

    @app.route("/admin/developers")
    @login_required
    def admin_developers():
        all_developers = Developer.query.order_by(Developer.name.asc()).all()
        return render_template("admin/developers.html", developers=all_developers)

    @app.route("/admin/developers/new", methods=["GET", "POST"])
    @login_required
    def admin_developer_new():
        if request.method == "POST":
            name = request.form.get("name", "").strip()
            if not name:
                flash("Developer name is required.", "error")
                return redirect(url_for("admin_developer_new"))
            if Developer.query.filter(db.func.lower(Developer.name) == name.lower()).first():
                flash(f"A developer named '{name}' already exists.", "error")
                return redirect(url_for("admin_developer_new"))

            logo_url = request.form.get("logo_url", "").strip()
            uploaded_name = save_uploaded_image(request.files.get("logo_file"), app.config["UPLOAD_FOLDER"])
            if uploaded_name:
                logo_url = url_for("uploaded_file", filename=uploaded_name)

            d = Developer(name=name, logo_url=logo_url)
            db.session.add(d)
            db.session.commit()
            flash("Developer added.", "success")
            return redirect(url_for("admin_developers"))

        return render_template("admin/developer_form.html", developer=None)

    @app.route("/admin/developers/<int:developer_id>/edit", methods=["GET", "POST"])
    @login_required
    def admin_developer_edit(developer_id):
        d = Developer.query.get_or_404(developer_id)
        if request.method == "POST":
            new_name = request.form.get("name", "").strip()
            if not new_name:
                flash("Developer name is required.", "error")
                return redirect(url_for("admin_developer_edit", developer_id=d.id))
            duplicate = Developer.query.filter(
                db.func.lower(Developer.name) == new_name.lower(), Developer.id != d.id
            ).first()
            if duplicate:
                flash(f"A developer named '{new_name}' already exists.", "error")
                return redirect(url_for("admin_developer_edit", developer_id=d.id))
            d.name = new_name

            logo_url = request.form.get("logo_url", "").strip()
            uploaded_name = save_uploaded_image(request.files.get("logo_file"), app.config["UPLOAD_FOLDER"])
            if uploaded_name:
                logo_url = url_for("uploaded_file", filename=uploaded_name)
            d.logo_url = logo_url

            db.session.commit()
            flash("Developer updated.", "success")
            return redirect(url_for("admin_developers"))

        return render_template("admin/developer_form.html", developer=d)

    @app.route("/admin/developers/<int:developer_id>/delete", methods=["POST"])
    @login_required
    def admin_developer_delete(developer_id):
        d = Developer.query.get_or_404(developer_id)
        db.session.delete(d)
        db.session.commit()
        flash("Developer deleted.", "success")
        return redirect(url_for("admin_developers"))

    # ---------- Admin: units ----------

    @app.route("/admin/compounds/<int:compound_id>/units", methods=["GET", "POST"])
    @login_required
    def admin_units(compound_id):
        c = Compound.query.get_or_404(compound_id)
        if request.method == "POST":
            image_url = request.form.get("image_url", "").strip()
            uploaded_name = save_uploaded_image(request.files.get("image_file"), app.config["UPLOAD_FOLDER"])
            if uploaded_name:
                image_url = url_for("uploaded_file", filename=uploaded_name)

            u = Unit(
                compound_id=c.id,
                unit_type=request.form.get("unit_type", "").strip(),
                phase=request.form.get("phase", "").strip(),
                delivery_year=request.form.get("delivery_year") or None,
                bedrooms=request.form.get("bedrooms") or None,
                bathrooms=request.form.get("bathrooms") or None,
                area_sqm=request.form.get("area_sqm") or None,
                price=request.form.get("price") or None,
                payment_plan=request.form.get("payment_plan", "").strip(),
                image_url=image_url,
                is_available=bool(request.form.get("is_available")),
                is_launch=bool(request.form.get("is_launch")),
            )
            db.session.add(u)
            db.session.commit()
            flash("Unit added.", "success")
            return redirect(url_for("admin_units", compound_id=c.id))

        return render_template("admin/units.html", compound=c)

    @app.route("/admin/units/<int:unit_id>/edit", methods=["GET", "POST"])
    @login_required
    def admin_unit_edit(unit_id):
        u = Unit.query.get_or_404(unit_id)
        if request.method == "POST":
            u.unit_type = request.form.get("unit_type", "").strip()
            u.phase = request.form.get("phase", "").strip()
            u.delivery_year = request.form.get("delivery_year") or None
            u.bedrooms = request.form.get("bedrooms") or None
            u.bathrooms = request.form.get("bathrooms") or None
            u.area_sqm = request.form.get("area_sqm") or None
            u.price = request.form.get("price") or None
            u.payment_plan = request.form.get("payment_plan", "").strip()

            image_url = request.form.get("image_url", "").strip()
            uploaded_name = save_uploaded_image(request.files.get("image_file"), app.config["UPLOAD_FOLDER"])
            if uploaded_name:
                image_url = url_for("uploaded_file", filename=uploaded_name)
            u.image_url = image_url

            u.is_available = bool(request.form.get("is_available"))
            u.is_launch = bool(request.form.get("is_launch"))
            db.session.commit()
            flash("Unit updated.", "success")
            return redirect(url_for("admin_units", compound_id=u.compound_id))

        return render_template("admin/unit_form.html", unit=u)

    @app.route("/admin/units/<int:unit_id>/delete", methods=["POST"])
    @login_required
    def admin_unit_delete(unit_id):
        u = Unit.query.get_or_404(unit_id)
        compound_id = u.compound_id
        db.session.delete(u)
        db.session.commit()
        flash("Unit deleted.", "success")
        return redirect(url_for("admin_units", compound_id=compound_id))

    # ---------- Admin: bulk import ----------

    def parse_bool(value):
        return str(value).strip().lower() in ("1", "true", "yes", "y")

    @app.route("/admin/import", methods=["GET"])
    @login_required
    def admin_import():
        return render_template("admin/import.html")

    @app.route("/admin/compounds/import", methods=["POST"])
    @login_required
    def admin_compounds_import():
        file = request.files.get("csv_file")
        if not file or file.filename == "":
            flash("Please choose a CSV file.", "error")
            return redirect(url_for("admin_import"))

        stream = io.StringIO(file.stream.read().decode("utf-8-sig"))
        reader = csv.DictReader(stream)

        created, skipped = 0, 0
        for row in reader:
            name = (row.get("name") or "").strip()
            if not name:
                skipped += 1
                continue

            slug = (row.get("slug") or "").strip() or slugify(name)
            base_slug, n = slug, 1
            while Compound.query.filter_by(slug=slug).first():
                n += 1
                slug = f"{base_slug}-{n}"

            c = Compound(
                name=name,
                slug=slug,
                developer=(row.get("developer") or "").strip(),
                location=(row.get("location") or "").strip(),
                area=(row.get("area") or "").strip(),
                location_detail=(row.get("location_detail") or "").strip(),
                short_description=(row.get("short_description") or "").strip(),
                full_description=(row.get("full_description") or "").strip(),
                min_price=row.get("min_price") or None,
                max_price=row.get("max_price") or None,
                land_area_acres=row.get("land_area_acres") or None,
                delivery_year=row.get("delivery_year") or None,
                cover_image_url=(row.get("cover_image_url") or "").strip(),
                contact_phone=(row.get("contact_phone") or "").strip(),
                contact_whatsapp=(row.get("contact_whatsapp") or "").strip(),
                is_featured=parse_bool(row.get("is_featured")),
                is_launch=parse_bool(row.get("is_launch")),
                is_published=parse_bool(row.get("is_published", "true")),
            )
            db.session.add(c)

            # Print the resolved slug back to the admin so bulk-imported
            # compounds are easy to match up with a units CSV afterwards.
            print(f"[compounds import] '{name}' -> slug='{slug}'")

            created += 1

        db.session.commit()
        flash(f"Imported {created} compound(s). Skipped {skipped} row(s) without a name.", "success")
        return redirect(url_for("admin_dashboard"))

    @app.route("/admin/units/import", methods=["POST"])
    @login_required
    def admin_units_import():
        file = request.files.get("csv_file")
        if not file or file.filename == "":
            flash("Please choose a CSV file.", "error")
            return redirect(url_for("admin_import"))

        stream = io.StringIO(file.stream.read().decode("utf-8-sig"))
        reader = csv.DictReader(stream)

        created, skipped, unknown_slugs = 0, 0, []
        for row in reader:
            compound_slug = (row.get("compound_slug") or "").strip()
            compound = Compound.query.filter_by(slug=compound_slug).first()
            if not compound:
                skipped += 1
                if compound_slug not in unknown_slugs:
                    unknown_slugs.append(compound_slug)
                continue

            u = Unit(
                compound_id=compound.id,
                unit_type=(row.get("unit_type") or "").strip(),
                phase=(row.get("phase") or "").strip(),
                delivery_year=row.get("delivery_year") or None,
                bedrooms=row.get("bedrooms") or None,
                bathrooms=row.get("bathrooms") or None,
                area_sqm=row.get("area_sqm") or None,
                price=row.get("price") or None,
                payment_plan=(row.get("payment_plan") or "").strip(),
                image_url=(row.get("image_url") or "").strip(),
                is_available=parse_bool(row.get("is_available", "true")),
                is_launch=parse_bool(row.get("is_launch")),
            )
            db.session.add(u)
            created += 1

        db.session.commit()

        message = f"Imported {created} unit(s). Skipped {skipped} row(s) with unknown compound_slug."
        if unknown_slugs:
            # Surface exactly which slugs didn't match, instead of failing silently.
            message += " Unknown slugs: " + ", ".join(unknown_slugs[:10])
            if len(unknown_slugs) > 10:
                message += f" (+{len(unknown_slugs) - 10} more)"
        flash(message, "success" if created else "error")
        return redirect(url_for("admin_dashboard"))

    # ---------- Admin: Resale/Rent listings ----------

    ALLOWED_LISTING_STATUS_FILTERS = {"pending", "approved", "rejected", "all"}

    @app.route("/admin/listings")
    @login_required
    def admin_listings():
        status_filter = request.args.get("status", "pending")
        if status_filter not in ALLOWED_LISTING_STATUS_FILTERS:
            status_filter = "pending"

        query = Listing.query
        if status_filter != "all":
            query = query.filter(Listing.status == status_filter)
        all_listings = query.order_by(Listing.submitted_at.desc()).all()

        pending_count = Listing.query.filter(Listing.status == "pending").count()
        approved_count = Listing.query.filter(Listing.status == "approved").count()
        rejected_count = Listing.query.filter(Listing.status == "rejected").count()

        return render_template(
            "admin/listings.html",
            listings=all_listings,
            status_filter=status_filter,
            pending_count=pending_count,
            approved_count=approved_count,
            rejected_count=rejected_count,
        )

    @app.route("/admin/listings/<int:listing_id>")
    @login_required
    def admin_listing_review(listing_id):
        listing = Listing.query.get_or_404(listing_id)
        return render_template("admin/listing_review.html", listing=listing)

    @app.route("/admin/listings/<int:listing_id>/approve", methods=["POST"])
    @login_required
    def admin_listing_approve(listing_id):
        listing = Listing.query.get_or_404(listing_id)
        listing.status = "approved"
        listing.reviewed_at = datetime.utcnow()
        listing.reviewed_by = request.form.get("reviewed_by", "").strip() or "Admin"
        db.session.commit()
        flash(f"'{listing.title}' approved and now live.", "success")
        return redirect(url_for("admin_listings"))

    @app.route("/admin/listings/<int:listing_id>/reject", methods=["POST"])
    @login_required
    def admin_listing_reject(listing_id):
        listing = Listing.query.get_or_404(listing_id)
        listing.status = "rejected"
        listing.reviewed_at = datetime.utcnow()
        listing.reviewed_by = request.form.get("reviewed_by", "").strip() or "Admin"
        db.session.commit()
        flash(f"'{listing.title}' rejected.", "success")
        return redirect(url_for("admin_listings"))

    @app.route("/admin/listings/<int:listing_id>/edit", methods=["GET", "POST"])
    @login_required
    def admin_listing_edit(listing_id):
        listing = Listing.query.get_or_404(listing_id)
        if request.method == "POST":
            listing_type = request.form.get("listing_type", "").strip()
            if listing_type in ALLOWED_LISTING_TYPES:
                listing.listing_type = listing_type

            listing.compound_id = request.form.get("compound_id") or None
            listing.title = request.form.get("title", "").strip()
            listing.area = request.form.get("area", "").strip()
            listing.location = request.form.get("location", "").strip()
            listing.unit_type = request.form.get("unit_type", "").strip()
            listing.bedrooms = request.form.get("bedrooms") or None
            listing.bathrooms = request.form.get("bathrooms") or None
            listing.area_sqm = request.form.get("area_sqm") or None
            listing.price = request.form.get("price") or None
            listing.rent_amount = request.form.get("rent_amount") or None
            listing.rent_cadence = request.form.get("rent_cadence", "").strip()
            listing.price_per_week = request.form.get("price_per_week") or None
            listing.high_season_multiplier = request.form.get("high_season_multiplier") or None
            listing.furnishing = request.form.get("furnishing", "").strip()
            listing.condition = request.form.get("condition", "").strip()
            listing.legal_status = request.form.get("legal_status", "").strip()
            listing.seller_type = request.form.get("seller_type", "").strip()
            listing.negotiable = bool(request.form.get("negotiable"))
            listing.owner_name = request.form.get("owner_name", "").strip()
            listing.owner_phone = request.form.get("owner_phone", "").strip()

            image_url = request.form.get("image_url", "").strip()
            uploaded_name = save_uploaded_image(request.files.get("image_file"), app.config["UPLOAD_FOLDER"])
            if uploaded_name:
                image_url = url_for("uploaded_file", filename=uploaded_name)
            listing.image_url = image_url

            db.session.commit()
            flash("Listing updated.", "success")
            return redirect(url_for("admin_listing_review", listing_id=listing.id))

        compounds_for_link = Compound.query.filter_by(is_published=True).order_by(Compound.name.asc()).all()
        return render_template("admin/listing_edit.html", listing=listing, compounds_for_link=compounds_for_link)

    @app.route("/admin/listings/<int:listing_id>/delete", methods=["POST"])
    @login_required
    def admin_listing_delete(listing_id):
        listing = Listing.query.get_or_404(listing_id)
        db.session.delete(listing)
        db.session.commit()
        flash("Listing deleted.", "success")
        return redirect(url_for("admin_listings"))

    return app


app = create_app()

if __name__ == "__main__":
    app.run(debug=True)
