// Route table. The dashboard is a single page, so a hash router keeps every
// route inside the fragment and GitHub Pages needs no rewrite rules.
//
//   #/             -> redirect to the cross-SDK summary view (#/summary)
//   #/summary      -> all SDKs at a glance (interoperability + conformance)
//   #/itk/go       -> Go interoperability results
//   #/acts/go      -> Go conformance results
//   #/go           -> legacy one-segment path, redirected to #/itk/go
//   #/nonsense     -> not-found view

import { Navigate, createHashRouter, useParams } from "react-router-dom";
import ActsView from "./acts/components/ActsView.tsx";
import ItkView from "./itk/components/ItkView.tsx";
import Layout from "./shared/components/Layout.tsx";
import NotFound from "./shared/components/NotFound.tsx";
import { SDKS, findSdk } from "./shared/sdks.ts";
import type { Domain } from "./shared/types.ts";
import SummaryView from "./summary/components/SummaryView.tsx";

export const DEFAULT_SDK = SDKS[0].id;
export const DEFAULT_DOMAIN: Domain = "itk";
export const SUMMARY_PATH = "/summary";

/** Canonical path for one suite/SDK combination. */
export const sdkPath = (domain: Domain, sdkId: string): string =>
  `/${domain}/${sdkId}`;

/**
 * The dashboard was single-suite once, so `#/go` used to be a real URL. Keep
 * those links alive by sending them to the interoperability view.
 */
function LegacySdkPath() {
  const { sdkId } = useParams();
  if (!findSdk(sdkId)) return <NotFound />;
  return <Navigate to={sdkPath("itk", sdkId as string)} replace />;
}

const toDefault = (domain: Domain) => (
  <Navigate to={sdkPath(domain, DEFAULT_SDK)} replace />
);

export const router = createHashRouter([
  {
    path: "/",
    element: <Layout />,
    children: [
      { index: true, element: <Navigate to={SUMMARY_PATH} replace /> },
      { path: "summary", element: <SummaryView /> },
      { path: "itk", element: toDefault("itk") },
      { path: "itk/:sdkId", element: <ItkView /> },
      { path: "acts", element: toDefault("acts") },
      { path: "acts/:sdkId", element: <ActsView /> },
      { path: ":sdkId", element: <LegacySdkPath /> },
      { path: "*", element: <NotFound /> },
    ],
  },
]);
