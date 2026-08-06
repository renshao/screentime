#!/usr/bin/env osascript -l JavaScript
//
// Reads the CURRENTLY displayed day from either
//   System Settings > Screen Time > App & Website Activity
//   System Settings > Screen Time > Pick-Ups
//
// It deliberately does NOT navigate between days, devices or screens: select
// what you want in the UI, then run this. Driving the date stepper was
// unreliable because the pane re-renders asynchronously.
//
// Usage: osascript -l JavaScript scrape_screentime.js
//
// Output is UTF-8, tab-separated, one record per line:
//   SCREEN    <"apps" | "pickups">
//   DEVICE    <device name, or "All Devices">
//   DATE      <date pop-up label, e.g. "Today, 5 August">
//   MODE      <"Show Apps" | "Show Websites" | ...>   (apps screen only)
//   TOTAL     <duration text, or a pick-up count>
//   CATEGORY  <category name>                  <duration text>
//   ROW       <disclosure level>  <app name>  <duration text or count>
//
// The leading "All Usage" / "All Pick-ups" row is skipped: it is just the
// total, which TOTAL already carries.
//
// Two things this has to work around:
//
//  1. Selecting a row in the table switches the header from the whole-day
//     summary to that row's detail, so the total and the category breakdown
//     become the selected app's numbers. We clear the selection first.
//
//  2. The window title carries a live "Updated at HH:MM" clock, and System
//     Events hands back *name-based* references, so any reference held across a
//     minute boundary goes stale. The fix is to resolve a short, direct element
//     path and read it quickly, retrying on failure.
//
// (Written in JXA rather than AppleScript because AppleScript's parser rejects
// several forms needed here - "cell 1", "static text 1", and loop variables
// used as element specifiers.)

ObjC.import('Foundation');

// Application.currentApplication().delay() is not reliably available when this
// runs under plain osascript, so sleep through the ObjC bridge instead.
function sleep(seconds) {
  $.NSThread.sleepForTimeInterval(seconds);
}

const ATTEMPTS = 8;
const RETRY_DELAY = 0.5;

// The first row of either table is the whole-day total, which TOTAL already
// carries, so it is not emitted as an item.
const TOTAL_ROW_LABELS = ['All Usage', 'All Pick-ups'];

function paneRoot() {
  const proc = Application('System Events').processes['System Settings'];
  const win = proc.windows[0];
  // window > group > splitter group > [sidebar, search, splitter, RIGHT PANE]
  const rightPane = win.groups[0].splitterGroups[0].groups[2];
  return rightPane.groups[0];
}

function textOf(el) {
  try {
    const v = el.value();
    return v === null || v === undefined ? '' : String(v);
  } catch (e) {
    return '';
  }
}

// The header only reports whole-day numbers while nothing is selected.
function clearSelection(outline) {
  const rows = outline.rows();
  for (let i = 0; i < rows.length; i++) {
    try {
      if (rows[i].selected()) rows[i].selected = false;
    } catch (e) { /* row vanished mid-pass; the recheck below will catch it */ }
  }
  for (let i = 0; i < rows.length; i++) {
    try {
      if (rows[i].selected()) throw new Error('could not clear the table selection');
    } catch (e) {
      if (e.message === 'could not clear the table selection') throw e;
    }
  }
}

function scrapeOnce(attempt) {
  const inner = paneRoot();
  const headerArea = inner.scrollAreas[0];
  const tableArea = inner.scrollAreas[1].groups[0];

  // --- the app / website table ---------------------------------------------
  let outline = null;
  try {
    outline = tableArea.scrollAreas[0].outlines[0];
    outline.rows();
  } catch (e) {
    // Not drawn yet. Retry a few times before concluding the day is empty.
    if (attempt < ATTEMPTS - 2) throw new Error('app table has not rendered yet');
    outline = null;
  }
  if (outline) {
    clearSelection(outline);
    // Clearing the selection makes the table re-render, and the list is
    // virtualized, so a read taken mid-rebuild can return only some of the
    // rows. Require the count to hold steady before trusting it - otherwise a
    // partial read would look like a legitimately shorter day.
    const before = outline.rows().length;
    sleep(0.3);
    const after = outline.rows().length;
    if (before !== after) {
      throw new Error(
        'row count still settling (' + before + ' then ' + after + ')');
    }
  }

  // --- header: device, date, day total, category breakdown -----------------
  // Children run: [Device label, devicePop, Usage label, usagePop,
  //                prev, today, next, TOTAL, group, group, cat, dur, ...]
  // so every static text appearing after the 2nd pop-up is what we want.
  const kids = headerArea.groups[0].uiElements();

  const popups = [];
  const headerTexts = [];
  let popSeen = 0;
  for (let i = 0; i < kids.length; i++) {
    let role;
    try {
      role = kids[i].role();
    } catch (e) {
      continue;
    }
    if (role === 'AXPopUpButton') {
      popSeen++;
      popups.push(kids[i]);
    } else if (popSeen >= 2 && role === 'AXStaticText') {
      headerTexts.push(textOf(kids[i]));
    }
  }

  if (popups.length < 2) {
    throw new Error(
      'expected the device and date pop-ups but found ' + popups.length +
      ' - is System Settings showing Screen Time > App & Website Activity?');
  }
  if (headerTexts.length < 1) {
    throw new Error('could not read the day total yet');
  }

  // "App & Website Activity" carries a view-mode pop-up ("Show Apps") above its
  // table; "Pick-Ups" has no such control. That structural difference is what
  // tells the two screens apart - more dependable than the window title or the
  // header wording, both of which are localized.
  let viewMode = null;
  try {
    const modePopups = tableArea.popUpButtons();
    if (modePopups.length > 0) viewMode = textOf(modePopups[0]);
  } catch (e) { /* no pop-up: the pick-ups screen */ }
  const screen = viewMode === null ? 'pickups' : 'apps';

  const lines = [];
  lines.push('SCREEN\t' + screen);
  lines.push('DEVICE\t' + textOf(popups[0]));
  lines.push('DATE\t' + textOf(popups[1]));
  if (viewMode !== null) lines.push('MODE\t' + viewMode);
  lines.push('TOTAL\t' + headerTexts[0]);
  for (let i = 1; i + 1 < headerTexts.length; i += 2) {
    lines.push('CATEGORY\t' + headerTexts[i] + '\t' + headerTexts[i + 1]);
  }

  if (!outline) return lines.join('\n') + '\n';

  const rows = outline.rows();
  for (let i = 0; i < rows.length; i++) {
    const row = rows[i];
    let name = '';
    let duration = '';
    let level = 0;

    try {
      const cells = row.uiElements();
      try {
        const texts = cells[0].uiElements()[0].staticTexts();
        if (texts.length > 0) name = textOf(texts[0]);
      } catch (e) { /* row without a name cell */ }
      try {
        const texts = cells[1].uiElements()[0].staticTexts();
        if (texts.length > 0) duration = textOf(texts[0]);
      } catch (e) { /* row without a duration cell */ }
    } catch (e) {
      continue;
    }

    try {
      level = parseInt(row.attributes.byName('AXDisclosureLevel').value(), 10) || 0;
    } catch (e) {
      level = 0;
    }

    if (!name) continue;
    if (TOTAL_ROW_LABELS.indexOf(name) !== -1) continue;

    lines.push('ROW\t' + level + '\t' + name + '\t' + duration);
  }

  return lines.join('\n') + '\n';
}

function run() {
  let lastErr = 'unknown error';
  for (let attempt = 0; attempt < ATTEMPTS; attempt++) {
    try {
      return scrapeOnce(attempt);
    } catch (e) {
      lastErr = e.message || String(e);
      sleep(RETRY_DELAY);
    }
  }
  throw new Error('gave up after ' + ATTEMPTS + ' attempts. Last error: ' + lastErr);
}
