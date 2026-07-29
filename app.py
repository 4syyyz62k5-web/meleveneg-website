import re
import csv
import io
import os
import uuid
from functools import wraps
from flask import Flask, render_template, request, redirect, url_for, flash, session, send_from_directory
from werkzeug.utils import secure_filename
from config import Config
from models import db, Compound, Unit, Lead

ALLOWED_IMAGE_EXTENSIONS = {"png", "jpg", "jpeg", "webp", "gif"}


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
        return {"footer_areas": [{"name": r[0], "count": r[1]} for r in rows]}

    # ---------- Uploaded file serving ----------

    @app.route("/uploads/<path:filename>")
    def uploaded_file(filename):
        return send_from_directory(app.config["UPLOAD_FOLDER"], filename)

    # ---------- Public pages ----------

    @app.route("/")
    def home():
        featured = Compound.query.filter_by(is_featured=True, is_published=True).limit(6).all()
        latest = Compound.query.filter_by(is_published=True).order_by(Compound.created_at.desc()).limit(8).all()

        area_rows = db.session.query(
            Compound.area, db.func.count(Compound.id)
        ).filter(Compound.is_published == True, Compound.area.isnot(None)).group_by(Compound.area).order_by(Compound.area).all()

        top_areas = []
        for area_name, count in area_rows:
            # Grab one compound in this area that has a cover image, to represent it visually
            sample = (
                Compound.query
                .filter(
                    Compound.area == area_name,
                    Compound.is_published == True,
                    Compound.cover_image_url.isnot(None),
                    Compound.cover_image_url != "",
                )
                .order_by(Compound.is_featured.desc(), Compound.created_at.desc())
                .first()
            )
            top_areas.append({
                "name": area_name,
                "count": count,
                "cover_image_url": sample.cover_image_url if sample else None,
            })

        # Show at most this many as folder cards; anything beyond that is
        # only reachable via the "View all areas" dropdown so the two lists
        # never repeat each other.
        CARD_LIMIT = 6
        all_locations_sorted = top_areas
        top_areas = all_locations_sorted[:CARD_LIMIT]

        # "New Launches" — soonest delivery first, most recently added as tiebreaker
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
            if row[0]
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
        )

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
        min_price = request.args.get("min_price", "").strip()
        max_price = request.args.get("max_price", "").strip()
        delivery_year = request.args.get("delivery_year", "").strip()
        search_query = request.args.get("q", "").strip()

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
            min_price=min_price,
            max_price=max_price,
            selected_delivery_year=delivery_year,
            search_query=search_query,
        )

    @app.route("/compound/<slug>")
    def compound_detail(slug):
        compound = Compound.query.filter_by(slug=slug, is_published=True).first_or_404()
        return render_template("compound_detail.html", compound=compound)

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
            flash("Thanks! One of our agents will call you shortly to help sell your property.", "success")
            return redirect(url_for("sell_property"))

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

    return app


app = create_app()

if __name__ == "__main__":
    app.run(debug=True)
