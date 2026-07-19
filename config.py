import os

class Config:
    # Render provides DATABASE_URL automatically once you attach a PostgreSQL instance.
    # Locally / before you attach a DB, it falls back to SQLite so the site still runs.
    raw_db_url = os.environ.get("DATABASE_URL", "sqlite:///meleveneg.db")

    # Render's DATABASE_URL sometimes starts with "postgres://" but SQLAlchemy needs "postgresql://"
    if raw_db_url.startswith("postgres://"):
        raw_db_url = raw_db_url.replace("postgres://", "postgresql://", 1)

    SQLALCHEMY_DATABASE_URI = raw_db_url
    SQLALCHEMY_TRACK_MODIFICATIONS = False

    SECRET_KEY = os.environ.get("SECRET_KEY", "change-this-in-render-environment-variables")

    # Placeholder — fill in once Circles integration is ready
    CIRCLES_APP_URL = os.environ.get("CIRCLES_APP_URL", "https://your-circles-app.onrender.com")
