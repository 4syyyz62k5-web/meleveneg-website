#!/usr/bin/env python3
"""
Lists files in UPLOAD_FOLDER that no row in the database references any more.

LISTS. It deletes nothing unless you pass --delete, and --delete additionally
requires --yes-i-have-a-backup, because the thing being removed is the only
copy: the Render disk is not backed up and an image deleted here is gone.

WHY THIS IS A SCRIPT AND NOT PART OF A REQUEST. An earlier version swept files
automatically when a listing was edited or deleted, and checked only two of
the five columns that can hold an image URL. Upload a compound cover, paste
the same /uploads/... path into a listing's "Or Photo URL", delete the
listing, and the compound's cover file was deleted while the compound still
pointed at it -- a broken image on a live page, unrecoverable. The lesson was
not "check five columns instead of two"; it was that deleting the only copy of
a file should never happen as a side effect of saving a form.

So the request path only ever REPORTS (see report_unreferenced in app.py), and
removal is this: run by hand, output read by a human first.

    export DATABASE_URL='postgres://...'
    export UPLOAD_FOLDER=/var/uploads

    python3 scripts/list_orphan_uploads.py                 # list, change nothing
    python3 scripts/list_orphan_uploads.py --delete --yes-i-have-a-backup

Files newer than --min-age-hours (default 24) are never listed: an upload that
is mid-request, or one belonging to a form a human has not submitted yet, has
no row pointing at it and is not an orphan.
"""

import argparse
import os
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from app import IMAGE_URL_COLUMNS, create_app  # noqa: E402
from models import db  # noqa: E402


def main(argv=None):
    p = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--delete", action="store_true",
                   help="Actually remove the files. Needs --yes-i-have-a-backup.")
    p.add_argument("--yes-i-have-a-backup", dest="confirmed", action="store_true",
                   help="Required alongside --delete. The Render disk has no "
                        "backup; a file removed here cannot be recovered.")
    p.add_argument("--min-age-hours", type=int, default=24,
                   help="Ignore files younger than this (default: 24). A file "
                        "being uploaded right now has no row yet.")
    args = p.parse_args(argv)

    if args.delete and not args.confirmed:
        raise SystemExit(
            "--delete also requires --yes-i-have-a-backup. Read the listing "
            "first: every file below is the only copy there is.")

    app = create_app()
    with app.app_context():
        folder = app.config["UPLOAD_FOLDER"]
        if not os.path.isdir(folder):
            raise SystemExit(f"UPLOAD_FOLDER does not exist: {folder}")

        # One pass per column, collecting every /uploads/ basename in use.
        referenced = set()
        per_column = {}
        for table, column in IMAGE_URL_COLUMNS:
            rows = db.session.execute(db.text(
                f"SELECT {column} FROM {table} "
                f"WHERE {column} IS NOT NULL AND {column} <> ''")).all()
            names = {os.path.basename(r[0]) for r in rows
                     if str(r[0]).startswith("/uploads/")}
            per_column[f"{table}.{column}"] = len(names)
            referenced |= names

        print(f"UPLOAD_FOLDER: {folder}")
        print("references found per column:")
        for key, n in per_column.items():
            print(f"  {key:<34} {n}")
        print(f"  {'distinct files referenced':<34} {len(referenced)}")

        cutoff = datetime.now(timezone.utc) - timedelta(hours=args.min_age_hours)
        on_disk, orphans, too_new = 0, [], 0
        for name in sorted(os.listdir(folder)):
            path = os.path.join(folder, name)
            if name.startswith(".") or not os.path.isfile(path):
                continue
            on_disk += 1
            if name in referenced:
                continue
            mtime = datetime.fromtimestamp(os.path.getmtime(path), timezone.utc)
            if mtime > cutoff:
                too_new += 1
                continue
            orphans.append((name, os.path.getsize(path), mtime))

        print(f"\nfiles on disk: {on_disk}")
        print(f"skipped as too new (< {args.min_age_hours}h): {too_new}")
        print(f"unreferenced: {len(orphans)}")
        if orphans:
            total = sum(size for _, size, _ in orphans)
            print(f"total size: {total / 1048576:.1f} MB\n")
            for name, size, mtime in orphans:
                print(f"  {name}  {size / 1024:>8.0f} KB  "
                      f"{mtime.strftime('%Y-%m-%d %H:%M')}")

        if not args.delete:
            print("\nNothing was deleted. Re-run with --delete "
                  "--yes-i-have-a-backup to remove the files listed above.")
            return 0

        removed = 0
        for name, _, _ in orphans:
            try:
                os.remove(os.path.join(folder, name))
                removed += 1
            except OSError as e:
                print(f"  could not remove {name}: {type(e).__name__}: {e}")
        print(f"\nRemoved {removed} file(s).")
        return 0


if __name__ == "__main__":
    sys.exit(main())
