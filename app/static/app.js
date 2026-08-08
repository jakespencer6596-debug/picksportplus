/* PickSportPlus. Vanilla JS only, no bundler.
   Three jobs: the confidence ranking control, the lock countdown, and small HTMX niceties. */
(function () {
  "use strict";

  var reduceMotion = window.matchMedia("(prefers-reduced-motion: reduce)").matches;

  /* ---------------------------------------------------------------- ranking */

  /* Confidence is positional. The top row stakes N points, the bottom stakes 1.
     Renumber after any move, whether by drag, by button, or by keyboard. */
  function renumber(list) {
    var rows = Array.prototype.slice.call(list.querySelectorAll(".game-row"));
    var n = rows.length;
    rows.forEach(function (row, i) {
      var points = n - i;
      var chip = row.querySelector(".conf-chip");
      if (chip) {
        chip.textContent = points;
        chip.setAttribute("aria-label", points + " " + (points === 1 ? "point" : "points"));
      }
      var input = row.querySelector("input[data-conf]");
      if (input) input.value = points;
      row.dataset.confidence = points;

      var up = row.querySelector(".rank-btn[data-dir='up']");
      var down = row.querySelector(".rank-btn[data-dir='down']");
      if (up) up.disabled = i === 0;
      if (down) down.disabled = i === n - 1;
    });
    updateSummary();
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

  function updateSummary() {
    var list = document.querySelector(".game-list");
    var summary = document.querySelector("[data-pick-summary]");
    if (!list || !summary) return;
    var rows = list.querySelectorAll(".game-row");
    var target = picksRequired(list);
    var picked = 0;
    rows.forEach(function (row) {
      if (row.querySelector(".team-btn.is-picked")) picked += 1;
    });
    summary.textContent =
      picked + " of " + target + " winner" + (target === 1 ? "" : "s") + " chosen";
    var save = document.querySelector("[data-save-btn]");
    if (save) save.disabled = picked !== target;
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

  function onClick(e) {
    var teamBtn = e.target.closest(".team-btn");
    if (teamBtn && !teamBtn.disabled) {
      var row = teamBtn.closest(".game-row");
      row.querySelectorAll(".team-btn").forEach(function (b) {
        var on = b === teamBtn;
        b.classList.toggle("is-picked", on);
        b.setAttribute("aria-pressed", on ? "true" : "false");
      });
      var hidden = row.querySelector("input[data-pick]");
      if (hidden) hidden.value = teamBtn.dataset.side;
      updateSummary();
      markDirty();
      return;
    }

    var rankBtn = e.target.closest(".rank-btn");
    if (rankBtn && !rankBtn.disabled) {
      var moved = move(rankBtn.closest(".game-row"), rankBtn.dataset.dir);
      if (moved) rankBtn.focus(); /* keep the keyboard user where they were */
    }
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
      onEnd: function () {
        renumber(list);
        markDirty();
      }
    });
  }

  function init() {
    document.addEventListener("click", onClick);
    document.addEventListener("keydown", onKeydown);
    initSortable();
    var list = document.querySelector(".game-list");
    if (list) renumber(list);
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

  window.PSP = { renumber: renumber, updateSummary: updateSummary, markSaved: markSaved };
})();
