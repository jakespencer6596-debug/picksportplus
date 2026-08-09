/* PickSportPlus. Vanilla JS only, no bundler.
   Three jobs: the confidence ranking control, the lock countdown, and small HTMX niceties. */
(function () {
  "use strict";

  var reduceMotion = window.matchMedia("(prefers-reduced-motion: reduce)").matches;

  /* ---------------------------------------------------------------- ranking */

  /* Sets one row's confidence display: the hidden input the form actually posts, the
     chip, and row.dataset.confidence, which every other function here treats as the
     single source of truth for "what is this row's confidence right now." When
     opts.syncTyped is set the visible Stage 1 number input is overwritten to match, which
     is what a position based reassignment (renumber, below) wants; a plain Stage 1 typed
     edit (syncRowConfidence, below) must NOT overwrite what the player is mid typing. */
  function applyRowConfidence(row, value, opts) {
    var str = value === "" || value === null || typeof value === "undefined" ? "" : String(value);
    var hidden = row.querySelector("input[data-conf]");
    if (hidden) hidden.value = str;
    row.dataset.confidence = str;
    if (opts && opts.syncTyped) {
      var typed = row.querySelector("[data-conf-input]");
      if (typed) typed.value = str;
    }
    var chip = row.querySelector(".conf-chip");
    if (!chip) return;
    if (str === "") {
      chip.textContent = "-";
      chip.classList.add("is-empty");
      chip.setAttribute("aria-label", "No points staked");
    } else {
      chip.textContent = str;
      chip.classList.remove("is-empty");
      chip.setAttribute("aria-label", str + " " + (str === "1" ? "point" : "points") + " staked");
    }
  }

  /* Confidence is positional, but Phase 4 scopes that position to the picked rows only,
     never the whole slate: walk the list top to bottom, skip any row with no team picked
     (it keeps its blank chip and blank hidden value, see applyRowConfidence(row, "")), and
     assign the picked rows picksCount down to 1 in the order they actually appear. This is
     what dragging, the up/down buttons, and "Reorder to inputs" all call after they move
     rows around, so all three paths produce identical state (they already shared move(),
     this keeps that true). It deliberately does NOT run on page load or on a plain team tap,
     both of which must leave an already-typed or already-saved confidence value alone; see
     syncRowConfidence for that path. */
  function renumber(list) {
    var rows = Array.prototype.slice.call(list.querySelectorAll(".game-row"));
    var n = rows.length;
    var pickedRows = rows.filter(function (row) {
      return !!row.querySelector(".team-btn.is-picked");
    });
    var pickedCount = pickedRows.length;
    pickedRows.forEach(function (row, i) {
      applyRowConfidence(row, pickedCount - i, { syncTyped: true });
    });
    rows.forEach(function (row, i) {
      var up = row.querySelector(".rank-btn[data-dir='up']");
      var down = row.querySelector(".rank-btn[data-dir='down']");
      if (up) up.disabled = i === 0;
      if (down) down.disabled = i === n - 1;
    });
    regroupDivider(list);
    updateSummary();
  }

  /* Stage 1: the typed number input drives the hidden field and the chip directly, no
     drag or reorder required. Only takes effect while the row's team is picked, so typing
     a value ahead of tapping a winner is remembered (the browser keeps it in the visible
     field) without it silently becoming a real, submitted confidence for a game with no
     winner chosen, which the server would otherwise reject as an invalid pick. */
  function syncRowConfidence(row) {
    var picked = !!row.querySelector(".team-btn.is-picked");
    var typed = row.querySelector("[data-conf-input]");
    var raw = typed ? typed.value.trim() : "";
    applyRowConfidence(row, picked ? raw : "");
    var list = row.closest(".game-list");
    if (list) regroupDivider(list);
    updateSummary();
  }

  /* The "Not picked" divider is a real <li data-divider> inside the same sortable list
     (see app.css .game-list-divider), purely cosmetic: it carries no .game-grip, so
     SortableJS (handle: ".game-grip") can never pick it up to drag, but a row CAN still be
     dropped on either side of it. That is fine on purpose. A row's picked state is driven
     only by whether its team is tapped, never by which side of this line it visually ends
     up on after a drag, so the divider never has to be perfectly in sync mid drag, only
     repositioned here afterward, purely for readability. See DECISIONS.md, Phase 4. */
  function regroupDivider(list) {
    var divider = list.querySelector("[data-divider]");
    if (!divider) return;
    var rows = Array.prototype.slice.call(list.querySelectorAll(".game-row"));
    var firstUnpicked = null;
    for (var i = 0; i < rows.length; i++) {
      if (!rows[i].querySelector(".team-btn.is-picked")) {
        firstUnpicked = rows[i];
        break;
      }
    }
    divider.hidden = !firstUnpicked;
    if (firstUnpicked) {
      list.insertBefore(divider, firstUnpicked);
    } else {
      list.appendChild(divider);
    }
  }

  /* Stage 2. Sorts the picked-with-a-valid-typed-value rows to the top, highest value
     first, and pushes everything else (no team picked, or no confidence typed yet) into
     the "Not picked" group. Duplicate typed values are not an error here, a stable sort
     just keeps their relative order, because the renumber() call right after this
     overwrites every picked row with a clean, gap free 1..n sequence anyway, which is what
     "so positions and typed values agree" means in practice. */
  function reorderToInputs(list) {
    var rows = Array.prototype.slice.call(list.querySelectorAll(".game-row"));
    var target = picksRequired(list);
    var ranked = [];
    var unranked = [];
    rows.forEach(function (row) {
      var picked = !!row.querySelector(".team-btn.is-picked");
      var raw = row.dataset.confidence || "";
      var value = raw === "" ? NaN : parseInt(raw, 10);
      if (picked && raw !== "" && !isNaN(value) && value >= 1 && value <= target) {
        ranked.push({ row: row, value: value });
      } else {
        unranked.push(row);
      }
    });
    ranked.sort(function (a, b) { return b.value - a.value; });
    ranked.forEach(function (item) { list.appendChild(item.row); });
    unranked.forEach(function (row) { list.appendChild(row); });
    renumber(list);
    markDirty();
  }

  function move(row, dir) {
    var list = row.parentElement;
    if (dir === "up" && row.previousElementSibling) {
      list.insertBefore(row, row.previousElementSibling);
    } else if (dir === "down" && row.nextElementSibling) {
      list.insertBefore(row.nextElementSibling, row);
    } else {
      return false;
    }
    renumber(list);
    markDirty();
    return true;
  }

  /* --------------------------------------------------------------- progress */

  /* How many picks make a complete submission. This can be smaller than the number of
     rows in the list (Phase 3: 15 required out of a 20 game slate), so it comes from
     data-picks-required on the list, set server side from pool.picks_required, never
     hard coded here. Falls back to the row count only if that attribute is missing. */
  function picksRequired(list) {
    var value = parseInt(list.dataset.picksRequired, 10);
    return value > 0 ? value : list.querySelectorAll(".game-row").length;
  }

  /* Stage 1 lives here too: the same pass that counts picked rows also checks every
     picked row's confidence value for a collision or an out of range value, so the save
     button gating and the duplicate warning share one source of truth rather than two
     passes that could disagree. Everything read here (.team-btn.is-picked,
     row.dataset.confidence) is exactly what applyRowConfidence/syncRowConfidence keep in
     sync, and exactly what gets posted, so "complete" here means "the server would accept
     this," even though the server remains the only real authority (validate_picks runs
     again there regardless of what this function ever decided). */
  function updateSummary() {
    var list = document.querySelector(".game-list");
    var summary = document.querySelector("[data-pick-summary]");
    if (!list || !summary) return;
    var rows = Array.prototype.slice.call(list.querySelectorAll(".game-row"));
    var target = picksRequired(list);
    var picked = 0;
    var counts = {};
    var invalid = false;
    var missing = false;

    rows.forEach(function (row) {
      if (!row.querySelector(".team-btn.is-picked")) return;
      picked += 1;
      var raw = row.dataset.confidence || "";
      if (raw === "") {
        missing = true;
        return;
      }
      var num = parseInt(raw, 10);
      if (isNaN(num) || num < 1 || num > target) {
        invalid = true;
        return;
      }
      counts[num] = (counts[num] || 0) + 1;
    });

    var duplicates = [];
    Object.keys(counts).forEach(function (key) {
      if (counts[key] > 1) duplicates.push(parseInt(key, 10));
    });
    duplicates.sort(function (a, b) { return a - b; });

    rows.forEach(function (row) {
      var input = row.querySelector("[data-conf-input]");
      if (!input) return;
      var isPicked = !!row.querySelector(".team-btn.is-picked");
      var raw = row.dataset.confidence || "";
      var num = raw === "" ? NaN : parseInt(raw, 10);
      var bad = isPicked && raw !== "" &&
        (duplicates.indexOf(num) !== -1 || isNaN(num) || num < 1 || num > target);
      input.classList.toggle("has-error", !!bad);
    });

    if (duplicates.length) {
      var words = duplicates.map(String);
      var joined = words.length === 1
        ? words[0]
        : words.slice(0, -1).join(", ") + " and " + words[words.length - 1];
      summary.textContent = picked + " of " + target + " assigned, value" +
        (words.length === 1 ? " " + joined + " is" : "s " + joined + " are") + " used twice";
    } else {
      summary.textContent =
        picked + " of " + target + " winner" + (target === 1 ? "" : "s") + " chosen";
    }

    var complete = picked === target && !invalid && !missing && duplicates.length === 0;
    var save = document.querySelector("[data-save-btn]");
    if (save) save.disabled = !complete;
    var lockOpen = document.querySelector("[data-lock-open]");
    if (lockOpen) lockOpen.disabled = !complete;

    var meter = document.querySelector("[data-pick-meter]");
    if (meter) {
      var pct = target ? Math.min(100, (picked / target) * 100) : 0;
      meter.style.setProperty("--pct", pct + "%");
      meter.setAttribute("aria-valuenow", String(picked));
      meter.setAttribute("aria-valuemax", String(target));
    }
  }

  function markDirty() {
    var badge = document.querySelector("[data-save-state]");
    if (badge) {
      badge.textContent = "Unsaved changes";
      badge.className = "save-state is-dirty";
    }
  }

  function markSaved(text) {
    var badge = document.querySelector("[data-save-state]");
    if (badge) {
      badge.textContent = text || "Saved";
      badge.className = "save-state is-saved";
    }
  }

  /* ------------------------------------------------------------ interaction */

  /* The smallest confidence value 1..required not already sitting on some other row.
     Used to give a brand new pick an immediate number instead of leaving it blank, without
     ever colliding with a value another row already has (typed, dragged, or previously
     auto assigned), including after a pick was undone and the numbers are no longer a
     clean run from 1. */
  function nextAvailableConfidence(list, required) {
    var used = {};
    Array.prototype.slice.call(list.querySelectorAll(".game-row")).forEach(function (row) {
      var v = row.dataset.confidence;
      if (v) used[v] = true;
    });
    for (var n = 1; n <= required; n++) {
      if (!used[String(n)]) return n;
    }
    return null;
  }

  function pickedRowCount(list) {
    return list.querySelectorAll(".game-row .team-btn.is-picked").length;
  }

  /* Told through the same element updateSummary already writes to, so it reads naturally
     next to the running count rather than as a separate popup. The next real change (a
     swap, an undo, typing a number) overwrites it via updateSummary as usual. */
  function announcePickLimitReached(required) {
    var summary = document.querySelector("[data-pick-summary]");
    if (summary) {
      summary.textContent =
        "All " + required + " picks made. Tap a pick again to undo it before picking another.";
    }
  }

  function onClick(e) {
    var teamBtn = e.target.closest(".team-btn");
    if (teamBtn && !teamBtn.disabled) {
      var row = teamBtn.closest(".game-row");
      var list = row.closest(".game-list");

      /* Tapping the team already picked on this game undoes the pick entirely (never just
         a no-op), since a hard cap on how many games can be picked (below) is only usable
         if there is a way to free up a slot again. Clears the confidence too, on purpose:
         a game with no winner chosen can never legitimately carry points. */
      if (teamBtn.classList.contains("is-picked")) {
        row.querySelectorAll(".team-btn").forEach(function (b) {
          b.classList.remove("is-picked");
          b.setAttribute("aria-pressed", "false");
        });
        var clearedHidden = row.querySelector("input[data-pick]");
        if (clearedHidden) clearedHidden.value = "";
        applyRowConfidence(row, "", { syncTyped: true });
        if (list) regroupDivider(list);
        updateSummary();
        markDirty();
        return;
      }

      var wasPicked = !!row.querySelector(".team-btn.is-picked");

      /* A tap that switches which side is picked on an already picked game never changes
         how many games are picked, so it never needs the cap check below, only a genuinely
         new pick does. */
      if (!wasPicked && list) {
        var required = picksRequired(list);
        if (pickedRowCount(list) >= required) {
          announcePickLimitReached(required);
          return;
        }
      }

      row.querySelectorAll(".team-btn").forEach(function (b) {
        var on = b === teamBtn;
        b.classList.toggle("is-picked", on);
        b.setAttribute("aria-pressed", on ? "true" : "false");
      });
      var hidden = row.querySelector("input[data-pick]");
      if (hidden) hidden.value = teamBtn.dataset.side;

      /* A fresh pick with nothing already typed into its Stage 1 input gets the next
         available confidence value immediately, in the order picks are made, so the row
         never sits blank until the player manually types or drags. A value the player
         already typed ahead of tapping is left alone, never overwritten. Dragging,
         "Reorder to inputs", and manual typing all still work exactly as before, this
         only fills in a starting point. */
      if (!wasPicked && list) {
        var typed = row.querySelector("[data-conf-input]");
        var alreadyTyped = typed && typed.value.trim() !== "";
        if (!alreadyTyped) {
          var next = nextAvailableConfidence(list, picksRequired(list));
          if (next !== null) applyRowConfidence(row, next, { syncTyped: true });
        }
      }

      syncRowConfidence(row);
      markDirty();
      return;
    }

    var rankBtn = e.target.closest(".rank-btn");
    if (rankBtn && !rankBtn.disabled) {
      var moved = move(rankBtn.closest(".game-row"), rankBtn.dataset.dir);
      if (moved) rankBtn.focus(); /* keep the keyboard user where they were */
    }
  }

  /* Stage 1's typed confidence input. Delegated like everything else here, fires on every
     keystroke so the duplicate/range check in updateSummary stays live while the player is
     mid edit, per Section 8: hold an invalid intermediate state without it blocking typing. */
  function onInput(e) {
    var input = e.target.closest("[data-conf-input]");
    if (!input) return;
    var row = input.closest(".game-row");
    if (!row) return;
    syncRowConfidence(row);
    markDirty();
  }

  /* Keyboard ranking without the buttons: alt plus arrow on a focused row. */
  function onKeydown(e) {
    if (!e.altKey) return;
    var row = e.target.closest(".game-row");
    if (!row) return;
    if (e.key === "ArrowUp") {
      e.preventDefault();
      move(row, "up");
    } else if (e.key === "ArrowDown") {
      e.preventDefault();
      move(row, "down");
    }
  }

  /* ----------------------------------------------------------- sortable tables */

  /* Season standings, the weekly leaderboard, and any other plain data table
     marked table[data-sortable]. One click sorting engine reused for both the
     header click path (desktop) and the <select data-sort-select-for> path
     (the stacked mobile view has no header row to click, see app.css's
     "Sortable tables" note), so there is exactly one place that decides row
     order: neither path duplicates the other's logic, one calls the other.

     A column is sortable by wrapping a <th scope="col"> in data-sortable-col,
     with data-sort-default-dir on it for which way a first click on that
     column goes. Numeric columns carry data-sort-value on every <td> in that
     column (the server already knows the real number; rendered text can be
     "No entry" or "." placeholders that are not numbers), text columns are
     read straight from the cell's own text. */

  var SORT_CARET_PATHS = {
    asc: '<path d="m18 15-6-6-6 6"/>',
    desc: '<path d="m6 9 6 6 6-6"/>'
  };

  function sortCaretSvg(dir) {
    return '<svg class="icon sort-caret" width="14" height="14" viewBox="0 0 24 24" ' +
      'fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" ' +
      'stroke-linejoin="round" aria-hidden="true" focusable="false">' +
      SORT_CARET_PATHS[dir] + "</svg>";
  }

  function sortCellValue(row, colIndex) {
    var cell = row.cells[colIndex];
    if (!cell) return "";
    if (cell.hasAttribute("data-sort-value")) {
      var num = parseFloat(cell.getAttribute("data-sort-value"));
      if (!isNaN(num)) return num;
    }
    return cell.textContent.trim().toLowerCase();
  }

  /* A stable sort (every engine this app supports guarantees Array#sort is
     stable) so rows tied on the sorted column keep the relative order the
     server already gave them, which is itself a meaningful secondary sort
     (see app/services/standings.py). */
  function sortTableRows(table, colIndex, dir) {
    var tbody = table.tBodies[0];
    if (!tbody) return;
    var mult = dir === "desc" ? -1 : 1;
    var rows = Array.prototype.slice.call(tbody.rows);
    rows.sort(function (a, b) {
      var av = sortCellValue(a, colIndex);
      var bv = sortCellValue(b, colIndex);
      if (av < bv) return -1 * mult;
      if (av > bv) return 1 * mult;
      return 0;
    });
    rows.forEach(function (row) { tbody.appendChild(row); });
  }

  function initSortableTable(table) {
    var headRow = table.tHead && table.tHead.rows[0];
    if (!headRow) return;
    var ths = Array.prototype.slice.call(headRow.cells).filter(function (th) {
      return th.hasAttribute("data-sortable-col");
    });
    if (!ths.length) return;

    var select = table.id
      ? document.querySelector('[data-sort-select-for="' + table.id + '"]')
      : null;

    var state = {
      col: parseInt(table.dataset.defaultSortCol, 10) || 0,
      dir: table.dataset.defaultSortDir === "desc" ? "desc" : "asc"
    };

    function render(resort) {
      if (resort) sortTableRows(table, state.col, state.dir);
      ths.forEach(function (th) {
        var caret = th.querySelector(".sort-caret");
        if (caret) caret.remove();
        if (th.cellIndex === state.col) {
          th.setAttribute("aria-sort", state.dir === "desc" ? "descending" : "ascending");
          th.insertAdjacentHTML("beforeend", sortCaretSvg(state.dir));
        } else {
          th.setAttribute("aria-sort", "none");
        }
      });
      if (select) select.value = state.col + ":" + state.dir;
    }

    ths.forEach(function (th) {
      th.tabIndex = 0;
      th.setAttribute("role", "columnheader");
      th.addEventListener("click", function () {
        if (state.col === th.cellIndex) {
          state.dir = state.dir === "asc" ? "desc" : "asc";
        } else {
          state.col = th.cellIndex;
          state.dir = th.dataset.sortDefaultDir === "desc" ? "desc" : "asc";
        }
        render(true);
      });
      th.addEventListener("keydown", function (e) {
        if (e.key === "Enter" || e.key === " ") {
          e.preventDefault();
          th.click();
        }
      });
    });

    if (select) {
      select.addEventListener("change", function () {
        var parts = select.value.split(":");
        state.col = parseInt(parts[0], 10);
        state.dir = parts[1] === "desc" ? "desc" : "asc";
        render(true);
      });
    }

    /* The server already rendered the rows in this exact default order (see
       season_standings/weekly_leaderboard's own sign-aware sort), so the
       initial render only has to mark the caret and aria-sort, not re-sort
       the DOM out from under a server order that may break ties (correct,
       weekly wins, name) this client side sort does not know about. */
    render(false);
  }

  function initSortableTables() {
    document.querySelectorAll("table[data-sortable]").forEach(initSortableTable);
  }

  /* ------------------------------------------------------------- view toggle */

  /* Results' "By player / By game" pick grid switch. Both grids are rendered
     server side; this only ever shows one and hides the other, see
     app.css's "By player / by game toggle" note. */
  function initViewToggle() {
    var buttons = document.querySelectorAll("[data-view-btn]");
    if (!buttons.length) return;

    function setView(view) {
      document.querySelectorAll("[data-view-panel]").forEach(function (panel) {
        panel.hidden = panel.dataset.viewPanel !== view;
      });
      buttons.forEach(function (btn) {
        var active = btn.dataset.viewBtn === view;
        btn.setAttribute("aria-pressed", active ? "true" : "false");
        btn.classList.toggle("is-active", active);
      });
    }

    buttons.forEach(function (btn) {
      btn.addEventListener("click", function () { setView(btn.dataset.viewBtn); });
    });

    var initial = document.querySelector("[data-view-btn].is-active");
    setView(initial ? initial.dataset.viewBtn : "player");
  }

  /* --------------------------------------------------------------- countdown */

  function tickCountdowns() {
    document.querySelectorAll("[data-countdown-to]").forEach(function (el) {
      var target = Date.parse(el.dataset.countdownTo);
      if (isNaN(target)) return;
      var diff = Math.floor((target - Date.now()) / 1000);
      if (diff <= 0) {
        el.textContent = "now";
        el.closest(".lockbar") && el.closest(".lockbar").classList.add("lockbar-strong");
        return;
      }
      var d = Math.floor(diff / 86400);
      var h = Math.floor((diff % 86400) / 3600);
      var m = Math.floor((diff % 3600) / 60);
      var s = diff % 60;
      if (d > 0) el.textContent = d + "d " + h + "h";
      else if (h > 0) el.textContent = h + "h " + m + "m";
      else el.textContent = m + "m " + s + "s";
      var bar = el.closest(".lockbar");
      if (bar && diff < 3600) bar.classList.add("lockbar-strong");
    });
  }

  /* ------------------------------------------------------------------- init */

  function initSortable() {
    var list = document.querySelector(".game-list[data-sortable]");
    if (!list || typeof window.Sortable === "undefined") return;
    window.Sortable.create(list, {
      handle: ".game-grip",
      animation: reduceMotion ? 0 : 150,
      ghostClass: "sortable-ghost",
      chosenClass: "sortable-chosen",
      dragClass: "sortable-drag",
      forceFallback: false,
      delay: 80,
      delayOnTouchOnly: true,
      touchStartThreshold: 4,
      /* SortableJS already reorders the real rows live as the dragged row passes over
         others, onChange fires on every one of those live moves. Renumber right then, not
         just onEnd, so the point values update while you are still dragging instead of
         only once you drop. onEnd still fires last (onChange does not fire for the final
         drop position on some input methods), so keep both. */
      onChange: function () {
        renumber(list);
      },
      onEnd: function () {
        renumber(list);
        markDirty();
      }
    });
  }

  /* Stage 2's button. A plain click handler, not a submit: reordering the list is a pure
     client side rearrangement, nothing is posted until Save or Lock actually fires. */
  function initReorderButton() {
    var btn = document.querySelector("[data-reorder-btn]");
    var list = document.querySelector(".game-list[data-sortable]");
    if (!btn || !list) return;
    btn.addEventListener("click", function () {
      reorderToInputs(list);
    });
  }

  /* Builds the confirmation panel's summary, most confident pick first, from exactly what
     is about to be posted (row.dataset.confidence and the picked team button), so the
     player is confirming the real submission, not a stale server rendered snapshot. */
  function buildLockSummary(list, target) {
    var rows = Array.prototype.slice.call(list.querySelectorAll(".game-row"));
    var picked = rows.filter(function (row) {
      return !!row.querySelector(".team-btn.is-picked") && row.dataset.confidence;
    });
    picked.sort(function (a, b) {
      return parseInt(b.dataset.confidence, 10) - parseInt(a.dataset.confidence, 10);
    });
    target.innerHTML = "";
    picked.forEach(function (row) {
      var nameEl = row.querySelector(".team-btn.is-picked .team-name");
      var abbrEl = row.querySelector(".team-btn.is-picked .team-abbr");
      var label = nameEl ? nameEl.textContent : (abbrEl ? abbrEl.textContent : "Pick");
      var li = document.createElement("li");
      li.textContent = row.dataset.confidence + ". " + label;
      target.appendChild(li);
    });
  }

  /* Stage 3's lock step. "Lock picks" only opens a confirmation panel, built fresh from
     the current on page state; the actual POST to /picks/lock is a second, separate tap
     on the confirm button inside that panel (hx-post, wired in picks.html), so locking
     can never happen from one accidental click the way Save can. See DECISIONS.md,
     Phase 4, for why this is a plain JS toggle rather than an HTMX round trip. */
  function initLockFlow() {
    var openBtn = document.querySelector("[data-lock-open]");
    var cancelBtn = document.querySelector("[data-lock-cancel]");
    var panel = document.querySelector("[data-lock-panel]");
    var summaryList = document.querySelector("[data-lock-summary]");
    var list = document.querySelector(".game-list[data-sortable]");
    if (!openBtn || !panel) return;

    openBtn.addEventListener("click", function () {
      if (list && summaryList) buildLockSummary(list, summaryList);
      panel.hidden = false;
      panel.scrollIntoView({ behavior: reduceMotion ? "auto" : "smooth", block: "nearest" });
    });

    if (cancelBtn) {
      cancelBtn.addEventListener("click", function () {
        panel.hidden = true;
      });
    }
  }

  function init() {
    document.addEventListener("click", onClick);
    document.addEventListener("keydown", onKeydown);
    document.addEventListener("input", onInput);
    initSortable();
    var list = document.querySelector(".game-list");
    if (list) {
      /* Not renumber(): the server already rendered every row's real confidence, saved
         or blank, and renumber() would overwrite a saved but not yet fully ranked entry
         with position based values that do not match what is actually stored. Only the
         divider position and the summary/gating need computing fresh on load. */
      regroupDivider(list);
      updateSummary();
    }
    initReorderButton();
    initLockFlow();
    initSortableTables();
    initViewToggle();
    tickCountdowns();
    setInterval(tickCountdowns, 1000);
  }

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", init);
  } else {
    init();
  }

  /* htmx does not swap a 4xx response by default, so without this the server's validation
     messages never reach the player: they would see only a small "Not saved" chip and no
     explanation of what was wrong. Our validation replies are 400, and a lock that closed
     mid-session is 403, so force the swap for client errors and let 5xx fall through to
     htmx's own error handling.

     This has to be a document level listener. On an error response htmx dispatches
     htmx:beforeSwap at the target, not at the element that triggered the request, so the
     same handler written as hx-on::before-swap on the save button never fires. */
  document.addEventListener("htmx:beforeSwap", function (e) {
    var xhr = e.detail && e.detail.xhr;
    if (xhr && xhr.status >= 400 && xhr.status < 500) {
      e.detail.shouldSwap = true;
      e.detail.isError = false;
    }
  });

  /* HTMX save feedback. The server returns the summary partial. */
  document.addEventListener("htmx:afterSwap", function (e) {
    if (!e.target || !e.target.matches("[data-save-target]")) return;
    var xhr = e.detail && e.detail.xhr;
    if (xhr && xhr.status >= 400) {
      /* The swapped partial already lists what is wrong. Only correct the chip. */
      var badge = document.querySelector("[data-save-state]");
      if (badge) {
        badge.textContent = "Not saved";
        badge.className = "save-state is-error";
      }
      return;
    }
    var el = e.target.querySelector("[data-saved-at]");
    markSaved(el ? el.dataset.savedAt : "Saved");
  });

  document.addEventListener("htmx:beforeRequest", function (e) {
    if (e.target.matches("[data-save-btn]")) {
      var badge = document.querySelector("[data-save-state]");
      if (badge) {
        badge.textContent = "Saving";
        badge.className = "save-state is-saving";
      }
    }
  });

  window.PSP = {
    renumber: renumber,
    updateSummary: updateSummary,
    markSaved: markSaved,
    reorderToInputs: reorderToInputs,
    syncRowConfidence: syncRowConfidence,
    regroupDivider: regroupDivider,
    sortTableRows: sortTableRows,
    initSortableTables: initSortableTables,
    initViewToggle: initViewToggle
  };
})();
