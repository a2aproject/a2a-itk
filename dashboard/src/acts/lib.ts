// Pure derivation helpers for the conformance view.
// Kept free of React/DOM so `node --test` can exercise them directly.

import type {
  ActsRun,
  Counts,
  Level,
  Outcome,
  RowFilter,
  SuiteGroup,
  TestResult,
  TestRow,
  Transport,
  TransportResult,
} from "./types.ts";

/** Column order of the conformance table, matching the specification's §3. */
export const TRANSPORTS: Transport[] = ["jsonrpc", "grpc", "rest"];

export const TRANSPORT_LABELS: Record<Transport, string> = {
  jsonrpc: "JSON-RPC",
  grpc: "gRPC",
  rest: "HTTP+JSON",
};

export const LEVELS: Level[] = ["must", "should", "may"];

/** Suite token: the part of `CORE-SEND-002` before the first dash. */
export const suiteOf = (id: string): string => id.split("-")[0] || "other";

/** Transports this run actually exercised, in table order. */
export const runTransports = (run: ActsRun): Transport[] =>
  TRANSPORTS.filter((t) => run.results[t] !== undefined);

export const transportResult = (
  run: ActsRun,
  transport: Transport,
): TransportResult | undefined => run.results[transport];

/**
 * Pivot the per-transport test lists into one row per test id.
 *
 * ACTS runs the same corpus over every transport, so the id sets normally
 * match. Union rather than intersection anyway: a transport-specific test
 * (GRPC-STATUS-001) or a corpus that moved between runs should still appear,
 * with a blank cell where it did not run.
 */
export function buildRows(run: ActsRun): TestRow[] {
  const transports = runTransports(run);
  const rows = new Map<string, TestRow>();

  for (const transport of transports) {
    for (const test of run.results[transport]?.tests ?? []) {
      let row = rows.get(test.id);
      if (!row) {
        row = {
          id: test.id,
          suite: suiteOf(test.id),
          level: test.level,
          cells: {},
          deviates: false,
          failing: false,
        };
        rows.set(test.id, row);
      }
      row.cells[transport] = test;
      if (test.result !== "pass") row.deviates = true;
      if (test.result === "fail" || test.result === "error") row.failing = true;
    }
  }

  return [...rows.values()];
}

/** Group rows into suites, preserving the order each first appeared. */
export function groupBySuite(rows: TestRow[]): SuiteGroup[] {
  const groups = new Map<string, TestRow[]>();
  for (const row of rows) {
    const existing = groups.get(row.suite);
    if (existing) existing.push(row);
    else groups.set(row.suite, [row]);
  }
  return [...groups].map(([suite, suiteRows]) => ({ suite, rows: suiteRows }));
}

export const rowMatches = (row: TestRow, filter: RowFilter): boolean => {
  if (filter === "all") return true;
  if (filter === "failing") return row.failing;
  return row.deviates && !row.failing;
};

/** How many rows each filter would show, for the filter control's counts. */
export function filterCounts(rows: TestRow[]): Record<RowFilter, number> {
  let failing = 0;
  let skipped = 0;
  for (const row of rows) {
    if (row.failing) failing++;
    else if (row.deviates) skipped++;
  }
  return { all: rows.length, failing, skipped };
}

/**
 * Whether a row can be expanded. Only deviations carry a reason worth reading;
 * an all-pass row would open onto nothing.
 */
export const hasDetail = (row: TestRow): boolean => row.deviates;

/** Everything that distinguishes one reason from another. */
const reasonKey = (test: TestResult): string =>
  JSON.stringify([
    test.result,
    test.skip_reason ?? "",
    test.failure?.message ?? "",
    test.failure?.step_id ?? "",
    test.failure?.expected ?? "",
    test.failure?.actual ?? "",
    test.failure?.assertion_path ?? "",
  ]);

export interface Deviation {
  /** Every transport that deviated for this reason, in table order. */
  transports: Transport[];
  test: TestResult;
}

/**
 * The reasons a row did not pass, one entry per distinct reason.
 *
 * A test that fails the same way on all three transports is the common case —
 * the SDK, not the binding, is what is wrong — so listing it once against three
 * transport names beats printing the same expected/actual block three times.
 */
export function deviationsOf(row: TestRow, transports: Transport[]): Deviation[] {
  const byReason = new Map<string, Deviation>();

  for (const transport of transports) {
    const test = row.cells[transport];
    if (!test || test.result === "pass") continue;
    const key = reasonKey(test);
    const existing = byReason.get(key);
    if (existing) existing.transports.push(transport);
    else byReason.set(key, { transports: [transport], test });
  }

  return [...byReason.values()];
}

/** Sum one field of `by_level` across every transport in the run. */
export function levelTotals(run: ActsRun): Record<Level, Counts> {
  const zero = (): Counts => ({
    total: 0,
    passed: 0,
    failed: 0,
    skipped: 0,
    errors: 0,
  });
  const totals: Record<Level, Counts> = {
    must: zero(),
    should: zero(),
    may: zero(),
  };

  for (const transport of runTransports(run)) {
    const byLevel = run.results[transport]?.by_level;
    if (!byLevel) continue;
    for (const level of LEVELS) {
      const counts = byLevel[level];
      if (!counts) continue;
      totals[level].total += counts.total;
      totals[level].passed += counts.passed;
      totals[level].failed += counts.failed;
      totals[level].skipped += counts.skipped;
      totals[level].errors += counts.errors;
    }
  }

  return totals;
}

/** `250954` -> `4m 11s`; whole seconds below a minute. */
export function formatDuration(ms: number | undefined): string {
  if (!ms || ms < 0) return "—";
  const seconds = Math.round(ms / 1000);
  if (seconds < 60) return `${seconds}s`;
  return `${Math.floor(seconds / 60)}m ${String(seconds % 60).padStart(2, "0")}s`;
}

/** `234/333` as a whole percentage, guarding the empty run. */
export const passRate = (passed: number, total: number): number =>
  total > 0 ? Math.round((passed / total) * 100) : 0;

/** Wording for a cell, used as its accessible label. */
export const outcomeLabel: Record<Outcome, string> = {
  pass: "passed",
  fail: "failed",
  skip: "skipped",
  error: "errored",
};
