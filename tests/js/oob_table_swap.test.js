"use strict";

/* Phase 6, found live against a real Chrome browser: an HTMX out-of-band swap response
   fragment for a <tbody> (app/routers/admin.py's _slate_action_fragment, used for add/remove/
   swap) must wrap that <tbody> in a real <table>. htmx parses a response fragment through a
   <template> element; in a real browser, a <tbody> with no enclosing <table> in that parse is
   silently dropped (table section elements only parse correctly in "in table" insertion
   mode), so an un-wrapped <tbody id="on-slate-tbody" hx-swap-oob="true"> response was
   completely inert there: the request succeeded, the database changed, and nothing ever
   reached the page. jsdom's own HTML parser does NOT reproduce that specific table-section
   drop (verified directly: a bare <tbody> survives a jsdom <template> parse where it does not
   in Chrome), so this file only asserts the actual fix (the wrapped shape parses correctly
   everywhere, jsdom included) rather than the bug itself; the real, browser-verified bug
   report and fix are in DECISIONS.md, and tests/test_slate_actions_no_js.py asserts the
   route's response literally contains the wrapping <table>. */

const test = require("node:test");
const assert = require("node:assert/strict");
const { JSDOM } = require("jsdom");

function templateFinds(html, selector) {
  const dom = new JSDOM("<!doctype html><html><body></body></html>");
  const template = dom.window.document.createElement("template");
  template.innerHTML = html;
  const found = template.content.querySelector(selector);
  dom.window.close();
  return found;
}

test("a <tbody> wrapped in a <table> survives HTML parsing intact, id and all", () => {
  const wrapped =
    '<table><tbody id="on-slate-tbody" hx-swap-oob="true"><tr><td>x</td></tr></tbody></table>';
  const found = templateFinds(wrapped, "#on-slate-tbody");
  assert.notEqual(found, null);
  assert.equal(found.tagName, "TBODY");
  assert.equal(found.getAttribute("hx-swap-oob"), "true");
  assert.equal(found.querySelectorAll("tr").length, 1);
});

test("a <div hx-swap-oob> (not table-section content) needs no such wrapper", () => {
  const html = '<div id="week-summary-body" hx-swap-oob="true">hello</div>';
  const found = templateFinds(html, "#week-summary-body");
  assert.notEqual(found, null);
  assert.equal(found.textContent, "hello");
});

test("a <datalist hx-swap-oob> also needs no such wrapper", () => {
  const html = '<datalist id="swap-candidates" hx-swap-oob="true"><option value="1"></option></datalist>';
  const found = templateFinds(html, "#swap-candidates");
  assert.notEqual(found, null);
  assert.equal(found.children.length, 1);
});
