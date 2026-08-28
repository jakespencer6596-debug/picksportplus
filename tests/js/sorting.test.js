"use strict";

/* Phase 4, "weekly tiebreak/sorting/performance work" (see PERF-REPORT.md and DECISIONS.md):
   the shared table sorting engine (app/static/app.js's table[data-sortable] machinery, now
   with localStorage persistence and an htmx-swap resort hook) and the picks page's own
   .game-list sort (a plain <select>, since an <ol> has no header row to click). Same jsdom
   harness as tests/js/pick_navigation.test.js: Node's built-in test runner, no bundler. */

const test = require("node:test");
const after = require("node:test").after;
const assert = require("node:assert/strict");
const fs = require("fs");
const path = require("path");
const { JSDOM } = require("jsdom");

const APP_JS = fs.readFileSync(
  path.join(__dirname, "..", "..", "app", "static", "app.js"),
  "utf8"
);

var windows = [];

function baseDoc(bodyHtml) {
  return "<!doctype html><html><body>" + bodyHtml + "</body></html>";
}

/* preSeed runs against the real window.localStorage BEFORE app.js's own init() executes,
   which is the accurate way to simulate "the browser already remembers a sort from an
   earlier visit": jsdom does not share localStorage between separate JSDOM() instances even
   at the same URL (each is a fully isolated environment), so a second setup() call is NOT a
   page reload, it is a different browser profile. Seeding the same window this test already
   has, before the code under test reads it, is what actually exercises the "restore on load"
   path. */
function setup(bodyHtml, preSeed) {
  return new Promise((resolve) => {
    var dom = new JSDOM(baseDoc(bodyHtml), {
      runScripts: "dangerously",
      url: "https://picksportplus.test/test",
    });
    var window = dom.window;
    window.matchMedia = function () {
      return { matches: false };
    };
    window.Element.prototype.scrollIntoView = function () {};
    windows.push(window);
    if (preSeed) preSeed(window);

    function loadAppJs() {
      var script = window.document.createElement("script");
      script.textContent = APP_JS;
      window.document.body.appendChild(script);
      resolve(window);
    }

    if (window.document.readyState === "loading") {
      window.document.addEventListener("DOMContentLoaded", loadAppJs);
    } else {
      loadAppJs();
    }
  });
}

after(() => {
  windows.forEach(function (window) {
    window.close();
  });
});

/* ------------------------------------------------------------ table sort */

function sortableTableHtml(id) {
  return (
    '<table id="' +
    id +
    '" data-sortable data-default-sort-col="0">' +
    "<thead><tr>" +
    '<th scope="col" data-sortable-col data-sort-default-dir="asc">Name</th>' +
    '<th scope="col" data-sortable-col data-sort-default-dir="asc">Points</th>' +
    "</tr></thead>" +
    '<tbody id="' +
    id +
    '-tbody">' +
    '<tr><td>Carol</td><td data-sort-value="30">30</td></tr>' +
    '<tr><td>Alice</td><td data-sort-value="10">10</td></tr>' +
    '<tr><td>Bob</td><td data-sort-value="20">20</td></tr>' +
    "</tbody></table>"
  );
}

function tableNames(window, tbodyId) {
  return Array.prototype.slice
    .call(window.document.querySelectorAll("#" + tbodyId + " tr"))
    .map(function (tr) {
      return tr.cells[0].textContent;
    });
}

test("clicking a numeric column header sorts by data-sort-value, not text", async () => {
  var window = await setup(sortableTableHtml("t1"));
  var pointsHeader = window.document.querySelectorAll("#t1 th")[1];
  pointsHeader.click();
  assert.deepEqual(tableNames(window, "t1-tbody"), ["Alice", "Bob", "Carol"]);
  assert.equal(pointsHeader.getAttribute("aria-sort"), "ascending");

  pointsHeader.click();
  assert.deepEqual(tableNames(window, "t1-tbody"), ["Carol", "Bob", "Alice"]);
  assert.equal(pointsHeader.getAttribute("aria-sort"), "descending");
});

test("a chosen sort is persisted to localStorage and restored on the next init", async () => {
  var window = await setup(sortableTableHtml("t2"));
  var pointsHeader = window.document.querySelectorAll("#t2 th")[1];
  pointsHeader.click(); // ascending by points

  assert.equal(window.localStorage.getItem("psp-sort:t2"), "1:asc");

  // A fresh load with that same value already sitting in localStorage (a real browser reload
  // keeps localStorage; jsdom does not share it between separate JSDOM() instances, so it is
  // seeded directly here, before app.js's own init() runs): initSortableTable must read it and
  // apply it immediately, with no click required.
  var restored = await setup(sortableTableHtml("t2"), function (w) {
    w.localStorage.setItem("psp-sort:t2", "1:asc");
  });
  assert.deepEqual(tableNames(restored, "t2-tbody"), ["Alice", "Bob", "Carol"]);
  var restoredHeader = restored.document.querySelectorAll("#t2 th")[1];
  assert.equal(restoredHeader.getAttribute("aria-sort"), "ascending");
});

test("an htmx:afterSwap on the tbody re-applies the active sort to new rows", async () => {
  var window = await setup(sortableTableHtml("t3"));
  var pointsHeader = window.document.querySelectorAll("#t3 th")[1];
  pointsHeader.click(); // ascending by points: Alice(10), Bob(20), Carol(30)

  // Simulate an HTMX partial swap dropping in a fresh, unsorted tbody (a slate action's own
  // OOB refresh always arrives in server default order, never pre-sorted).
  var tbody = window.document.getElementById("t3-tbody");
  tbody.innerHTML =
    '<tr><td>Dana</td><td data-sort-value="5">5</td></tr>' +
    '<tr><td>Eli</td><td data-sort-value="25">25</td></tr>';

  var evt = new window.Event("htmx:afterSwap", { bubbles: true });
  Object.defineProperty(evt, "target", { value: tbody });
  window.document.dispatchEvent(evt);

  assert.deepEqual(tableNames(window, "t3-tbody"), ["Dana", "Eli"]);
});

/* ------------------------------------------------------------ picks sort */

function pickRow(id, opts) {
  opts = opts || {};
  var confidence = opts.confidence === undefined ? "" : String(opts.confidence);
  return (
    '<li class="game-row" data-game-id="' +
    id +
    '" data-confidence="' +
    confidence +
    '" data-sort-slate="' +
    opts.slate +
    '" data-sort-kickoff="' +
    opts.kickoff +
    '" data-sort-league="' +
    opts.league +
    '" data-sort-closeness="' +
    opts.closeness +
    '" data-sort-source="' +
    opts.source +
    '" data-sort-matchup="' +
    opts.matchup +
    '" tabindex="0">' +
    '<span class="game-grip"></span>' +
    '<div class="conf-controls">' +
    '<input type="number" class="input conf-input" data-conf-input value="' +
    confidence +
    '">' +
    '<span class="conf-chip num">' +
    (confidence || "-") +
    "</span>" +
    "</div>" +
    '<input type="hidden" data-conf name="confidence-' +
    id +
    '" value="' +
    confidence +
    '">' +
    "</li>"
  );
}

function picksPageHtml(rows) {
  return (
    '<select data-game-sort-select>' +
    '<option value="slate:asc">Slate order</option>' +
    '<option value="kickoff:asc">Kickoff, earliest first</option>' +
    '<option value="kickoff:desc">Kickoff, latest first</option>' +
    '<option value="league:asc">League, A to Z</option>' +
    '<option value="closeness:asc">Closest first</option>' +
    '<option value="source:asc">Source, A to Z</option>' +
    '<option value="matchup:asc">Matchup, A to Z</option>' +
    "</select>" +
    '<button type="button" data-game-sort-reset>Reset to slate order</button>' +
    '<p data-pick-summary></p>' +
    '<div data-pick-meter></div>' +
    '<ol class="game-list" data-sortable data-picks-required="3">' +
    rows.join("") +
    '<li class="game-list-divider" data-divider hidden><span>Not picked</span></li>' +
    "</ol>"
  );
}

function gameListIds(window) {
  return Array.prototype.slice
    .call(window.document.querySelectorAll(".game-row"))
    .map(function (row) {
      return row.dataset.gameId;
    });
}

test("sorting the picks page by kickoff time reorders rows without touching confidence", async () => {
  var window = await setup(
    picksPageHtml([
      pickRow(1, { slate: 1, kickoff: 300, league: "nfl", closeness: 3, source: "espn", matchup: "C at D", confidence: 3 }),
      pickRow(2, { slate: 2, kickoff: 100, league: "ncaaf", closeness: 1, source: "cfbd", matchup: "A at B", confidence: 1 }),
      pickRow(3, { slate: 3, kickoff: 200, league: "nfl", closeness: 2, source: "odds_api", matchup: "E at F", confidence: 2 }),
    ])
  );

  var select = window.document.querySelector("[data-game-sort-select]");
  select.value = "kickoff:asc";
  select.dispatchEvent(new window.Event("change", { bubbles: true }));

  assert.deepEqual(gameListIds(window), ["2", "3", "1"]);
  // Confidence values travel with their own row, untouched by the sort.
  assert.equal(window.document.querySelector('[data-game-id="1"]').dataset.confidence, "3");
  assert.equal(window.document.querySelector('[data-game-id="2"]').dataset.confidence, "1");
  assert.equal(window.document.querySelector('[data-game-id="3"]').dataset.confidence, "2");
});

test("the keyboard tab sequence follows the picks page's new sorted order", async () => {
  var window = await setup(
    picksPageHtml([
      pickRow(1, { slate: 1, kickoff: 300, league: "nfl", closeness: 3, source: "espn", matchup: "C at D" }),
      pickRow(2, { slate: 2, kickoff: 100, league: "ncaaf", closeness: 1, source: "cfbd", matchup: "A at B" }),
    ])
  );
  var select = window.document.querySelector("[data-game-sort-select]");
  select.value = "kickoff:asc";
  select.dispatchEvent(new window.Event("change", { bubbles: true }));

  var inputs = Array.prototype.slice.call(window.document.querySelectorAll(".conf-input"));
  assert.equal(inputs[0].closest(".game-row").dataset.gameId, "2");
  inputs[0].focus();
  var evt = new window.KeyboardEvent("keydown", { key: "Tab", bubbles: true, cancelable: true });
  inputs[0].dispatchEvent(evt);
  assert.equal(window.document.activeElement.closest(".game-row").dataset.gameId, "1");
});

test('"Reset to slate order" restores the server-rendered slate rank order', async () => {
  var window = await setup(
    picksPageHtml([
      pickRow(1, { slate: 1, kickoff: 300, league: "nfl", closeness: 3, source: "espn", matchup: "C at D" }),
      pickRow(2, { slate: 2, kickoff: 100, league: "ncaaf", closeness: 1, source: "cfbd", matchup: "A at B" }),
    ])
  );
  var select = window.document.querySelector("[data-game-sort-select]");
  select.value = "kickoff:asc";
  select.dispatchEvent(new window.Event("change", { bubbles: true }));
  assert.deepEqual(gameListIds(window), ["2", "1"]);

  window.document.querySelector("[data-game-sort-reset]").click();
  assert.deepEqual(gameListIds(window), ["1", "2"]);
  assert.equal(select.value, "slate:asc");
});

test("the picks page sort choice is persisted and restored on the next load", async () => {
  var rows = [
    pickRow(1, { slate: 1, kickoff: 300, league: "nfl", closeness: 3, source: "espn", matchup: "C at D" }),
    pickRow(2, { slate: 2, kickoff: 100, league: "ncaaf", closeness: 1, source: "cfbd", matchup: "A at B" }),
  ];
  var window = await setup(picksPageHtml(rows));
  var select = window.document.querySelector("[data-game-sort-select]");
  select.value = "kickoff:asc";
  select.dispatchEvent(new window.Event("change", { bubbles: true }));
  assert.equal(window.localStorage.getItem("psp-sort:picks"), "kickoff:asc");

  // Same reasoning as the table persistence test above: seed the value a real reload would
  // already have in localStorage, on the window this fresh setup() call actually uses.
  var restored = await setup(picksPageHtml(rows), function (w) {
    w.localStorage.setItem("psp-sort:picks", "kickoff:asc");
  });
  assert.deepEqual(gameListIds(restored), ["2", "1"]);
  assert.equal(restored.document.querySelector("[data-game-sort-select]").value, "kickoff:asc");
});
