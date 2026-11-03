import os

class Config:
    # Render provides DATABASE_URL automatically once you attach a PostgreSQL instance.
    # Locally / before you attach a DB, it falls back to SQLite so the site still runs.
    raw_db_url = os.environ.get("DATABASE_URL", "sqlite:///meleveneg.db")

    # Render's DATABASE_URL sometimes starts with "postgres://" but SQLAlchemy needs
    # an explicit driver. We use psycopg3 (the "psycopg" driver) for better compatibility
    # with newer Python versions than the older psycopg2 package.
    if raw_db_url.startswith("postgres://"):
        raw_db_url = raw_db_url.replace("postgres://", "postgresql+psycopg://", 1)
    elif raw_db_url.startswith("postgresql://"):
        raw_db_url = raw_db_url.replace("postgresql://", "postgresql+psycopg://", 1)

    SQLALCHEMY_DATABASE_URI = raw_db_url
    SQLALCHEMY_TRACK_MODIFICATIONS = False

    SECRET_KEY = os.environ.get("SECRET_KEY", "change-this-in-render-environment-variables")

    # Password to access /admin — set this as an environment variable on Render!
    ADMIN_PASSWORD = os.environ.get("ADMIN_PASSWORD", "changeme123")

    # Where uploaded photos are stored. This should point at a Render persistent disk
    # mount path (e.g. /var/uploads) so files survive redeploys. Falls back to a local
    # folder for testing if no persistent disk is attached yet.
    UPLOAD_FOLDER = os.environ.get("UPLOAD_FOLDER", "/var/uploads")

    # Two ceilings, not one. Six phone photos in a single request is 30-50 MB,
    # so an 8 MB cap made the gallery impossible; raising it site-wide would
    # instead let any form anywhere accept 48 MB of anything.
    #
    # So: the big number applies only on the endpoints that actually take a
    # file (see UPLOAD_ENDPOINTS in app.py), and every other route is held to
    # the small one. app.py enforces this with a Request subclass that reads
    # the endpoint being dispatched.
    # 64, not 48. Measured: six 4000x3000 JPEGs at quality 92 come to about
    # 50 MB, and the first attempt at 48 rejected exactly the batch this
    # feature exists for, with a 413 and no clue that one fewer photo would
    # have worked. Werkzeug spools a body this size to a temp file rather
    # than holding it in memory, and the images are then processed one at a
    # time, so the ceiling costs disk during the request and not RAM.
    # Three ceilings, because the three groups of routes are not comparable.
    #
    # Admin forms sit behind a login and may legitimately carry twenty images,
    # so they get the largest. /list-your-property is open to anyone on the
    # internet and is capped at six images, so it gets a smaller one -- 40 MB
    # is six 6-7 MB phone photos, and a public route should not be able to ask
    # the server to buffer 64 MB. Everything else takes no file at all.
    MAX_UPLOAD_CONTENT_LENGTH = int(os.environ.get("MAX_UPLOAD_MB", "64")) * 1024 * 1024
    MAX_VISITOR_UPLOAD_CONTENT_LENGTH = int(
        os.environ.get("MAX_VISITOR_UPLOAD_MB", "40")) * 1024 * 1024
    MAX_CONTENT_LENGTH = int(os.environ.get("MAX_REQUEST_MB", "2")) * 1024 * 1024

    # --- Image processing -------------------------------------------------
    # Longest edge after downscaling. 1600 is wide enough for a full-bleed
    # gallery on a retina laptop and small enough that six of them cost about
    # a megabyte in total.
    IMAGE_MAX_WIDTH = int(os.environ.get("IMAGE_MAX_WIDTH", "1600"))
    IMAGE_JPEG_QUALITY = int(os.environ.get("IMAGE_JPEG_QUALITY", "80"))
    # 40 MP is above any phone camera and far below Pillow's own 89 MP
    # default. The limit exists for decompression bombs: a 100 KB PNG can
    # describe a 2 GB bitmap, and the cost is paid on decode, not on upload.
    IMAGE_MAX_PIXELS = int(os.environ.get("IMAGE_MAX_PIXELS", str(40_000_000)))

    # How many images one listing may carry.
    LISTING_MAX_IMAGES_VISITOR = int(os.environ.get("LISTING_MAX_IMAGES_VISITOR", "6"))
    LISTING_MAX_IMAGES_ADMIN = int(os.environ.get("LISTING_MAX_IMAGES_ADMIN", "20"))

    # No expiry at all, rather than Flask-WTF's default hour.
    #
    # The token is rendered into the page once, at load. The chatbot and the
    # smart-search box live in that same page and POST from it whenever the
    # visitor gets round to typing -- which can be the next morning, on a tab
    # that was never reloaded. Any finite limit turns that into a silent
    # failure on a page that looks perfectly fine, and the visitor has no way
    # to know a refresh would fix it.
    #
    # What is given up is small: a token with no clock still only works
    # alongside the matching session cookie, and that cookie expires when the
    # browser session ends. The limit was never the thing protecting these
    # endpoints -- the per-IP rate limits are.
    WTF_CSRF_TIME_LIMIT = None

    # Placeholder — fill in once Circles integration is ready
    CIRCLES_APP_URL = os.environ.get("CIRCLES_APP_URL", "https://your-circles-app.onrender.com")

    # Base URL used to build absolute links in /sitemap.xml (deliberately not
    # derived from the incoming request's Host header, so it's always this
    # domain regardless of how the app was reached).
    SITE_URL = os.environ.get("SITE_URL", "https://meleveneg.com")

    # --- Spam defences for the public forms -------------------------------
    #
    # Every threshold here is an env var because the first version of any of
    # these numbers is a guess, and the cost of guessing wrong is a real
    # submission refused. Tuning one must not need a deploy.

    # Floor prices, per listing type -- a single floor could not work, because
    # the field being checked changes with the type and a 100k floor on a
    # monthly rent would refuse genuine ones. The spam seen so far (7, 25, 67
    # and 73 EGP) falls under all three.
    LISTING_MIN_PRICE_EGP = int(os.environ.get("LISTING_MIN_PRICE_EGP", "100000"))
    LISTING_MIN_RENT_EGP = int(os.environ.get("LISTING_MIN_RENT_EGP", "3000"))
    LISTING_MIN_WEEKLY_EGP = int(os.environ.get("LISTING_MIN_WEEKLY_EGP", "2000"))

    # Gibberish detection thresholds. See is_gibberish() in app.py.
    # 2, not 3: "PX" is a real compound on this site. A one-character title
    # is not plausible, a two-character one demonstrably is -- and the cost
    # of the stricter number is refusing a genuine listing by name.
    TEXT_MIN_LENGTH = int(os.environ.get("TEXT_MIN_LENGTH", "2"))
    TEXT_MAX_CONSONANT_RUN = int(os.environ.get("TEXT_MAX_CONSONANT_RUN", "5"))
    TEXT_MAX_UNSPACED = int(os.environ.get("TEXT_MAX_UNSPACED", "14"))
    TEXT_MAX_CASE_FLIPS = int(os.environ.get("TEXT_MAX_CASE_FLIPS", "3"))

    # Minimum seconds between a form being rendered and submitted. A human
    # cannot fill the listing form in two seconds; a script does it in
    # milliseconds.
    FORM_MIN_SECONDS = int(os.environ.get("FORM_MIN_SECONDS", "2"))
    # How long a rendered form stays submittable. Generous on purpose: a tab
    # left open overnight is a person, not an attack, and an expired form says
    # so rather than being silently dropped.
    FORM_MAX_AGE_SECONDS = int(os.environ.get("FORM_MAX_AGE_SECONDS", str(48 * 3600)))

    # Per-IP limits. 5/hour rather than 3 because mobile carriers put many
    # subscribers behind one CGNAT address, so a shared IP is normal traffic
    # and not evidence of anything.
    RATE_LIMIT_LISTING = os.environ.get("RATE_LIMIT_LISTING", "5 per hour; 3 per minute")
    RATE_LIMIT_LEAD = os.environ.get("RATE_LIMIT_LEAD", "10 per hour; 4 per minute")
    # The two AI endpoints spend money per call, so they get their own budget.
    RATE_LIMIT_AI = os.environ.get("RATE_LIMIT_AI", "30 per hour; 6 per minute")

    # Hosts that genuinely sit behind Cloudflare, and therefore the only ones
    # whose CF-Connecting-IP header can be believed. See client_ip() in app.py
    # for why this list exists and what goes wrong without it.
    TRUSTED_CF_HOSTS = {
        h.strip().lower()
        for h in os.environ.get(
            "TRUSTED_CF_HOSTS", "meleveneg.com,www.meleveneg.com").split(",")
        if h.strip()
    }

    # Claude API key for /api/chat (the property-search chatbot). Get one at
    # https://console.anthropic.com/settings/keys — set as a real environment
    # variable on Render; locally, .env fills this in (see load_dotenv() in app.py).
    CLAUDE_API_KEY = os.environ.get("CLAUDE_API_KEY", "")
