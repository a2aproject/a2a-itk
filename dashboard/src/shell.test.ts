import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { fileURLToPath } from "node:url";
import test from "node:test";

const read = (rel: string) =>
  readFileSync(fileURLToPath(new URL(rel, import.meta.url)), "utf8");

const html = read("../index.html");
const layout = read("./components/Layout.tsx");
const header = read("./components/Header.tsx");

/** Collapse whitespace so JSX line wrapping does not count as a difference. */
const squash = (s: string) => s.replace(/\s+/g, " ").trim();

const between = (source: string, open: RegExp, close: string): string => {
  const start = source.search(open);
  assert.notEqual(start, -1, `could not find ${open} in source`);
  const from = start + source.slice(start).indexOf(">") + 1;
  const end = source.indexOf(close, from);
  assert.notEqual(end, -1, `could not find ${close} in source`);
  return squash(source.slice(from, end));
};

test("shell heading matches the one Layout renders", () => {
  assert.equal(
    between(html, /<h1[^>]*>/, "</h1>"),
    between(layout, /<h1[^>]*>/, "</h1>"),
  );
});

test("shell intro paragraph matches the one Layout renders", () => {
  // Strip the JSX whitespace escapes the layout needs but HTML does not.
  const fromLayout = between(layout, /<p>/, "</p>").replace(/\{" "\}/g, "");
  assert.equal(squash(between(html, /<p>\s*Nightly/, "</p>")), squash(fromLayout));
});

test("shell brand matches the one Header renders", () => {
  for (const fragment of ["Integration Test Kit", "./a2a-icon.svg"]) {
    assert.ok(html.includes(fragment), `shell is missing ${fragment}`);
    assert.ok(header.includes(fragment), `Header is missing ${fragment}`);
  }
});

test("shell links match the resource nav Header renders", () => {
  const hrefs = (source: string) =>
    [...source.matchAll(/href="(https:\/\/[^"]+)"/g)]
      .map((m) => m[1])
      .filter((h) => !h.includes("fonts.") && !h.includes("goo.gle"));

  const shellNav = html.slice(html.indexOf('aria-label="Resources"'));
  for (const href of hrefs(header)) {
    assert.ok(shellNav.includes(href), `shell nav is missing ${href}`);
  }
});

test("the shell's no-JS extras and spinner are toggled by a noscript rule", () => {
  const noscript = html.slice(html.indexOf("<noscript>", html.indexOf("#boot-no-js")));
  assert.match(noscript, /#boot-no-js\s*\{\s*display:\s*block/);
  assert.match(noscript, /#boot-loading\s*\{\s*display:\s*none/);
});
