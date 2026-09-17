import { useEffect, useState } from "react";
import { sortRuns } from "./sdks.ts";
import type { RunMeta } from "./types.ts";

export type MetricsState<T> =
  | { status: "loading" }
  | { status: "empty" }
  | { status: "ready"; runs: T[] };

/**
 * Load one SDK's published metrics file, newest run first. A missing or empty
 * file is not an error: an SDK simply may not publish that suite yet. `file` is
 * optional so callers can run the hook before knowing the route is valid.
 */
export function useMetrics<T extends RunMeta>(
  file: string | undefined,
): MetricsState<T> {
  const [state, setState] = useState<MetricsState<T>>({ status: "loading" });

  useEffect(() => {
    if (!file) return;
    const controller = new AbortController();
    setState({ status: "loading" });

    fetch(file, { signal: controller.signal })
      .then((response) => {
        if (!response.ok) throw new Error(`HTTP ${response.status}`);
        return response.json() as Promise<T[]>;
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
