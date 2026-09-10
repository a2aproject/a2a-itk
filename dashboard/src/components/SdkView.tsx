import { useEffect } from "react";
import { Link, useParams } from "react-router-dom";
import Dashboard from "./Dashboard.tsx";
import NotFound from "./NotFound.tsx";
import SdkTabs from "./SdkTabs.tsx";
import { SDKS } from "../lib.ts";
import { DEFAULT_SDK, sdkPath } from "../routes.tsx";
import { useMetrics } from "../useMetrics.ts";

/** One SDK's nightly metrics, selected by the `:sdkId` route param. */
export default function SdkView() {
  const { sdkId } = useParams();
  const sdk = SDKS.find((s) => s.id === sdkId);
  const metrics = useMetrics(sdk?.file);

  useEffect(() => {
    if (sdk) document.title = `${sdk.label} SDK compatibility — A2A Integration Test Kit`;
  }, [sdk]);

  // An unknown `:sdkId` is a bad URL, not a bad SDK. Render the 404 in place
  // rather than redirecting, so the address bar still shows what was asked for.
  if (!sdk) return <NotFound />;

  return (
    <>
      <SdkTabs active={sdk.id} />

      {metrics.status === "loading" && (
        <p className="notice" role="status">
          <span className="spinner" aria-hidden="true" /> Loading the {sdk.label}{" "}
          interoperability matrix…
        </p>
      )}

      {metrics.status === "empty" && (
        <p className="notice" role="status">
          No nightly metrics are published for the {sdk.label} SDK yet.{" "}
          <Link to={sdkPath(DEFAULT_SDK)}>See another SDK.</Link>
        </p>
      )}

      {metrics.status === "ready" && (
        // Remounting on SDK change resets the selected run to the newest one.
        <Dashboard key={sdk.id} runs={metrics.runs} sdk={sdk} />
      )}
    </>
  );
}
