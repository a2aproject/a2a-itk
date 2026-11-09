// Run with: npm test  (node --test, no framework)

import assert from "node:assert/strict";
import test from "node:test";
import type { ActsRun, Counts } from "../acts/types.ts";
import type { Run, Scenario } from "../itk/types.ts";
import { SDKS } from "../shared/sdks.ts";
import { buildSdkOverview, summarizeActsRun, summarizeItkRun } from "./lib.ts";

const counts = (over: Partial<Counts> = {}): Counts => ({
  total: 0,
  passed: 0,
  failed: 0,
  skipped: 0,
  errors: 0,
  ...over,
});

const scenario = (over: Partial<Scenario>): Scenario => ({
  name: "s",
  sdks: ["current", "go_v10"],
  protocols: ["jsonrpc"],
  behavior: "send_message",
  passed: true,
  ...over,
});

test("summarizeItkRun counts overall and per-protocol results for the active SDK", () => {
  const run: Run = {
    timestamp: "2026-11-09T02:15:00Z",
    commit_sha: "28f8a62abcdef",
    all_passed: false,
    scenarios: [
      scenario({ protocols: ["jsonrpc"], passed: true }),
      scenario({ protocols: ["jsonrpc"], passed: false }),
      scenario({ protocols: ["grpc"], passed: true }),
      // Unrelated pair (neither current nor python): ignored.
      scenario({ sdks: ["go_v10", "rust_v10"], protocols: ["http_json"], passed: true }),
    ],
  };

  const summary = summarizeItkRun(run, "python");
  assert.equal(summary.allPassed, false);
  assert.equal(summary.passed, 2);
  assert.equal(summary.total, 3);
  assert.deepEqual(summary.byProtocol.jsonrpc, {
    passed: 1,
    total: 2,
    status: "mixed",
  });
  assert.deepEqual(summary.byProtocol.grpc, {
    passed: 1,
    total: 1,
    status: "pass",
  });
  assert.deepEqual(summary.byProtocol.http_json, {
    passed: 0,
    total: 0,
    status: "none",
  });
});

test("summarizeItkRun marks all-failing protocol as fail", () => {
  const run: Run = {
    timestamp: "2026-11-09T02:15:00Z",
    commit_sha: "e93f834",
    all_passed: false,
    scenarios: [scenario({ protocols: ["jsonrpc"], passed: false })],
  };

  const summary = summarizeItkRun(run, "dotnet");
  assert.equal(summary.byProtocol.jsonrpc.status, "fail");
});

test("summarizeActsRun aggregates total, MUST, and per-transport conformance", () => {
  const run: ActsRun = {
    timestamp: "2026-11-09T02:20:00Z",
    commit_sha: "31455e9",
    sdk: "a2a-java",
    sdk_version: "1.0",
    acts_version: "1.0",
    spec_version: "1.0",
    transports: ["jsonrpc", "grpc"],
    conformant: false,
    summary: counts({ total: 189, passed: 176, failed: 13 }),
    results: {
      jsonrpc: {
        conformant: false,
        summary: counts({ total: 101, passed: 93, failed: 8 }),
        by_level: {
          must: counts({ total: 59, passed: 57, failed: 2 }),
          should: counts({ total: 42, passed: 36, failed: 6 }),
          may: counts(),
        },
        tests: [],
      },
      grpc: {
        conformant: true,
        summary: counts({ total: 88, passed: 83, failed: 5 }),
        by_level: {
          must: counts({ total: 47, passed: 47, failed: 0 }),
          should: counts({ total: 41, passed: 36, failed: 5 }),
          may: counts(),
        },
        tests: [],
      },
    },
  };

  const summary = summarizeActsRun(run);
  assert.equal(summary.conformant, false);
  assert.equal(summary.passed, 176);
  assert.equal(summary.total, 189);
  assert.equal(summary.mustPassed, 104);
  assert.equal(summary.mustTotal, 106);
  assert.deepEqual(summary.byTransport.jsonrpc, {
    conformant: false,
    passed: 93,
    total: 101,
    mustPassed: 57,
    mustTotal: 59,
  });
  assert.deepEqual(summary.byTransport.grpc, {
    conformant: true,
    passed: 83,
    total: 88,
    mustPassed: 47,
    mustTotal: 47,
  });
  assert.equal(summary.byTransport.rest, undefined);
});

test("buildSdkOverview handles missing suite runs gracefully", () => {
  const overview = buildSdkOverview(SDKS[0], undefined, undefined);
  assert.equal(overview.sdk.id, "python");
  assert.equal(overview.itk, undefined);
  assert.equal(overview.acts, undefined);
});
