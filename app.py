from flask import Flask, render_template, request, redirect, url_for, flash
from config import Config
from models import db, Compound, Unit, Lead

def create_app():
    app = Flask(__name__)
    app.config.from_object(Config)
    db.init_app(app)

    with app.app_context():
        db.create_all()

    # ---------- Public pages ----------

    @app.route("/")
    def home():
        featured = Compound.query.filter_by(is_featured=True, is_published=True).limit(6).all()
        latest = Compound.query.filter_by(is_published=True).order_by(Compound.created_at.desc()).limit(8).all()
        return render_template("index.html", featured=featured, latest=latest)

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
            flash("تم استلام طلبك! هيتواصل معاك فريق Meleven قريب.", "success")
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
        flash("تم استلام طلبك بخصوص المشروع! هيتواصل معاك فريق Meleven قريب.", "success")
        return redirect(url_for("compound_detail", slug=slug))

    return app


app = create_app()

if __name__ == "__main__":
    app.run(debug=True)
