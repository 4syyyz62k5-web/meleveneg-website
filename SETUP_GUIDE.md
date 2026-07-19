# Meleven Website — Setup Guide

## 1. Create the GitHub repo
1. Go to github.com > New repository > name it `meleveneg-website`
2. Set it to Private (or Public, your choice)
3. Upload all these files, keeping the folder structure exactly as-is:
   ```
   app.py
   config.py
   models.py
   seed.py
   requirements.txt
   templates/
     base.html
     index.html
     compounds.html
     compound_detail.html
     about.html
     contact.html
   static/
     css/style.css
     img/
       logo-white.png      (used in header/footer — navy background)
       logo-color.png       (spare — use on white/cream backgrounds if needed)
       icon-square.png      (used as favicon)
       placeholder.jpg      (add any neutral property photo here for cards without a real image)
   ```

### Note on fonts
Your official brand fonts are **Klavika Bold** (headings) and **Kiona Regular** (body) —
both paid fonts, so they can't be pulled from Google Fonts automatically. The site
currently uses **Montserrat** and **Jost** as close free stand-ins. If you own web-license
files for Klavika/Kiona, send them over and I'll wire them in with `@font-face` for an
exact match.

## 2. Create the Render Web Service
1. Render dashboard > New > Web Service
2. Connect it to the `meleveneg-website` GitHub repo
3. Settings:
   - **Build Command:** `pip install -r requirements.txt`
   - **Start Command:** `gunicorn app:app`
4. Add Environment Variables:
   - `SECRET_KEY` = any random long string
   - (leave `DATABASE_URL` — Render will inject it automatically once you attach a database)

## 3. Create the PostgreSQL Database
1. Render dashboard > New > PostgreSQL
2. Once created, go back to your Web Service > Environment
3. Render usually lets you link the database directly, which auto-fills `DATABASE_URL`.
   If not, copy the "Internal Database URL" from the Postgres page and paste it as
   the `DATABASE_URL` environment variable on your Web Service.
4. Deploy. Tables are created automatically on first run (via `db.create_all()`).

## 4. Add sample data (optional but recommended for first look)
1. On your Web Service page > Shell tab
2. Run: `python seed.py`
3. Refresh your site — you'll see "Silversands North Coast" as a sample listing.

## 5. Connect meleveneg.com (currently on GoDaddy)
You do NOT need to move the domain away from GoDaddy. You're only pointing its DNS at Render.

1. On Render: Web Service > Settings > Custom Domains > Add `meleveneg.com` and `www.meleveneg.com`
2. Render will show you DNS records to add (usually an A record + a CNAME, or just a CNAME for www)
3. Go to GoDaddy > My Products > DNS > Manage DNS for meleveneg.com
4. Add the exact records Render gave you (delete GoDaddy's default parking records if they conflict)
5. Wait for DNS propagation (10 minutes to a few hours)
6. Render will show a green checkmark once it verifies the domain and issues an SSL certificate automatically

## 6. Adding real compounds
Right now there's no admin panel — compounds are added directly via the database.
Two options going forward:
  - **Quick path:** I can give you a small admin page (password protected) to add/edit
    compounds without touching the database directly — recommended next step.
  - **Manual path:** Use Render's Shell tab with a Python script similar to `seed.py`.

## 7. Circles integration (future)
The `Lead` table in `models.py` already has a `synced_to_circles` flag and `source_page`
field ready for this. When you're ready, the plan is:
  - Either an API call from this site to Circles when a lead is submitted, or
  - A "Refer & Earn" link that sends visitors directly into Circles' referral flow
We'll build this once Circles' referral-intake side is ready to receive it.
