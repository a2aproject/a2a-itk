// Run with: npm test  (node --test, no framework)

import assert from "node:assert/strict";
import test from "node:test";
import {
  buildRows,
  deviationsOf,
  filterCounts,
  formatDuration,
  groupBySuite,
  levelTotals,
  passRate,
  rowMatches,
  runTransports,
  suiteOf,
} from "./lib.ts";
import type { ActsRun, Counts, TestResult, TransportResult } from "./types.ts";

const counts = (over: Partial<Counts> = {}): Counts => ({
  total: 0,
  passed: 0,
  failed: 0,
  skipped: 0,
  errors: 0,
  ...over,
});

const transport = (tests: TestResult[]): TransportResult => ({
  conformant: tests.every((t) => t.result !== "fail"),
  summary: { ...counts({ total: tests.length }), duration_ms: 1000 },
  by_level: { must: counts(), should: counts(), may: counts() },
  tests,
});

const run = (results: ActsRun["results"]): ActsRun => ({
  timestamp: "2026-09-17T02:10:43Z",
  commit_sha: "734fc73",
  sdk: "a2a-rs",
  sdk_version: "unknown",
  acts_version: "1.0",
  spec_version: "1.0",
  transports: Object.keys(results),
  conformant: false,
  summary: counts(),
  results,
});

const pass = (id: string): TestResult => ({ id, level: "must", result: "pass" });
const skip = (id: string, reason: string): TestResult => ({
  id,
  level: "should",
  result: "skip",
  skip_reason: reason,
});
const fail = (id: string, message: string): TestResult => ({
  id,
  level: "must",
  result: "fail",
  failure: { message, step_id: "send" },
});

test("suiteOf takes the leading token of a test id", () => {
  assert.equal(suiteOf("CORE-SEND-002"), "CORE");
  assert.equal(suiteOf("SEC-EXTCARD-001"), "SEC");
  assert.equal(suiteOf(""), "other");
});

test("runTransports lists only the transports the run exercised, in table order", () => {
  const r = run({ rest: transport([]), jsonrpc: transport([]) });
  assert.deepEqual(runTransports(r), ["jsonrpc", "rest"]);
});

test("buildRows pivots per-transport lists into one row per test id", () => {
  const r = run({
    jsonrpc: transport([pass("CORE-SEND-001"), fail("CORE-SEND-002", "boom")]),
    grpc: transport([pass("CORE-SEND-001"), pass("CORE-SEND-002")]),
  });
  const rows = buildRows(r);

  assert.equal(rows.length, 2);
  assert.deepEqual(
    rows.map((row) => row.id),
    ["CORE-SEND-001", "CORE-SEND-002"],
  );
  assert.equal(rows[0].deviates, false);
  assert.equal(rows[1].deviates, true);
  assert.equal(rows[1].failing, true);
  assert.equal(rows[1].cells.grpc?.result, "pass");
});

test("buildRows unions ids, so a transport-only test still gets a row", () => {
  const r = run({
    jsonrpc: transport([pass("JSONRPC-ENV-001")]),
    grpc: transport([pass("GRPC-STATUS-001")]),
  });
  const rows = buildRows(r);

  assert.deepEqual(
    rows.map((row) => row.id).sort(),
    ["GRPC-STATUS-001", "JSONRPC-ENV-001"],
  );
  // The transport that never ran it leaves a hole rather than a false pass.
  const grpcOnly = rows.find((row) => row.id === "GRPC-STATUS-001");
  assert.equal(grpcOnly?.cells.jsonrpc, undefined);
});

test("a skip counts as a deviation but not as a failure", () => {
  const r = run({ jsonrpc: transport([skip("CARD-CACHE-001", "no headers")]) });
  const [row] = buildRows(r);

  assert.equal(row.deviates, true);
  assert.equal(row.failing, false);
  assert.equal(rowMatches(row, "all"), true);
  assert.equal(rowMatches(row, "skipped"), true);
  assert.equal(rowMatches(row, "failing"), false);
});

test("a row that both fails and skips is counted as failing, not skipped", () => {
  const r = run({
    jsonrpc: transport([fail("JSONRPC-ERR-002", "status 400")]),
    grpc: transport([skip("JSONRPC-ERR-002", "targets jsonrpc")]),
  });
  const rows = buildRows(r);

  assert.equal(rows[0].failing, true);
  assert.equal(rowMatches(rows[0], "skipped"), false);
  assert.deepEqual(filterCounts(rows), { all: 1, failing: 1, skipped: 0 });
});

test("groupBySuite keeps the order each suite first appeared", () => {
  const r = run({
    jsonrpc: transport([
      pass("CORE-SEND-001"),
      pass("CARD-DISC-001"),
      pass("CORE-GET-001"),
    ]),
  });
  const groups = groupBySuite(buildRows(r));

  assert.deepEqual(
    groups.map((g) => g.suite),
    ["CORE", "CARD"],
  );
  assert.equal(groups[0].rows.length, 2);
});

test("deviationsOf merges transports that failed for the same reason", () => {
  const same = (id: string) => fail(id, "expected an error, but the call succeeded");
  const r = run({
    jsonrpc: transport([same("CORE-SEND-002")]),
    grpc: transport([same("CORE-SEND-002")]),
    rest: transport([same("CORE-SEND-002")]),
  });
  const [row] = buildRows(r);
  const deviations = deviationsOf(row, runTransports(r));

  assert.equal(deviations.length, 1);
  assert.deepEqual(deviations[0].transports, ["jsonrpc", "grpc", "rest"]);
});

test("deviationsOf keeps reasons apart when they differ", () => {
  const r = run({
    jsonrpc: transport([fail("JSONRPC-ERR-002", "status should equal 200")]),
    grpc: transport([skip("JSONRPC-ERR-002", "targets jsonrpc; this runner speaks grpc")]),
    rest: transport([skip("JSONRPC-ERR-002", "targets jsonrpc; this runner speaks rest")]),
  });
  const [row] = buildRows(r);
  const deviations = deviationsOf(row, runTransports(r));

  assert.equal(deviations.length, 3);
  assert.deepEqual(
    deviations.map((d) => d.transports),
    [["jsonrpc"], ["grpc"], ["rest"]],
  );
});

test("deviationsOf ignores the transports that passed", () => {
  const r = run({
    jsonrpc: transport([pass("CORE-CAP-002")]),
    grpc: transport([fail("CORE-CAP-002", "stream opened")]),
  });
  const [row] = buildRows(r);

  assert.deepEqual(
    deviationsOf(row, runTransports(r)).map((d) => d.transports),
    [["grpc"]],
  );
});

test("levelTotals sums by_level across every transport", () => {
  const withLevels = (must: Counts): TransportResult => ({
    ...transport([]),
    by_level: { must, should: counts(), may: counts() },
  });
  const r = run({
    jsonrpc: withLevels(counts({ total: 65, passed: 49, failed: 9 })),
    grpc: withLevels(counts({ total: 65, passed: 45, failed: 11 })),
  });

  assert.deepEqual(
    levelTotals(r).must,
    counts({ total: 130, passed: 94, failed: 20 }),
  );
});

test("formatDuration reads as minutes and seconds past a minute", () => {
  assert.equal(formatDuration(250954), "4m 11s");
  assert.equal(formatDuration(33000), "33s");
  assert.equal(formatDuration(60000), "1m 00s");
  assert.equal(formatDuration(undefined), "—");
});

test("passRate rounds, and an empty run is 0 rather than NaN", () => {
  assert.equal(passRate(234, 333), 70);
  assert.equal(passRate(0, 0), 0);
});
