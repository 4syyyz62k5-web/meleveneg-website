"""
Run this once after your database is set up to add sample data,
so the site isn't empty on first launch. Delete or edit freely.

How to run on Render:
  Go to your service > Shell tab > run: python seed.py
"""

from app import create_app
from models import db, Compound, Unit

app = create_app()

with app.app_context():
    if Compound.query.filter_by(slug="silversands").first():
        print("Sample data already exists — skipping.")
    else:
        compound = Compound(
            name="Silversands North Coast",
            slug="silversands",
            developer="Ora Developers",
            area="North Coast",
            location_detail="Kilo 247, International Coastal Road, Sidi Heneish",
            short_description="A 506-acre luxury coastal resort by Ora Developers in Sidi Heneish.",
            full_description=(
                "Silversands North Coast is a flagship project by Ora Developers, "
                "spanning 506 acres in Sidi Heneish. The resort features a one-kilometer "
                "sandy beachfront, multiple clubhouses, retail zones, and several premium "
                "phases including The Cove, Acclaro, and Silvertown."
            ),
            min_price=4500000,
            max_price=25000000,
            land_area_acres=506,
            delivery_year=2028,
            cover_image_url="",
            is_featured=True,
            is_published=True,
        )
        db.session.add(compound)
        db.session.commit()

        db.session.add_all([
            Unit(compound_id=compound.id, unit_type="Chalet", bedrooms=2, area_sqm=110,
                 price=6500000, payment_plan="10% down, 8-year installments"),
            Unit(compound_id=compound.id, unit_type="Twin House", bedrooms=4, area_sqm=240,
                 price=14500000, payment_plan="15% down, 7-year installments"),
            Unit(compound_id=compound.id, unit_type="Standalone Villa", bedrooms=5, area_sqm=380,
                 price=24000000, payment_plan="20% down, 6-year installments"),
        ])
        db.session.commit()
        print("Sample compound 'Silversands North Coast' added.")
