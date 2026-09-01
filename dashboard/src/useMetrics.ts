import { useEffect, useState } from "react";
import { sortRuns } from "./lib.ts";
import type { Run } from "./types.ts";

export type MetricsState =
  | { status: "loading" }
  | { status: "empty" }
  | { status: "ready"; runs: Run[] };

/**
 * Load one SDK's nightly metrics file, newest run first. A missing or empty
 * file is not an error: an SDK simply may not publish metrics yet. `file` is
 * optional so callers can run the hook before knowing the route is valid.
 */
export function useMetrics(file: string | undefined): MetricsState {
  const [state, setState] = useState<MetricsState>({ status: "loading" });

  useEffect(() => {
    if (!file) return;
    const controller = new AbortController();
    setState({ status: "loading" });

    fetch(file, { signal: controller.signal })
      .then((response) => {
        if (!response.ok) throw new Error(`HTTP ${response.status}`);
        return response.json() as Promise<Run[]>;
      })
      .then((runs) => {
        if (!Array.isArray(runs) || runs.length === 0) throw new Error("no runs");
        setState({ status: "ready", runs: sortRuns(runs) });
      })
      .catch(() => {
        if (!controller.signal.aborted) setState({ status: "empty" });
      });

    return () => controller.abort();
  }, [file]);

  return state;
}
