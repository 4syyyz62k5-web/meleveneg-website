"""End-to-end local test of the resale flow and the CSRF/upload hardening.

Uses Flask's test client against a throwaway SQLite file, so nothing touches
the real instance/meleveneg.db or production.
"""

import io
import re
import sys
from PIL import Image

# Shared setup lives in tests/support.py and MUST be imported before anything
# touches `app`: it sets the environment Config reads at import time, registers
# the SQLite foreign-key PRAGMA before the first connection, and hands back the
# module-level app. See the module docstring there for why each of those three
# is order-sensitive.
import os

# Before support is imported, because Config reads the environment at
# import time. The timing gate has its own tests in test_spam.py; making
# every case here wait two real seconds would add a minute for nothing.
# form_ts is still sent and still validated.
os.environ["FORM_MIN_SECONDS"] = "0"

import support
from support import (application, appmod, check, fields, finish, login,
                     UPLOADS, assert_foreign_keys_enforced)

assert_foreign_keys_enforced()





def token(client, path):
    html = client.get(path).get_data(as_text=True)
    m = re.search(r'name="csrf_token" value="([^"]+)"', html)
    return m.group(1) if m else None


def stamp(client, path):
    """The signed form_ts a public form now renders. Without it the gate
    treats the submission as a script and drops it silently."""
    html = client.get(path).get_data(as_text=True)
    m = re.search(r'name="form_ts" value="([^"]+)"', html)
    return m.group(1) if m else None


# A GENUINE PNG, not just the 8-byte signature plus padding. The old fixture
# was signature-only and passed because validation was magic-bytes-only too;
# process_image() now decodes with Pillow, which correctly rejects it. The
# weaker fixture would have been testing the weaker check.
def _real_png():
    import io as _io
    from PIL import Image as _Image
    b = _io.BytesIO()
    _Image.new("RGB", (60, 40), (90, 120, 150)).save(b, "PNG")
    return b.getvalue()


PNG = _real_png()

with application.app_context():
    from models import db, Compound
    db.session.add(Compound(name="Testville", slug="testville",
                            developer="Test Dev", is_published=True))
    db.session.commit()

c = application.test_client()

print("\n1. CSRF is actually enforced")
r = c.post("/contact", data={"name": "X", "phone": "1"})
check("POST /contact with no token is refused", r.status_code in (302, 303, 400),
      f"got {r.status_code}")
r = c.post("/list-your-property", data={"listing_type": "resale", "title": "T",
                                        "owner_name": "O", "owner_phone": "1"})
check("POST /list-your-property with no token is refused",
      r.status_code in (302, 303, 400), f"got {r.status_code}")

print("\n2. Public forms still work WITH a token")
t = token(c, "/contact")
check("/contact renders a csrf token", bool(t))
r = c.post("/contact", data={"csrf_token": t, "form_ts": stamp(c, "/contact"), "name": "Lead One",
                             "phone": "0100", "message": "hi"},
           follow_redirects=True)
check("/contact accepts a tokened POST", r.status_code == 200)

print("\n3. Visitor submission lands pending and invisible")
t = token(c, "/list-your-property")
r = c.post("/list-your-property", data={
    "csrf_token": t, "form_ts": stamp(c, "/list-your-property"), "listing_type": "resale", "title": "Visitor Flat",
    "owner_name": "Mr Owner", "owner_phone": "01234567890",
    "owner_email": "owner@example.com", "area": "Maadi", "price": "5000000",
    "furnishing": "furnished", "condition": "Good",
    "legal_status": "registered_title", "seller_type": "owner",
}, follow_redirects=True)
check("visitor submission accepted", r.status_code == 200)
with application.app_context():
    from models import Listing
    l = Listing.query.filter_by(title="Visitor Flat").first()
    check("stored with status=pending", l is not None and l.status == "pending",
          getattr(l, "status", "missing"))
    check("stored with source=visitor", l is not None and l.source == "visitor",
          getattr(l, "source", "missing"))
    visitor_id = l.id
body = c.get("/resale").get_data(as_text=True)
check("pending listing absent from /resale", "Visitor Flat" not in body)
check("/resale empty state is the no-listings one, not the filter one",
      "No properties listed here yet" in body)
check("owner name absent from /resale", "Mr Owner" not in body)
r = c.get(f"/listing/{visitor_id}")
check("pending listing detail 404s", r.status_code == 404, f"got {r.status_code}")

print("\n4. Whitelists reject junk")
t = token(c, "/list-your-property")
r = c.post("/list-your-property", data={
    "csrf_token": t, "form_ts": stamp(c, "/list-your-property"), "listing_type": "resale", "title": "Junk Enum",
    "owner_name": "O", "owner_phone": "1", "legal_status": "<script>x</script>",
}, follow_redirects=True)
with application.app_context():
    from models import Listing
    check("row with bad legal_status not created",
          Listing.query.filter_by(title="Junk Enum").first() is None)

print("\n5. Upload: real content check, not the extension")
t = token(c, "/list-your-property")
fake = io.BytesIO(b"<html>not an image at all</html>")
r = c.post("/list-your-property", data={
    "csrf_token": t, "form_ts": stamp(c, "/list-your-property"), "listing_type": "resale", "title": "Fake Image",
    "owner_name": "O", "owner_phone": "1",
    "image_file": (fake, "payload.jpg"),
}, content_type="multipart/form-data", follow_redirects=True)
with application.app_context():
    from models import Listing
    check("renamed non-image rejected, listing not created",
          Listing.query.filter_by(title="Fake Image").first() is None)
check("rejection reason shown to the submitter",
      "not an image" in r.get_data(as_text=True).lower())

t = token(c, "/list-your-property")
r = c.post("/list-your-property", data={
    "csrf_token": t, "form_ts": stamp(c, "/list-your-property"), "listing_type": "resale", "title": "Real Image",
    "owner_name": "O", "owner_phone": "1",
    "image_file": (io.BytesIO(PNG), "photo.png"),
}, content_type="multipart/form-data", follow_redirects=True)
with application.app_context():
    from models import Listing
    ri = Listing.query.filter_by(title="Real Image").first()
    check("genuine PNG accepted", ri is not None and bool(ri.image_url),
          getattr(ri, "image_url", "missing"))

t = token(c, "/list-your-property")
r = c.post("/list-your-property", data={
    "csrf_token": t, "form_ts": stamp(c, "/list-your-property"), "listing_type": "resale", "title": "Mislabelled",
    "owner_name": "O", "owner_phone": "1",
    "image_file": (io.BytesIO(PNG), "photo.webp"),
}, content_type="multipart/form-data", follow_redirects=True)
with application.app_context():
    from models import Listing
    check("PNG bytes named .webp rejected",
          Listing.query.filter_by(title="Mislabelled").first() is None)

print("\n6. Admin login, then add a listing that publishes immediately")
t = token(c, "/admin/login")
r = c.post("/admin/login", data={"csrf_token": t, "password": "testpw"},
           follow_redirects=True)
check("admin login works with a token", r.status_code == 200)
form_html = c.get("/admin/listings/new").get_data(as_text=True)
check("/admin/listings/new renders", "New Resale / Rent Listing" in form_html)
check("owner_email input present on the admin form",
      'name="owner_email"' in form_html)
check("condition is a select, not free text",
      '<select name="condition">' in form_html)
t = re.search(r'name="csrf_token" value="([^"]+)"', form_html).group(1)
with application.app_context():
    from models import Compound
    cid = Compound.query.filter_by(slug="testville").first().id
r = c.post("/admin/listings/new", data={
    "csrf_token": t, "listing_type": "resale", "title": "My Own Unit",
    "compound_id": str(cid), "area": "Testville", "price": "9000000",
    "area_sqm": "180", "bedrooms": "3", "furnishing": "semi",
    "condition": "Excellent", "legal_status": "registered_title",
    "seller_type": "owner", "owner_name": "Mahmoud", "owner_phone": "0111",
    "owner_email": "m@example.com",
}, follow_redirects=True)
with application.app_context():
    from models import Listing
    mine = Listing.query.filter_by(title="My Own Unit").first()
    check("admin listing created", mine is not None)
    check("published immediately (status=approved)",
          mine is not None and mine.status == "approved", getattr(mine, "status", "?"))
    check("source=admin", mine is not None and mine.source == "admin",
          getattr(mine, "source", "?"))
    check("linked to the compound", mine is not None and mine.compound_id == cid)
    check("owner_email saved", mine is not None and mine.owner_email == "m@example.com")
    mine_id = mine.id

print("\n7. It is visible publicly at once, owner details are not")
pub = application.test_client()
body = pub.get("/resale").get_data(as_text=True)
check("admin listing appears on /resale immediately", "My Own Unit" in body)
check("owner name not on /resale", "Mahmoud" not in body)
check("owner phone not on /resale", "0111" not in body)
detail = pub.get(f"/listing/{mine_id}").get_data(as_text=True)
check("listing detail renders", "My Own Unit" in detail)
check("owner name not on the detail page", "Mahmoud" not in detail)
check("owner phone not on the detail page", "0111" not in detail)
check("owner email not on the detail page", "m@example.com" not in detail)

print("\n8. Interested button records a Lead tied to the listing")
t = token(pub, f"/listing/{mine_id}")
check("detail page carries a csrf token", bool(t))
r = pub.post(f"/listing/{mine_id}/interested", data={
    "csrf_token": t, "form_ts": stamp(pub, f"/listing/{mine_id}"),
    "name": "Buyer B", "phone": "0999",
    "message": "I want this"}, follow_redirects=True)
with application.app_context():
    from models import Lead
    lead = Lead.query.filter_by(name="Buyer B").first()
    check("lead created", lead is not None)
    check("lead linked to the listing",
          lead is not None and lead.listing_id == mine_id,
          getattr(lead, "listing_id", "?"))

print("\n9. Approving the visitor listing publishes it, owner stays hidden")
t = token(c, f"/admin/listings/{visitor_id}")
r = c.post(f"/admin/listings/{visitor_id}/approve",
           data={"csrf_token": t, "reviewed_by": "Mahmoud"}, follow_redirects=True)
with application.app_context():
    from models import Listing
    v = Listing.query.get(visitor_id)
    check("visitor listing approved", v.status == "approved")
    check("source still visitor after approval", v.source == "visitor", v.source)
body = pub.get("/resale").get_data(as_text=True)
check("approved visitor listing now on /resale", "Visitor Flat" in body)
check("owner name still hidden after approval", "Mr Owner" not in body)
d = pub.get(f"/listing/{visitor_id}").get_data(as_text=True)
check("owner phone hidden on approved detail page", "01234567890" not in d)
check("owner email hidden on approved detail page", "owner@example.com" not in d)

print("\n10. Nav/footer: Resale back, Rent still hidden")
home = pub.get("/").get_data(as_text=True)
check("Resale link present", 'href="/resale"' in home)
check("Rent link still absent", 'href="/rent"' not in home)
check("/rent still reachable directly", pub.get("/rent").status_code == 200)

print("\n11. CSV import publishes immediately and reports problems")
csv_body = (
    "title,listing_type,compound_slug,area,price,furnishing,condition,legal_status,seller_type,negotiable\n"
    "CSV Unit One,resale,testville,Testville,7000000,furnished,Good,registered_title,owner,true\n"
    "CSV Unit Two,resale,no-such-slug,Nowhere,8000000,,,,,\n"
    ",resale,testville,Skipped,1,,,,,\n"
    "CSV Bad Enum,resale,testville,X,1,wat,,,,\n"
)
t = token(c, "/admin/listings")
r = c.post("/admin/listings/import", data={
    "csrf_token": t,
    "csv_file": (io.BytesIO(csv_body.encode()), "listings.csv"),
}, content_type="multipart/form-data", follow_redirects=True)
flash = r.get_data(as_text=True)
with application.app_context():
    from models import Listing
    one = Listing.query.filter_by(title="CSV Unit One").first()
    two = Listing.query.filter_by(title="CSV Unit Two").first()
    bad = Listing.query.filter_by(title="CSV Bad Enum").first()
    check("good CSV row imported and approved",
          one is not None and one.status == "approved" and one.source == "admin")
    check("good CSV row linked by compound_slug", one is not None and one.compound_id == cid)
    check("unmatched slug imported UNLINKED, not dropped",
          two is not None and two.compound_id is None)
    check("bad-enum row rejected", bad is None)
check("import reported the skipped no-title row", "Skipped 1 row" in flash)
check("import reported the unknown slug", "no-such-slug" in flash)
check("import reported the rejected row", "Rejected" in flash)

print("\n12. Sitemap carries approved listings only")
sm = pub.get("/sitemap.xml").get_data(as_text=True)
check(f"approved admin listing in sitemap", f"/listing/{mine_id}" in sm)
with application.app_context():
    from models import Listing
    pend = Listing(listing_type="resale", status="pending", source="visitor",
                   title="Never Approved", owner_name="N", owner_phone="1")
    db.session.add(pend)
    db.session.commit()
    pend_id = pend.id
sm = pub.get("/sitemap.xml").get_data(as_text=True)
check("pending listing absent from sitemap", f"/listing/{pend_id}" not in sm)

print("\n13. The other fetch POST endpoints still accept their header token")
page = pub.get("/").get_data(as_text=True)
m = re.search(r"'X-CSRFToken': '([^']+)'", page)
check("X-CSRFToken rendered into the page JS", bool(m))
if m:
    r = pub.post("/api/chat", json={"message": "hi", "history": []},
                 headers={"X-CSRFToken": m.group(1)})
    # 503 = no CLAUDE_API_KEY configured locally, which still proves CSRF passed.
    check("/api/chat passes CSRF with the header", r.status_code in (200, 502, 503),
          f"got {r.status_code}")
    r = pub.post("/api/chat", json={"message": "hi", "history": []})
    check("/api/chat without the header is refused", r.status_code in (400, 302, 303),
          f"got {r.status_code}")

print("\n14. The rate limit itself")
appmod.limiter.enabled = True
rl = application.test_client()
codes = []
for i in range(5):
    tt = token(rl, "/list-your-property")
    codes.append(rl.post("/list-your-property", data={
        "csrf_token": tt, "form_ts": stamp(rl, "/list-your-property"),
        "listing_type": "resale", "title": f"RL {i}",
        "owner_name": "O", "owner_phone": "1"}).status_code)
check("4th rapid submission is throttled (429)", 429 in codes, str(codes))
check("first submissions are not throttled", codes[0] != 429, str(codes))
appmod.limiter.enabled = False

sys.exit(finish("resale"))
