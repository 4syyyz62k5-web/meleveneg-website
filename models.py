from flask_sqlalchemy import SQLAlchemy
from datetime import datetime
db = SQLAlchemy()
class Compound(db.Model):
    __tablename__ = "compounds"
    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(150), nullable=False)
    slug = db.Column(db.String(150), unique=True, nullable=False)  # used in the URL, e.g. /compound/silversands
    developer = db.Column(db.String(150))
    location = db.Column(db.String(150))  # top-level region, e.g. "New Cairo", "North Coast"
    area = db.Column(db.String(150))  # sub-area within the location, e.g. "Mostakbal City", "Sidi Heneish"
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
    is_launch = db.Column(db.Boolean, default=False)  # marks the WHOLE compound as a launch, as a shortcut to flagging every unit individually
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
    phase = db.Column(db.String(150))  # e.g. "Shore Residence", "Town Island", "Lagoon Town"
    delivery_year = db.Column(db.Integer)
    bedrooms = db.Column(db.Integer)
    bathrooms = db.Column(db.Integer)
    area_sqm = db.Column(db.Numeric(10, 2))
    price = db.Column(db.Numeric(14, 2))
    currency = db.Column(db.String(10), default="EGP")
    payment_plan = db.Column(db.String(255))  # e.g. "10% DP, 8 years installments"
    image_url = db.Column(db.String(500))  # falls back to the compound's cover image if empty
    is_available = db.Column(db.Boolean, default=True)
    is_launch = db.Column(db.Boolean, default=False)  # shows a "Launch" badge on nawy.com-style listings; drives the homepage "New Launches" section
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
    listing_id = db.Column(db.Integer, db.ForeignKey("listings.id"), nullable=True)
    listing = db.relationship("Listing")
    source_page = db.Column(db.String(255))  # which page the lead came from
    # Once Circles integration is built, this flags whether the lead has been pushed there
    synced_to_circles = db.Column(db.Boolean, default=False)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)


class Developer(db.Model):
    """A developer's display logo, looked up by matching Compound.developer (free text)
    against this table's `name`. Kept as its own small table rather than a foreign key
    on Compound, so nothing about existing compound data has to change to use it."""
    __tablename__ = "developers"
    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(150), unique=True, nullable=False)
    logo_url = db.Column(db.String(500))
    created_at = db.Column(db.DateTime, default=datetime.utcnow)


LISTING_TYPE_LABELS = {
    "resale": "Resale",
    "rent_annual": "For Rent — Annual",
    "rent_seasonal": "For Rent — Seasonal",
}

LEGAL_STATUS_LABELS = {
    "registered_title": "Registered Title",
    "court_validated": "Court Validated",
    "preliminary_contract": "Preliminary Contract",
    "unknown": "Unknown",
}


class Listing(db.Model):
    """A visitor-submitted Resale/Rent property, entirely separate from the
    Compound/Unit inventory (those are developer-sale projects; this is
    individually-owned units offered by their owner or an agent). New
    submissions land as status='pending' and are never shown publicly until
    an admin approves them via /admin/listings."""
    __tablename__ = "listings"
    id = db.Column(db.Integer, primary_key=True)

    listing_type = db.Column(db.String(20), nullable=False)  # resale / rent_annual / rent_seasonal
    status = db.Column(db.String(20), nullable=False, default="pending")  # pending / approved / rejected

    # Optional link to an existing Compound, for when the owner's unit sits
    # inside a development we already track (e.g. "Resale in Mountain View
    # Hyde Park"). Left null for anything else — most submissions won't have
    # a matching compound in the system, and that's expected, not an error.
    compound_id = db.Column(db.Integer, db.ForeignKey("compounds.id"), nullable=True)
    compound = db.relationship("Compound", backref="listings")

    title = db.Column(db.String(200), nullable=False)
    area = db.Column(db.String(150))
    location = db.Column(db.String(150))
    unit_type = db.Column(db.String(100))
    bedrooms = db.Column(db.Integer)
    bathrooms = db.Column(db.Integer)
    area_sqm = db.Column(db.Numeric(10, 2))

    price = db.Column(db.Numeric(14, 2))  # resale only
    rent_amount = db.Column(db.Numeric(14, 2))  # rent_annual / rent_seasonal
    rent_cadence = db.Column(db.String(20))  # free text, e.g. "Monthly", "Per Week"
    price_per_week = db.Column(db.Numeric(14, 2))  # rent_seasonal only
    high_season_multiplier = db.Column(db.Numeric(4, 2))  # rent_seasonal only
    currency = db.Column(db.String(10), default="EGP")

    furnishing = db.Column(db.String(20))  # unfurnished / semi / furnished
    condition = db.Column(db.String(50))
    legal_status = db.Column(db.String(30))  # registered_title / court_validated / preliminary_contract / unknown
    seller_type = db.Column(db.String(20))  # owner / agent
    negotiable = db.Column(db.Boolean, default=False)

    # Admin-only — never rendered on any public template.
    owner_name = db.Column(db.String(150), nullable=False)
    owner_phone = db.Column(db.String(50), nullable=False)

    submitted_at = db.Column(db.DateTime, default=datetime.utcnow)
    reviewed_at = db.Column(db.DateTime)
    reviewed_by = db.Column(db.String(150))  # free text — there's no per-admin-user login, just a shared password

    image_url = db.Column(db.String(500))

    def listing_type_label(self):
        return LISTING_TYPE_LABELS.get(self.listing_type, self.listing_type or "—")

    def legal_status_label(self):
        return LEGAL_STATUS_LABELS.get(self.legal_status, self.legal_status or "—")

    def price_display(self):
        if self.listing_type == "resale":
            if self.price:
                return f"{int(self.price):,} {self.currency}"
            return "Price on request"
        if self.listing_type == "rent_seasonal" and self.price_per_week:
            return f"{int(self.price_per_week):,} {self.currency} / week"
        if self.rent_amount:
            suffix = f" / {self.rent_cadence}" if self.rent_cadence else ""
            return f"{int(self.rent_amount):,} {self.currency}{suffix}"
        return "Price on request"
