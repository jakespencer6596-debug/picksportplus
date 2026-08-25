"use strict";

/* Phase 1, "Tab entry and season tiebreak": keyboard navigation between confidence inputs
   and between a row's two team choices, implemented in app/static/app.js (onKeydown and its
   helpers). This is the repo's first front-end test, so it also builds the minimum harness
   needed to run one: Node's own built-in test runner plus jsdom, no bundler, no framework,
   matching the app itself (Vanilla JS only, no CSS framework, per SPEC.md Section 4). See
   DECISIONS.md, "Tab entry and season tiebreak", for why this was added instead of, say,
   Playwright: the behavior under test is pure DOM/keyboard logic with no real rendering,
   animation, or network involved, which jsdom covers completely and far more cheaply than a
   real browser would. */

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

function teamButton(side, label, picked) {
  return (
    '<button type="button" class="team-btn' +
    (picked ? " is-picked" : "") +
    '" data-side="' +
    side +
    '" aria-pressed="' +
    (picked ? "true" : "false") +
    '">' +
    '<span class="team-abbr">' +
    label +
    "</span>" +
    "</button>"
  );
}

function gameRow(id, opts) {
  opts = opts || {};
  var picked = opts.picked || null; // "home" | "away" | null
  var confidence = opts.confidence === undefined ? "" : String(opts.confidence);
  var isPicked = picked !== null;
  return (
    '<li class="game-row' +
    (isPicked ? "" : " is-not-picked") +
    '" data-game-id="' +
    id +
    '" data-confidence="' +
    confidence +
    '" tabindex="0">' +
    '<span class="game-grip"></span>' +
    '<div class="game-teams game-teams-at">' +
    teamButton("away", "AW" + id, picked === "away") +
    '<span class="game-at">at</span>' +
    teamButton("home", "HM" + id, picked === "home") +
    "</div>" +
    '<div class="game-meta">' +
    '<div class="conf-controls">' +
    '<label class="conf-field">' +
    '<span class="visually-hidden">Confidence for Away' +
    id +
    " at Home" +
    id +
    "</span>" +
    '<input type="number" inputmode="numeric" class="input conf-input" data-conf-input ' +
    'min="1" max="3" step="1" value="' +
    confidence +
    '">' +
    "</label>" +
    '<span class="conf-chip num' +
    (confidence ? "" : " is-empty") +
    '">' +
    (confidence || "-") +
    "</span>" +
    "</div>" +
    "</div>" +
    '<div class="rank-controls">' +
    '<button type="button" class="rank-btn" data-dir="up"></button>' +
    '<button type="button" class="rank-btn" data-dir="down"></button>' +
    "</div>" +
    '<input type="hidden" data-pick name="winner-' +
    id +
    '" value="' +
    (picked || "") +
    '">' +
    '<input type="hidden" data-conf name="confidence-' +
    id +
    '" value="' +
    confidence +
    '">' +
    "</li>"
  );
}

function buildPage(rows, picksRequired) {
  return (
    "<!doctype html><html><body>" +
    '<p data-pick-summary></p>' +
    '<div data-pick-meter></div>' +
    '<button type="button" data-save-btn disabled></button>' +
    '<button type="button" data-reorder-btn>Reorder</button>' +
    '<div class="panel">' +
    '<button type="button" data-lock-open disabled>Lock picks</button>' +
    '<div data-lock-panel hidden><ol data-lock-summary></ol></div>' +
    "</div>" +
    '<div data-save-target></div>' +
    '<ol class="game-list" data-sortable data-picks-required="' +
    picksRequired +
    '">' +
    rows.join("") +
    '<li class="game-list-divider" data-divider hidden><span>Not picked</span></li>' +
    "</ol>" +
    '<a href="#save">Back to save</a>' +
    "</body></html>"
  );
}

/* One jsdom window per test, app.js loaded fresh into it (the IIFE re-runs its own init()
   against this window's document, exactly like a real page load). matchMedia and
   scrollIntoView are not implemented by jsdom; both are stubbed before app.js runs, since
   app.js reads matchMedia once at module load and calls scrollIntoView on every focus move
   this test exercises. app.js's own init() also starts a real setInterval (the lock
   countdown); every window this creates is tracked here and closed in one top level after()
   hook, which is what actually clears those intervals, otherwise the test process never
   exits. */
var windows = [];

/* jsdom parses the document string asynchronously even though the constructor call itself
   is synchronous: readyState is still "loading" and DOMContentLoaded has not fired yet at
   the moment this function returns, so app.js's own init() (registered on DOMContentLoaded,
   exactly like it would be in a real browser) has not run and has not wired up any event
   listener yet. Returning a promise that resolves only once DOMContentLoaded has actually
   fired, then injecting app.js, is what makes a synchronous-looking test (focus, dispatch a
   key, assert) safe to write against a jsdom window: every test below awaits this. */
function setup(rows, picksRequired) {
  return new Promise((resolve) => {
    var dom = new JSDOM(buildPage(rows, picksRequired), {
      runScripts: "dangerously",
      url: "https://picksportplus.test/picks",
    });
    var window = dom.window;
    window.matchMedia = function () {
      return { matches: false };
    };
    window.Element.prototype.scrollIntoView = function () {};
    windows.push(window);

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

function confInputs(window) {
  return Array.prototype.slice.call(window.document.querySelectorAll(".conf-input"));
}

function tab(el, opts) {
  opts = opts || {};
  var evt = new el.ownerDocument.defaultView.KeyboardEvent("keydown", {
    key: "Tab",
    shiftKey: !!opts.shift,
    bubbles: true,
    cancelable: true,
  });
  el.dispatchEvent(evt);
  return evt;
}

function arrow(el, key) {
  var evt = new el.ownerDocument.defaultView.KeyboardEvent("keydown", {
    key: key,
    bubbles: true,
    cancelable: true,
  });
  el.dispatchEvent(evt);
  return evt;
}

test("Tab from confidence input N focuses confidence input N+1", async () => {
  var window = await setup(
    [gameRow(1, { picked: "home", confidence: 1 }), gameRow(2, { picked: "away", confidence: 2 })],
    2
  );
  var inputs = confInputs(window);
  inputs[0].focus();
  var evt = tab(inputs[0]);
  assert.equal(window.document.activeElement, inputs[1]);
  assert.equal(evt.defaultPrevented, true);
});

test("Shift+Tab from confidence input N focuses confidence input N-1", async () => {
  var window = await setup(
    [gameRow(1, { picked: "home", confidence: 1 }), gameRow(2, { picked: "away", confidence: 2 })],
    2
  );
  var inputs = confInputs(window);
  inputs[1].focus();
  tab(inputs[1], { shift: true });
  assert.equal(window.document.activeElement, inputs[0]);
});

test("Shift+Tab on the first confidence input is left alone (no wrap to the last row)", async () => {
  var window = await setup(
    [gameRow(1, { picked: "home", confidence: 1 }), gameRow(2, { picked: "away", confidence: 2 })],
    2
  );
  var inputs = confInputs(window);
  inputs[0].focus();
  var evt = tab(inputs[0], { shift: true });
  assert.equal(evt.defaultPrevented, false);
  assert.equal(window.document.activeElement, inputs[0]);
});

test("Tab from the last confidence input never wraps to the first", async () => {
  var window = await setup(
    [gameRow(1, { picked: "home", confidence: 1 }), gameRow(2, { picked: "away", confidence: 2 })],
    2
  );
  var inputs = confInputs(window);
  inputs[1].focus();
  tab(inputs[1]);
  assert.notEqual(window.document.activeElement, inputs[0]);
});

test("Arrow Down and Arrow Up on a confidence input match Tab and Shift+Tab", async () => {
  var window = await setup(
    [gameRow(1, { picked: "home", confidence: 1 }), gameRow(2, { picked: "away", confidence: 2 })],
    2
  );
  var inputs = confInputs(window);
  inputs[0].focus();
  arrow(inputs[0], "ArrowDown");
  assert.equal(window.document.activeElement, inputs[1]);
  arrow(inputs[1], "ArrowUp");
  assert.equal(window.document.activeElement, inputs[0]);
});

test("After a reorder, the tab sequence follows the new visual order", async () => {
  var window = await setup(
    [
      gameRow(1, { picked: "home", confidence: 1 }),
      gameRow(2, { picked: "away", confidence: 2 }),
      gameRow(3, { picked: "home", confidence: 3 }),
    ],
    3
  );
  var list = window.document.querySelector(".game-list");
  // Simulate a drag: row 3 moved to the very top, exactly what SortableJS's onEnd would leave
  // behind, then the same renumber() call app.js's own onEnd handler makes.
  var rows = Array.prototype.slice.call(list.querySelectorAll(".game-row"));
  list.insertBefore(rows[2], rows[0]);
  window.PSP.renumber(list);

  var inputs = confInputs(window);
  assert.equal(inputs[0].closest(".game-row").dataset.gameId, "3");
  inputs[0].focus();
  tab(inputs[0]);
  assert.equal(window.document.activeElement.closest(".game-row").dataset.gameId, "1");
});

test("After Reorder to inputs, the tab sequence follows the new visual order", async () => {
  var window = await setup(
    [
      gameRow(1, { picked: "home", confidence: 1 }),
      gameRow(2, { picked: "away", confidence: 3 }),
      gameRow(3, { picked: "home", confidence: 2 }),
    ],
    3
  );
  var list = window.document.querySelector(".game-list");
  window.PSP.reorderToInputs(list);

  var inputs = confInputs(window);
  // Highest typed value (game 2, value 3) sorts to the top.
  assert.equal(inputs[0].closest(".game-row").dataset.gameId, "2");
  inputs[0].focus();
  tab(inputs[0]);
  assert.equal(window.document.activeElement.closest(".game-row").dataset.gameId, "3");
});

test("Tab from the final confidence input reaches Lock picks once picks are complete", async () => {
  var window = await setup(
    [
      gameRow(1, { picked: "home", confidence: 3 }),
      gameRow(2, { picked: "away", confidence: 2 }),
      gameRow(3, { picked: "home", confidence: 1 }),
    ],
    3
  );
  var lockBtn = window.document.querySelector("[data-lock-open]");
  assert.equal(lockBtn.disabled, false, "a full, valid set of picks enables Lock on load");

  var inputs = confInputs(window);
  inputs[2].focus();
  tab(inputs[2]);
  assert.equal(window.document.activeElement, lockBtn);
});

test("A duplicate confidence value does not interrupt focus movement", async () => {
  var window = await setup(
    [gameRow(1, { picked: "home", confidence: 1 }), gameRow(2, { picked: "away", confidence: 1 })],
    2
  );
  var summary = window.document.querySelector("[data-pick-summary]");
  assert.match(summary.textContent, /used twice/);

  var inputs = confInputs(window);
  inputs[0].focus();
  tab(inputs[0]);
  assert.equal(window.document.activeElement, inputs[1]);
});

test("Team pick controls stay in the normal tab order (never tabindex=-1)", async () => {
  var window = await setup([gameRow(1, { picked: null, confidence: "" })], 1);
  window.document.querySelectorAll(".team-btn").forEach(function (btn) {
    assert.notEqual(btn.getAttribute("tabindex"), "-1");
  });
});

test("Team pick controls are still operable by mouse/click", async () => {
  var window = await setup([gameRow(1, { picked: null, confidence: "" })], 1);
  var away = window.document.querySelector('.team-btn[data-side="away"]');
  away.dispatchEvent(new window.MouseEvent("click", { bubbles: true, cancelable: true }));
  assert.equal(away.classList.contains("is-picked"), true);
});

test("Arrow Left and Arrow Right move between a row's two team choices", async () => {
  var window = await setup([gameRow(1, { picked: "away", confidence: 1 })], 1);
  var away = window.document.querySelector('.team-btn[data-side="away"]');
  var home = window.document.querySelector('.team-btn[data-side="home"]');
  away.focus();
  arrow(away, "ArrowRight");
  assert.equal(window.document.activeElement, home);
  arrow(home, "ArrowLeft");
  assert.equal(window.document.activeElement, away);
});
