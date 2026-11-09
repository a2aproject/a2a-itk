// Pure derivation helpers for the cross-SDK summary view.
// Kept free of React/DOM so `node --test` can exercise them directly.

import { TRANSPORTS, levelTotals, runTransports } from "../acts/lib.ts";
import type { ActsRun, Transport } from "../acts/types.ts";
import { PROTOCOLS, runPassed, scenarioIncludesSdk } from "../itk/lib.ts";
import type { CellStatus, Run } from "../itk/types.ts";
import type { SdkTarget } from "../shared/types.ts";
import type {
  ActsSdkSummary,
  ItkProtocol,
  ItkSdkSummary,
  ProtocolCounts,
  SdkOverview,
  TransportSummary,
} from "./types.ts";

export { PROTOCOLS as ITK_PROTOCOLS, TRANSPORTS as ACTS_TRANSPORTS };

export const ITK_PROTOCOL_LABELS: Record<ItkProtocol, string> = {
  jsonrpc: "JSON-RPC",
  grpc: "gRPC",
  http_json: "HTTP+JSON",
};

function countsStatus(passed: number, total: number): CellStatus {
  if (total === 0) return "none";
  if (passed === total) return "pass";
  if (passed === 0) return "fail";
  return "mixed";
}

/** Summarize the newest interoperability run for one SDK. */
export function summarizeItkRun(run: Run, sdkId: string): ItkSdkSummary {
  const scenarios = (run.scenarios ?? []).filter((s) =>
    scenarioIncludesSdk(s, sdkId),
  );
  const total = scenarios.length;
  const passed = scenarios.filter((s) => s.passed).length;

  const byProtocol = {} as Record<ItkProtocol, ProtocolCounts>;
  for (const protocol of PROTOCOLS) {
    const hits = scenarios.filter((s) => (s.protocols ?? []).includes(protocol));
    const pTotal = hits.length;
    const pPassed = hits.filter((s) => s.passed).length;
    byProtocol[protocol] = {
      passed: pPassed,
      total: pTotal,
      status: countsStatus(pPassed, pTotal),
    };
  }

  return {
    timestamp: run.timestamp,
    commit_sha: run.commit_sha,
    allPassed: runPassed(run) && total > 0,
    passed,
    total,
    byProtocol,
  };
}

/** Summarize the newest conformance run for one SDK. */
export function summarizeActsRun(run: ActsRun): ActsSdkSummary {
  const must = levelTotals(run).must;
  const byTransport: Partial<Record<Transport, TransportSummary>> = {};

  for (const transport of runTransports(run)) {
    const res = run.results[transport];
    if (!res) continue;
    byTransport[transport] = {
      conformant: res.conformant,
      passed: res.summary.passed,
      total: res.summary.total,
      mustPassed: res.by_level?.must?.passed ?? 0,
      mustTotal: res.by_level?.must?.total ?? 0,
    };
  }

  return {
    timestamp: run.timestamp,
    commit_sha: run.commit_sha,
    conformant: run.conformant,
    passed: run.summary.passed,
    total: run.summary.total,
    mustPassed: must.passed,
    mustTotal: must.total,
    byTransport,
  };
}

/** Combine an SDK's newest ITK and ACTS runs into one overview row. */
export function buildSdkOverview(
  sdk: SdkTarget,
  itkRun: Run | undefined,
  actsRun: ActsRun | undefined,
): SdkOverview {
  return {
    sdk,
    itk: itkRun ? summarizeItkRun(itkRun, sdk.id) : undefined,
    acts: actsRun ? summarizeActsRun(actsRun) : undefined,
  };
}
