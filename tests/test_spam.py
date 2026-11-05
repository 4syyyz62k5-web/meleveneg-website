"""Tests for the spam defences: IP keying, price floors, gibberish, honeypot,
timing, link handling, rate limits, bulk reject, and the newly-gated forms.

Throwaway SQLite, Flask test client. Nothing touches the real DB.
"""

import re
import sys
import time
# Shared setup lives in tests/support.py and MUST be imported before anything
# touches `app`: it sets the environment Config reads at import time, registers
# the SQLite foreign-key PRAGMA before the first connection, and hands back the
# module-level app. See the module docstring there for why each of those three
# is order-sensitive.
import support
from support import (application, appmod, check, fields, finish, login,
                     UPLOADS, assert_foreign_keys_enforced)

assert_foreign_keys_enforced()





def tok(client, path):
    html = client.get(path).get_data(as_text=True)
    m = re.search(r'name="csrf_token" value="([^"]+)"', html)
    return m.group(1) if m else None


def ts(client, path):
    html = client.get(path).get_data(as_text=True)
    m = re.search(r'name="form_ts" value="([^"]+)"', html)
    return m.group(1) if m else None


def listing_count(title):
    with application.app_context():
        from models import Listing
        return Listing.query.filter_by(title=title).count()


def submit(client, extra, path="/list-your-property", sleep=True):
    t, stamp = tok(client, path), ts(client, path)
    if sleep:
        time.sleep(application.config["FORM_MIN_SECONDS"] + 0.3)
    data = {"csrf_token": t, "form_ts": stamp, "listing_type": "resale",
            "owner_name": "Real Person", "owner_phone": "01000000000"}
    data.update(extra)
    return client.post(path, data=data, follow_redirects=True)


c = application.test_client()

print("\n0. IP keying: CF-Connecting-IP only believed on a trusted host")
with application.test_request_context(
        "/", base_url="https://meleveneg.com",
        headers={"CF-Connecting-IP": "9.9.9.9", "X-Forwarded-For": "8.8.8.8"}):
    check("trusted host -> uses CF-Connecting-IP",
          appmod.client_ip() == "9.9.9.9", appmod.client_ip())
with application.test_request_context(
        "/", base_url="https://meleveneg-website.onrender.com",
        headers={"CF-Connecting-IP": "9.9.9.9"}):
    got = appmod.client_ip()
    check("untrusted host -> header IGNORED (spoof-proof)", got != "9.9.9.9", got)
with application.test_request_context(
        "/", base_url="https://meleveneg.com",
        headers={"X-Forwarded-For": "7.7.7.7, 1.1.1.1"}):
    check("trusted host, no CF header -> leftmost XFF",
          appmod.client_ip() == "7.7.7.7", appmod.client_ip())

print("\n1. Price floors (visitor)")
r = submit(c, {"title": "Nice flat in Maadi", "price": "73"})
check("73 EGP sale refused", listing_count("Nice flat in Maadi") == 0)
check("refusal explains the floor", "look right" in r.get_data(as_text=True))
r = submit(c, {"title": "Proper flat in Maadi", "price": "100000"})
check("100,000 EGP sale accepted", listing_count("Proper flat in Maadi") == 1)
r = submit(c, {"title": "Monthly rental in Zamalek",
               "listing_type": "rent_annual", "rent_amount": "9000"})
check("9,000 EGP monthly rent accepted (not held to the sale floor)",
      listing_count("Monthly rental in Zamalek") == 1)
r = submit(c, {"title": "Silly rent in Zamalek",
               "listing_type": "rent_annual", "rent_amount": "25"})
check("25 EGP rent refused", listing_count("Silly rent in Zamalek") == 0)
r = submit(c, {"title": "No price given here", "price": ""})
check("blank price still accepted (absence is not a low price)",
      listing_count("No price given here") == 1)

print("\n2. Gibberish")
for bad, label in [("Mzdmvx", "no vowels, 6 chars"),
                   ("Cqgnhdtqpj", "no vowels, 10 chars"),
                   ("Qwrtplkjhgfdszx", "15 chars unspaced"),
                   ("aBcDeFgH", "alternating case")]:
    submit(c, {"title": bad, "price": "500000"})
    check(f"title '{bad}' refused ({label})", listing_count(bad) == 0)
for good in ["Badya", "PX", "Palm Hills New Cairo", "Hacienda Bay",
             "شقة في الساحل الشمالي", "فيلا للبيع", "SODIC West", "PK2 Katameya"]:
    submit(c, {"title": good, "price": "5000000"})
    check(f"title '{good}' accepted", listing_count(good) == 1,
          f"count={listing_count(good)}")
submit(c, {"title": "Fine title", "area": "Mzdmvx", "price": "5000000"})
check("gibberish in AREA refused too", listing_count("Fine title") == 0)

print("\n3a. Honeypot -> silent accept, nothing written")
r = submit(c, {"title": "Honeypot caught me", "price": "5000000",
               appmod.HONEYPOT_FIELD: "http://spam.ru"})
check("honeypot row NOT written", listing_count("Honeypot caught me") == 0)
check("submitter still told it succeeded",
      "pending review" in r.get_data(as_text=True))

print("\n3b. Timing")
t, stamp = tok(c, "/list-your-property"), ts(c, "/list-your-property")
r = c.post("/list-your-property", data={
    "csrf_token": t, "form_ts": stamp, "listing_type": "resale",
    "title": "Too fast to be human", "owner_name": "X", "owner_phone": "1",
    "price": "5000000"}, follow_redirects=True)
check("instant submission NOT written", listing_count("Too fast to be human") == 0)
check("instant submission answered as success (silent)",
      "pending review" in r.get_data(as_text=True))
r = submit(c, {"title": "Human paced entry", "price": "5000000"})
check("submission after the delay accepted",
      listing_count("Human paced entry") == 1)
t = tok(c, "/list-your-property")
r = c.post("/list-your-property", data={
    "csrf_token": t, "form_ts": "forged-not-signed", "listing_type": "resale",
    "title": "Forged timestamp", "owner_name": "X", "owner_phone": "1",
    "price": "5000000"}, follow_redirects=True)
check("unsigned form_ts refused", listing_count("Forged timestamp") == 0)

print("\n4. Rate limit, keyed per client IP")
appmod.limiter.enabled = True
appmod.limiter.reset()
codes_same, codes_diff = [], []
BU = "https://meleveneg.com"
for i in range(6):
    rl = application.test_client()
    page = rl.get("/list-your-property", base_url=BU).get_data(as_text=True)
    t = re.search(r'name="csrf_token" value="([^"]+)"', page).group(1)
    stamp = re.search(r'name="form_ts" value="([^"]+)"', page).group(1)
    codes_same.append(rl.post("/list-your-property", data={
        "csrf_token": t, "form_ts": stamp, "listing_type": "resale",
        "title": f"RL same {i}", "owner_name": "X", "owner_phone": "1"},
        base_url="https://meleveneg.com",
        headers={"CF-Connecting-IP": "5.5.5.5",
                 "Referer": "https://meleveneg.com/list-your-property"}).status_code)
check("same IP is throttled within the hour", 429 in codes_same, str(codes_same))
# The exact count matters, not just that throttling happens. Read under a
# doubled limiter registration this came out as 1, which would have concealed
# a limit half as permissive as the configured "3 per minute".
check("exactly 3 accepted, matching '3 per minute'",
      codes_same.count(302) == 3, str(codes_same))
appmod.limiter.reset()
for i in range(6):
    rl = application.test_client()
    page = rl.get("/list-your-property", base_url=BU).get_data(as_text=True)
    t = re.search(r'name="csrf_token" value="([^"]+)"', page).group(1)
    stamp = re.search(r'name="form_ts" value="([^"]+)"', page).group(1)
    codes_diff.append(rl.post("/list-your-property", data={
        "csrf_token": t, "form_ts": stamp, "listing_type": "resale",
        "title": f"RL diff {i}", "owner_name": "X", "owner_phone": "1"},
        base_url="https://meleveneg.com",
        headers={"CF-Connecting-IP": f"5.5.5.{i + 10}",
                 "Referer": "https://meleveneg.com/list-your-property"}).status_code)
check("different IPs each get their own budget", 429 not in codes_diff, str(codes_diff))
appmod.limiter.enabled = False
appmod.limiter.reset()

print("\n5. Links")
submit(c, {"title": "Buy now http://spam.ru", "price": "5000000"})
check("link in title refused", listing_count("Buy now http://spam.ru") == 0)
submit(c, {"title": "Clean title", "area": "www.spam.ru", "price": "5000000"})
check("link in area refused", listing_count("Clean title") == 0)
t, stamp = tok(c, "/contact"), ts(c, "/contact")
time.sleep(application.config["FORM_MIN_SECONDS"] + 0.3)
c.post("/contact", data={"csrf_token": t, "form_ts": stamp, "name": "Link Leaver",
                         "phone": "0100", "message": "see http://example.com/x"},
       follow_redirects=True)
with application.app_context():
    from models import Lead
    ld = Lead.query.filter_by(name="Link Leaver").first()
    check("link in MESSAGE is kept, not refused", ld is not None)
    check("and it is flagged for review", ld is not None and ld.flagged, )
    check("flag reason recorded",
          ld is not None and "link" in (ld.flag_reason or ""), getattr(ld, "flag_reason", "?"))
t, stamp = tok(c, "/contact"), ts(c, "/contact")
time.sleep(application.config["FORM_MIN_SECONDS"] + 0.3)
c.post("/contact", data={"csrf_token": t, "form_ts": stamp, "name": "No Links",
                         "phone": "0100", "message": "just a question"},
       follow_redirects=True)
with application.app_context():
    from models import Lead
    ld = Lead.query.filter_by(name="No Links").first()
    check("plain message not flagged", ld is not None and not ld.flagged)

print("\n7. The previously-unprotected forms")
t, stamp = tok(c, "/sell"), ts(c, "/sell")
time.sleep(application.config["FORM_MIN_SECONDS"] + 0.3)
with application.app_context():
    from models import Lead
    before = Lead.query.count()
r = c.post("/sell", data={"csrf_token": t, "form_ts": stamp},
           follow_redirects=True)
with application.app_context():
    from models import Lead
    check("/sell empty POST creates NO lead", Lead.query.count() == before)
check("/sell says name and phone are required",
      "name and phone number are required" in r.get_data(as_text=True))
t, stamp = tok(c, "/sell"), ts(c, "/sell")
time.sleep(application.config["FORM_MIN_SECONDS"] + 0.3)
c.post("/sell", data={"csrf_token": t, "form_ts": stamp, "name": "Seller S",
                      "phone": "0101"}, follow_redirects=True)
with application.app_context():
    from models import Lead
    check("/sell with name+phone works",
          Lead.query.filter_by(name="Seller S").first() is not None)
t, stamp = tok(c, "/sell"), ts(c, "/sell")
time.sleep(application.config["FORM_MIN_SECONDS"] + 0.3)
c.post("/sell", data={"csrf_token": t, "form_ts": stamp, "name": "Honey Sell",
                      "phone": "1", appmod.HONEYPOT_FIELD: "x"},
       follow_redirects=True)
with application.app_context():
    from models import Lead
    check("/sell honeypot blocks silently",
          Lead.query.filter_by(name="Honey Sell").first() is None)

print("\n   honeypot + form_ts present on every public form")
for path in ("/list-your-property", "/sell", "/contact"):
    html = c.get(path).get_data(as_text=True)
    check(f"{path} renders both", appmod.HONEYPOT_FIELD in html and "form_ts" in html)

print("\n   AI endpoints are rate-limited now")
appmod.limiter.enabled = True
appmod.limiter.reset()
for rule, payload in (("/api/chat", {"message": "hi", "history": []}),
                      ("/api/smart-search", {"query": "hi"})):
    codes = []
    for i in range(9):
        ac = application.test_client()
        page = ac.get("/", base_url="https://meleveneg.com").get_data(as_text=True)
        m = re.search(r"'X-CSRFToken': '([^']+)'", page)
        codes.append(ac.post(rule, json=payload, base_url="https://meleveneg.com",
                             headers={"X-CSRFToken": m.group(1) if m else "",
                                      "CF-Connecting-IP": "6.6.6.6",
                                      "Referer": "https://meleveneg.com/"}).status_code)
    check(f"{rule} throttles a loop from one IP", 429 in codes, str(codes))
    appmod.limiter.reset()
appmod.limiter.enabled = False

print("\n6. Bulk reject")
tc = application.test_client()
t = tok(tc, "/admin/login")
tc.post("/admin/login", data={"csrf_token": t, "password": "testpw"},
        follow_redirects=True)
with application.app_context():
    from models import db, Listing
    for title, price in [("BULK one", 5000000), ("BULK two", 6000000),
                         ("BULK three", 7000000)]:
        db.session.add(Listing(listing_type="resale", status="pending",
                               source="visitor", title=title, price=price,
                               owner_name="O", owner_phone="1"))
    db.session.add(Listing(listing_type="resale", status="pending",
                           source="visitor", title="CHEAP spam", price=73,
                           owner_name="O", owner_phone="1"))
    db.session.add(Listing(listing_type="resale", status="pending",
                           source="visitor", title="NOPRICE pending",
                           owner_name="O", owner_phone="1"))
    db.session.commit()
    ids = [Listing.query.filter_by(title=t2).first().id
           for t2 in ("BULK one", "BULK two")]
t = tok(tc, "/admin/listings")
tc.post("/admin/listings/bulk-reject",
        data={"csrf_token": t, "listing_ids": [str(i) for i in ids],
              "reviewed_by": "Mahmoud"}, follow_redirects=True)
with application.app_context():
    from models import Listing
    check("selected rows rejected",
          all(Listing.query.get(i).status == "rejected" for i in ids))
    check("unselected row untouched",
          Listing.query.filter_by(title="BULK three").first().status == "pending")
t = tok(tc, "/admin/listings")
r = tc.post("/admin/listings/reject-under-minimum",
            data={"csrf_token": t, "reviewed_by": "Mahmoud"}, follow_redirects=True)
with application.app_context():
    from models import Listing
    check("under-minimum pending row rejected",
          Listing.query.filter_by(title="CHEAP spam").first().status == "rejected")
    check("priced-above row still pending",
          Listing.query.filter_by(title="BULK three").first().status == "pending")
    check("row with NO price left alone",
          Listing.query.filter_by(title="NOPRICE pending").first().status == "pending")
check("report names what was rejected", "under 100,000" in r.get_data(as_text=True))

sys.exit(finish("spam"))
