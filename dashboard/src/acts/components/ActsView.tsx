import { useEffect } from "react";
import { Link, useParams } from "react-router-dom";
import ActsDashboard from "./ActsDashboard.tsx";
import DomainTabs from "../../shared/components/DomainTabs.tsx";
import NotFound from "../../shared/components/NotFound.tsx";
import SdkTabs from "../../shared/components/SdkTabs.tsx";
import { findSdk } from "../../shared/sdks.ts";
import { useMetrics } from "../../shared/useMetrics.ts";
import { DEFAULT_SDK, sdkPath } from "../../routes.tsx";
import type { ActsRun } from "../types.ts";

/** One SDK's nightly conformance run, selected by the `:sdkId` param. */
export default function ActsView() {
  const { sdkId } = useParams();
  const sdk = findSdk(sdkId);
  const metrics = useMetrics<ActsRun>(sdk?.files.acts);

  useEffect(() => {
    if (sdk) {
      document.title = `${sdk.label} conformance — A2A Integration Test Kit`;
    }
  }, [sdk]);

  if (!sdk) return <NotFound />;

  return (
    <>
      <DomainTabs active="acts" sdkId={sdk.id} />
      <SdkTabs domain="acts" active={sdk.id} />

      {metrics.status === "loading" && (
        <p className="notice" role="status">
          <span className="spinner" aria-hidden="true" /> Loading the {sdk.label}{" "}
          conformance report…
        </p>
      )}

      {metrics.status === "empty" && (
        <p className="notice" role="status">
          No conformance runs are published for the {sdk.label} SDK yet. Its nightly
          ACTS job either has not landed or has not run.{" "}
          <Link to={sdkPath("acts", DEFAULT_SDK)}>See another SDK.</Link>
        </p>
      )}

      {metrics.status === "ready" && (
        <ActsDashboard key={sdk.id} runs={metrics.runs} sdk={sdk} />
      )}
    </>
  );
}
