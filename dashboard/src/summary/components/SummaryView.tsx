import { useEffect, useState } from "react";
import type { ActsRun } from "../../acts/types.ts";
import type { Run } from "../../itk/types.ts";
import DomainTabs from "../../shared/components/DomainTabs.tsx";
import { SDKS, sortRuns } from "../../shared/sdks.ts";
import type { RunMeta } from "../../shared/types.ts";
import { buildSdkOverview } from "../lib.ts";
import type { SdkOverview } from "../types.ts";
import SummaryTable from "./SummaryTable.tsx";

async function fetchNewestRun<T extends RunMeta>(
  file: string,
  signal: AbortSignal,
): Promise<T | undefined> {
  try {
    const response = await fetch(file, { signal });
    if (!response.ok) return undefined;
    const runs = (await response.json()) as T[];
    if (!Array.isArray(runs) || runs.length === 0) return undefined;
    return sortRuns(runs)[0];
  } catch {
    return undefined;
  }
}

/** Cross-SDK summary view combining interoperability and conformance in one table. */
export default function SummaryView() {
  const [rows, setRows] = useState<SdkOverview[] | null>(null);

  useEffect(() => {
    document.title = "Summary — A2A Integration Test Kit";
  }, []);

  useEffect(() => {
    const controller = new AbortController();

    Promise.all(
      SDKS.map(async (sdk) => {
        const [itkRun, actsRun] = await Promise.all([
          fetchNewestRun<Run>(sdk.files.itk, controller.signal),
          fetchNewestRun<ActsRun>(sdk.files.acts, controller.signal),
        ]);
        return buildSdkOverview(sdk, itkRun, actsRun);
      }),
    ).then((overviews) => {
      if (!controller.signal.aborted) {
        setRows(overviews);
      }
    });

    return () => controller.abort();
  }, []);

  return (
    <>
      <DomainTabs active="summary" />

      {rows === null ? (
        <p className="notice" role="status">
          <span className="spinner" aria-hidden="true" /> Loading the SDK
          compatibility summary…
        </p>
      ) : (
        <SummaryTable rows={rows} />
      )}
    </>
  );
}
