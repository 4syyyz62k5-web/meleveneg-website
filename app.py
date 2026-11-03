import re
import csv
import warnings
import io
import json
import os
from datetime import datetime, timezone
import uuid
import xml.etree.ElementTree as ET
from datetime import date, datetime
from functools import wraps
from flask import Flask, render_template, request, redirect, url_for, flash, session, send_from_directory, jsonify, Response, abort, current_app
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
from flask import Request as FlaskRequest
from flask_wtf.csrf import CSRFProtect, CSRFError
from itsdangerous import BadSignature, SignatureExpired, URLSafeTimedSerializer
from werkzeug.middleware.proxy_fix import ProxyFix
from PIL import Image, ImageOps
from pillow_heif import register_heif_opener

# iPhones upload HEIC. Without this Pillow cannot open one at all, and the
# upload fails with something unhelpful about an unidentified file.
register_heif_opener()

from config import Config
from models import db, Compound, Unit, Lead, Developer, Listing, ListingImage

# Pillow's own default is ~89 MP and only warns. Both are wrong here: no phone
# produces 40 MP, and a warning is not a defence. A 100 KB PNG can describe a
# 2 GB bitmap, and the cost lands on decode -- after the upload limit has
# already been satisfied -- so this is the only place it can be stopped.
# Set once at import, because it is a module-level Pillow setting rather than
# anything per-request.
Image.MAX_IMAGE_PIXELS = Config.IMAGE_MAX_PIXELS
warnings.simplefilter("error", Image.DecompressionBombWarning)

# Every endpoint that accepts a file, and therefore the only ones allowed a
# large request body. Taken from a grep of request.files in this file, not from
# memory: the public listing form, seven admin image forms, and three CSV
# imports. Miss one and that form starts refusing ordinary uploads with a 413.
# Verified against app.url_map rather than written from memory: three of these
# were wrong on the first attempt ("admin_compounds_new" for
# "admin_compound_new", and so on), and a name that matches no route silently
# leaves that form on the small limit -- so an ordinary admin photo upload
# would start failing with a 413 and nothing would say why. There is a test
# asserting every name here resolves.
# Open to anyone, so held to the smaller of the two upload ceilings.
VISITOR_UPLOAD_ENDPOINTS = frozenset({"list_property"})

UPLOAD_ENDPOINTS = frozenset({
    "list_property",
    "admin_listing_new", "admin_listing_edit",
    "admin_compound_new", "admin_compound_edit",
    "admin_developer_new", "admin_developer_edit",
    "admin_units", "admin_unit_edit",
    "admin_compounds_import", "admin_units_import", "admin_listings_import",
})


class RouteAwareRequest(FlaskRequest):
    """Applies the big body limit on upload endpoints and the small one anywhere else.

    Flask reads MAX_CONTENT_LENGTH through this property, and Werkzeug consults
    it when the form data is first parsed -- which happens inside the view, by
    which point url_rule is set. So the endpoint is known in time.

    The alternative was one site-wide ceiling, and neither value works for
    both: 8 MB made six phone photos impossible, and 48 MB would have let every
    other form on the site accept 48 MB of anything.
    """

    @property
    def max_content_length(self):
        endpoint = self.url_rule.endpoint if self.url_rule else None
        if endpoint in VISITOR_UPLOAD_ENDPOINTS:
            key = "MAX_VISITOR_UPLOAD_CONTENT_LENGTH"
        elif endpoint in UPLOAD_ENDPOINTS:
            key = "MAX_UPLOAD_CONTENT_LENGTH"
        else:
            key = "MAX_CONTENT_LENGTH"
        return current_app.config.get(key)


def client_ip():
    """The visitor's own address, for rate-limiting purposes.

    get_remote_address() returns request.remote_addr, which behind Cloudflare
    and Render is a PROXY. Keying limits on it does not count per visitor at
    all -- it lumps the whole internet into a handful of buckets, so the
    spammer sending one request every few days never touches a limit while
    three unrelated people submitting in the same minute lock out the fourth.
    Cloudflare puts the real client in CF-Connecting-IP and rewrites it on
    every request, so a visitor cannot forge it.

    THE HEADER IS ONLY BELIEVED ON A CLOUDFLARE-FRONTED HOST, and that guard
    is the whole point of this function. A Render service also answers on its
    own <service>.onrender.com name, which Cloudflare never sees. A request
    sent straight there can carry any CF-Connecting-IP it likes and get a
    fresh, empty rate-limit bucket for every single request -- which would
    make every limit below decorative. So the header counts only when the Host
    is one we know sits behind Cloudflare (Config.TRUSTED_CF_HOSTS); on any
    other host we fall back to the socket address, which cannot be chosen by
    the caller. If the site ever moves domain, that list has to move with it.
    """
    host = (request.host or "").split(":")[0].lower()
    if host in current_app.config.get("TRUSTED_CF_HOSTS", set()):
        cf = request.headers.get("CF-Connecting-IP", "").strip()
        if cf:
            return cf
        # Behind Cloudflare, XFF's leftmost entry is the client. Only read it
        # on a trusted host, for exactly the reason above.
        xff = request.headers.get("X-Forwarded-For", "")
        if xff:
            first = xff.split(",")[0].strip()
            if first:
                return first
    return get_remote_address()


# In-memory storage, per-process. With a single gunicorn worker that is one
# shared budget, which is what the numbers in Config assume. Every extra
# worker multiplies every limit, because each process counts on its own --
# so if Render is running more than one, either the worker count comes down
# to 1 or this needs a shared backend (Redis) to mean anything.
limiter = Limiter(key_func=client_ip)

# Flask-WTF was in requirements.txt from the start and never wired in, so
# every POST in this app -- the admin login, every admin create/edit/delete,
# and the public lead and listing forms -- accepted a cross-site submission.
# CSRFProtect covers all of them at once; each form then needs its hidden
# token, and the three fetch() callers send it as an X-CSRFToken header
# (Flask-WTF reads that header by default), so none of them is exempted.
csrf = CSRFProtect()

ALLOWED_IMAGE_EXTENSIONS = {"png", "jpg", "jpeg", "webp", "gif", "heic", "heif"}

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


# First bytes that actually identify each format we accept. Checked instead of
# trusting the filename: /list-your-property takes a file from anyone with no
# login, and an extension is just a string the uploader chose -- renaming
# payload.html to photo.jpg passed allowed_image() and landed on the Render
# disk, from where /uploads/<name> would serve it back.
#
# imghdr would have done this, but it is deprecated in 3.11 and gone in 3.13;
# these four signatures are stable and need no dependency.
IMAGE_SIGNATURES = (
    (b"\x89PNG\r\n\x1a\n", "png"),
    (b"\xff\xd8\xff", "jpg"),
    (b"GIF87a", "gif"),
    (b"GIF89a", "gif"),
)


# HEIC/HEIF is ISO base media format: a 'ftyp' box at offset 4, then a brand.
# Listed explicitly rather than matched loosely, because the same container
# shape also carries MP4 video.
HEIF_BRANDS = {b"heic", b"heix", b"heim", b"heis", b"hevc", b"hevm", b"hevs",
               b"mif1", b"msf1"}


def sniff_image_type(stream):
    """The real format of an uploaded file, or None. Leaves the stream rewound.

    WEBP is matched separately because its signature is split: 'RIFF', then a
    four-byte length, then 'WEBP'. HEIC likewise: a 'ftyp' box and a brand.
    """
    head = stream.read(32)
    stream.seek(0)
    for signature, kind in IMAGE_SIGNATURES:
        if head.startswith(signature):
            return kind
    if head[:4] == b"RIFF" and head[8:12] == b"WEBP":
        return "webp"
    if head[4:8] == b"ftyp" and head[8:12] in HEIF_BRANDS:
        return "heic"
    return None


# jpg/jpeg name the same format; every other extension must match the sniffed
# type exactly, so a real PNG called .webp is refused rather than quietly
# stored under a lying name.
EQUIVALENT_IMAGE_TYPES = {"jpg": {"jpg", "jpeg"}, "jpeg": {"jpg", "jpeg"},
                          "heic": {"heic", "heif"}, "heif": {"heic", "heif"}}


def save_uploaded_image(file_storage, upload_folder):
    """Saves an uploaded image under a uuid4 name. Returns (filename, error).

    filename is None when nothing was uploaded (error None too) or when the
    file was rejected (error says why, for the caller to flash). Returning the
    reason matters on the public form: silently dropping the image left the
    submitter believing their photo went through.
    """
    if not file_storage or file_storage.filename == "":
        return None, None
    if not allowed_image(file_storage.filename):
        return None, ("Images must be one of: "
                      + ", ".join(sorted(ALLOWED_IMAGE_EXTENSIONS)) + ".")

    ext = secure_filename(file_storage.filename).rsplit(".", 1)[1].lower()
    actual = sniff_image_type(file_storage.stream)
    if actual is None:
        return None, "That file is not an image, whatever its name says."
    if actual not in EQUIVALENT_IMAGE_TYPES.get(ext, {ext}):
        return None, (f"That file is a {actual.upper()} but is named .{ext}. "
                      f"Rename it to match and try again.")

    os.makedirs(upload_folder, exist_ok=True)
    # Stored under the SNIFFED type, not the claimed extension -- the bytes
    # are the thing being served later, so they decide the name.
    unique_name = f"{uuid.uuid4().hex}.{actual}"
    file_storage.save(os.path.join(upload_folder, unique_name))
    return unique_name, None


def process_image(raw, upload_folder):
    """Normalises one uploaded image and writes it. Returns (filename, error).

    The order of these steps is load-bearing:

      verify()          decodes enough to prove the bytes are really an image,
                        which magic-byte sniffing cannot. It CONSUMES the file
                        object, so the image has to be reopened afterwards --
                        Pillow documents this and silently misbehaves if you
                        don't.
      draft()           tells the JPEG decoder to scale down during decode, so
                        a 12 MP file never becomes a 12 MP bitmap. It must be
                        called before any pixel is touched, and the requested
                        box must be ASPECT-CORRECT: asking for a square
                        (1600, 1600) against a 4000x3000 photo makes Pillow
                        refuse the 1/2 step, because the resulting height of
                        1500 is under the 1600 asked for. Measured on six 12 MP
                        JPEGs, getting this right took the batch from 2.35s and
                        135 MB to 1.30s and 57 MB. Written wrong it still works
                        and saves nothing, which is why it is spelled out here.
      exif_transpose()  applies the camera's Orientation tag. It has to happen
                        BEFORE the metadata is dropped, or every phone photo
                        taken in portrait comes out on its side.
      flatten           a PNG with transparency saved straight to JPEG raises;
                        compositing onto white is what a viewer expects anyway.
      save()            a fresh JPEG with no exif argument, so every tag the
                        camera wrote -- GPS included -- is gone. That is the
                        point, not a side effect.

    One image at a time, closed as it goes: six at once in memory is the
    difference between fitting on a 512 MB instance and not.
    """
    cfg = current_app.config
    max_width = cfg["IMAGE_MAX_WIDTH"]
    probe = img = None
    try:
        probe = Image.open(io.BytesIO(raw))
        probe.verify()
    except Exception:
        return None, "That file is not an image Pillow can read."
    finally:
        if probe is not None:
            probe.close()

    try:
        img = Image.open(io.BytesIO(raw))
        if img.format == "JPEG":
            target_h = max(1, round(img.height * max_width / img.width))
            img.draft("RGB", (max_width, target_h))

        fixed = ImageOps.exif_transpose(img)
        if fixed is not img:
            img.close()
            img = fixed

        if img.mode in ("RGBA", "LA", "P"):
            rgba = img.convert("RGBA")
            flat = Image.new("RGB", rgba.size, (255, 255, 255))
            flat.paste(rgba, mask=rgba.split()[-1])
            rgba.close()
            img.close()
            img = flat
        elif img.mode != "RGB":
            converted = img.convert("RGB")
            img.close()
            img = converted

        if img.width > max_width:
            h = max(1, round(img.height * max_width / img.width))
            resized = img.resize((max_width, h), Image.LANCZOS)
            img.close()
            img = resized

        os.makedirs(upload_folder, exist_ok=True)
        name = f"{uuid.uuid4().hex}.jpg"
        img.save(os.path.join(upload_folder, name), "JPEG",
                 quality=cfg["IMAGE_JPEG_QUALITY"], optimize=True)
        return name, None
    except Image.DecompressionBombWarning:
        return None, "That image is far too large to process."
    except Exception as e:
        return None, f"That image could not be processed ({type(e).__name__})."
    finally:
        if img is not None:
            img.close()


def save_uploaded_images(files, upload_folder, max_count):
    """Validates and stores a list of uploads. Returns (filenames, error).

    ALL-OR-NOTHING, deliberately. Every file is checked and processed before
    any of them counts, and a single bad one discards the whole batch --
    including deleting the ones already written. A listing that silently kept
    four of six photos would look finished and be wrong, and the submitter has
    no way to tell which two went missing.
    """
    real = [f for f in files if f and f.filename]
    if not real:
        return [], None
    if len(real) > max_count:
        return [], (f"Please upload at most {max_count} images "
                    f"({len(real)} were selected).")

    written = []
    for f in real:
        if not allowed_image(f.filename):
            error = (f"'{f.filename}': images must be one of "
                     + ", ".join(sorted(ALLOWED_IMAGE_EXTENSIONS)) + ".")
            break
        ext = secure_filename(f.filename).rsplit(".", 1)[1].lower()
        actual = sniff_image_type(f.stream)
        if actual is None:
            error = f"'{f.filename}' is not an image, whatever its name says."
            break
        if actual not in EQUIVALENT_IMAGE_TYPES.get(ext, {ext}):
            error = (f"'{f.filename}' is a {actual.upper()} but is named "
                     f".{ext}. Rename it to match and try again.")
            break
        f.stream.seek(0)
        name, err = process_image(f.stream.read(), upload_folder)
        if err:
            error = f"'{f.filename}': {err}"
            break
        written.append(name)
    else:
        return written, None

    # Reached only via break: undo the partial batch.
    for name in written:
        try:
            os.remove(os.path.join(upload_folder, name))
        except OSError:
            pass
    return [], error + " Nothing was saved -- please fix it and try again."


# Every column in this app that can hold an image URL. There are five, and an
# earlier version of the check below knew about two of them.
#
# That was a data-loss bug, not an oversight to tidy later. Upload a compound
# cover, paste the same /uploads/... path into a listing's "Or Photo URL"
# field, then delete the listing: the file was unreferenced as far as
# Listing/ListingImage could see, so it was deleted, and the compound's cover
# broke on a live page with no way back. Any new image column has to be added
# here or the same hole reopens.
IMAGE_URL_COLUMNS = (
    ("compounds", "cover_image_url"),
    ("units", "image_url"),
    ("developers", "logo_url"),
    ("listings", "image_url"),
    ("listing_images", "url"),
)


def upload_references(url):
    """Every place this URL is still referenced, as "table.column" strings.

    Queried as plain SQL over the table list above rather than through the
    models, so adding a column to IMAGE_URL_COLUMNS is the only change needed
    and no import can be forgotten.
    """
    found = []
    for table, column in IMAGE_URL_COLUMNS:
        row = db.session.execute(
            db.text(f"SELECT 1 FROM {table} WHERE {column} = :u LIMIT 1"),
            {"u": url},
        ).first()
        if row:
            found.append(f"{table}.{column}")
    return found


def unreferenced_uploads(urls):
    """Which of `urls` are local uploads that nothing references any more.

    REPORTS ONLY. Nothing in the request path deletes a file: the cost of a
    wrong answer here is a live page losing its image permanently, and the
    cost of being wrong the other way is some disk. Actual removal lives in
    scripts/list_orphan_uploads.py and needs an explicit --delete.
    """
    out = []
    for url in dict.fromkeys(urls):
        if not url or not url.startswith("/uploads/"):
            continue        # external address: never ours to touch
        base = os.path.basename(url)
        if not base or base != secure_filename(base):
            continue
        if not upload_references(url):
            out.append(url)
    return out


def report_unreferenced(urls, context):
    """Logs what became unreferenced. Deletes nothing. Returns the count."""
    orphans = unreferenced_uploads(urls)
    for url in orphans:
        print(f"[uploads] {context}: {url} is no longer referenced "
              f"(NOT deleted -- run scripts/list_orphan_uploads.py)")
    return len(orphans)


# The values these fields are allowed to hold, taken from the <select> options
# the public form actually offers. Only listing_type was ever checked; the rest
# were written straight from request.form, so anything at all could be POSTed
# into them and would then render raw on the public listing page.
#
# "" is allowed throughout and means "not stated" -- these are optional fields,
# and refusing a blank would make them required by the back door.
LISTING_FURNISHING = {"", "unfurnished", "semi", "furnished"}
LISTING_CONDITION = {"", "New", "Excellent", "Good", "Needs Renovation"}
LISTING_LEGAL_STATUS = {"", "registered_title", "court_validated",
                        "preliminary_contract", "unknown"}
LISTING_SELLER_TYPE = {"", "owner", "agent"}

LISTING_ENUMS = {
    "furnishing": LISTING_FURNISHING,
    "condition": LISTING_CONDITION,
    "legal_status": LISTING_LEGAL_STATUS,
    "seller_type": LISTING_SELLER_TYPE,
}


# --------------------------------------------------------------------------
# Spam defences for the public forms.
#
# These exist because of what actually arrived: titles and areas like
# "Cqgnhdtqpj" and "Mzdmvx", prices of 7, 25, 67 and 73 EGP, one request every
# few days. Slow, crude, and all of it before CSRF and rate limiting existed.
#
# Every check below answers "could a person have typed this", never "is this
# person a spammer". That distinction is the whole design: a wrong guess here
# refuses a real seller their listing, so each rule has to be one a human
# would never trip, and each is a separate small rule rather than one score,
# so a refusal can say which one it was.

ARABIC_RE = re.compile(r"[\u0600-\u06FF\u0750-\u077F\uFB50-\uFDFF\uFE70-\uFEFF]")
VOWELS = set("aeiouy")
LINK_RE = re.compile(
    r"(https?://|www\.|\b[a-z0-9][a-z0-9-]{1,}\.(?:com|net|org|ru|xyz|top|club|online|site|info|biz|link|shop)\b)",
    re.IGNORECASE)

HONEYPOT_FIELD = "company_website"
FORM_TS_FIELD = "form_ts"
_FORM_TS_SALT = "meleven-form-timestamp"


def has_arabic(text):
    return bool(ARABIC_RE.search(text or ""))


def contains_link(text):
    return bool(LINK_RE.search(text or ""))


def _case_flips(token):
    """Times the case changes inside one token: 'aBcD' is 3.

    Random mixed-case strings do this; names do not. 'PK2' and 'SODIC' and
    'Palm' all score 0 or 1.
    """
    flips = 0
    letters = [ch for ch in token if ch.isalpha()]
    for a, b in zip(letters, letters[1:]):
        if a.isupper() != b.isupper():
            flips += 1
    return flips


def _longest_consonant_run(token):
    run = best = 0
    for ch in token.lower():
        if ch.isalpha() and ch not in VOWELS:
            run += 1
            best = max(best, run)
        else:
            run = 0
    return best


def is_gibberish(text, field="text"):
    """(bad, reason) -- reason is shown to the submitter, so it has to be plain.

    ARABIC IS EXEMPT FROM THE LETTER RULES. The vowel and consonant-run tests
    are facts about Latin spelling; Arabic script has no Latin vowels at all,
    so applying them would refuse every Arabic title ever written. Case
    flipping is moot there too -- Arabic has no case. Length and shape still
    apply.
    """
    raw = (text or "").strip()
    if not raw:
        return False, ""

    cfg = current_app.config
    if len(raw) < cfg["TEXT_MIN_LENGTH"]:
        return True, f"The {field} is too short to be a real one."

    # Unbroken wall of characters -- applies whatever the script, because the
    # test is about shape, not spelling.
    if " " not in raw and len(raw) >= cfg["TEXT_MAX_UNSPACED"]:
        return True, (f"The {field} looks like one long run of characters. "
                      f"Please write it as words.")

    if has_arabic(raw):
        return False, ""

    for token in re.split(r"[\s\-_/,.()]+", raw):
        letters = [ch for ch in token if ch.isalpha()]
        if len(letters) < 4:
            # Short tokens are where the real names live: PX, PK2, 6th, El.
            continue
        if not (set(token.lower()) & VOWELS):
            return True, (f"'{token}' in the {field} has no vowels, so it "
                          f"reads as random characters rather than a word.")
        if _longest_consonant_run(token) >= cfg["TEXT_MAX_CONSONANT_RUN"]:
            return True, (f"'{token}' in the {field} has too many consonants "
                          f"in a row to be a word.")
        if _case_flips(token) >= cfg["TEXT_MAX_CASE_FLIPS"]:
            return True, (f"'{token}' in the {field} mixes upper and lower "
                          f"case in a way that reads as generated.")
    return False, ""


def form_timestamp():
    """A signed 'this form was rendered now' token, for the templates.

    Signed rather than a plain number so a script cannot post a fabricated
    older timestamp to clear the minimum-fill-time check.
    """
    return URLSafeTimedSerializer(
        current_app.config["SECRET_KEY"], salt=_FORM_TS_SALT).dumps("")


def check_form_timing(form):
    """(verdict, message) where verdict is 'ok', 'too_fast' or 'stale'.

    'too_fast' is a silent reject at the call site -- telling a bot which
    check it failed is how its author fixes it. 'stale' is NOT silent: a tab
    left open for two days is a person, and they need to be told to reload.
    """
    cfg = current_app.config
    raw = (form.get(FORM_TS_FIELD) or "").strip()
    if not raw:
        return "too_fast", ""
    serializer = URLSafeTimedSerializer(cfg["SECRET_KEY"], salt=_FORM_TS_SALT)
    try:
        serializer.loads(raw, max_age=cfg["FORM_MAX_AGE_SECONDS"])
    except SignatureExpired:
        return "stale", ("This form had been open too long to submit safely. "
                         "Please reload the page and send it again.")
    except BadSignature:
        return "too_fast", ""
    _, issued_at = serializer.loads(raw, return_timestamp=True)
    age = (datetime.now(timezone.utc) - issued_at).total_seconds()
    if age < cfg["FORM_MIN_SECONDS"]:
        return "too_fast", ""
    return "ok", ""


def honeypot_tripped(form):
    """True when the CSS-hidden decoy field came back with anything in it.

    Hidden with CSS rather than type=hidden on purpose: a bot that skips
    type=hidden inputs would walk straight past a hidden one, while a bot that
    fills every text input it can see in the HTML falls for this.
    """
    return bool((form.get(HONEYPOT_FIELD) or "").strip())


def listing_price_floor(listing_type, form):
    """(bad, reason) for a visitor listing priced below the floor for its type.

    Never applied to admin-entered listings: the floor is a spam filter, not a
    business rule, and Mahmoud's own listings do not pass through it.
    """
    cfg = current_app.config
    checks = {
        "resale": ("price", cfg["LISTING_MIN_PRICE_EGP"], "sale price"),
        "rent_annual": ("rent_amount", cfg["LISTING_MIN_RENT_EGP"], "rent"),
        "rent_seasonal": ("price_per_week", cfg["LISTING_MIN_WEEKLY_EGP"],
                          "weekly price"),
    }
    if listing_type not in checks:
        return False, ""
    field, floor, label = checks[listing_type]
    raw = (form.get(field) or "").strip()
    if not raw:
        return False, ""
    try:
        value = float(raw)
    except ValueError:
        return True, f"The {label} is not a number."
    if value < floor:
        return True, (f"A {label} under {floor:,.0f} EGP doesn't look right — "
                      f"please check the figure. Contact us if your property "
                      f"genuinely falls below this.")
    return False, ""


# Fields a link is never legitimate in. `message` is deliberately absent: a
# real enquiry can reasonably paste a listing or a Facebook page, so links
# there are flagged for review instead of refused.
NO_LINK_FIELDS = ("title", "area", "location", "unit_type", "owner_name",
                  "rent_cadence", "name", "compound_name", "property_type")


def check_no_links(form, fields=NO_LINK_FIELDS):
    for field in fields:
        if contains_link(form.get(field) or ""):
            return True, (f"Links aren't allowed in the {field.replace('_', ' ')} "
                          f"field.")
    return False, ""


def public_form_gate(form, text_fields=()):
    """Runs the checks every public form shares. Returns (verdict, message).

    verdict is one of:
      "ok"      -- carry on
      "silent"  -- stop, but tell the submitter it worked. Reserved for the
                   honeypot and the too-fast check, which only a script trips:
                   naming the check that caught it is how its author fixes it.
      "reject"  -- stop and show the message. Everything a person could
                   plausibly trip, so they get told what to change.

    text_fields are checked for gibberish; the link check runs over the fixed
    NO_LINK_FIELDS list regardless.
    """
    if honeypot_tripped(form):
        return "silent", ""

    verdict, message = check_form_timing(form)
    if verdict == "too_fast":
        return "silent", ""
    if verdict == "stale":
        return "reject", message

    bad, message = check_no_links(form)
    if bad:
        return "reject", message

    for field in text_fields:
        bad, message = is_gibberish(form.get(field) or "", field.replace("_", " "))
        if bad:
            return "reject", message

    return "ok", ""


def flag_links_for_review(form, field="message"):
    """(flagged, reason) for a field where a link is allowed but worth a look."""
    if contains_link(form.get(field) or ""):
        return True, f"contains a link in {field}"
    return False, ""


def validate_listing_enums(form):
    """Returns (values, errors) for the four whitelisted listing fields."""
    values, errors = {}, []
    for field, allowed in LISTING_ENUMS.items():
        raw = (form.get(field) or "").strip()
        if raw not in allowed:
            errors.append(f"{field.replace('_', ' ')} is not a recognised value.")
            values[field] = ""
        else:
            values[field] = raw
    return values, errors


def create_app():
    app = Flask(__name__)
    app.config.from_object(Config)
    app.request_class = RouteAwareRequest
    # Render terminates TLS in front of the app, so without this url_for(...,
    # _external=True) builds http:// links and request.is_secure is False.
    # The rate-limit key does NOT depend on this -- see client_ip().
    app.wsgi_app = ProxyFix(app.wsgi_app, x_for=1, x_proto=1, x_host=1)
    db.init_app(app)
    limiter.init_app(app)
    csrf.init_app(app)
    app.jinja_env.globals["slugify"] = slugify  # so templates can build /developer/<slug> links
    # Every public form renders these two: the signed render-time stamp and
    # the name of the decoy field, so the name lives in one place.
    app.jinja_env.globals["form_timestamp"] = form_timestamp
    app.jinja_env.globals["honeypot_field"] = HONEYPOT_FIELD

    @app.errorhandler(CSRFError)
    def handle_csrf_error(e):
        """A failed token is almost always a stale page, not an attack.

        Flask-WTF's own 400 page says "The CSRF token has expired", which to
        someone who just spent ten minutes on the listing form reads as the
        site being broken. This says what to do instead, and sends them back
        where they came from with their place kept.
        """
        flash("That form had expired, so nothing was submitted. "
              "Please try again -- your details were not sent anywhere.", "error")
        return redirect(request.referrer or url_for("home")), 303

    @app.errorhandler(413)
    def handle_too_large(e):
        """Over MAX_CONTENT_LENGTH. Flask aborts before any route runs, so
        this is the only place that can explain it."""
        endpoint = request.url_rule.endpoint if request.url_rule else None
        if endpoint in VISITOR_UPLOAD_ENDPOINTS:
            key = "MAX_VISITOR_UPLOAD_CONTENT_LENGTH"
        elif endpoint in UPLOAD_ENDPOINTS:
            key = "MAX_UPLOAD_CONTENT_LENGTH"
        else:
            key = "MAX_CONTENT_LENGTH"
        limit_mb = app.config[key] // (1024 * 1024)
        flash(f"That upload is too large. The limit here is {limit_mb} MB — "
              f"nothing was saved.", "error")
        return redirect(request.referrer or url_for("list_property")), 303

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

        # `source` distinguishes a listing entered in /admin from one the
        # public submitted. DEFAULT 'visitor' is deliberate for the backfill:
        # every row that predates this column arrived through
        # /list-your-property, and an unknown origin must never be promoted to
        # "ours" by a default. Written as DEFAULT + UPDATE rather than
        # NOT NULL, because adding a NOT NULL column to a populated table
        # fails on Postgres unless every existing row already has a value.
        existing_lead_columns2 = [col["name"] for col in inspector.get_columns("leads")]
        if "flagged" not in existing_lead_columns2:
            with db.engine.connect() as connection:
                connection.execute(db.text(
                    "ALTER TABLE leads ADD COLUMN flagged BOOLEAN DEFAULT FALSE"))
                connection.execute(db.text(
                    "ALTER TABLE leads ADD COLUMN flag_reason VARCHAR(255)"))
                connection.execute(db.text(
                    "UPDATE leads SET flagged = FALSE WHERE flagged IS NULL"))
                connection.commit()

        if "source" not in existing_listing_columns:
            with db.engine.connect() as connection:
                connection.execute(db.text(
                    "ALTER TABLE listings ADD COLUMN source VARCHAR(20) DEFAULT 'visitor'"))
                connection.execute(db.text(
                    "UPDATE listings SET source = 'visitor' WHERE source IS NULL"))
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

        # Full-bleed promo banner: rotates through the same handpicked
        # (is_featured) pool the "Featured" grid below already uses, one at a
        # time, switching to the next automatically every 2 days -- rather
        # than always showing whichever one the DB happens to return first
        # (no ORDER BY previously, so it was effectively frozen on one
        # compound). Ordered by name for a stable, predictable rotation
        # sequence that doesn't reshuffle if compounds are added/removed from
        # the pool. ROTATION_EPOCH is the day this rotation started -- day 0
        # (today, and the day after) lands on index 0 of the alphabetical
        # list; every 2 days after that it advances to the next one and wraps
        # back to the start once it reaches the end.
        promo_pool = (
            Compound.query.filter_by(is_featured=True, is_published=True)
            .order_by(Compound.name.asc())
            .all()
        )
        promo_compound = None
        if promo_pool:
            ROTATION_EPOCH = date(2026, 8, 29)
            days_elapsed = max(0, (date.today() - ROTATION_EPOCH).days)
            promo_compound = promo_pool[(days_elapsed // 2) % len(promo_pool)]

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
            .order_by(db.func.count(Compound.id).desc())
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

        # Every region now shows directly in the Top Areas card grid (there
        # are only a handful of real top-level locations) -- no "View all
        # areas" overflow needed; locations_menu naturally ends up empty and
        # that section just doesn't render.
        CARD_LIMIT = len(top_areas)
        all_locations_sorted = top_areas
        top_areas = all_locations_sorted[:CARD_LIMIT]

        # "New Launches" — compounds flagged directly (Compound.is_launch) OR
        # with at least one unit explicitly flagged (Unit.is_launch), most
        # recently added first. Falls back to the old "soonest delivery
        # year" sort if nothing has been flagged yet, so the section never
        # sits empty while launch flags are still being set in the admin.
        # Cap sized to fit however many compounds are genuinely flagged right now
        # (14 as of this change). It's a display cap, not a curation rule -- what
        # belongs in the section is decided by the is_launch flag in the admin, so
        # raise this if more compounds get flagged than it currently allows,
        # otherwise the newest ones silently push older flagged ones out of view.
        new_launches = (
            Compound.query
            .outerjoin(Unit, Unit.compound_id == Compound.id)
            .filter(
                Compound.is_published == True,
                db.or_(Compound.is_launch == True, Unit.is_launch == True),
            )
            .distinct()
            .order_by(Compound.created_at.desc())
            .limit(14)
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
        # Alphabetical here (unlike the count-desc order used to pick the top
        # cards above) -- this is a "look one up" dropdown, not a prominence
        # ranking, so A-Z is the more useful order to browse it in.
        locations_menu = [
            {"name": a["name"], "count": a["count"], "cover_image_url": a["cover_image_url"]}
            for a in sorted(all_locations_sorted[CARD_LIMIT:], key=lambda a: a["name"])
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

        # Unit.currency == "EGP": down_payment/installment/max_price above are all
        # entered in EGP (the calculator has no currency selector), so a USD-priced
        # unit (e.g. several El Gouna resort compounds, priced in USD on nawy.com
        # and imported with that raw number rather than a converted EGP figure)
        # would otherwise get compared against an EGP threshold as if its price
        # were EGP -- wildly wrong in either direction. Simplest correct fix
        # without inventing an exchange rate: this residential-investment
        # calculator just doesn't match non-EGP units at all; they're still fully
        # visible/searchable elsewhere (compound page, /compounds, chatbot).
        initial_calc_base_query = (
            Unit.query
            .join(Compound, Unit.compound_id == Compound.id)
            .filter(Unit.is_available == True, Compound.is_published == True, Unit.price.isnot(None),
                    Unit.price <= initial_max_price, Unit.unit_type.notin_(NON_RESIDENTIAL_UNIT_TYPES),
                    Unit.currency == "EGP")
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
            promo_compound=promo_compound,
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

        # Unit.currency == "EGP": see the identical comment on initial_calc_base_query
        # above -- this calculator's inputs are all EGP, so non-EGP units are
        # excluded from matching rather than compared against an EGP threshold.
        query = (
            Unit.query
            .join(Compound, Unit.compound_id == Compound.id)
            .filter(Unit.is_available == True, Compound.is_published == True, Unit.price.isnot(None),
                    Unit.unit_type.notin_(NON_RESIDENTIAL_UNIT_TYPES), Unit.currency == "EGP")
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
                        Unit.unit_type.notin_(NON_RESIDENTIAL_UNIT_TYPES), Unit.currency == "EGP")
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
    # Each call reaches the Claude API and costs money, so this is the one
    # limit that protects a bill rather than a table. The CSRF token stops
    # another site triggering it; nothing stopped a script that reads the
    # token off the page and loops.
    @limiter.limit(lambda: current_app.config["RATE_LIMIT_AI"])
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
    # Each call reaches the Claude API and costs money, so this is the one
    # limit that protects a bill rather than a table. The CSRF token stops
    # another site triggering it; nothing stopped a script that reads the
    # token off the page and loops.
    @limiter.limit(lambda: current_app.config["RATE_LIMIT_AI"])
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
            # Sub-areas are no longer listed inline here -- this page shows only
            # the main regions; clicking one goes to location_areas() below for
            # its actual sub-area cards, so there's no need to compute them twice.
            location_list.append({
                "name": location_name,
                "count": count,
                "cover_image_url": sample.cover_image_url if sample else None,
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

        # This filter's inputs are entered in EGP with no currency selector, so a
        # USD-priced compound (see NON_RESIDENTIAL_UNIT_TYPES's neighboring comment
        # in api_properties_count() for the same underlying issue) is excluded
        # from EGP price-range filtering entirely rather than compared against an
        # EGP threshold as if its min/max_price were EGP -- it's still fully
        # visible/browsable via every other filter or a direct link.
        if min_price:
            try:
                query = query.filter(Compound.max_price >= int(min_price), Compound.currency == "EGP")
            except ValueError:
                pass

        if max_price:
            try:
                query = query.filter(Compound.min_price <= int(max_price), Compound.currency == "EGP")
            except ValueError:
                pass

        all_compounds = query.order_by(Compound.name.asc()).all()

        # Options for the filter sidebar
        areas = sorted({row[0] for row in db.session.query(Compound.area).distinct() if row[0]})
        developers = sorted({row[0] for row in db.session.query(Compound.developer).distinct() if row[0]})
        delivery_years = sorted(
            {row[0] for row in db.session.query(Compound.delivery_year).distinct() if row[0]}
        )

        # When exactly one filter is active and it's a top-level region (e.g.
        # "North Coast", picked from the homepage Top Areas widget or
        # /locations) rather than an already-specific sub-area, show that
        # region's real sub-areas as a filter bar above the results -- same
        # page, no separate drill-down page. Clicking a chip re-filters this
        # same /compounds view by that sub-area instead of navigating away.
        sub_area_bar = []
        if len(selected_areas) == 1 and selected_areas[0]:
            current = selected_areas[0].strip()
            is_main_region = (
                db.session.query(Compound.id)
                .filter(db.func.lower(Compound.location) == current.lower(), Compound.is_published == True)
                .first()
                is not None
            )
            if is_main_region:
                sub_area_rows = (
                    db.session.query(Compound.area, db.func.count(Compound.id))
                    .filter(
                        db.func.lower(Compound.location) == current.lower(),
                        Compound.is_published == True,
                        Compound.area.isnot(None), Compound.area != "",
                    )
                    .group_by(Compound.area)
                    .order_by(db.func.count(Compound.id).desc())
                    .all()
                )
                sub_area_bar = [{"name": r[0], "count": r[1]} for r in sub_area_rows]

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
            sub_area_bar=sub_area_bar,
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
    @limiter.limit(lambda: current_app.config["RATE_LIMIT_LEAD"], methods=["POST"])
    def sell_property():
        if request.method == "POST":
            name = request.form.get("name", "").strip()
            phone = request.form.get("phone", "").strip()
            email = request.form.get("email", "").strip()
            # This route had no validation at all: an empty POST created a Lead
            # with an empty name and an empty phone, which is a row nobody can
            # act on and the easiest thing in the world to automate.
            if not name or not phone:
                flash("Your name and phone number are required.", "error")
                return redirect(url_for("sell_property"))

            verdict, message = public_form_gate(
                request.form,
                text_fields=("name", "compound_name", "location", "property_type"))
            if verdict == "silent":
                return redirect(url_for("list_property"))
            if verdict == "reject":
                flash(message, "error")
                return redirect(url_for("sell_property"))
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
    @limiter.limit(lambda: current_app.config["RATE_LIMIT_LEAD"], methods=["POST"])
    def contact():
        if request.method == "POST":
            thanks = ("Thanks for reaching out! Our team at Meleven will "
                      "contact you shortly.")
            verdict, message = public_form_gate(request.form, text_fields=("name",))
            if verdict == "silent":
                flash(thanks, "success")
                return redirect(url_for("contact"))
            if verdict == "reject":
                flash(message, "error")
                return redirect(url_for("contact"))

            flagged, reason = flag_links_for_review(request.form)
            lead = Lead(
                name=request.form.get("name", "").strip(),
                phone=request.form.get("phone", "").strip(),
                email=request.form.get("email", "").strip(),
                message=request.form.get("message", "").strip(),
                source_page="contact_page",
                flagged=flagged,
                flag_reason=reason,
            )
            db.session.add(lead)
            db.session.commit()
            flash(thanks, "success")
            return redirect(url_for("contact"))

        return render_template("contact.html")

    @app.route("/compound/<slug>/interested", methods=["POST"])
    @limiter.limit(lambda: current_app.config["RATE_LIMIT_LEAD"])
    def compound_interested(slug):
        compound = Compound.query.filter_by(slug=slug).first_or_404()
        thanks = ("Thanks for your interest! Our team at Meleven will contact "
                  "you shortly about this project.")
        verdict, message = public_form_gate(request.form, text_fields=("name",))
        if verdict == "silent":
            flash(thanks, "success")
            return redirect(url_for("compound_detail", slug=slug))
        if verdict == "reject":
            flash(message, "error")
            return redirect(url_for("compound_detail", slug=slug))

        flagged, reason = flag_links_for_review(request.form)
        lead = Lead(
            name=request.form.get("name", "").strip(),
            phone=request.form.get("phone", "").strip(),
            email=request.form.get("email", "").strip(),
            message=request.form.get("message", "").strip(),
            compound_id=compound.id,
            source_page=f"compound:{slug}",
            flagged=flagged,
            flag_reason=reason,
        )
        db.session.add(lead)
        db.session.commit()
        flash(thanks, "success")
        return redirect(url_for("compound_detail", slug=slug))

    # ---------- Resale/Rent listings (public) ----------
    # Entirely separate from the Compound/Unit developer-sale inventory —
    # these are individually-owned units submitted by their owner or an
    # agent. Every submission lands as status="pending" and is invisible on
    # every public route below until an admin approves it under /admin.

    ALLOWED_LISTING_TYPES = {"resale", "rent_annual", "rent_seasonal"}

    # Open to anyone, so it is rate-limited. Before this there was no limit on
    # it at all: one script could fill the listings table and the uploads disk
    # overnight, and every row would have landed in the pending queue for a
    # human to wade through. The per-hour cap is the one that matters; the
    # per-minute cap just stops a burst.
    @app.route("/list-your-property", methods=["GET", "POST"])
    @limiter.limit(lambda: current_app.config["RATE_LIMIT_LISTING"], methods=["POST"])
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

            verdict, message = public_form_gate(
                request.form, text_fields=("title", "area", "location"))
            if verdict == "silent":
                # Answered exactly as a success, and nothing written.
                flash("Thanks! Your listing has been submitted and is pending "
                      "review — we'll publish it once approved.", "success")
                return redirect(url_for("list_property"))
            if verdict == "reject":
                flash(message, "error")
                return redirect(url_for("list_property"))

            bad, message = listing_price_floor(listing_type, request.form)
            if bad:
                flash(message, "error")
                return redirect(url_for("list_property"))

            # Rejected rather than silently blanked: a submitter who picked
            # something from a dropdown should not have it quietly discarded,
            # and a value that was not in any dropdown did not come from the
            # form at all.
            enums, enum_errors = validate_listing_enums(request.form)
            if enum_errors:
                for message in enum_errors:
                    flash(message, "error")
                return redirect(url_for("list_property"))

            image_url = request.form.get("image_url", "").strip()
            names, upload_error = save_uploaded_images(
                request.files.getlist("image_files") + request.files.getlist("image_file"),
                app.config["UPLOAD_FOLDER"],
                app.config["LISTING_MAX_IMAGES_VISITOR"])
            if upload_error:
                # The photos are optional, but a rejected batch must not be
                # passed over in silence -- the submitter needs to know.
                flash(upload_error, "error")
                return redirect(url_for("list_property"))
            gallery_urls = [url_for("uploaded_file", filename=n) for n in names]
            if image_url:
                gallery_urls.append(image_url)
            if gallery_urls:
                image_url = gallery_urls[0]

            listing = Listing(
                listing_type=listing_type,
                status="pending",
                source="visitor",
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
                furnishing=enums["furnishing"],
                condition=enums["condition"],
                legal_status=enums["legal_status"],
                seller_type=enums["seller_type"],
                negotiable=bool(request.form.get("negotiable")),
                owner_name=owner_name,
                owner_phone=owner_phone,
                owner_email=request.form.get("owner_email", "").strip(),
                image_url=image_url,
            )
            db.session.add(listing)
            db.session.flush()          # need listing.id for the image rows
            for i, u in enumerate(gallery_urls):
                db.session.add(ListingImage(listing_id=listing.id, url=u, position=i))
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
    @limiter.limit(lambda: current_app.config["RATE_LIMIT_LEAD"])
    def listing_interested(listing_id):
        listing = Listing.query.filter_by(id=listing_id, status="approved").first_or_404()
        thanks = ("Thanks for your interest! Our team at Meleven will contact "
                  "you shortly about this property.")
        verdict, message = public_form_gate(request.form, text_fields=("name",))
        if verdict == "silent":
            flash(thanks, "success")
            return redirect(url_for("listing_detail", listing_id=listing.id))
        if verdict == "reject":
            flash(message, "error")
            return redirect(url_for("listing_detail", listing_id=listing.id))

        flagged, reason = flag_links_for_review(request.form)
        lead = Lead(
            name=request.form.get("name", "").strip(),
            phone=request.form.get("phone", "").strip(),
            email=request.form.get("email", "").strip(),
            message=request.form.get("message", "").strip(),
            listing_id=listing.id,
            source_page=f"listing:{listing.id}",
            flagged=flagged,
            flag_reason=reason,
        )
        db.session.add(lead)
        db.session.commit()
        flash(thanks, "success")
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
            uploaded_name, upload_error = save_uploaded_image(
                request.files.get("cover_image_file"), app.config["UPLOAD_FOLDER"])
            if upload_error:
                flash(upload_error, "error")
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
            c.currency = (request.form.get("currency", "").strip() or "EGP").upper()
            c.land_area_acres = request.form.get("land_area_acres") or None
            c.delivery_year = request.form.get("delivery_year") or None

            cover_image_url = request.form.get("cover_image_url", "").strip()
            uploaded_name, upload_error = save_uploaded_image(
                request.files.get("cover_image_file"), app.config["UPLOAD_FOLDER"])
            if upload_error:
                flash(upload_error, "error")
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
            uploaded_name, upload_error = save_uploaded_image(
                request.files.get("logo_file"), app.config["UPLOAD_FOLDER"])
            if upload_error:
                flash(upload_error, "error")
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
            uploaded_name, upload_error = save_uploaded_image(
                request.files.get("logo_file"), app.config["UPLOAD_FOLDER"])
            if upload_error:
                flash(upload_error, "error")
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
            uploaded_name, upload_error = save_uploaded_image(
                request.files.get("image_file"), app.config["UPLOAD_FOLDER"])
            if upload_error:
                flash(upload_error, "error")
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
            u.currency = (request.form.get("currency", "").strip() or "EGP").upper()
            u.payment_plan = request.form.get("payment_plan", "").strip()

            image_url = request.form.get("image_url", "").strip()
            uploaded_name, upload_error = save_uploaded_image(
                request.files.get("image_file"), app.config["UPLOAD_FOLDER"])
            if upload_error:
                flash(upload_error, "error")
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
                currency=(row.get("currency") or "").strip().upper() or "EGP",
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
                currency=(row.get("currency") or "").strip().upper() or "EGP",
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
            updated, errors = _listing_from_form(request.form, request.files, listing)
            if errors:
                for message in errors:
                    flash(message, "error")
                return redirect(url_for("admin_listing_edit", listing_id=listing.id))

            _sync_gallery(listing, updated._gallery_urls)
            db.session.commit()
            # After the commit, never before: a rollback would otherwise leave
            # restored rows pointing at files that are gone.
            # Reported, not removed -- see unreferenced_uploads().
            loose = report_unreferenced(updated._sweep_urls,
                                        f"listing {listing.id} edited")
            msg = "Listing updated."
            if loose:
                msg += (f" {loose} image file(s) are now unused; they are kept "
                        f"on disk until you run the cleanup script.")
            flash(msg, "success")
            return redirect(url_for("admin_listing_review", listing_id=listing.id))

        compounds_for_link = Compound.query.filter_by(is_published=True).order_by(Compound.name.asc()).all()
        return render_template("admin/listing_form.html", listing=listing,
                               compounds_for_link=compounds_for_link,
                               max_images=app.config["LISTING_MAX_IMAGES_ADMIN"],
                               image_max_width=app.config["IMAGE_MAX_WIDTH"])

    # Fields the CSV import reads, in this order. Mirrors the Listing model and
    # the admin form; `title` is the only one that is required, exactly as on
    # /list-your-property. Deliberately the same shape as the compounds/units
    # importers above, including printing what it resolved back to the log.
    LISTING_CSV_FIELDS = [
        "title", "listing_type", "compound_slug", "area", "location",
        "unit_type", "bedrooms", "bathrooms", "area_sqm", "price",
        "rent_amount", "rent_cadence", "price_per_week",
        "high_season_multiplier", "currency", "furnishing", "condition",
        "legal_status", "seller_type", "negotiable", "owner_name",
        "owner_phone", "owner_email", "image_url", "images",
    ]

    def _listing_from_form(form, files, listing=None):
        """Builds or updates a Listing from the admin form.

        Returns (listing, errors). Shared by the create route below and
        nothing else yet -- kept separate so the create and edit paths cannot
        drift on validation, which is how owner_email ended up unwritable.
        """
        errors = []
        listing_type = (form.get("listing_type") or "").strip()
        if listing_type not in ALLOWED_LISTING_TYPES:
            errors.append("Please choose a valid listing type.")
        title = (form.get("title") or "").strip()
        if not title:
            errors.append("Title is required.")

        enums, enum_errors = validate_listing_enums(form)
        errors.extend(enum_errors)

        # New uploads, the optional external URL, and -- when editing -- the
        # existing rows with whatever positions, cover choice and deletions the
        # admin asked for. Resolved into one ordered list of URLs, with the
        # sweep list for files that stopped being referenced.
        existing = list(listing.images) if listing else []
        kept, removed_urls = [], []
        for row in existing:
            if form.get(f"delete_image_{row.id}"):
                removed_urls.append(row.url)
                continue
            raw_pos = (form.get(f"position_{row.id}") or "").strip()
            try:
                pos = int(raw_pos)
            except ValueError:
                pos = row.position
            kept.append((pos, row.url))

        names, upload_error = save_uploaded_images(
            files.getlist("image_files") + files.getlist("image_file"),
            app.config["UPLOAD_FOLDER"],
            app.config["LISTING_MAX_IMAGES_ADMIN"])
        if upload_error:
            errors.append(upload_error)

        # New ones land after everything kept, in the order they were chosen.
        start = max((p for p, _ in kept), default=-1) + 1
        for i, n in enumerate(names):
            kept.append((start + i, url_for("uploaded_file", filename=n)))
        external = (form.get("image_url") or "").strip()
        if external and external not in [u for _, u in kept]:
            kept.append((max((p for p, _ in kept), default=-1) + 1, external))

        # Stable sort, so equal positions keep the order they were listed in
        # rather than being shuffled -- the admin types these numbers by hand
        # and duplicates are expected.
        kept.sort(key=lambda pair: pair[0])
        ordered = [u for _, u in kept]

        cover_url = (form.get("cover_url") or "").strip()
        if cover_url and cover_url in ordered:
            ordered.remove(cover_url)
            ordered.insert(0, cover_url)

        total_cap = app.config["LISTING_MAX_IMAGES_ADMIN"]
        if len(ordered) > total_cap:
            errors.append(f"A listing can hold at most {total_cap} images "
                          f"({len(ordered)} would result).")

        if errors:
            # Nothing is committed, so the just-written files are orphans
            # already: remove them here rather than leaving them on the disk.
            for n in names:
                try:
                    os.remove(os.path.join(app.config["UPLOAD_FOLDER"], n))
                except OSError:
                    pass
            return None, errors

        target = listing or Listing()
        target.listing_type = listing_type
        target.title = title
        target.compound_id = form.get("compound_id") or None
        target.area = (form.get("area") or "").strip()
        target.location = (form.get("location") or "").strip()
        target.unit_type = (form.get("unit_type") or "").strip()
        target.bedrooms = form.get("bedrooms") or None
        target.bathrooms = form.get("bathrooms") or None
        target.area_sqm = form.get("area_sqm") or None
        target.price = form.get("price") or None
        target.rent_amount = form.get("rent_amount") or None
        target.rent_cadence = (form.get("rent_cadence") or "").strip()
        target.price_per_week = form.get("price_per_week") or None
        target.high_season_multiplier = form.get("high_season_multiplier") or None
        target.currency = (form.get("currency") or "").strip().upper() or "EGP"
        target.furnishing = enums["furnishing"]
        target.condition = enums["condition"]
        target.legal_status = enums["legal_status"]
        target.seller_type = enums["seller_type"]
        target.negotiable = bool(form.get("negotiable"))
        target.owner_name = (form.get("owner_name") or "").strip()
        target.owner_phone = (form.get("owner_phone") or "").strip()
        target.owner_email = (form.get("owner_email") or "").strip()
        # image_url stays the cover, so everything still reading it directly
        # keeps working. Nothing else in this function writes it.
        target.image_url = ordered[0] if ordered else ""
        target._gallery_urls = ordered
        target._sweep_urls = removed_urls
        return target, []

    def _sync_gallery(listing, urls):
        """Replaces a listing's image rows with `urls`, cover first.

        Rebuilt rather than diffed: the admin form submits the whole desired
        order every time, so reconciling row by row would be more code for the
        same result.
        """
        for row in list(listing.images):
            db.session.delete(row)
        db.session.flush()
        for i, u in enumerate(urls):
            db.session.add(ListingImage(listing_id=listing.id, url=u, position=i))

    @app.route("/admin/listings/new", methods=["GET", "POST"])
    @login_required
    def admin_listing_new():
        """Mahmoud's own resale units, published immediately.

        status="approved" and reviewed_at are set here rather than left to the
        pending queue: the review queue exists to check what strangers send in,
        and sending your own listing to yourself for approval is a step that
        can only ever say yes. source="admin" records which side it came from,
        permanently -- see the note on Listing.source.

        owner_name/owner_phone are required on the public form (they are how we
        reach a stranger) but optional here, because on Mahmoud's own unit the
        owner is Mahmoud and the fields are never shown publicly either way.
        """
        if request.method == "POST":
            listing, errors = _listing_from_form(request.form, request.files)
            if errors:
                for message in errors:
                    flash(message, "error")
                return redirect(url_for("admin_listing_new"))

            listing.status = "approved"
            listing.source = "admin"
            listing.reviewed_at = datetime.utcnow()
            listing.reviewed_by = (request.form.get("reviewed_by") or "").strip() or "Admin"
            db.session.add(listing)
            db.session.flush()
            _sync_gallery(listing, listing._gallery_urls)
            db.session.commit()
            flash(f"'{listing.title}' added and published with "
                  f"{len(listing._gallery_urls)} image(s).", "success")
            return redirect(url_for("admin_listing_review", listing_id=listing.id))

        compounds_for_link = Compound.query.filter_by(is_published=True).order_by(Compound.name.asc()).all()
        return render_template("admin/listing_form.html", listing=None,
                               compounds_for_link=compounds_for_link,
                               max_images=app.config["LISTING_MAX_IMAGES_ADMIN"],
                               image_max_width=app.config["IMAGE_MAX_WIDTH"])

    @app.route("/admin/listings/import", methods=["POST"])
    @login_required
    def admin_listings_import():
        """Bulk-adds resale/rent listings from a CSV, all published immediately.

        Same contract as /admin/compounds/import: a row without the one
        required field is skipped and counted rather than failing the file, and
        what was skipped is reported instead of swallowed.

        compound_slug (not compound_id) links a row to a compound, because a
        slug is the thing a human can read off the admin dashboard -- the same
        choice /admin/units/import already made. An unmatched slug does NOT
        skip the row: the listing is still worth having without the link, so it
        is imported unlinked and the slug is reported.
        """
        file = request.files.get("csv_file")
        if not file or file.filename == "":
            flash("Please choose a CSV file.", "error")
            return redirect(url_for("admin_listings"))

        stream = io.StringIO(file.stream.read().decode("utf-8-sig"))
        reader = csv.DictReader(stream)

        created, skipped, unknown_slugs, rejected = 0, 0, [], []
        for i, row in enumerate(reader, start=2):
            title = (row.get("title") or "").strip()
            if not title:
                skipped += 1
                continue

            listing_type = (row.get("listing_type") or "resale").strip() or "resale"
            if listing_type not in ALLOWED_LISTING_TYPES:
                rejected.append(f"row {i}: listing_type '{listing_type}'")
                continue

            enums, enum_errors = validate_listing_enums(row)
            if enum_errors:
                rejected.append(f"row {i}: " + "; ".join(enum_errors))
                continue

            compound_id = None
            slug = (row.get("compound_slug") or "").strip()
            if slug:
                compound = Compound.query.filter_by(slug=slug).first()
                if compound:
                    compound_id = compound.id
                elif slug not in unknown_slugs:
                    unknown_slugs.append(slug)

            listing = Listing(
                listing_type=listing_type,
                status="approved",
                source="admin",
                reviewed_at=datetime.utcnow(),
                reviewed_by="CSV import",
                compound_id=compound_id,
                title=title,
                area=(row.get("area") or "").strip(),
                location=(row.get("location") or "").strip(),
                unit_type=(row.get("unit_type") or "").strip(),
                bedrooms=row.get("bedrooms") or None,
                bathrooms=row.get("bathrooms") or None,
                area_sqm=row.get("area_sqm") or None,
                price=row.get("price") or None,
                rent_amount=row.get("rent_amount") or None,
                rent_cadence=(row.get("rent_cadence") or "").strip(),
                price_per_week=row.get("price_per_week") or None,
                high_season_multiplier=row.get("high_season_multiplier") or None,
                currency=(row.get("currency") or "").strip().upper() or "EGP",
                furnishing=enums["furnishing"],
                condition=enums["condition"],
                legal_status=enums["legal_status"],
                seller_type=enums["seller_type"],
                negotiable=parse_bool(row.get("negotiable")),
                owner_name=(row.get("owner_name") or "").strip(),
                owner_phone=(row.get("owner_phone") or "").strip(),
                owner_email=(row.get("owner_email") or "").strip(),
            )
            # `images` is pipe-separated and the first entry is the cover.
            # Values are used as given -- a URL or a filename this app already
            # wrote -- because a CSV cannot carry bytes to process. image_url
            # stays supported as the single-image spelling of the same thing.
            shots = [u.strip() for u in (row.get("images") or "").split("|")
                     if u.strip()]
            single = (row.get("image_url") or "").strip()
            if single and single not in shots:
                shots.append(single)
            listing.image_url = shots[0] if shots else ""
            db.session.add(listing)
            db.session.flush()
            for pos, u in enumerate(shots):
                db.session.add(ListingImage(listing_id=listing.id, url=u,
                                            position=pos))
            print(f"[listings import] '{title}' -> {listing_type}, "
                  f"compound_slug='{slug}' -> compound_id={compound_id}")
            created += 1

        db.session.commit()

        message = f"Imported {created} listing(s), published immediately."
        if skipped:
            message += f" Skipped {skipped} row(s) with no title."
        if unknown_slugs:
            message += (" Imported without a compound link (slug not found): "
                        + ", ".join(unknown_slugs[:10]))
        if rejected:
            message += " Rejected: " + "; ".join(rejected[:5])
            if len(rejected) > 5:
                message += f" (+{len(rejected) - 5} more)"
        flash(message, "success" if created else "error")
        return redirect(url_for("admin_listings", status="approved"))

    @app.route("/admin/listings/bulk-reject", methods=["POST"])
    @login_required
    def admin_listings_bulk_reject():
        """Rejects every ticked listing in one go.

        Rejects rather than deletes, like the single-row button: status
        'rejected' keeps the row, so a wrong call is one click back. A spam
        queue is also the worst place to delete from -- the one row you got
        wrong is the one a real seller sent.
        """
        ids = request.form.getlist("listing_ids")
        if not ids:
            flash("Nothing was selected.", "error")
            return redirect(url_for("admin_listings"))

        rows = Listing.query.filter(Listing.id.in_(ids)).all()
        reviewer = (request.form.get("reviewed_by") or "").strip() or "Admin"
        for listing in rows:
            listing.status = "rejected"
            listing.reviewed_at = datetime.utcnow()
            listing.reviewed_by = reviewer
        db.session.commit()
        flash(f"Rejected {len(rows)} listing(s).", "success")
        return redirect(url_for("admin_listings",
                                status=request.form.get("return_to") or "pending"))

    @app.route("/admin/listings/reject-under-minimum", methods=["POST"])
    @login_required
    def admin_listings_reject_under_minimum():
        """Rejects every pending listing priced below the floor for its type.

        Reads the same Config floors the public form enforces, so this clears
        exactly the backlog that arrived before the floor existed and nothing
        else. A pending row with no price at all is left alone: no price is
        not a low price, and some genuine submissions arrive without one.
        """
        cfg = current_app.config
        floors = {
            "resale": (Listing.price, cfg["LISTING_MIN_PRICE_EGP"]),
            "rent_annual": (Listing.rent_amount, cfg["LISTING_MIN_RENT_EGP"]),
            "rent_seasonal": (Listing.price_per_week, cfg["LISTING_MIN_WEEKLY_EGP"]),
        }
        reviewer = (request.form.get("reviewed_by") or "").strip() or "Admin"
        total, per_type = 0, []
        for listing_type, (column, floor) in floors.items():
            rows = Listing.query.filter(
                Listing.status == "pending",
                Listing.listing_type == listing_type,
                column.isnot(None),
                column < floor,
            ).all()
            for listing in rows:
                listing.status = "rejected"
                listing.reviewed_at = datetime.utcnow()
                listing.reviewed_by = reviewer
            if rows:
                per_type.append(f"{len(rows)} {listing_type} under {floor:,}")
            total += len(rows)
        db.session.commit()

        if total:
            flash(f"Rejected {total} pending listing(s): " + "; ".join(per_type)
                  + ". Nothing without a price was touched.", "success")
        else:
            flash("No pending listing is priced below the minimums.", "success")
        return redirect(url_for("admin_listings", status="pending"))

    @app.route("/admin/listings/<int:listing_id>/delete", methods=["POST"])
    @login_required
    def admin_listing_delete(listing_id):
        listing = Listing.query.get_or_404(listing_id)
        # Collected before the delete, swept after the commit. The rows have to
        # be gone before unreferenced_uploads() can see them as unused.
        owned = listing.gallery() + ([listing.image_url] if listing.image_url else [])
        db.session.delete(listing)
        db.session.commit()
        loose = report_unreferenced(owned, f"listing {listing_id} deleted")
        flash("Listing deleted." + (f" {loose} image file(s) are now unused "
                                    f"and were kept on disk." if loose else ""),
              "success")
        return redirect(url_for("admin_listings"))

    return app


app = create_app()

if __name__ == "__main__":
    app.run(debug=True)
