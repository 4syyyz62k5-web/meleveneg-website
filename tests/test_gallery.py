"""Tests for multi-image listings: upload, caps, corrupt files, EXIF, GPS,
HEIC, ordering, cover choice, deletion-with-orphan-sweep, CSV, per-route size
limits, and the memory/time cost of six 12MP photos in one request.

Throwaway SQLite and a throwaway upload folder. Nothing touches the real DB.
"""

import io
import os
import re
import resource
import sys
import tempfile
import time
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





def jpeg(w=800, h=600, exif=None, colour=(120, 90, 60)):
    img = Image.new("RGB", (w, h), colour)
    buf = io.BytesIO()
    if exif is not None:
        img.save(buf, "JPEG", quality=90, exif=exif)
    else:
        img.save(buf, "JPEG", quality=90)
    img.close()
    buf.seek(0)
    return buf


def png_with_alpha(w=400, h=300):
    img = Image.new("RGBA", (w, h), (0, 0, 0, 0))
    buf = io.BytesIO()
    img.save(buf, "PNG")
    img.close()
    buf.seek(0)
    return buf


def rss_mb():
    ru = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    return ru / (1024 * 1024) if ru > 1 << 20 else ru / 1024


def n_files():
    return len([f for f in os.listdir(UPLOADS) if not f.startswith(".")])


def run_process(raw):
    """process_image() reads current_app.config, so it needs a context."""
    with application.app_context():
        return appmod.process_image(raw, UPLOADS)


c = application.test_client()
t = fields(c, "/admin/login")[0]
c.post("/admin/login", data={"csrf_token": t, "password": "testpw"},
       follow_redirects=True)

print("\n1. Upload endpoint allowlist resolves")
routes = {r.endpoint for r in application.url_map.iter_rules()}
bad = [e for e in appmod.UPLOAD_ENDPOINTS if e not in routes]
check("every name in UPLOAD_ENDPOINTS is a real route", not bad, str(bad))
check("there are 12 of them", len(appmod.UPLOAD_ENDPOINTS) == 12,
      str(len(appmod.UPLOAD_ENDPOINTS)))

print("\n2. Visitor form: three images -> three ordered rows")
ct, ft = fields(c, "/list-your-property")
r = c.post("/list-your-property", data={
    "csrf_token": ct, "form_ts": ft, "listing_type": "resale",
    "title": "Three photo flat", "owner_name": "O", "owner_phone": "1",
    "price": "5000000",
    "image_files": [(jpeg(), "a.jpg"), (jpeg(colour=(10, 180, 90)), "b.jpg"),
                    (jpeg(colour=(200, 40, 40)), "c.jpg")],
}, content_type="multipart/form-data", follow_redirects=True)
with application.app_context():
    from models import Listing, ListingImage
    l = Listing.query.filter_by(title="Three photo flat").first()
    check("listing created", l is not None)
    check("three image rows", l is not None and len(l.images) == 3,
          len(l.images) if l else "?")
    check("positions are 0,1,2",
          l is not None and [i.position for i in l.images] == [0, 1, 2],
          [i.position for i in l.images] if l else "?")
    check("image_url equals the first image (cover kept in sync)",
          l is not None and l.image_url == l.images[0].url)
    check("gallery() returns three", l is not None and len(l.gallery()) == 3)
    three_id = l.id if l else None
check("three files written to disk", n_files() == 3, n_files())

print("\n3. Visitor cap of 6")
before = n_files()
ct, ft = fields(c, "/list-your-property")
r = c.post("/list-your-property", data={
    "csrf_token": ct, "form_ts": ft, "listing_type": "resale",
    "title": "Seven photo flat", "owner_name": "O", "owner_phone": "1",
    "price": "5000000",
    "image_files": [(jpeg(), f"{i}.jpg") for i in range(7)],
}, content_type="multipart/form-data", follow_redirects=True)
with application.app_context():
    from models import Listing
    check("7 images refused, no listing created",
          Listing.query.filter_by(title="Seven photo flat").first() is None)
check("refusal names the cap", "at most 6 images" in r.get_data(as_text=True))
check("nothing written to disk for the refused batch", n_files() == before, n_files())

print("\n4. One corrupt file among good ones -> whole request refused")
before = n_files()
ct, ft = fields(c, "/list-your-property")
r = c.post("/list-your-property", data={
    "csrf_token": ct, "form_ts": ft, "listing_type": "resale",
    "title": "Mixed batch flat", "owner_name": "O", "owner_phone": "1",
    "price": "5000000",
    "image_files": [(jpeg(), "ok1.jpg"),
                    (io.BytesIO(b"\xff\xd8\xff" + b"garbage" * 50), "broken.jpg"),
                    (jpeg(), "ok2.jpg")],
}, content_type="multipart/form-data", follow_redirects=True)
body = r.get_data(as_text=True)
with application.app_context():
    from models import Listing
    check("no listing created", Listing.query.filter_by(title="Mixed batch flat").first() is None)
check("error names the bad file", "broken.jpg" in body, body[-300:])
check("error says nothing was saved", "Nothing was saved" in body)
check("NO partial files left on disk", n_files() == before, f"{n_files()} vs {before}")

print("\n5. EXIF orientation is applied, GPS is stripped")
exif = Image.Exif()
exif[274] = 6                      # Orientation: rotate 90 CW
# Pillow wants plain rationals here, not nested (num, den) tuples.
gps = {1: "N", 2: (30.0, 3.0, 0.0), 3: "E", 4: (31.0, 14.0, 0.0)}
exif[34853] = gps                  # GPSInfo
src = jpeg(w=800, h=400, exif=exif)
name, err = run_process(src.getvalue())
check("image with EXIF processed", err is None, str(err))
if name:
    with Image.open(os.path.join(UPLOADS, name)) as out:
        check("orientation applied (800x400 landscape became 400x800 portrait)",
              out.size == (400, 800), str(out.size))
        out_exif = out.getexif()
        check("no GPS tag in the output", 34853 not in out_exif, str(dict(out_exif)))
        check("no orientation tag left either", 274 not in out_exif)
    os.remove(os.path.join(UPLOADS, name))

print("\n6. Downscale and transparency")
name, err = run_process(jpeg(w=4000, h=3000).getvalue())
if name:
    with Image.open(os.path.join(UPLOADS, name)) as out:
        check("4000px wide becomes 1600px", out.size == (1600, 1200), str(out.size))
    os.remove(os.path.join(UPLOADS, name))
name, err = run_process(png_with_alpha().getvalue())
check("transparent PNG accepted", err is None, str(err))
if name:
    with Image.open(os.path.join(UPLOADS, name)) as out:
        px = out.convert("RGB").getpixel((5, 5))
        check("alpha flattened onto white, not black", px == (255, 255, 255), str(px))
    os.remove(os.path.join(UPLOADS, name))

print("\n7. HEIC")
# pillow-heif is not a production dependency any more: Render runs Python
# 3.14.3 and 0.22.0 has no cp314 wheel, so it broke the deploy. The library may
# still be installed locally, so this covers BOTH states -- and the refusal
# path is the one that matters, because that is what production does.
if appmod.HEIF_AVAILABLE:
    heic_buf = io.BytesIO()
    Image.new("RGB", (900, 600), (70, 130, 180)).save(heic_buf, "HEIF", quality=80)
    heic_bytes = heic_buf.getvalue()
    check("a real HEIC was produced for the test", heic_bytes[4:8] == b"ftyp",
          heic_bytes[:16].hex())
    check("sniff_image_type recognises it",
          appmod.sniff_image_type(io.BytesIO(heic_bytes)) == "heic")
    name, err = run_process(heic_bytes)
    check("HEIC processed into a JPEG",
          err is None and name and name.endswith(".jpg"), str(err))
    if name:
        os.remove(os.path.join(UPLOADS, name))
else:
    print("   pillow-heif not installed — testing the REFUSAL path instead")
    # A minimal but structurally real HEIC header: 'ftyp' at offset 4 with the
    # 'heic' brand, which is exactly what sniff_image_type matches on.
    heic_bytes = (b"\x00\x00\x00\x18ftypheic\x00\x00\x00\x00"
                  b"heicmif1" + b"\x00" * 64)
    check("sniff_image_type still recognises HEIC without the library",
          appmod.sniff_image_type(io.BytesIO(heic_bytes)) == "heic",
          str(appmod.sniff_image_type(io.BytesIO(heic_bytes))))
    ct, ft = fields(c, "/list-your-property")
    r = c.post("/list-your-property", data={
        "csrf_token": ct, "form_ts": ft, "listing_type": "resale",
        "title": "HEIC should be refused", "owner_name": "O",
        "owner_phone": "1", "price": "5000000",
        "image_files": [(io.BytesIO(heic_bytes), "IMG_4821.HEIC")],
    }, content_type="multipart/form-data", follow_redirects=True)
    body = r.get_data(as_text=True)
    with application.app_context():
        from models import Listing
        check("HEIC upload refused, no listing created",
              Listing.query.filter_by(title="HEIC should be refused").first() is None)
    check("the refusal names HEIC", "HEIC" in body)
    check("the refusal names a format that works",
          "JPG" in body and "PNG" in body, body[-300:])
    check("the refusal tells an iPhone user how to change it",
          "Most Compatible" in body)

print("\n8. Decompression bomb guard")
check("MAX_IMAGE_PIXELS set to the configured limit",
      Image.MAX_IMAGE_PIXELS == application.config["IMAGE_MAX_PIXELS"],
      str(Image.MAX_IMAGE_PIXELS))

print("\n9. Admin: ordering, cover choice, deletion + orphan sweep")
with application.app_context():
    from models import Listing
    cnt_before = n_files()
ct, ft = fields(c, "/admin/listings/new")
r = c.post("/admin/listings/new", data={
    "csrf_token": ct, "form_ts": ft, "listing_type": "resale",
    "title": "Admin gallery unit", "price": "9000000",
    "image_files": [(jpeg(colour=(1, 2, 3)), "one.jpg"),
                    (jpeg(colour=(4, 5, 6)), "two.jpg"),
                    (jpeg(colour=(7, 8, 9)), "three.jpg")],
}, content_type="multipart/form-data", follow_redirects=True)
with application.app_context():
    from models import Listing
    al = Listing.query.filter_by(title="Admin gallery unit").first()
    check("admin listing created with 3 images",
          al is not None and len(al.images) == 3, len(al.images) if al else "?")
    admin_id, urls = (al.id, [i.url for i in al.images]) if al else (None, [])
    ids = [i.id for i in al.images] if al else []

# reorder: make the third the cover, and delete the second
ct, ft = fields(c, f"/admin/listings/{admin_id}/edit")
r = c.post(f"/admin/listings/{admin_id}/edit", data={
    "csrf_token": ct, "form_ts": ft, "listing_type": "resale",
    "title": "Admin gallery unit", "price": "9000000",
    "owner_name": "O", "owner_phone": "1",
    f"position_{ids[0]}": "5", f"position_{ids[2]}": "1",
    f"delete_image_{ids[1]}": "on",
    "cover_url": urls[2],
}, content_type="multipart/form-data", follow_redirects=True)
with application.app_context():
    from models import Listing
    al = Listing.query.get(admin_id)
    remaining = [i.url for i in al.images]
    check("deleted image is gone from the gallery", urls[1] not in remaining)
    check("two images remain", len(remaining) == 2, len(remaining))
    check("chosen cover is first", remaining and remaining[0] == urls[2],
          str(remaining))
    check("image_url follows the chosen cover", al.image_url == urls[2])
deleted_name = os.path.basename(urls[1])
check("the removed image's FILE is KEPT on disk (report-only, never deleted)",
      os.path.exists(os.path.join(UPLOADS, deleted_name)))
check("the kept images' files are still there",
      all(os.path.exists(os.path.join(UPLOADS, os.path.basename(u)))
          for u in remaining))

print("\n10. Orphan detection covers ALL five image columns")
check("five columns are declared", len(appmod.IMAGE_URL_COLUMNS) == 5,
      str(appmod.IMAGE_URL_COLUMNS))
check("compound covers are among them",
      ("compounds", "cover_image_url") in appmod.IMAGE_URL_COLUMNS)
check("developer logos are among them",
      ("developers", "logo_url") in appmod.IMAGE_URL_COLUMNS)
check("unit photos are among them",
      ("units", "image_url") in appmod.IMAGE_URL_COLUMNS)
with application.app_context():
    from models import db, Compound, Developer, Unit
    # The exact bug this replaced: a file used as a compound cover, handed to
    # the orphan check as though a listing had just released it.
    shared = "/uploads/shared_cover.jpg"
    open(os.path.join(UPLOADS, "shared_cover.jpg"), "wb").write(b"x")
    comp = Compound(name="Shared Cover Co", slug="shared-cover",
                    is_published=True, cover_image_url=shared)
    db.session.add(comp)
    db.session.commit()
    check("a file used ONLY as a compound cover is not called an orphan",
          appmod.unreferenced_uploads([shared]) == [], 
          str(appmod.unreferenced_uploads([shared])))
    check("upload_references names the column that holds it",
          "compounds.cover_image_url" in appmod.upload_references(shared),
          str(appmod.upload_references(shared)))
    dev = Developer(name="Logo Co", logo_url="/uploads/logo_only.jpg")
    db.session.add(dev); db.session.commit()
    check("a developer logo is not called an orphan",
          appmod.unreferenced_uploads(["/uploads/logo_only.jpg"]) == [])
    u = Unit(compound_id=comp.id, unit_type="Villa",
             image_url="/uploads/unit_only.jpg")
    db.session.add(u); db.session.commit()
    check("a unit photo is not called an orphan",
          appmod.unreferenced_uploads(["/uploads/unit_only.jpg"]) == [])
    check("external URLs are never reported",
          appmod.unreferenced_uploads(["https://example.com/x.jpg"]) == [])
    check("a genuinely unused upload IS reported",
          appmod.unreferenced_uploads(["/uploads/nothing_points_here.jpg"])
          == ["/uploads/nothing_points_here.jpg"])
check("the compound cover file still exists after all that",
      os.path.exists(os.path.join(UPLOADS, "shared_cover.jpg")))

print("\n11. Deleting a listing removes its rows and KEEPS its files")
survivors = [os.path.basename(u) for u in remaining]
ct, ft = fields(c, "/admin/listings")
c.post(f"/admin/listings/{admin_id}/delete",
       data={"csrf_token": ct}, follow_redirects=True)
with application.app_context():
    from models import Listing, ListingImage
    check("listing gone", Listing.query.get(admin_id) is None)
    check("its image rows cascaded away",
          ListingImage.query.filter_by(listing_id=admin_id).count() == 0)
check("its files are still on disk (nothing is deleted in a request)",
      all(os.path.exists(os.path.join(UPLOADS, n)) for n in survivors))

print("\n12. Old single-image listings keep working")
with application.app_context():
    from models import db, Listing
    legacy = Listing(listing_type="resale", status="approved", source="admin",
                     title="Legacy one image", owner_name="O", owner_phone="1",
                     image_url="https://example.com/legacy.jpg", price=1000000)
    db.session.add(legacy)
    db.session.commit()
    check("gallery() falls back to image_url",
          legacy.gallery() == ["https://example.com/legacy.jpg"])
    check("cover() returns it", legacy.cover() == "https://example.com/legacy.jpg")
    check("image_count() is 1", legacy.image_count() == 1)
    legacy_id = legacy.id
page = c.get(f"/listing/{legacy_id}").get_data(as_text=True)
check("its detail page renders", "Legacy one image" in page)
check("no gallery strip for a single image", "lg-strip" not in page)

print("\n13. Detail page shows a gallery when there are several")
with application.app_context():
    from models import db, Listing
    Listing.query.get(three_id).status = "approved"
    db.session.commit()
page = c.get(f"/listing/{three_id}").get_data(as_text=True)
check("gallery strip present", "lg-strip" in page)
check("three thumbnails", page.count('class="lg-thumb') == 3,
      page.count('class="lg-thumb'))
listings_page = c.get("/resale").get_data(as_text=True)
check("card shows a photo count badge", "card-photo-count" in listings_page)

print("\n14. CSV import with an images column")
csv_body = ("title,listing_type,price,images\n"
            "CSV gallery unit,resale,8000000,"
            "https://x.test/1.jpg|https://x.test/2.jpg|https://x.test/3.jpg\n"
            "CSV single unit,resale,7000000,https://x.test/only.jpg\n")
ct, ft = fields(c, "/admin/listings")
c.post("/admin/listings/import", data={
    "csrf_token": ct,
    "csv_file": (io.BytesIO(csv_body.encode()), "l.csv"),
}, content_type="multipart/form-data", follow_redirects=True)
with application.app_context():
    from models import Listing
    g = Listing.query.filter_by(title="CSV gallery unit").first()
    check("pipe-separated images imported", g is not None and len(g.images) == 3,
          len(g.images) if g else "?")
    check("first becomes the cover",
          g is not None and g.image_url == "https://x.test/1.jpg")
    s1 = Listing.query.filter_by(title="CSV single unit").first()
    check("single image via the images column works",
          s1 is not None and len(s1.images) == 1)

print("\n15. Per-route body limits")
small = application.config["MAX_CONTENT_LENGTH"]
big = application.config["MAX_UPLOAD_CONTENT_LENGTH"]
visitor = application.config["MAX_VISITOR_UPLOAD_CONTENT_LENGTH"]
check(f"small limit is {small // 1048576} MB", small == 2 * 1048576)
check(f"admin upload limit is {big // 1048576} MB", big == 64 * 1048576)
check(f"visitor upload limit is {visitor // 1048576} MB", visitor == 40 * 1048576)
check("the public route is capped below the admin one", visitor < big)
for ep in sorted(appmod.UPLOAD_ENDPOINTS):
    rule = next(r for r in application.url_map.iter_rules() if r.endpoint == ep)
    path = str(rule).replace("<int:listing_id>", "1").replace("<int:compound_id>", "1") \
                    .replace("<int:unit_id>", "1").replace("<int:developer_id>", "1") \
                    .replace("<slug>", "x")
    with application.test_request_context(path, method="POST"):
        from flask import request as rq
        got = rq.max_content_length
    expected = visitor if ep in appmod.VISITOR_UPLOAD_ENDPOINTS else big
    check(f"{ep} gets its upload limit "
          f"({expected // 1048576} MB)", got == expected, f"{got}")
for path in ("/contact", "/sell", "/compound/x/interested"):
    with application.test_request_context(path):
        from flask import request as rq
        got = rq.max_content_length
    check(f"{path} held to the small limit", got == small, f"{got}")

print("\n16. Six 12MP photos in ONE request: time and memory")
bench = os.path.join(tempfile.mkdtemp(), "bench.py")
with open(bench, "w") as fh:
    fh.write(f"""
import io, os, re, resource, sys, tempfile, time
UP = {UPLOADS!r}
os.environ.update(SECRET_KEY="t", ADMIN_PASSWORD="testpw", UPLOAD_FOLDER=UP,
                  FORM_MIN_SECONDS="0",
                  DATABASE_URL={os.environ['DATABASE_URL']!r})
sys.path.insert(0, {os.getcwd()!r})
import app as appmod
from PIL import Image
a = appmod.create_app(); a.config["WTF_CSRF_ENABLED"] = True
appmod.limiter.enabled = False
c = a.test_client()
h = c.get("/admin/login").get_data(as_text=True)
t = re.search(r'name="csrf_token" value="([^"]+)"', h).group(1)
c.post("/admin/login", data={{"csrf_token": t, "password": "testpw"}}, follow_redirects=True)
shots = []
for i in range(6):
    # effect_noise is incompressible, so the JPEG is a realistic size.
    n = Image.effect_noise((4000, 3000), 48).convert("RGB")
    b = io.BytesIO(); n.save(b, "JPEG", quality=92); n.close(); b.seek(0)
    shots.append(b)
payload = sum(len(b.getvalue()) for b in shots)
h = c.get("/admin/listings/new").get_data(as_text=True)
ct = re.search(r'name="csrf_token" value="([^"]+)"', h).group(1)
m = re.search(r'name="form_ts" value="([^"]+)"', h)
ft = m.group(1) if m else ""   # admin forms have no timing gate
base = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
t0 = time.perf_counter()
r = c.post("/admin/listings/new", data={{
    "csrf_token": ct, "form_ts": ft, "listing_type": "resale",
    "title": "Six twelve megapixel", "price": "9000000",
    "image_files": [(b, "big%d.jpg" % i) for i, b in enumerate(shots)],
}}, content_type="multipart/form-data", follow_redirects=True)
elapsed = time.perf_counter() - t0
peak = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
with a.app_context():
    from models import Listing
    l = Listing.query.filter_by(title="Six twelve megapixel").first()
    n_imgs = len(l.images) if l else 0
    stored = sum(os.path.getsize(os.path.join(UP, os.path.basename(i.url)))
                 for i in l.images) if l else 0
scale = 1048576 if peak > (1 << 20) else 1024
print("RESULT %d %.3f %.1f %.1f %d %d" % (
    n_imgs, elapsed, base / scale, peak / scale, payload, stored))
""")
import subprocess
out = subprocess.run([sys.executable, bench], capture_output=True, text=True)
line = [l for l in out.stdout.splitlines() if l.startswith("RESULT")]
if not line:
    check("six 12MP photos processed in one request", False, out.stderr[-400:])
else:
    n_imgs, elapsed, base, peak, payload, stored = line[0].split()[1:]
    n_imgs, elapsed = int(n_imgs), float(elapsed)
    base, peak = float(base), float(peak)
    payload, stored = int(payload), int(stored)
    print(f"   payload       : {payload / 1048576:.1f} MB "
          f"({payload / 6 / 1048576:.1f} MB each)")
    print(f"   stored        : {stored / 1048576:.2f} MB "
          f"({stored / 6 / 1024:.0f} KB each)")
    print(f"   request time  : {elapsed:.2f}s   (gunicorn default timeout 30s)")
    print(f"   peak RSS      : {peak:.0f} MB  (process baseline {base:.0f} MB, "
          f"so the request cost about {peak - base:.0f} MB)")
    check("all six accepted in one request", n_imgs == 6, str(n_imgs))
    check("well inside the gunicorn 30s timeout", elapsed < 10, f"{elapsed:.2f}s")
    check("request memory cost stays under 200 MB", peak - base < 200,
          f"{peak - base:.0f} MB")

print("\n17. Spam gate still intact on the changed form")
ct, ft = fields(c, "/list-your-property")
r = c.post("/list-your-property", data={
    "csrf_token": ct, "form_ts": ft, "listing_type": "resale",
    "title": "Honeypot with photos", "owner_name": "O", "owner_phone": "1",
    "price": "5000000", appmod.HONEYPOT_FIELD: "x",
    "image_files": [(jpeg(), "a.jpg")],
}, content_type="multipart/form-data", follow_redirects=True)
with application.app_context():
    from models import Listing
    check("honeypot still blocks (silently) with images attached",
          Listing.query.filter_by(title="Honeypot with photos").first() is None)
r = c.post("/list-your-property", data={
    "listing_type": "resale", "title": "No token with photos",
    "owner_name": "O", "owner_phone": "1",
    "image_files": [(jpeg(), "a.jpg")],
}, content_type="multipart/form-data", follow_redirects=True)
with application.app_context():
    from models import Listing
    check("CSRF still enforced with images attached",
          Listing.query.filter_by(title="No token with photos").first() is None)
ct, ft = fields(c, "/list-your-property")
r = c.post("/list-your-property", data={
    "csrf_token": ct, "form_ts": ft, "listing_type": "resale",
    "title": "Cqgnhdtqpj", "owner_name": "O", "owner_phone": "1",
    "price": "5000000", "image_files": [(jpeg(), "a.jpg")],
}, content_type="multipart/form-data", follow_redirects=True)
with application.app_context():
    from models import Listing
    check("gibberish still refused with images attached",
          Listing.query.filter_by(title="Cqgnhdtqpj").first() is None)


print("\n18. Deleting rows that other tables point at (the 500 that escaped)")
# These two are why the PRAGMA above exists. Both of them passed on SQLite
# with foreign keys off and 500'd on Postgres.
with application.app_context():
    from models import db, Listing, ListingImage, Lead, Compound, Unit
    l = Listing(listing_type="resale", status="approved", source="admin",
                title="FK delete target", owner_name="O", owner_phone="1",
                price=5000000)
    db.session.add(l)
    db.session.flush()
    for i in range(3):
        fn = f"fkdel_{i}.jpg"
        open(os.path.join(UPLOADS, fn), "wb").write(b"x")
        db.session.add(ListingImage(listing_id=l.id, url=f"/uploads/{fn}", position=i))
    l.image_url = "/uploads/fkdel_0.jpg"
    for k in range(2):
        db.session.add(Lead(name=f"FK asker {k}", phone="9", listing_id=l.id,
                            source_page=f"listing:{l.id}"))
    db.session.commit()
    fk_id = l.id
t = tok2 = None
html = c.get("/admin/listings").get_data(as_text=True)
import re as _re
tok2 = _re.search(r'name="csrf_token" value="([^"]+)"', html).group(1)
r = c.post(f"/admin/listings/{fk_id}/delete", data={"csrf_token": tok2},
            follow_redirects=True)
check("deleting a listing with images AND leads does not 500",
      r.status_code == 200, f"HTTP {r.status_code}")
with application.app_context():
    from models import Listing, ListingImage, Lead
    check("the listing is gone", Listing.query.get(fk_id) is None)
    check("its listing_images rows are gone",
          ListingImage.query.filter_by(listing_id=fk_id).count() == 0)
    kept = Lead.query.filter(Lead.name.like("FK asker%")).all()
    check("both leads were KEPT, not deleted", len(kept) == 2, str(len(kept)))
    check("their listing_id was nulled",
          all(k.listing_id is None for k in kept))
    check("their source_page still names the listing",
          all(f"listing:{fk_id}" == (k.source_page or "") for k in kept))
check("all three image files are still on disk",
      all(os.path.exists(os.path.join(UPLOADS, f"fkdel_{i}.jpg")) for i in range(3)))

with application.app_context():
    from models import db, Compound, Unit, Lead, Listing
    comp = Compound(name="FK compound", slug="fk-compound", is_published=True)
    db.session.add(comp)
    db.session.flush()
    db.session.add(Unit(compound_id=comp.id, unit_type="Villa"))
    db.session.add(Lead(name="FK compound asker", phone="9",
                        compound_id=comp.id, source_page="compound:fk-compound"))
    db.session.add(Listing(listing_type="resale", status="approved",
                           source="admin", title="FK linked listing",
                           owner_name="O", owner_phone="1",
                           compound_id=comp.id))
    db.session.commit()
    comp_id = comp.id
html = c.get("/admin/listings").get_data(as_text=True)
tok2 = _re.search(r'name="csrf_token" value="([^"]+)"', html).group(1)
r = c.post(f"/admin/compounds/{comp_id}/delete", data={"csrf_token": tok2},
            follow_redirects=True)
check("deleting a compound with a unit AND a lead does not 500",
      r.status_code == 200, f"HTTP {r.status_code}")
with application.app_context():
    from models import Compound, Unit, Lead, Listing
    check("the compound is gone", Compound.query.get(comp_id) is None)
    check("its unit cascaded away",
          Unit.query.filter_by(compound_id=comp_id).count() == 0)
    cl = Lead.query.filter_by(name="FK compound asker").first()
    check("its lead was kept and detached",
          cl is not None and cl.compound_id is None)
    li = Listing.query.filter_by(title="FK linked listing").first()
    check("a listing that pointed at it survives with compound_id nulled",
          li is not None and li.compound_id is None)

sys.exit(finish("gallery"))
