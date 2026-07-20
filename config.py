import os

class Config:
    # Render provides DATABASE_URL automatically once you attach a PostgreSQL instance.
    # Locally / before you attach a DB, it falls back to SQLite so the site still runs.
    raw_db_url = os.environ.get("DATABASE_URL", "sqlite:///meleveneg.db")

    # Render's DATABASE_URL sometimes starts with "postgres://" but SQLAlchemy needs
    # an explicit driver. We use psycopg3 (the "psycopg" driver) for better compatibility
    # with newer Python versions than the older psycopg2 package.
    if raw_db_url.startswith("postgres://"):
        raw_db_url = raw_db_url.replace("postgres://", "postgresql+psycopg://", 1)
    elif raw_db_url.startswith("postgresql://"):
        raw_db_url = raw_db_url.replace("postgresql://", "postgresql+psycopg://", 1)

    SQLALCHEMY_DATABASE_URI = raw_db_url
    SQLALCHEMY_TRACK_MODIFICATIONS = False

    SECRET_KEY = os.environ.get("SECRET_KEY", "change-this-in-render-environment-variables")

    # Password to access /admin — set this as an environment variable on Render!
    ADMIN_PASSWORD = os.environ.get("ADMIN_PASSWORD", "changeme123")

    # Where uploaded photos are stored. This should point at a Render persistent disk
    # mount path (e.g. /var/uploads) so files survive redeploys. Falls back to a local
    # folder for testing if no persistent disk is attached yet.
    UPLOAD_FOLDER = os.environ.get("UPLOAD_FOLDER", "/var/uploads")

    # Placeholder — fill in once Circles integration is ready
    CIRCLES_APP_URL = os.environ.get("CIRCLES_APP_URL", "https://your-circles-app.onrender.com")
