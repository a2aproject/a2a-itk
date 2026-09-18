import { useState } from "react";
import RunDetail from "./RunDetail.tsx";
import RunHistory from "../../shared/components/RunHistory.tsx";
import type { SdkTarget } from "../../shared/types.ts";
import { runPassed } from "../lib.ts";
import type { Run } from "../types.ts";

interface Props {
  runs: Run[];
  sdk: SdkTarget;
}

/**
 * Owns the "which run is selected" state. ItkView mounts this with
 * `key={sdk.id}`, so switching SDK remounts it and the selection resets to the
 * newest run without an extra effect.
 */
export default function Dashboard({ runs, sdk }: Props) {
  const [runIndex, setRunIndex] = useState(0);
  const run = runs[runIndex] ?? runs[0];

  return (
    <div className="layout">
      <RunHistory
        runs={runs}
        activeIndex={runIndex}
        onSelect={setRunIndex}
        isOk={runPassed}
        okLabel="all passed"
        failLabel="has failures"
      />
      <RunDetail run={run} sdk={sdk} />
    </div>
  );
}
