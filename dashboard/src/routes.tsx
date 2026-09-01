// Route table. The dashboard is a single page, so a hash router keeps every
// route inside the fragment and GitHub Pages needs no rewrite rules.
//
//   #/          -> redirect to the default SDK
//   #/go        -> the Go SDK view
//   #/nonsense  -> not-found view

import { Navigate, createHashRouter } from "react-router-dom";
import Layout from "./components/Layout.tsx";
import NotFound from "./components/NotFound.tsx";
import SdkView from "./components/SdkView.tsx";
import { SDKS } from "./lib.ts";

export const DEFAULT_SDK = SDKS[0].id;

/** Canonical path for one SDK tab. */
export const sdkPath = (sdkId: string): string => `/${sdkId}`;

export const router = createHashRouter([
  {
    path: "/",
    element: <Layout />,
    children: [
      { index: true, element: <Navigate to={sdkPath(DEFAULT_SDK)} replace /> },
      { path: ":sdkId", element: <SdkView /> },
      { path: "*", element: <NotFound /> },
    ],
  },
]);
