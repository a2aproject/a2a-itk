import { useEffect } from "react";
import { Link, useParams } from "react-router-dom";
import ActsDashboard from "./ActsDashboard.tsx";
import VersionTabs from "./VersionTabs.tsx";
import DomainTabs from "../../shared/components/DomainTabs.tsx";
import NotFound from "../../shared/components/NotFound.tsx";
import SdkTabs from "../../shared/components/SdkTabs.tsx";
import { DEFAULT_ACTS_VERSION, actsFile, findSdk, isActsVersion } from "../../shared/sdks.ts";
import { useMetrics } from "../../shared/useMetrics.ts";
import { DEFAULT_SDK, actsPath } from "../../routes.tsx";
import type { ActsRun } from "../types.ts";

/**
 * One SDK's nightly conformance runs at one A2A version, selected by the
 * `:sdkId` and `:version` params. Without a version it is 1.0.
 */
export default function ActsView() {
  const { sdkId, version: versionParam = DEFAULT_ACTS_VERSION } = useParams();
  const sdk = findSdk(sdkId);
  const version = isActsVersion(versionParam) ? versionParam : undefined;
  const metrics = useMetrics<ActsRun>(sdk && version ? actsFile(sdk, version) : undefined);

  useEffect(() => {
    if (sdk && version) {
      document.title = `${sdk.label} conformance at A2A ${version} — A2A Integration Test Kit`;
    }
  }, [sdk, version]);

  if (!sdk || !version) return <NotFound />;

  return (
    <>
      <DomainTabs active="acts" sdkId={sdk.id} />
      <SdkTabs
        domain="acts"
        active={sdk.id}
        pathFor={(id) => actsPath(id, version)}
      />
      <VersionTabs sdkId={sdk.id} active={version} />

      {metrics.status === "loading" && (
        <p className="notice" role="status">
          <span className="spinner" aria-hidden="true" /> Loading the {sdk.label}{" "}
          conformance report at A2A {version}…
        </p>
      )}

      {metrics.status === "empty" && version === DEFAULT_ACTS_VERSION && (
        <p className="notice" role="status">
          No conformance runs are published for the {sdk.label} SDK yet. Its nightly
          ACTS job either has not landed or has not run.{" "}
          <Link to={actsPath(DEFAULT_SDK, version)}>See another SDK.</Link>
        </p>
      )}

      {metrics.status === "empty" && version !== DEFAULT_ACTS_VERSION && (
        <p className="notice" role="status">
          No conformance runs at A2A {version} are published for the {sdk.label} SDK
          yet. An SDK publishes them once its agent serves A2A {version} and its
          nightly ACTS job runs at that version.{" "}
          <Link to={actsPath(sdk.id, DEFAULT_ACTS_VERSION)}>
            See its runs at A2A {DEFAULT_ACTS_VERSION}.
          </Link>
        </p>
      )}

      {metrics.status === "ready" && (
        <ActsDashboard key={`${sdk.id}-${version}`} runs={metrics.runs} sdk={sdk} />
      )}
    </>
  );
}
