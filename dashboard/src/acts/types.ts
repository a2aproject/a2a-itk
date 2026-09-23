/**
 * Shape of the nightly conformance metrics each SDK repo publishes as
 * `acts_<sdk>.json`, written by a2a-itk's scripts/process_acts_results.py.
 */

import type { RunMeta } from "../shared/types.ts";

/** ACTS transport ids, as they appear as keys of `ActsRun.results`. */
export type Transport = "jsonrpc" | "grpc" | "rest";

/** Requirement level from the specification: a MUST failure breaks conformance. */
export type Level = "must" | "should" | "may";

export type Outcome = "pass" | "fail" | "skip" | "error";

export interface Failure {
  message: string;
  /** Which step of the test's scenario went wrong. */
  step_id?: string;
  expected?: string;
  actual?: string;
  /** JSONPath into the response that the assertion was reading. */
  assertion_path?: string;
}

export interface TestResult {
  id: string;
  level: Level;
  result: Outcome;
  /** Present when `result` is `skip`. */
  skip_reason?: string;
  /** Present when `result` is `fail` or `error`. */
  failure?: Failure;
}

export interface Counts {
  total: number;
  passed: number;
  failed: number;
  skipped: number;
  errors: number;
}

export interface Summary extends Counts {
  duration_ms?: number;
}

export interface TransportResult {
  conformant: boolean;
  summary: Summary;
  by_level: Record<Level, Counts>;
  tests: TestResult[];
}

export interface ActsRun extends RunMeta {
  sdk: string;
  sdk_version: string;
  acts_version: string;
  spec_version: string;
  /** Transports this run exercised, in the order the runner reported them. */
  transports: string[];
  conformant: boolean;
  summary: Summary;
  results: Partial<Record<Transport, TransportResult>>;
}

/**
 * One test across every transport — the unit the conformance table renders as
 * a row. `cells` is undefined for a transport the run did not exercise.
 */
export interface TestRow {
  id: string;
  /** Leading token of the id, e.g. `CORE` in `CORE-SEND-002`. */
  suite: string;
  level: Level;
  cells: Partial<Record<Transport, TestResult>>;
  /** True when at least one transport did something other than pass. */
  deviates: boolean;
  /** True when at least one transport failed or errored. */
  failing: boolean;
}

export interface SuiteGroup {
  suite: string;
  rows: TestRow[];
}

/** Which rows the conformance table shows. */
export type RowFilter = "all" | "failing" | "skipped";
