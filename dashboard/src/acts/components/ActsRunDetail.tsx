import ActsLegend from "./ActsLegend.tsx";
import ActsSummary from "./ActsSummary.tsx";
import ConformanceTable from "./ConformanceTable.tsx";
import type { SdkTarget } from "../../shared/types.ts";
import type { ActsRun } from "../types.ts";

/** Everything about one conformance run: the verdict, then the evidence. */
export default function ActsRunDetail({
  run,
  sdk,
}: {
  run: ActsRun;
  sdk: SdkTarget;
}) {
  return (
    <div className="detail">
      <ActsSummary run={run} sdk={sdk} />
      {/* Keyed by run so switching runs resets the filter and open rows. */}
      <ConformanceTable key={`${run.timestamp}-${run.commit_sha}`} run={run} />
      <ActsLegend />
    </div>
  );
}
