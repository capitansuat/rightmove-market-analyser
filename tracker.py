"""Daily Rightmove snapshot tracker.

Captures daily listing states into SQLite, then derives status transitions
(Active -> STC, STC -> Active, price changes, new/removed listings) by
comparing snapshots. Gives true time-to-STC metrics that Rightmove
doesn't expose.

Usage:
    python3 tracker.py snapshot --postcode "SW1A 2AA" --radius 1 --max-price 500000
    python3 tracker.py report [--days 30]
"""

from __future__ import annotations

import argparse
import sqlite3
from datetime import date, datetime, timedelta
from pathlib import Path

from server import _scrape_market

DB_DEFAULT = Path(__file__).parent / "data" / "tracker.db"

SCHEMA = """
CREATE TABLE IF NOT EXISTS observations (
    snapshot_date TEXT NOT NULL,
    property_id   INTEGER NOT NULL,
    price         INTEGER,
    is_stc        INTEGER,
    dom           INTEGER,
    address       TEXT,
    type          TEXT,
    bedrooms      INTEGER,
    PRIMARY KEY (snapshot_date, property_id)
);
"""


def get_db(db_path: Path) -> sqlite3.Connection:
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(db_path)
    conn.execute(SCHEMA)
    return conn


def cmd_snapshot(args: argparse.Namespace) -> None:
    today = date.today().isoformat()
    data = _scrape_market(args.postcode, args.radius, args.max_price)
    conn = get_db(Path(args.db))
    rows = [
        (today, l["id"], l["price"], int(l["is_stc"]), l["dom"],
         l["address"], l["type"], l["bedrooms"])
        for l in data["listings"]
    ]
    conn.executemany(
        "INSERT OR REPLACE INTO observations VALUES (?,?,?,?,?,?,?,?)", rows
    )
    conn.commit()
    print(f"{today}: {len(rows)} listings saved "
          f"(active {data['active_count']}, stc {data['stc_count']})")


def cmd_report(args: argparse.Namespace) -> None:
    conn = get_db(Path(args.db))
    cutoff = (date.today() - timedelta(days=args.days)).isoformat()

    dates = [r[0] for r in conn.execute(
        "SELECT DISTINCT snapshot_date FROM observations WHERE snapshot_date >= ? ORDER BY 1",
        (cutoff,),
    )]
    if len(dates) < 2:
        print(f"Snapshots in range: {len(dates)} — need at least 2 for transitions.")
        return

    print(f"Snapshots: {len(dates)} ({dates[0]} .. {dates[-1]})\n")

    went_stc, fell_through, price_changes, appeared, disappeared = [], [], [], [], []

    for prev_d, curr_d in zip(dates, dates[1:]):
        prev = {r[0]: r for r in conn.execute(
            "SELECT property_id, price, is_stc, dom, address FROM observations WHERE snapshot_date = ?",
            (prev_d,))}
        curr = {r[0]: r for r in conn.execute(
            "SELECT property_id, price, is_stc, dom, address FROM observations WHERE snapshot_date = ?",
            (curr_d,))}

        for pid, (_, price, stc, dom, addr) in curr.items():
            if pid not in prev:
                appeared.append((curr_d, addr, price))
                continue
            _, p_price, p_stc, _, _ = prev[pid]
            if not p_stc and stc:
                went_stc.append((curr_d, addr, price, dom))
            elif p_stc and not stc:
                fell_through.append((curr_d, addr, price))
            if p_price and price and p_price != price:
                price_changes.append((curr_d, addr, p_price, price))

        for pid, (_, price, stc, _, addr) in prev.items():
            if pid not in curr:
                disappeared.append((curr_d, addr, price, "was STC" if stc else "was active"))

    def section(title: str, rows: list, fmt) -> None:
        print(f"--- {title} ({len(rows)}) ---")
        for r in rows:
            print(f"  {fmt(r)}")
        print()

    section("Went STC", went_stc,
            lambda r: f"{r[0]}  {r[1][:50]}  £{r[2]:,}  (DOM {r[3]} days)")
    section("Fell through (STC -> Active)", fell_through,
            lambda r: f"{r[0]}  {r[1][:50]}  £{r[2]:,}")
    section("Price changes", price_changes,
            lambda r: f"{r[0]}  {r[1][:45]}  £{r[2]:,} -> £{r[3]:,} ({(r[3]-r[2])*100//r[2]:+d}%)")
    section("New listings", appeared,
            lambda r: f"{r[0]}  {r[1][:50]}  £{r[2]:,}")
    section("Removed (sold or withdrawn)", disappeared,
            lambda r: f"{r[0]}  {r[1][:50]}  £{r[2]:,}  {r[3]}")

    if went_stc:
        doms = sorted(r[3] for r in went_stc)
        print(f"Observed time-to-STC: median {doms[len(doms)//2]} days "
              f"(n={len(doms)}, min {doms[0]}, max {doms[-1]})")


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__)
    sub = p.add_subparsers(dest="command", required=True)

    s = sub.add_parser("snapshot", help="Capture today's listing states")
    s.add_argument("--postcode", required=True)
    s.add_argument("--radius", type=float, default=1.0)
    s.add_argument("--max-price", type=int, default=500000)
    s.add_argument("--db", default=str(DB_DEFAULT))
    s.set_defaults(func=cmd_snapshot)

    r = sub.add_parser("report", help="Show transitions between snapshots")
    r.add_argument("--days", type=int, default=30)
    r.add_argument("--db", default=str(DB_DEFAULT))
    r.set_defaults(func=cmd_report)

    args = p.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
