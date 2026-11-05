"""Shared setup for the test suites. Import this FIRST, before anything else.

Every suite needs the same three things done in the same order before `app` is
imported, and all three are order-sensitive:

  1. Environment variables. Config reads os.environ at class-definition time,
     so they have to be set before `from config import Config` runs inside
     app.py -- not after.

  2. The SQLite foreign-key PRAGMA. SQLite ignores foreign keys unless told
     otherwise; Postgres always enforces them. A real 500 escaped 195 passing
     tests because of exactly that gap: deleting a listing that had a Lead
     pointing at it violated leads.listing_id, production refused it, and
     every local suite committed it happily. The listener has to be registered
     BEFORE the first connection is made -- register it afterwards and
     `PRAGMA foreign_keys` reads back 0, which is how the first attempt at
     this convinced itself the bug did not exist.

  3. The app object. Use the MODULE-LEVEL one. app.py ends with
     `app = create_app()`, so importing it has already built an app and
     registered every @limiter.limit on the shared module-level limiter.
     Calling create_app() again registers them all a second time and the
     limiter then counts each request twice -- measured: 3 of 7 POSTs accepted
     via the module-level app (correct for "3 per minute"), 1 of 7 via a
     second create_app(). Gunicorn serves app:app, so the module-level object
     is the one production runs.

Everything here writes to throwaway temp directories. Nothing touches
instance/meleveneg.db or production.
"""

import os
import re
import sys
import tempfile
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent

# --- 1. environment, before app.py reads it -------------------------------
UPLOADS = tempfile.mkdtemp(prefix="meleven-test-uploads-")

os.environ.setdefault("SECRET_KEY", "test-secret")
os.environ.setdefault("ADMIN_PASSWORD", "testpw")
os.environ["UPLOAD_FOLDER"] = UPLOADS
os.environ["DATABASE_URL"] = (
    "sqlite:///" + os.path.join(tempfile.mkdtemp(prefix="meleven-test-db-"), "t.db"))
# FORM_MIN_SECONDS is deliberately NOT set here. test_spam.py tests the
# two-second gate itself and needs the real default; the other suites set it to
# 0 before importing this module, because otherwise every one of their
# submissions would sleep for two real seconds. Imposing 0 on all three from
# here silently disabled test_spam's timing assertions -- which is how this
# comment came to exist.
os.environ.setdefault("TRUSTED_CF_HOSTS", "localhost,meleveneg.com")

sys.path.insert(0, str(REPO_ROOT))
os.chdir(REPO_ROOT)

# --- 2. make SQLite behave like Postgres about foreign keys ---------------
from sqlalchemy import event as _sa_event          # noqa: E402
from sqlalchemy.engine import Engine as _SaEngine  # noqa: E402


@_sa_event.listens_for(_SaEngine, "connect")
def _enforce_sqlite_foreign_keys(dbapi_conn, _record):
    try:
        cur = dbapi_conn.cursor()
        cur.execute("PRAGMA foreign_keys=ON")
        cur.close()
    except Exception:
        pass        # not SQLite; Postgres needs no telling


# --- 3. the app -----------------------------------------------------------
import app as appmod                                # noqa: E402

application = appmod.app
application.config["WTF_CSRF_ENABLED"] = True
# Off by default: a suite makes far more submissions in a few seconds than any
# real limit allows, and a 429 is indistinguishable from a bug. The limits
# themselves are asserted explicitly in test_spam.py, which turns this back on
# for exactly those cases.
appmod.limiter.enabled = False


def assert_foreign_keys_enforced():
    """Fails loudly if the PRAGMA did not take. Called by each suite."""
    with application.app_context():
        from models import db
        on = db.session.execute(db.text("PRAGMA foreign_keys")).scalar()
    if on != 1:
        raise SystemExit(
            "PRAGMA foreign_keys is %r, not 1. The listener did not take, so "
            "this run would pass on constraint violations that production "
            "rejects. Check that support.py is imported before app." % (on,))


# --- small helpers every suite uses --------------------------------------
PASS, FAIL = [], []


def check(name, cond, detail=""):
    (PASS if cond else FAIL).append(name)
    print(f"  {'PASS' if cond else 'FAIL'}  {name}"
          + (f"  -- {detail}" if detail and not cond else ""))


def fields(client, path):
    """(csrf_token, form_ts) as rendered on a page. Either may be None."""
    html = client.get(path).get_data(as_text=True)
    t = re.search(r'name="csrf_token" value="([^"]+)"', html)
    f = re.search(r'name="form_ts" value="([^"]+)"', html)
    return (t.group(1) if t else None), (f.group(1) if f else None)


def login(client, password="testpw"):
    token, _ = fields(client, "/admin/login")
    return client.post("/admin/login",
                       data={"csrf_token": token, "password": password},
                       follow_redirects=True)


def finish(label):
    print(f"\n{'=' * 64}\n{label}: {len(PASS)} passed, {len(FAIL)} failed")
    if FAIL:
        print("FAILED:")
        for f in FAIL:
            print("  -", f)
    return 1 if FAIL else 0
