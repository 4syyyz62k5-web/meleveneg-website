#!/usr/bin/env python3
"""
Merges every compound.csv and units.csv found anywhere under scripts/output/
into two combined files:
    scripts/output/all_compounds.csv
    scripts/output/all_units.csv

Searches recursively (not just one level deep) — compound folders may be
nested inside sub-folders (e.g. manually organized in Finder into per-developer
groups) as well as sitting directly under scripts/output/.

Before merging, every compound.csv found must share the exact same header as
the rest (same columns, same order) — same for units.csv. A file whose header
doesn't match is SKIPPED and reported, never silently merged in with
misaligned columns.

Usage:
    python scripts/nawy_merge.py
    python scripts/nawy_merge.py --out-dir scripts/output
"""

import argparse
import csv
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent


def read_rows(path):
    with open(path, newline="", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        return reader.fieldnames or [], list(reader)


def merge_files(paths, log):
    """Merges a list of same-shaped CSVs. Returns (header, rows, used_paths, skipped)
    where skipped is a list of (path, reason) for any file whose header didn't match."""
    canonical_header = None
    rows, used_paths, skipped = [], [], []

    for path in paths:
        header, file_rows = read_rows(path)
        if not header:
            skipped.append((path, "empty file (no header)"))
            continue
        if canonical_header is None:
            canonical_header = header
        elif len(header) != len(canonical_header):
            skipped.append((path, f"{len(header)} column(s), expected {len(canonical_header)}"))
            continue
        elif header != canonical_header:
            skipped.append((path, "same column count but different column names/order"))
            continue
        rows.extend(file_rows)
        used_paths.append(path)

    return canonical_header, rows, used_paths, skipped


def write_merged(path, header, rows):
    with open(path, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=header)
        writer.writeheader()
        for row in rows:
            writer.writerow(row)


def main():
    # Line-buffer stdout so a long run's progress shows up as it happens rather than only at exit
    # (same reasoning as nawy_to_csv.py's main()).
    sys.stdout.reconfigure(line_buffering=True)

    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--out-dir", default=str(PROJECT_ROOT / "scripts" / "output"))
    args = parser.parse_args()

    out_dir = Path(args.out_dir)
    if not out_dir.is_dir():
        sys.exit(f"{out_dir} doesn't exist — nothing to merge.")

    compound_paths = sorted(out_dir.rglob("compound.csv"))
    unit_paths = sorted(out_dir.rglob("units.csv"))
    print(f"Found {len(compound_paths)} compound.csv file(s) and {len(unit_paths)} units.csv file(s) under {out_dir}")

    c_header, c_rows, c_used, c_skipped = merge_files(compound_paths, log=print)
    u_header, u_rows, u_used, u_skipped = merge_files(unit_paths, log=print)

    compounds_out = out_dir / "all_compounds.csv"
    units_out = out_dir / "all_units.csv"
    if c_header:
        write_merged(compounds_out, c_header, c_rows)
    if u_header:
        write_merged(units_out, u_header, u_rows)

    print("\n" + "=" * 60)
    print(f"Merged {len(c_used)} compound.csv file(s) -> {len(c_rows)} compound(s) -> {compounds_out}")
    print(f"Merged {len(u_used)} units.csv file(s) -> {len(u_rows)} unit(s) -> {units_out}")

    if c_skipped:
        print(f"\n⚠️  Skipped {len(c_skipped)} compound.csv file(s) — column mismatch:")
        for path, reason in c_skipped:
            print(f"  - {path}: {reason}")
    if u_skipped:
        print(f"\n⚠️  Skipped {len(u_skipped)} units.csv file(s) — column mismatch:")
        for path, reason in u_skipped:
            print(f"  - {path}: {reason}")

    if not c_used and not u_used:
        print("\nNothing was merged — no compound.csv/units.csv files found under this directory.")


if __name__ == "__main__":
    main()
