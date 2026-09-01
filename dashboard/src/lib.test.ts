// Run with: npm test  (node --test, no framework)

import assert from "node:assert/strict";
import test from "node:test";
import {
  BEHAVIORS,
  cellStatus,
  formatSdkName,
  pairwiseGroups,
  runPassed,
  scenarioIncludesSdk,
  sdkKind,
  sortRuns,
  summaryTopology,
} from "./lib.ts";
import { layoutTopology } from "./topology.ts";
import type { Scenario } from "./types.ts";

const scenario = (over: Partial<Scenario>): Scenario => ({
  name: "s",
  sdks: ["current", "go_v10"],
  edges: ["0->1", "1->0"],
  protocols: ["jsonrpc"],
  behavior: "send_message",
  streaming: false,
  passed: true,
  ...over,
});

const [flat, streaming] = BEHAVIORS;

test("formatSdkName expands compact versions and special-cases .NET", () => {
  assert.equal(formatSdkName("current"), "Current");
  assert.equal(formatSdkName("python_v10"), "Python v1.0");
  assert.equal(formatSdkName("dotnet_v10"), ".NET v1.0");
  assert.equal(formatSdkName("ts_v03"), "TypeScript v0.3");
  assert.equal(formatSdkName("go"), "Go");
  assert.equal(formatSdkName("rust_main"), "Rust main");
  assert.equal(formatSdkName(""), "");
});

test("sdkKind maps versioned ids onto a language token", () => {
  assert.equal(sdkKind("current"), "current");
  assert.equal(sdkKind("java_v10"), "java");
  assert.equal(sdkKind("elixir_v1"), "other");
});

test("runPassed prefers all_passed but falls back to the scenarios", () => {
  assert.equal(runPassed({ timestamp: "", commit_sha: "", all_passed: false, scenarios: [] }), false);
  assert.equal(
    runPassed({ timestamp: "", commit_sha: "", scenarios: [scenario({ passed: false })] }),
    false,
  );
  assert.equal(runPassed({ timestamp: "", commit_sha: "", scenarios: [scenario({})] }), true);
});

test("sortRuns puts the newest run first without mutating the input", () => {
  const runs = [
    { timestamp: "2026-01-01T00:00:00Z", commit_sha: "a" },
    { timestamp: "2026-03-01T00:00:00Z", commit_sha: "b" },
  ];
  assert.deepEqual(
    sortRuns(runs).map((r) => r.commit_sha),
    ["b", "a"],
  );
  assert.equal(runs[0].commit_sha, "a");
});

test("summaryTopology stars every peer when the run is only pairwise", () => {
  const { sdks, edges } = summaryTopology([
    scenario({ sdks: ["current", "python_v10"] }),
    scenario({ sdks: ["current", "go_v10"] }),
    scenario({ sdks: ["current", "python_v10"] }),
  ]);
  assert.deepEqual(sdks, ["current", "python_v10", "go_v10"]);
  assert.deepEqual(edges, ["0->1", "1->0", "0->2", "2->0"]);
});

test("summaryTopology keeps the giant-star scenario's own ordering", () => {
  const { sdks } = summaryTopology([
    scenario({ sdks: ["current", "go_v10", "python_v10"] }),
    scenario({ sdks: ["current", "python_v10"] }),
  ]);
  assert.deepEqual(sdks, ["current", "go_v10", "python_v10"]);
});

test("pairwiseGroups buckets A-B with B-A and orders by the summary star", () => {
  const scenarios = [
    scenario({ sdks: ["go_v10", "current"], protocols: ["grpc"] }),
    scenario({ sdks: ["current", "go_v10"], protocols: ["jsonrpc"] }),
    scenario({ sdks: ["current", "python_v10"] }),
    scenario({ sdks: ["current", "go_v10", "python_v10"] }), // 3-node: excluded
  ];
  const groups = pairwiseGroups(scenarios, ["current", "python_v10", "go_v10"]);

  assert.deepEqual(
    groups.map((g) => g.sdks),
    [
      ["current", "python_v10"],
      ["current", "go_v10"],
    ],
  );
  assert.equal(groups[1].scenarios.length, 2, "both orderings land in one bucket");
});

test("pairwiseGroups flips edge indices when only the reversed pair exists", () => {
  const groups = pairwiseGroups(
    [scenario({ sdks: ["go_v10", "current"], edges: ["0->1"] })],
    ["current", "go_v10"],
  );
  assert.deepEqual(groups[0].sdks, ["current", "go_v10"]);
  // "go->current" in source order must become "current->go" in display order.
  assert.deepEqual(groups[0].edges, ["1->0"]);
});

test("cellStatus distinguishes pass, fail, mixed and uncovered", () => {
  const pass = scenario({ passed: true });
  const fail = scenario({ passed: false });

  assert.equal(cellStatus([pass], flat, "jsonrpc"), "pass");
  assert.equal(cellStatus([fail], flat, "jsonrpc"), "fail");
  assert.equal(cellStatus([pass, fail], flat, "jsonrpc"), "mixed");
  assert.equal(cellStatus([pass], flat, "grpc"), "none", "protocol not covered");
  assert.equal(cellStatus([pass], streaming, "jsonrpc"), "none", "streaming differs");
});

test("cellStatus treats a missing streaming flag as non-streaming", () => {
  const noFlag = scenario({});
  delete noFlag.streaming;
  assert.equal(cellStatus([noFlag], flat, "jsonrpc"), "pass");
  assert.equal(cellStatus([noFlag], streaming, "jsonrpc"), "none");
});

test("scenarioIncludesSdk matches versioned peers and always matches current", () => {
  assert.equal(scenarioIncludesSdk(scenario({ sdks: ["go_v10", "java_v10"] }), "java"), true);
  assert.equal(scenarioIncludesSdk(scenario({ sdks: ["go_v10", "java_v10"] }), "rust"), false);
  assert.equal(scenarioIncludesSdk(scenario({ sdks: ["current", "go_v10"] }), "rust"), true);
});

test("layoutTopology roots on current and draws one line per pair", () => {
  const { nodes, lines, arrows, viewBox } = layoutTopology(
    ["python_v10", "current", "go_v10"],
    ["1->0", "0->1", "1->2", "2->1"],
  );

  assert.equal(nodes.find((n) => n.root)?.sdk, "current");
  assert.equal(nodes.filter((n) => !n.root).every((n) => n.pos.x === 320), true);
  assert.equal(lines.length, 2, "reciprocal edges collapse into one line");
  assert.equal(lines.every((l) => l.bidirectional), true);
  assert.equal(arrows.length, 4, "two arrowheads per bidirectional line");
  assert.equal(viewBox, "0 0 400 300");
});

test("layoutTopology uses the narrow band and one arrowhead for a one-way pair", () => {
  const { lines, arrows, viewBox, box } = layoutTopology(["current", "go_v10"], ["0->1"]);
  assert.equal(lines.length, 1);
  assert.equal(lines[0].bidirectional, false);
  assert.equal(arrows.length, 1);
  assert.equal(viewBox, "0 105 400 90");
  assert.equal(box.w, 140, "pairwise pills are wider");
});

test("layoutTopology never lets peer pills touch, growing the canvas instead", () => {
  const peers = (n: number) =>
    layoutTopology(
      ["current", ...Array.from({ length: n }, (_, i) => `peer${i}_v10`)],
      Array.from({ length: n }, (_, i) => `0->${i + 1}`),
    );

  for (const n of [2, 3, 5, 6, 8, 12]) {
    const { nodes, box, viewBox } = peers(n);
    const ys = nodes.filter((node) => !node.root).map((node) => node.pos.y);
    const gaps = ys.slice(1).map((y, i) => y - ys[i] - box.h);
    assert.ok(
      Math.min(...gaps) >= 16 - 1e-9,
      `${n} peers: smallest gap was ${Math.min(...gaps)}`,
    );

    // Every pill must sit inside the viewBox.
    const height = Number(viewBox.split(" ")[3]);
    assert.ok(Math.min(...ys) - box.h / 2 >= 0, `${n} peers: first pill clipped`);
    assert.ok(Math.max(...ys) + box.h / 2 <= height, `${n} peers: last pill clipped`);
  }

  // Small topologies keep the original 300-unit canvas and spread.
  assert.equal(peers(2).viewBox, "0 0 400 300");
  assert.equal(peers(5).viewBox, "0 0 400 300");
  assert.ok(Number(peers(8).viewBox.split(" ")[3]) > 300, "8 peers should grow");
});

test("layoutTopology routes edges clear of every unrelated pill", () => {
  for (const n of [2, 3, 5, 8, 12]) {
    const sdks = ["current", ...Array.from({ length: n }, (_, i) => `peer${i}_v10`)];
    const edges = Array.from({ length: n }, (_, i) => [`0->${i + 1}`, `${i + 1}->0`]).flat();
    const { nodes, lines, box } = layoutTopology(sdks, edges);

    for (const line of lines) {
      for (const node of nodes) {
        if (node.index === line.from || node.index === line.to) continue;
        // Walk the segment and assert it never enters this pill's rectangle.
        for (let s = 0; s <= 500; s++) {
          const t = s / 500;
          const x = line.start.x + t * (line.end.x - line.start.x);
          const y = line.start.y + t * (line.end.y - line.start.y);
          const inside =
            Math.abs(x - node.pos.x) < box.w / 2 && Math.abs(y - node.pos.y) < box.h / 2;
          assert.ok(
            !inside,
            `${n} peers: edge ${line.key} crosses ${node.sdk} at (${x.toFixed(1)}, ${y.toFixed(1)})`,
          );
        }
      }
    }
  }
});

test("layoutTopology anchors edges on the facing pill face, not its centre", () => {
  const { nodes, lines, box } = layoutTopology(
    ["current", "a_v10", "b_v10", "c_v10"],
    ["0->1", "0->2", "0->3"],
  );
  const root = nodes.find((n) => n.root)!;

  for (const line of lines) {
    // Leaves the root's right face, arrives on the peer's left face.
    assert.equal(line.start.x, root.pos.x + box.w / 2);
    assert.equal(line.start.y, root.pos.y);

    const peer = nodes.find((n) => n.index === line.to)!;
    assert.equal(line.end.x, peer.pos.x - box.w / 2);
    assert.equal(line.end.y, peer.pos.y, "arrow meets the peer at its edge midpoint");
  }
});

test("layoutTopology ignores self-loops and out-of-range edges", () => {
  const { lines } = layoutTopology(["current", "go_v10"], ["0->0", "0->5", "x->1"]);
  assert.equal(lines.length, 0);
});
