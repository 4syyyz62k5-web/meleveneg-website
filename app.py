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
        ).filter(Compound.is_published == True, Compound.area.isnot(None)).group_by(Compound.area).all()
        top_areas = [{"name": r[0], "count": r[1]} for r in area_rows]

        return render_template("index.html", featured=featured, latest=latest, top_areas=top_areas)

    @app.route("/compounds")
    def compounds():
        area = request.args.get("area")
        query = Compound.query.filter_by(is_published=True)
        if area:
            query = query.filter_by(area=area)
        all_compounds = query.order_by(Compound.name.asc()).all()

        # Distinct areas for the filter dropdown
        areas = [row[0] for row in db.session.query(Compound.area).distinct() if row[0]]

        return render_template("compounds.html", compounds=all_compounds, areas=areas, selected_area=area)

    @app.route("/compound/<slug>")
    def compound_detail(slug):
        compound = Compound.query.filter_by(slug=slug, is_published=True).first_or_404()
        return render_template("compound_detail.html", compound=compound)

    @app.route("/about")
    def about():
        return render_template("about.html")

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
        all_leads = Lead.query.order_by(Lead.created_at.desc()).all()
        return render_template("admin/leads.html", leads=all_leads)

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
            c.developer = request.form.get("developer", "").strip()
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

        created, skipped = 0, 0
        for row in reader:
            compound_slug = (row.get("compound_slug") or "").strip()
            compound = Compound.query.filter_by(slug=compound_slug).first()
            if not compound:
                skipped += 1
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
        flash(f"Imported {created} unit(s). Skipped {skipped} row(s) with unknown compound_slug.", "success")
        return redirect(url_for("admin_dashboard"))

    return app


app = create_app()

if __name__ == "__main__":
    app.run(debug=True)
