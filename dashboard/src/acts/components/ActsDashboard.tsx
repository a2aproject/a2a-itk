import { useState } from "react";
import ActsRunDetail from "./ActsRunDetail.tsx";
import RunHistory from "../../shared/components/RunHistory.tsx";
import type { SdkTarget } from "../../shared/types.ts";
import type { ActsRun } from "../types.ts";

interface Props {
  runs: ActsRun[];
  sdk: SdkTarget;
}

/**
 * Owns the "which run is selected" state. ActsView mounts this with
 * `key={sdk.id}`, so switching SDK resets the selection to the newest run.
 */
export default function ActsDashboard({ runs, sdk }: Props) {
  const [runIndex, setRunIndex] = useState(0);
  const run = runs[runIndex] ?? runs[0];

  return (
    <div className="layout">
      <RunHistory
        runs={runs}
        activeIndex={runIndex}
        onSelect={setRunIndex}
        isOk={(r) => r.conformant}
        okLabel="conformant"
        failLabel="not conformant"
      />
      <ActsRunDetail run={run} sdk={sdk} />
    </div>
  );
}
