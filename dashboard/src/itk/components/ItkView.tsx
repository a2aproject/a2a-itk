import { useEffect } from "react";
import { Link, useParams } from "react-router-dom";
import Dashboard from "./Dashboard.tsx";
import DomainTabs from "../../shared/components/DomainTabs.tsx";
import NotFound from "../../shared/components/NotFound.tsx";
import SdkTabs from "../../shared/components/SdkTabs.tsx";
import { findSdk } from "../../shared/sdks.ts";
import { useMetrics } from "../../shared/useMetrics.ts";
import { DEFAULT_SDK, sdkPath } from "../../routes.tsx";
import type { Run } from "../types.ts";

/** One SDK's nightly interoperability run, selected by the `:sdkId` param. */
export default function ItkView() {
  const { sdkId } = useParams();
  const sdk = findSdk(sdkId);
  const metrics = useMetrics<Run>(sdk?.files.itk);

  useEffect(() => {
    if (sdk) {
      document.title = `${sdk.label} interoperability — A2A Integration Test Kit`;
    }
  }, [sdk]);

  // An unknown `:sdkId` is a bad URL, not a bad SDK. Render the 404 in place
  // rather than redirecting, so the address bar still shows what was asked for.
  if (!sdk) return <NotFound />;

  return (
    <>
      <DomainTabs active="itk" sdkId={sdk.id} />
      <SdkTabs domain="itk" active={sdk.id} />

      {metrics.status === "loading" && (
        <p className="notice" role="status">
          <span className="spinner" aria-hidden="true" /> Loading the {sdk.label}{" "}
          interoperability matrix…
        </p>
      )}

      {metrics.status === "empty" && (
        <p className="notice" role="status">
          No interoperability runs are published for the {sdk.label} SDK yet.{" "}
          <Link to={sdkPath("itk", DEFAULT_SDK)}>See another SDK.</Link>
        </p>
      )}

      {metrics.status === "ready" && (
        // Remounting on SDK change resets the selected run to the newest one.
        <Dashboard key={sdk.id} runs={metrics.runs} sdk={sdk} />
      )}
    </>
  );
}
