# Screen Time logger

Records the kids' Apple Screen Time into a local SQLite database, by reading the
numbers straight out of the macOS System Settings window.

## Why it works this way

Family members' Screen Time is **not stored on this Mac in any readable form**.
The Screen Time pane fetches a child's usage from iCloud each time you open it
and then discards it. The candidate local databases are all dead ends:

- `~/Library/Application Support/Knowledge/knowledgeC.db` - this Mac's own usage only
- `~/Library/Group Containers/group.com.apple.ScreenTime` - empty
- `~/Library/Application Support/com.apple.RemoteManagementAgent/...` - MDM config, no usage
- `/private/var/db/rmd` - the real family usage store, but SIP-protected

Apple confirms there is no public API for raw Screen Time values, even for a
parent: `FamilyControls` / `DeviceActivityReport` only render a report inside a
sandboxed extension. Every published open-source tool reads the *parent's own*
device, not family members'.

So this reads the values off the rendered UI via the Accessibility API. That is
the only route to per-app, per-day numbers for a child's device.

## Setup

Terminal needs Accessibility permission:
**System Settings > Privacy & Security > Accessibility** -> enable Terminal.

## Use

1. Open **System Settings > Screen Time**, then either
   **App & Website Activity** or **Pick-Ups**.
2. Pick the device (e.g. "Nolan's iPhone") and the day you want.
3. Run:

```sh
python3 log_screentime.py            # store the day now on screen
python3 log_screentime.py --dry-run  # parse and print, write nothing
python3 log_screentime.py --force    # overwrite despite the safety check
```

Repeat per device, per day, and per screen. The device name is stored with every
row, so several devices coexist in one database.

The script detects which screen is showing and writes to the matching tables -
you don't tell it which. Detection is structural, not by window title: the app
screen has a "Show Apps" pop-up above its table and the pick-ups screen doesn't.

If the device is left on **All Devices**, that combined total is stored under the
device name `All Devices` rather than being split per device.

Re-running for a day already recorded **replaces** that day, so scraping the
same date again later just refreshes it with newer numbers. Because a replace
throws away the old rows, the script refuses to overwrite a fuller record with a
thinner one (fewer apps, or a smaller total) unless you pass `--force` - the
Settings table is virtualized and an ill-timed read can come back short.

## Schema

`screentime.db`. The App & Website Activity tables are keyed by
`(device, usage_date, view_mode)`:

- **`day_summary`** - the day's total, plus the date label as displayed and the scrape timestamp
- **`category_usage`** - the Social / Entertainment / Other breakdown
- **`app_usage`** - one row per app or website, with `seconds` and the original text

The Pick-Ups tables are keyed by `(device, usage_date)` - that screen has no view
mode:

- **`pickup_summary`** - total pick-ups for the day
- **`pickup_usage`** - one row per app, with a `pickups` count

Durations are stored both as text (`"1 hour, 52 minutes"`) and as integer
`seconds`, so queries can just use `seconds`. Pick-ups are plain integers.

```sh
# biggest apps across all devices last week
sqlite3 -column -header screentime.db "
  SELECT device, item_name, SUM(seconds)/60 AS minutes
  FROM app_usage
  WHERE usage_date >= date('now', '-7 days')
  GROUP BY device, item_name
  ORDER BY minutes DESC
  LIMIT 20;"

# most picked-up apps last week
sqlite3 -column -header screentime.db "
  SELECT device, item_name, SUM(pickups) AS pickups
  FROM pickup_usage
  WHERE usage_date >= date('now', '-7 days')
  GROUP BY device, item_name
  ORDER BY pickups DESC
  LIMIT 20;"
```

## Files

- `scrape_screentime.js` - JXA; reads the visible pane, prints tab-separated records
- `log_screentime.py` - runs the scraper, parses durations and dates, writes SQLite

The scraper is deliberately read-only apart from clearing the table selection,
and never navigates between days or devices - driving the date stepper proved
unreliable because the pane re-renders asynchronously.

### Notes for future changes

Two macOS quirks the scraper works around, both easy to reintroduce:

- Selecting a row switches the header from the whole-day summary to that row's
  detail, so the total and categories silently become one app's numbers. The
  scraper clears the selection first.
- The window title carries a live "Updated at HH:MM" clock and System Events
  returns *name-based* references, so any reference held across a minute
  boundary goes stale. Resolve a short element path and read it quickly.

It is JXA rather than AppleScript because AppleScript's parser rejects forms
needed here (`cell 1`, `static text 1`, loop variables as element specifiers),
and silently fails to dereference elements outside a `tell` block.
