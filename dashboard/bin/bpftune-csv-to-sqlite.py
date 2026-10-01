#!/usr/bin/env python3
"""One-time CSV to SQLite migration."""
import csv, os, sqlite3, sys, time
from pathlib import Path

HIST = Path("/var/lib/bpftune/history")
DB = HIST / "bpftune.db"
BATCH_SIZE = 5000

def csv_to_sqlite(conn, csv_path, table, force=False):
    if not csv_path.exists():
        print(f"  {csv_path.name}: not found, skipping")
        return 0
    existing = conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
    if existing > 0 and not force:
        print(f"  {table}: {existing} rows exist, skipping")
        return 0
    if force and existing > 0:
        conn.execute(f"DELETE FROM {table}"); conn.commit()
    table_cols = set(r[1] for r in conn.execute(f"PRAGMA table_info({table})").fetchall())
    with open(csv_path, newline="", encoding="utf-8") as f:
        reader = csv.reader(f)
        header = next(reader)
        col_map = [(i, c) for i, c in enumerate(header) if c in table_cols]
        sqlite_cols = [c for _, c in col_map]
        csv_indices = [i for i, _ in col_map]
        placeholders = ",".join(["?"] * len(sqlite_cols))
        sql = f"INSERT INTO {table} ({','.join(sqlite_cols)}) VALUES ({placeholders})"
        batch, total, t0 = [], 0, time.time()
        for row in reader:
            if len(row) < len(header): continue
            batch.append([row[i] if i < len(row) else "" for i in csv_indices])
            if len(batch) >= BATCH_SIZE:
                conn.executemany(sql, batch); conn.commit()
                total += len(batch); batch = []
                print(f"    {total:,} rows ({total/(time.time()-t0):.0f}/s)", end="\r")
        if batch:
            conn.executemany(sql, batch); conn.commit()
            total += len(batch)
    print(f"    {total:,} rows in {time.time()-t0:.1f}s              ")
    return total

def main():
    force = "--force" in sys.argv
    if not DB.exists():
        print(f"ERROR: {DB} not found. Start the collector first.")
        return 1
    conn = sqlite3.connect(str(DB))
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    print(f"Migrating CSV -> SQLite: {DB}")
    total = 0
    for csv_name, table in [("buckets.v2.csv", "buckets"), ("swaps.csv", "swaps"), ("srate.csv", "srate")]:
        print(f"=== {csv_name} -> {table} ===")
        total += csv_to_sqlite(conn, HIST / csv_name, table, force)
    print(f"\nDone: {total:,} total rows")
    for t in ["buckets", "swaps", "srate"]:
        print(f"  {t}: {conn.execute(f'SELECT COUNT(*) FROM {t}').fetchone()[0]:,} rows")
    conn.close()

if __name__ == "__main__":
    sys.exit(main())
