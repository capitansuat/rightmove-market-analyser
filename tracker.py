"""Daily Rightmove snapshot tracker.

Captures daily listing states into SQLite, then derives status transitions
(Active -> STC, STC -> Active, price changes, new/removed listings) by
comparing snapshots. Gives true time-to-STC metrics that Rightmove
doesn't expose. Listings that disappear while STC have likely completed;
cross-check with Land Registry PPD 1-3 months later for confirmation.

Usage:
    python3 tracker.py snapshot --postcode "SW1A 2AA" --radius 1 --max-price 500000
    python3 tracker.py report [--days 30]
"""

from __future__ import annotations

import argparse
import sqlite3
from datetime import date, timedelta
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


def take_snapshot(postcode: str, radius: float = 1.0, max_price: int = 500000,
                  db_path: Path = DB_DEFAULT) -> dict:
    """Capture today's listing states into the DB. Returns summary."""
    today = date.today().isoformat()
    data = _scrape_market(postcode, radius, max_price)
    conn = get_db(db_path)
    rows = [
        (today, l["id"], l["price"], int(l["is_stc"]), l["dom"],
         l["address"], l["type"], l["bedrooms"])
        for l in data["listings"]
    ]
    conn.executemany(
        "INSERT OR REPLACE INTO observations VALUES (?,?,?,?,?,?,?,?)", rows
    )
    conn.commit()
    return {
        "snapshot_date": today,
        "listings_saved": len(rows),
        "active": data["active_count"],
        "stc": data["stc_count"],
    }


def compute_transitions(db_path: Path = DB_DEFAULT, days: int = 30) -> dict:
    """Compare consecutive snapshots and return all status transitions."""
    conn = get_db(db_path)
    cutoff = (date.today() - timedelta(days=days)).isoformat()

    dates = [r[0] for r in conn.execute(
        "SELECT DISTINCT snapshot_date FROM observations WHERE snapshot_date >= ? ORDER BY 1",
        (cutoff,),
    )]
    result: dict = {"snapshots": dates, "went_stc": [], "fell_through": [],
                    "price_changes": [], "new_listings": [], "removed": []}
    if len(dates) < 2:
        result["note"] = f"Only {len(dates)} snapshot(s) in range; need 2+ for transitions."
        return result

    for prev_d, curr_d in zip(dates, dates[1:]):
        prev = {r[0]: r for r in conn.execute(
            "SELECT property_id, price, is_stc, dom, address FROM observations WHERE snapshot_date = ?",
            (prev_d,))}
        curr = {r[0]: r for r in conn.execute(
            "SELECT property_id, price, is_stc, dom, address FROM observations WHERE snapshot_date = ?",
            (curr_d,))}

        for pid, (_, price, stc, dom, addr) in curr.items():
            if pid not in prev:
                result["new_listings"].append(
                    {"date": curr_d, "address": addr, "price": price})
                continue
            _, p_price, p_stc, _, _ = prev[pid]
            if not p_stc and stc:
                result["went_stc"].append(
                    {"date": curr_d, "address": addr, "price": price, "dom": dom})
            elif p_stc and not stc:
                result["fell_through"].append(
                    {"date": curr_d, "address": addr, "price": price})
            if p_price and price and p_price != price:
                result["price_changes"].append(
                    {"date": curr_d, "address": addr,
                     "from": p_price, "to": price,
                     "pct": round((price - p_price) * 100 / p_price, 1)})

        for pid, (_, price, stc, _, addr) in prev.items():
            if pid not in curr:
                result["removed"].append(
                    {"date": curr_d, "address": addr, "price": price,
                     "status": "likely completed (was STC)" if stc
                               else "likely withdrawn (was active)"})

    doms = sorted(t["dom"] for t in result["went_stc"])
    if doms:
        result["time_to_stc"] = {
            "median_days": doms[len(doms) // 2],
            "min": doms[0], "max": doms[-1], "n": len(doms),
        }
    return result


def cmd_snapshot(args: argparse.Namespace) -> None:
    s = take_snapshot(args.postcode, args.radius, args.max_price, Path(args.db))
    print(f"{s['snapshot_date']}: {s['listings_saved']} listings saved "
          f"(active {s['active']}, stc {s['stc']})")


def cmd_report(args: argparse.Namespace) -> None:
    r = compute_transitions(Path(args.db), args.days)
    if "note" in r:
        print(r["note"])
        return

    print(f"Snapshots: {len(r['snapshots'])} ({r['snapshots'][0]} .. {r['snapshots'][-1]})\n")

    def section(title: str, rows: list, fmt) -> None:
        print(f"--- {title} ({len(rows)}) ---")
        for row in rows:
            print(f"  {fmt(row)}")
        print()

    section("Went STC", r["went_stc"],
            lambda t: f"{t['date']}  {t['address'][:50]}  £{t['price']:,}  (DOM {t['dom']} days)")
    section("Fell through (STC -> Active)", r["fell_through"],
            lambda t: f"{t['date']}  {t['address'][:50]}  £{t['price']:,}")
    section("Price changes", r["price_changes"],
            lambda t: f"{t['date']}  {t['address'][:45]}  £{t['from']:,} -> £{t['to']:,} ({t['pct']:+.1f}%)")
    section("New listings", r["new_listings"],
            lambda t: f"{t['date']}  {t['address'][:50]}  £{t['price']:,}")
    section("Removed", r["removed"],
            lambda t: f"{t['date']}  {t['address'][:50]}  £{t['price']:,}  {t['status']}")

    if "time_to_stc" in r:
        s = r["time_to_stc"]
        print(f"Observed time-to-STC: median {s['median_days']} days "
              f"(n={s['n']}, min {s['min']}, max {s['max']})")


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
