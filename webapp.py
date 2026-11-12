#!/usr/bin/env python3
"""Local web app for browsing the scraped Screen Time data.

Reads screentime.db live, so anything scraped since the page loaded shows up on
refresh. Standard library only - no pip install, no network access.

  python3 webapp.py                 # http://localhost:8765
  python3 webapp.py --port 9000
  python3 webapp.py --db other.db
"""

import argparse
import base64
import binascii
import hashlib
import hmac
import http.cookies
import json
import os
import sqlite3
import sys
import time
from datetime import date, datetime, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

import log_screentime as st  # the storage logic and its safety guard live there

HERE = os.path.dirname(os.path.abspath(__file__))
DEFAULT_DB = os.path.join(HERE, "screentime.db")
INDEX = os.path.join(HERE, "index.html")

# Websites are reported alongside apps and overlap them, so only the app view is
# charted; mixing the two would double count.
VIEW_MODE = "Show Apps"
# The calendar heatmap's span, ending today - a GitHub-style year.
RANGE_DAYS = 365

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


def device_data(conn, device, today=None):
    """Daily totals for the last year, for the calendar heatmap.

    Only the per-day totals - the per-app breakdown for a whole year would be a
    large download nobody reads at once, so it is fetched a day at a time from
    /api/day. Screen time and pick-ups are scraped from separate screens, so a
    day can have one without the other; the day list is the union of both.
    """
    today = today or date.today()
    latest = conn.execute(
        "SELECT MAX(usage_date) AS d FROM ("
        "  SELECT usage_date FROM day_summary WHERE device = ? AND view_mode = ?"
        "  UNION ALL"
        "  SELECT usage_date FROM pickup_summary WHERE device = ?)",
        (device, VIEW_MODE, device)).fetchone()["d"]
    # A scrape dated after the server's today (clock or timezone skew) still
    # belongs on the calendar rather than silently off its right edge.
    end = max(today, date.fromisoformat(latest)) if latest else today
    start = end - timedelta(days=RANGE_DAYS - 1)
    bounds = (start.isoformat(), end.isoformat())

    summaries = {r["usage_date"]: r for r in conn.execute(
        "SELECT usage_date, total_seconds, total_text, scraped_at "
        "FROM day_summary WHERE device = ? AND view_mode = ? "
        "AND usage_date BETWEEN ? AND ?",
        (device, VIEW_MODE) + bounds).fetchall()}
    pickup_totals = {r["usage_date"]: r for r in conn.execute(
        "SELECT usage_date, total_pickups, scraped_at FROM pickup_summary "
        "WHERE device = ? AND usage_date BETWEEN ? AND ?",
        (device,) + bounds).fetchall()}

    days = []
    for iso in sorted(set(summaries) | set(pickup_totals)):  # oldest -> newest
        summary = summaries.get(iso)
        pick = pickup_totals.get(iso)
        days.append({
            "usage_date": iso,
            # None (not 0) where that screen was never scraped for this day
            "total_seconds": summary["total_seconds"] if summary else None,
            "total_text": summary["total_text"] if summary else None,
            "scraped_at": (summary or pick)["scraped_at"],
            "total_pickups": pick["total_pickups"] if pick else None,
            "pickups_scraped_at": pick["scraped_at"] if pick else None,
        })

    return {"device": device, "start": bounds[0], "end": bounds[1], "days": days}


def day_detail(conn, device, iso):
    """Per-app, per-category and per-app pick-up rows for one day."""
    apps = [{
        "name": r["item_name"],
        "level": r["item_level"],
        "seconds": r["seconds"],
        "text": r["duration_text"],
    } for r in conn.execute(
        "SELECT item_name, item_level, seconds, duration_text FROM app_usage "
        "WHERE device = ? AND view_mode = ? AND usage_date = ? "
        "ORDER BY seconds DESC", (device, VIEW_MODE, iso)).fetchall()]

    categories = [{
        "name": r["category_name"],
        "seconds": r["seconds"],
        "text": r["duration_text"],
    } for r in conn.execute(
        "SELECT category_name, seconds, duration_text FROM category_usage "
        "WHERE device = ? AND view_mode = ? AND usage_date = ? "
        "ORDER BY seconds DESC", (device, VIEW_MODE, iso)).fetchall()]

    pickups = [{
        "name": r["item_name"],
        "pickups": r["pickups"],
    } for r in conn.execute(
        "SELECT item_name, pickups FROM pickup_usage "
        "WHERE device = ? AND usage_date = ? ORDER BY pickups DESC",
        (device, iso)).fetchall()]

    return {"device": device, "usage_date": iso, "apps": apps,
            "categories": categories, "pickups": pickups}


LOOPBACK = ("127.0.0.1", "localhost", "::1")
MAX_BODY = 5 * 1024 * 1024

SESSION_COOKIE = "st_session"
SESSION_MAX_AGE = 90 * 24 * 3600  # 90 days


def session_value(token, expires_at):
    """A self-verifying cookie: an expiry plus an HMAC of it under the token.

    No server-side session store is needed, the cookie does not contain the
    token itself, and an expired one cannot be replayed because the expiry is
    inside the signed material.
    """
    message = ("screentime-session|%d" % expires_at).encode("utf-8")
    mac = hmac.new(token.encode("utf-8"), message, hashlib.sha256).hexdigest()
    return "%d.%s" % (expires_at, mac)


def session_valid(token, value):
    try:
        expires_str, mac = (value or "").split(".", 1)
        expires_at = int(expires_str)
    except (ValueError, AttributeError):
        return False
    if expires_at < time.time():
        return False
    return hmac.compare_digest(mac, session_value(token, expires_at).split(".", 1)[1])


LOGIN_PAGE = """<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Screen Time</title>
<style>
  :root {
    color-scheme: light;
    --page:#f9f9f7; --surface:#fcfcfb; --text:#0b0b0b; --muted:#52514e;
    --border:rgba(11,11,11,0.10); --accent:#2a78d6; --danger:#d03b3b;
  }
  @media (prefers-color-scheme: dark) { :root {
    color-scheme: dark;
    --page:#0d0d0d; --surface:#1a1a19; --text:#ffffff; --muted:#c3c2b7;
    --border:rgba(255,255,255,0.10); --accent:#3987e5; --danger:#e66767;
  } }
  * { box-sizing:border-box; }
  body { margin:0; min-height:100vh; display:flex; align-items:center;
    justify-content:center; background:var(--page); color:var(--text);
    font:15px/1.5 system-ui,-apple-system,"Segoe UI",sans-serif; padding:20px; }
  form { background:var(--surface); border:1px solid var(--border);
    border-radius:12px; padding:26px 24px; width:100%; max-width:340px; }
  h1 { margin:0 0 4px; font-size:18px; font-weight:600; }
  p { margin:0 0 20px; font-size:13px; color:var(--muted); }
  label { display:block; font-size:12px; color:var(--muted); margin-bottom:6px; }
  input { width:100%; font:inherit; padding:9px 11px; color:var(--text);
    background:var(--page); border:1px solid var(--border); border-radius:8px; }
  button { width:100%; margin-top:14px; font:inherit; font-weight:600;
    padding:10px; color:#fff; background:var(--accent); border:0;
    border-radius:8px; cursor:pointer; }
  .error { color:var(--danger); font-size:13px; margin:0 0 14px; }
</style></head>
<body>
  <form method="POST" action="/login">
    <h1>Screen Time</h1>
    <p>This page is protected. Enter the access token.</p>
    <!--ERROR-->
    <label for="token">Access token</label>
    <input id="token" name="token" type="password" autocomplete="current-password"
           autofocus required>
    <button type="submit">Sign in</button>
  </form>
</body></html>
"""


class Handler(BaseHTTPRequestHandler):
    db_path = DEFAULT_DB
    default_device = DEFAULT_DEVICE
    token = None

    def log_message(self, fmt, *args):  # quieter console
        pass

    def _authorized(self):
        """True when the caller may proceed. Three credentials are accepted:

        - `Authorization: Bearer <token>` - what the scraper sends
        - `Authorization: Basic <anything>:<token>` - for curl
        - the session cookie set by signing in through the form

        This only reports; callers decide whether to answer with the login page
        or a 401, since a browser wants the form and an API client does not.
        """
        if not self.token:
            return True

        header = self.headers.get("Authorization", "")
        supplied = None
        if header.startswith("Bearer "):
            supplied = header[7:].strip()
        elif header.startswith("Basic "):
            try:
                decoded = base64.b64decode(header[6:].strip()).decode("utf-8")
            except (binascii.Error, UnicodeDecodeError, ValueError):
                decoded = ""
            if ":" in decoded:
                supplied = decoded.split(":", 1)[1]
        if supplied is not None and hmac.compare_digest(supplied, self.token):
            return True

        raw = self.headers.get("Cookie")
        if raw:
            try:
                jar = http.cookies.SimpleCookie(raw)
            except http.cookies.CookieError:
                return False
            morsel = jar.get(SESSION_COOKIE)
            if morsel and session_valid(self.token, morsel.value):
                return True
        return False

    def _send_unauthorized(self):
        self._send(401, "authentication required", "text/plain; charset=utf-8")

    def _send_login_page(self, error=None, status=200):
        # Plain substitution, not %-formatting or .format(): the page's CSS
        # contains both "100%" and braces, either of which those would choke on.
        banner = '<p class="error">%s</p>' % error if error else ""
        markup = LOGIN_PAGE.replace("<!--ERROR-->", banner)
        self._send(status, markup, "text/html; charset=utf-8")

    def _is_https(self):
        # Behind a TLS-terminating proxy the connection here is plain HTTP, so
        # the proxy's header is the only way to know the browser used HTTPS.
        return self.headers.get("X-Forwarded-Proto", "").lower() == "https"

    def _set_session_cookie(self, value, max_age):
        parts = ["%s=%s" % (SESSION_COOKIE, value), "Path=/",
                 "Max-Age=%d" % max_age, "HttpOnly", "SameSite=Lax"]
        if self._is_https():
            parts.append("Secure")
        self.send_header("Set-Cookie", "; ".join(parts))

    def _redirect(self, location, cookie=None, max_age=SESSION_MAX_AGE):
        self.send_response(303)
        self.send_header("Location", location)
        if cookie is not None:
            self._set_session_cookie(cookie, max_age)
        self.send_header("Content-Length", "0")
        self.end_headers()

    def _handle_login(self):
        if not self.token:
            self._redirect("/")
            return
        try:
            length = int(self.headers.get("Content-Length") or 0)
            body = self.rfile.read(min(length, 4096)).decode("utf-8")
        except (ValueError, UnicodeDecodeError):
            body = ""
        supplied = (parse_qs(body).get("token") or [""])[0]

        if supplied and hmac.compare_digest(supplied, self.token):
            expires_at = int(time.time()) + SESSION_MAX_AGE
            self._redirect("/", session_value(self.token, expires_at))
        else:
            self._send_login_page("That token was not accepted.", status=401)

    def _handle_logout(self):
        self._redirect("/", cookie="", max_age=0)

    def _read_json_body(self):
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            length = 0
        if length <= 0:
            raise ValueError("empty request body")
        if length > MAX_BODY:
            raise ValueError("request body too large")
        return json.loads(self.rfile.read(length).decode("utf-8"))

    def do_POST(self):
        path = urlparse(self.path).path

        if path == "/login":
            self._handle_login()
            return
        if path == "/logout":
            self._handle_logout()
            return

        if path != "/api/ingest":
            self._send(404, "not found", "text/plain; charset=utf-8")
            return
        if not self._authorized():
            self._send_unauthorized()
            return

        try:
            payload = self._read_json_body()
        except (ValueError, json.JSONDecodeError) as exc:
            self._json({"error": "bad request body: %s" % exc}, 400)
            return

        try:
            usage_date = date.fromisoformat(payload["usage_date"])
            data = {
                "screen": payload.get("screen", "apps"),
                "device": payload["device"],
                "date_label": payload.get("date_label", ""),
                "view_mode": payload.get("view_mode"),
                "total_text": payload["total_text"],
                "categories": [tuple(c) for c in payload.get("categories", [])],
                "items": [tuple(i) for i in payload.get("items", [])],
            }
        except (KeyError, TypeError, ValueError) as exc:
            self._json({"error": "malformed payload: %s" % exc}, 400)
            return

        scraped_at = payload.get("scraped_at") or \
            datetime.now().isoformat(timespec="seconds")
        force = bool(payload.get("force"))

        try:
            if data["screen"] == "pickups":
                st.store_pickups(self.db_path, data, usage_date, scraped_at, force)
            else:
                st.store(self.db_path, data, usage_date, scraped_at, force)
        except st.ScrapeError as exc:
            # The anti-regression guard rejected it - the client can retry
            # with force. 409 so that is distinguishable from a bad request.
            self._json({"error": str(exc)}, 409)
            return
        except sqlite3.Error as exc:
            self._json({"error": "database error: %s" % exc}, 500)
            return

        self._json({"stored": len(data["items"]), "screen": data["screen"],
                    "device": data["device"],
                    "usage_date": usage_date.isoformat()})

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

        if url.path == "/logout":
            self._handle_logout()
            return

        if not self._authorized():
            # A person gets a form to fill in; a script gets a status code.
            if url.path in ("/", "/index.html", "/login"):
                self._send_login_page()
            else:
                self._send_unauthorized()
            return

        if url.path == "/login":
            self._redirect("/")
            return

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
                if url.path in ("/api/data", "/api/day"):
                    devices = list_devices(conn)
                    device = pick_device(
                        devices, (query.get("device") or [None])[0],
                        self.default_device)
                if url.path == "/api/data":
                    if device is None:
                        today = date.today()
                        self._json({
                            "devices": [], "device": None, "days": [],
                            "start": (today - timedelta(days=RANGE_DAYS - 1)).isoformat(),
                            "end": today.isoformat()})
                        return
                    payload = device_data(conn, device)
                    payload["devices"] = devices
                    self._json(payload)
                    return
                if url.path == "/api/day":
                    try:
                        iso = date.fromisoformat((query.get("date") or [""])[0]).isoformat()
                    except ValueError:
                        self._json({"error": "date must be YYYY-MM-DD"}, 400)
                        return
                    if device is None:
                        self._json({"error": "no devices recorded yet"}, 404)
                        return
                    self._json(day_detail(conn, device, iso))
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
    parser.add_argument("--token", default=os.environ.get("SCREENTIME_TOKEN"),
                        help="shared secret required to read or ingest. "
                             "Defaults to $SCREENTIME_TOKEN")
    parser.add_argument("--no-auth", action="store_true",
                        help="serve without a token even off-loopback. Only "
                             "sensible on a private network such as Tailscale")
    args = parser.parse_args()

    off_loopback = args.host not in LOOPBACK
    if off_loopback and not args.token and not args.no_auth:
        print("refusing to serve on %s without a token: this data would be "
              "readable by anyone who can reach the port.\n"
              "  set one:      export SCREENTIME_TOKEN=$(python3 -c "
              "'import secrets;print(secrets.token_urlsafe(32))')\n"
              "  or override:  --no-auth  (private networks only)"
              % args.host, file=sys.stderr)
        return 2

    Handler.db_path = args.db
    Handler.default_device = args.device
    Handler.token = args.token
    server = ThreadingHTTPServer((args.host, args.port), Handler)
    print("Screen Time viewer on http://%s:%d  (ctrl-c to stop)"
          % ("localhost" if args.host in ("127.0.0.1", "") else args.host, args.port))
    if off_loopback:
        print("reachable from other machines; %s"
              % ("token required" if args.token else "NO AUTH - anyone who can "
                 "reach this port can read the data"))
    print("reading %s" % args.db)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nstopped")
    return 0


if __name__ == "__main__":
    sys.exit(main())
