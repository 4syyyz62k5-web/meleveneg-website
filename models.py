from flask_sqlalchemy import SQLAlchemy
from datetime import datetime

db = SQLAlchemy()


class Compound(db.Model):
    __tablename__ = "compounds"

    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(150), nullable=False)
    slug = db.Column(db.String(150), unique=True, nullable=False)  # used in the URL, e.g. /compound/silversands
    developer = db.Column(db.String(150))
    area = db.Column(db.String(150))  # e.g. "North Coast", "New Cairo"
    location_detail = db.Column(db.String(255))  # e.g. "Kilo 247, International Coastal Road"

    short_description = db.Column(db.String(500))
    full_description = db.Column(db.Text)

    min_price = db.Column(db.Numeric(14, 2))
    max_price = db.Column(db.Numeric(14, 2))
    currency = db.Column(db.String(10), default="EGP")

    land_area_acres = db.Column(db.Numeric(10, 2))
    delivery_year = db.Column(db.Integer)

    cover_image_url = db.Column(db.String(500))
    contact_phone = db.Column(db.String(50))       # for the "Call" button
    contact_whatsapp = db.Column(db.String(50))     # for the "WhatsApp" button (digits only, e.g. 201234567890)
    is_featured = db.Column(db.Boolean, default=False)
    is_published = db.Column(db.Boolean, default=True)

    created_at = db.Column(db.DateTime, default=datetime.utcnow)

    units = db.relationship("Unit", backref="compound", lazy=True, cascade="all, delete-orphan")

    def price_range_display(self):
        if self.min_price and self.max_price:
            return f"{int(self.min_price):,} - {int(self.max_price):,} {self.currency}"
        elif self.min_price:
            return f"Starting {int(self.min_price):,} {self.currency}"
        return "Price on request"

    def bedrooms_range(self):
        """Returns e.g. '2 - 5 Beds' based on available units, or None if no unit data."""
        beds = [u.bedrooms for u in self.units if u.bedrooms]
        if not beds:
            return None
        lo, hi = min(beds), max(beds)
        return f"{lo} Bed" if lo == hi else f"{lo} - {hi} Beds"

    def bathrooms_range(self):
        baths = [u.bathrooms for u in self.units if u.bathrooms]
        if not baths:
            return None
        lo, hi = min(baths), max(baths)
        return f"{lo} Bath" if lo == hi else f"{lo} - {hi} Baths"


class Unit(db.Model):
    __tablename__ = "units"

    id = db.Column(db.Integer, primary_key=True)
    compound_id = db.Column(db.Integer, db.ForeignKey("compounds.id"), nullable=False)

    unit_type = db.Column(db.String(100))  # Chalet, Villa, Townhouse, Apartment...
    bedrooms = db.Column(db.Integer)
    bathrooms = db.Column(db.Integer)
    area_sqm = db.Column(db.Numeric(10, 2))
    price = db.Column(db.Numeric(14, 2))
    currency = db.Column(db.String(10), default="EGP")
    payment_plan = db.Column(db.String(255))  # e.g. "10% DP, 8 years installments"
    image_url = db.Column(db.String(500))  # falls back to the compound's cover image if empty

    is_available = db.Column(db.Boolean, default=True)


class Lead(db.Model):
    """Captures inquiries from the Contact / 'Interested' forms.
    Later, this is the table that feeds referrals into Circles."""
    __tablename__ = "leads"

    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(150), nullable=False)
    phone = db.Column(db.String(50), nullable=False)
    email = db.Column(db.String(150))
    message = db.Column(db.Text)

    compound_id = db.Column(db.Integer, db.ForeignKey("compounds.id"), nullable=True)
    source_page = db.Column(db.String(255))  # which page the lead came from

    # Once Circles integration is built, this flags whether the lead has been pushed there
    synced_to_circles = db.Column(db.Boolean, default=False)

    created_at = db.Column(db.DateTime, default=datetime.utcnow)
