#!/usr/bin/env python3
"""Local web app for browsing the scraped Screen Time data.

Reads screentime.db live, so anything scraped since the page loaded shows up on
refresh. Standard library only - no pip install, no network access.

  python3 webapp.py                 # http://localhost:8765
  python3 webapp.py --port 9000
  python3 webapp.py --db other.db
"""

import argparse
import json
import os
import sqlite3
import sys
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

HERE = os.path.dirname(os.path.abspath(__file__))
DEFAULT_DB = os.path.join(HERE, "screentime.db")
INDEX = os.path.join(HERE, "index.html")

# Websites are reported alongside apps and overlap them, so only the app view is
# charted; mixing the two would double count.
VIEW_MODE = "Show Apps"
DAYS = 7

# Shown when the page is opened without an explicit ?device=. Matched loosely,
# because Screen Time spells the devices inconsistently - "Nolan's iPhone" uses a
# curly apostrophe while "Nolan's Large iPhone" uses a straight one.
DEFAULT_DEVICE = "Nolan's iPhone"


def _norm(name):
    return (name or "").replace("’", "'").casefold().strip()


def pick_device(devices, requested, default):
    """Resolve which device to show: explicit request, then default, then first."""
    for candidate in (requested, default):
        if not candidate:
            continue
        for device in devices:
            if _norm(device) == _norm(candidate):
                return device
    return devices[0] if devices else None


def connect(db_path):
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    return conn


def list_devices(conn):
    rows = conn.execute(
        "SELECT device FROM day_summary WHERE view_mode = ? "
        "UNION SELECT device FROM pickup_summary ORDER BY device",
        (VIEW_MODE,)).fetchall()
    return [r["device"] for r in rows]


def device_data(conn, device):
    """The most recent DAYS days that have any data at all for this device.

    Screen time and pick-ups are scraped from separate screens, so a day can
    have one without the other; the day list is the union of both.
    """
    dates = [r["usage_date"] for r in conn.execute(
        "SELECT usage_date FROM ("
        "  SELECT usage_date FROM day_summary WHERE device = ? AND view_mode = ?"
        "  UNION"
        "  SELECT usage_date FROM pickup_summary WHERE device = ?"
        ") ORDER BY usage_date DESC LIMIT ?",
        (device, VIEW_MODE, device, DAYS)).fetchall()]
    dates.reverse()  # oldest -> newest for the chart

    apps = {}
    categories = {}
    pickups = {}
    pickup_totals = {}
    days = []

    if dates:
        placeholders = ",".join("?" * len(dates))

        summaries = {r["usage_date"]: dict(r) for r in conn.execute(
            "SELECT usage_date, total_seconds, total_text, scraped_at "
            "FROM day_summary WHERE device = ? AND view_mode = ? "
            "AND usage_date IN (%s)" % placeholders,
            [device, VIEW_MODE] + dates).fetchall()}

        for r in conn.execute(
                "SELECT usage_date, total_pickups, scraped_at FROM pickup_summary "
                "WHERE device = ? AND usage_date IN (%s)" % placeholders,
                [device] + dates).fetchall():
            pickup_totals[r["usage_date"]] = {
                "total_pickups": r["total_pickups"],
                "scraped_at": r["scraped_at"],
            }

        for iso in dates:
            summary = summaries.get(iso)
            pick = pickup_totals.get(iso)
            days.append({
                "usage_date": iso,
                # None (not 0) where that screen was never scraped for this day
                "total_seconds": summary["total_seconds"] if summary else None,
                "total_text": summary["total_text"] if summary else None,
                "scraped_at": (summary or pick or {}).get("scraped_at"),
                "total_pickups": pick["total_pickups"] if pick else None,
                "pickups_scraped_at": pick["scraped_at"] if pick else None,
            })

        for r in conn.execute(
                "SELECT usage_date, item_name, pickups FROM pickup_usage "
                "WHERE device = ? AND usage_date IN (%s) "
                "ORDER BY pickups DESC" % placeholders,
                [device] + dates).fetchall():
            pickups.setdefault(r["usage_date"], []).append({
                "name": r["item_name"],
                "pickups": r["pickups"],
            })

        rows = conn.execute(
            "SELECT usage_date, item_name, item_level, seconds, duration_text "
            "FROM app_usage WHERE device = ? AND view_mode = ? "
            "AND usage_date IN (%s) ORDER BY seconds DESC" % placeholders,
            [device, VIEW_MODE] + dates).fetchall()
        for r in rows:
            apps.setdefault(r["usage_date"], []).append({
                "name": r["item_name"],
                "level": r["item_level"],
                "seconds": r["seconds"],
                "text": r["duration_text"],
            })

        rows = conn.execute(
            "SELECT usage_date, category_name, seconds, duration_text "
            "FROM category_usage WHERE device = ? AND view_mode = ? "
            "AND usage_date IN (%s) ORDER BY seconds DESC" % placeholders,
            [device, VIEW_MODE] + dates).fetchall()
        for r in rows:
            categories.setdefault(r["usage_date"], []).append({
                "name": r["category_name"],
                "seconds": r["seconds"],
                "text": r["duration_text"],
            })

    return {"device": device, "days": days, "apps": apps,
            "categories": categories, "pickups": pickups}


class Handler(BaseHTTPRequestHandler):
    db_path = DEFAULT_DB
    default_device = DEFAULT_DEVICE

    def log_message(self, fmt, *args):  # quieter console
        pass

    def _send(self, status, body, content_type):
        if isinstance(body, str):
            body = body.encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _json(self, payload, status=200):
        self._send(status, json.dumps(payload), "application/json; charset=utf-8")

    def do_GET(self):
        url = urlparse(self.path)
        query = parse_qs(url.query)

        if url.path in ("/", "/index.html"):
            try:
                with open(INDEX, "rb") as fh:
                    self._send(200, fh.read(), "text/html; charset=utf-8")
            except OSError:
                self._send(500, "index.html is missing", "text/plain; charset=utf-8")
            return

        if url.path.startswith("/api/"):
            if not os.path.exists(self.db_path):
                self._json({"error": "no database yet - run log_screentime.py first"}, 404)
                return
            conn = connect(self.db_path)
            try:
                if url.path == "/api/devices":
                    self._json({"devices": list_devices(conn)})
                    return
                if url.path == "/api/data":
                    devices = list_devices(conn)
                    device = pick_device(
                        devices, (query.get("device") or [None])[0],
                        self.default_device)
                    if device is None:
                        self._json({"devices": [], "device": None, "days": [],
                                    "apps": {}, "categories": {}, "pickups": {}})
                        return
                    payload = device_data(conn, device)
                    payload["devices"] = devices
                    self._json(payload)
                    return
            except sqlite3.Error as exc:
                self._json({"error": str(exc)}, 500)
                return
            finally:
                conn.close()

        self._send(404, "not found", "text/plain; charset=utf-8")


def main():
    parser = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--db", default=DEFAULT_DB,
                        help="SQLite file to read (default: %(default)s)")
    parser.add_argument("--port", type=int, default=8765,
                        help="port to listen on (default: %(default)s)")
    parser.add_argument("--host", default="127.0.0.1",
                        help="address to bind (default: %(default)s). Use "
                             "0.0.0.0 to reach it from other machines - there "
                             "is no authentication, so only on a trusted network")
    parser.add_argument("--device", default=DEFAULT_DEVICE,
                        help="device selected on load (default: %(default)s)")
    args = parser.parse_args()

    Handler.db_path = args.db
    Handler.default_device = args.device
    server = ThreadingHTTPServer((args.host, args.port), Handler)
    print("Screen Time viewer on http://%s:%d  (ctrl-c to stop)"
          % ("localhost" if args.host in ("127.0.0.1", "") else args.host, args.port))
    if args.host == "0.0.0.0":
        print("listening on all interfaces - anyone on this network can read "
              "this data, and it is served over plain HTTP")
    print("reading %s" % args.db)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nstopped")
    return 0


if __name__ == "__main__":
    sys.exit(main())
