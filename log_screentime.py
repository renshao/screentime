#!/usr/bin/env python3
"""Record the Screen Time day currently shown in System Settings into SQLite.

Select the device and the date yourself in
  System Settings > Screen Time > App & Website Activity
then run this. Re-running for a day already in the database replaces that day's
rows, so scraping the same date again later just refreshes it with the newer
numbers.

  ./log_screentime.py                 # store what's on screen
  ./log_screentime.py --dry-run       # parse and print, don't write
  ./log_screentime.py --db other.db   # use a different database file
"""

import argparse
import json
import os
import re
import sqlite3
import subprocess
import sys
import urllib.error
import urllib.request
from datetime import date, datetime, timedelta

HERE = os.path.dirname(os.path.abspath(__file__))
SCRAPER = os.path.join(HERE, "scrape_screentime.js")
DEFAULT_DB = os.path.join(HERE, "screentime.db")

SCHEMA = """
CREATE TABLE IF NOT EXISTS day_summary (
    device        TEXT    NOT NULL,
    usage_date    TEXT    NOT NULL,   -- ISO YYYY-MM-DD
    view_mode     TEXT    NOT NULL,   -- "Show Apps" / "Show Websites" / ...
    total_seconds INTEGER NOT NULL,
    total_text    TEXT    NOT NULL,   -- as displayed, e.g. "1 hour, 52 minutes"
    date_label    TEXT    NOT NULL,   -- as displayed, e.g. "Today, 5 August"
    scraped_at    TEXT    NOT NULL,   -- ISO timestamp, local time
    PRIMARY KEY (device, usage_date, view_mode)
);

CREATE TABLE IF NOT EXISTS category_usage (
    device        TEXT    NOT NULL,
    usage_date    TEXT    NOT NULL,
    view_mode     TEXT    NOT NULL,
    category_name TEXT    NOT NULL,
    seconds       INTEGER NOT NULL,
    duration_text TEXT    NOT NULL,
    scraped_at    TEXT    NOT NULL,
    PRIMARY KEY (device, usage_date, view_mode, category_name)
);

CREATE TABLE IF NOT EXISTS app_usage (
    device        TEXT    NOT NULL,
    usage_date    TEXT    NOT NULL,
    view_mode     TEXT    NOT NULL,
    item_name     TEXT    NOT NULL,   -- app name or website host
    item_level    INTEGER NOT NULL,   -- 0 = top level, >0 = nested under an app
    seconds       INTEGER NOT NULL,
    duration_text TEXT    NOT NULL,
    scraped_at    TEXT    NOT NULL,
    PRIMARY KEY (device, usage_date, view_mode, item_name)
);

CREATE INDEX IF NOT EXISTS app_usage_by_date
    ON app_usage (usage_date, device);

-- Pick-Ups is a separate screen with no view mode, and counts rather than
-- durations, so it gets its own pair of tables.
CREATE TABLE IF NOT EXISTS pickup_summary (
    device        TEXT    NOT NULL,
    usage_date    TEXT    NOT NULL,
    total_pickups INTEGER NOT NULL,
    date_label    TEXT    NOT NULL,
    scraped_at    TEXT    NOT NULL,
    PRIMARY KEY (device, usage_date)
);

CREATE TABLE IF NOT EXISTS pickup_usage (
    device     TEXT    NOT NULL,
    usage_date TEXT    NOT NULL,
    item_name  TEXT    NOT NULL,
    pickups    INTEGER NOT NULL,
    scraped_at TEXT    NOT NULL,
    PRIMARY KEY (device, usage_date, item_name)
);

CREATE INDEX IF NOT EXISTS pickup_usage_by_date
    ON pickup_usage (usage_date, device);
"""

MONTHS = {
    m.lower(): i
    for i, m in enumerate(
        ["January", "February", "March", "April", "May", "June", "July",
         "August", "September", "October", "November", "December"], start=1)
}

WEEKDAYS = {
    d.lower(): i
    for i, d in enumerate(
        ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday",
         "Sunday"])
}

_DURATION_RE = re.compile(r"(\d+)\s*(hour|minute|second)s?", re.IGNORECASE)
_UNIT_SECONDS = {"hour": 3600, "minute": 60, "second": 1}


class ScrapeError(Exception):
    pass


def parse_duration(text):
    """"1 hour, 52 minutes" -> 6720. Returns 0 for text with no recognised units."""
    seconds = 0
    for amount, unit in _DURATION_RE.findall(text or ""):
        seconds += int(amount) * _UNIT_SECONDS[unit.lower()]
    return seconds


def parse_count(text):
    """"17" -> 17, "1,024" -> 1024. Raises if it isn't a plain number."""
    cleaned = (text or "").replace(",", "").strip()
    if not cleaned.isdigit():
        raise ScrapeError('expected a pick-up count but got "%s"' % text)
    return int(cleaned)


def parse_date_label(label, today=None):
    """"Today, 5 August" / "Yesterday, 4 August" / "Saturday, 11 July" -> date.

    The UI never shows a year, so the year is inferred as the most recent
    occurrence of that day-and-month at or before today.
    """
    today = today or date.today()
    label = (label or "").strip()

    if "," in label:
        prefix, rest = label.split(",", 1)
    else:
        prefix, rest = "", label
    prefix = prefix.strip().lower()
    rest = rest.strip()

    if prefix == "today":
        return today
    if prefix == "yesterday":
        return today - timedelta(days=1)

    match = re.match(r"^(\d{1,2})\s+([A-Za-z]+)$", rest)
    if match:
        day, month_name = int(match.group(1)), match.group(2)
    else:
        match = re.match(r"^([A-Za-z]+)\s+(\d{1,2})$", rest)
        if not match:
            raise ScrapeError(
                'cannot read a single day out of the date "%s". Set the Usage '
                "pop-up to a specific day rather than a week." % label)
        month_name, day = match.group(1), int(match.group(2))

    month = MONTHS.get(month_name.lower())
    if month is None:
        raise ScrapeError('unrecognised month name in date "%s"' % label)

    for year in (today.year, today.year - 1):
        try:
            candidate = date(year, month, day)
        except ValueError:
            continue
        if candidate <= today:
            break
    else:
        raise ScrapeError('could not resolve a year for date "%s"' % label)

    expected = WEEKDAYS.get(prefix)
    if expected is not None and candidate.weekday() != expected:
        print(
            'warning: "%s" resolved to %s, which is a %s'
            % (label, candidate.isoformat(), candidate.strftime("%A")),
            file=sys.stderr)

    return candidate


def run_scraper():
    try:
        proc = subprocess.run(
            ["osascript", "-l", "JavaScript", SCRAPER],
            capture_output=True, text=True, timeout=90)
    except FileNotFoundError:
        raise ScrapeError("osascript not found - this only runs on macOS")
    except subprocess.TimeoutExpired:
        raise ScrapeError("the scraper timed out after 90s")

    if proc.returncode != 0:
        detail = (proc.stderr or proc.stdout).strip()
        raise ScrapeError(
            "the scraper failed.\n%s\n\n"
            "Check that System Settings is open on Screen Time > App & Website "
            "Activity, and that Terminal has Accessibility permission in\n"
            "System Settings > Privacy & Security > Accessibility." % detail)
    return proc.stdout


def parse_scraper_output(text):
    screen = "apps"
    device = date_label = mode = total_text = None
    categories = []
    items = []

    for line in text.splitlines():
        if not line.strip():
            continue
        parts = line.split("\t")
        kind = parts[0]

        if kind == "SCREEN":
            screen = parts[1]
        elif kind == "DEVICE":
            device = parts[1]
        elif kind == "DATE":
            date_label = parts[1]
        elif kind == "MODE":
            mode = parts[1]
        elif kind == "TOTAL":
            total_text = parts[1]
        elif kind == "CATEGORY" and len(parts) >= 3:
            categories.append((parts[1], parts[2]))
        elif kind == "ROW" and len(parts) >= 4:
            items.append((int(parts[1]), parts[2], parts[3]))

    required = [("device", device), ("date", date_label), ("total", total_text)]
    if screen == "apps":
        required.append(("view mode", mode))
    missing = [name for name, value in required if not value]
    if missing:
        raise ScrapeError(
            "the scraper did not report: %s. Raw output:\n%s"
            % (", ".join(missing), text))

    if screen == "pickups":
        # The only "category" on this screen restates the total, so drop it
        # rather than store a row that duplicates total_pickups.
        categories = []

    return {
        "screen": screen,
        "device": device,
        "date_label": date_label,
        "view_mode": mode,
        "total_text": total_text,
        "categories": categories,
        "items": items,
    }


def existing_record(conn, key):
    """(item_count, total_seconds) already stored for this day, or None."""
    row = conn.execute(
        "SELECT total_seconds FROM day_summary "
        "WHERE device = ? AND usage_date = ? AND view_mode = ?", key).fetchone()
    if row is None:
        return None
    count = conn.execute(
        "SELECT COUNT(*) FROM app_usage "
        "WHERE device = ? AND usage_date = ? AND view_mode = ?", key).fetchone()[0]
    return count, row[0]


def existing_pickups(conn, key):
    """(item_count, total_pickups) already stored for this day, or None."""
    row = conn.execute(
        "SELECT total_pickups FROM pickup_summary "
        "WHERE device = ? AND usage_date = ?", key).fetchone()
    if row is None:
        return None
    count = conn.execute(
        "SELECT COUNT(*) FROM pickup_usage "
        "WHERE device = ? AND usage_date = ?", key).fetchone()[0]
    return count, row[0]


def store_pickups(db_path, data, usage_date, scraped_at, force=False):
    conn = sqlite3.connect(db_path)
    try:
        conn.executescript(SCHEMA)
        key = (data["device"], usage_date.isoformat())

        total = parse_count(data["total_text"])
        items = [(name, parse_count(text)) for _, name, text in data["items"]]

        previous = existing_pickups(conn, key)
        if previous is not None and not force:
            old_count, old_total = previous
            if len(items) < old_count or total < old_total:
                raise ScrapeError(
                    "this scrape looks incomplete, so it was not stored.\n"
                    "  already stored: %d apps, %d pick-ups\n"
                    "  this scrape:    %d apps, %d pick-ups\n"
                    "Re-run to try again, or pass --force to overwrite anyway."
                    % (old_count, old_total, len(items), total))

        with conn:
            for table in ("pickup_summary", "pickup_usage"):
                conn.execute(
                    "DELETE FROM %s WHERE device = ? AND usage_date = ?" % table,
                    key)

            conn.execute(
                "INSERT INTO pickup_summary (device, usage_date, total_pickups, "
                "date_label, scraped_at) VALUES (?, ?, ?, ?, ?)",
                key + (total, data["date_label"], scraped_at))

            conn.executemany(
                "INSERT INTO pickup_usage (device, usage_date, item_name, "
                "pickups, scraped_at) VALUES (?, ?, ?, ?, ?)",
                [key + (name, count, scraped_at) for name, count in items])
    finally:
        conn.close()


def store(db_path, data, usage_date, scraped_at, force=False):
    conn = sqlite3.connect(db_path)
    try:
        conn.executescript(SCHEMA)
        key = (data["device"], usage_date.isoformat(), data["view_mode"])

        # The table is virtualized, so a read taken while it is re-rendering can
        # come back short. Since storing replaces the day wholesale, refuse to
        # trade a fuller record for a thinner one unless explicitly forced.
        previous = existing_record(conn, key)
        if previous is not None and not force:
            old_count, old_total = previous
            new_count = len(data["items"])
            new_total = parse_duration(data["total_text"])
            if new_count < old_count or new_total < old_total:
                raise ScrapeError(
                    "this scrape looks incomplete, so it was not stored.\n"
                    "  already stored: %d items, %d seconds\n"
                    "  this scrape:    %d items, %d seconds\n"
                    "Re-run to try again, or pass --force to overwrite anyway."
                    % (old_count, old_total, new_count, new_total))

        with conn:
            # Replace the day wholesale rather than upserting row by row, so
            # apps that dropped out of the list since the last scrape go away.
            for table in ("day_summary", "category_usage", "app_usage"):
                conn.execute(
                    "DELETE FROM %s WHERE device = ? AND usage_date = ? "
                    "AND view_mode = ?" % table, key)

            conn.execute(
                "INSERT INTO day_summary (device, usage_date, view_mode, "
                "total_seconds, total_text, date_label, scraped_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?)",
                key + (parse_duration(data["total_text"]), data["total_text"],
                       data["date_label"], scraped_at))

            conn.executemany(
                "INSERT INTO category_usage (device, usage_date, view_mode, "
                "category_name, seconds, duration_text, scraped_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?)",
                [key + (name, parse_duration(text), text, scraped_at)
                 for name, text in data["categories"]])

            conn.executemany(
                "INSERT INTO app_usage (device, usage_date, view_mode, "
                "item_name, item_level, seconds, duration_text, scraped_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                [key + (name, level, parse_duration(text), text, scraped_at)
                 for level, name, text in data["items"]])
    finally:
        conn.close()


def send_to_server(base_url, token, data, usage_date, scraped_at, force=False):
    """POST a scrape to a remote webapp.py instead of writing SQLite locally."""
    payload = {
        "screen": data["screen"],
        "device": data["device"],
        "usage_date": usage_date.isoformat(),
        "date_label": data["date_label"],
        "view_mode": data["view_mode"],
        "total_text": data["total_text"],
        "categories": data["categories"],
        "items": data["items"],
        "scraped_at": scraped_at,
        "force": force,
    }
    request = urllib.request.Request(
        base_url.rstrip("/") + "/api/ingest",
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST")
    if token:
        request.add_header("Authorization", "Bearer " + token)

    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            return json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", "replace")
        try:
            detail = json.loads(detail).get("error", detail)
        except json.JSONDecodeError:
            pass
        if exc.code == 401:
            raise ScrapeError(
                "the server rejected the token. Pass --token or set "
                "$SCREENTIME_TOKEN to the same value the server was started with.")
        raise ScrapeError("the server refused this scrape (HTTP %d).\n%s"
                          % (exc.code, detail))
    except urllib.error.URLError as exc:
        raise ScrapeError("could not reach %s: %s" % (base_url, exc.reason))


def main():
    parser = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--db", default=DEFAULT_DB,
                        help="SQLite file to write (default: %(default)s)")
    parser.add_argument("--dry-run", action="store_true",
                        help="print what would be stored, without writing")
    parser.add_argument("--force", action="store_true",
                        help="overwrite even if this scrape looks incomplete")
    parser.add_argument("--server", default=os.environ.get("SCREENTIME_SERVER"),
                        help="base URL of a webapp.py instance to send this "
                             "scrape to instead of writing --db locally. "
                             "Defaults to $SCREENTIME_SERVER")
    parser.add_argument("--token", default=os.environ.get("SCREENTIME_TOKEN"),
                        help="shared secret for --server. Defaults to "
                             "$SCREENTIME_TOKEN")
    args = parser.parse_args()

    if args.server and args.server.startswith("http://") and \
            not args.server.startswith(("http://localhost", "http://127.0.0.1")):
        print("warning: %s is plain HTTP, so the token and this data cross the "
              "network unencrypted." % args.server, file=sys.stderr)

    try:
        data = parse_scraper_output(run_scraper())
        usage_date = parse_date_label(data["date_label"])
    except ScrapeError as exc:
        print("error: %s" % exc, file=sys.stderr)
        return 1

    scraped_at = datetime.now().isoformat(timespec="seconds")
    pickups = data["screen"] == "pickups"

    try:
        if pickups:
            print("%s  %s  (pick-ups)" % (data["device"], usage_date.isoformat()))
            print("  total %s pick-ups" % data["total_text"])
        else:
            print("%s  %s  (%s)"
                  % (data["device"], usage_date.isoformat(), data["view_mode"]))
            print("  total %s (%d seconds)"
                  % (data["total_text"], parse_duration(data["total_text"])))
        for name, text in data["categories"]:
            print("  category  %-28s %s" % (name, text))
        for level, name, text in data["items"]:
            print("  %s%-38s %s" % ("  " * level, name, text))
    except ScrapeError as exc:
        print("error: %s" % exc, file=sys.stderr)
        return 1

    if not pickups and data["view_mode"] != "Show Apps":
        print('note: view mode is "%s"; stored separately from "Show Apps" data.'
              % data["view_mode"])
    if data["device"] == "All Devices":
        print("note: this is the combined total across devices, stored under "
              'the device name "All Devices".')

    if args.dry_run:
        print("\ndry run - nothing written")
        return 0

    label = "apps" if pickups else "items"
    try:
        if args.server:
            send_to_server(args.server, args.token, data, usage_date,
                           scraped_at, force=args.force)
            target = args.server
        else:
            if pickups:
                store_pickups(args.db, data, usage_date, scraped_at, force=args.force)
            else:
                store(args.db, data, usage_date, scraped_at, force=args.force)
            target = args.db
    except ScrapeError as exc:
        print("\nerror: %s" % exc, file=sys.stderr)
        return 1

    print("\nstored %d %s in %s at %s"
          % (len(data["items"]), label, target, scraped_at))
    return 0


if __name__ == "__main__":
    sys.exit(main())
