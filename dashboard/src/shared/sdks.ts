// The SDK registry, shared by both domains.
//
// Keep the `files` values in step with dashboard/scripts/fetch-metrics.sh,
// which is what actually puts them in public/.

import type { Domain, SdkTarget } from "./types.ts";

export const SDKS: SdkTarget[] = [
  {
    id: "python",
    label: "Python",
    repo: "a2a-python",
    files: { itk: "itk_python.json", acts: "acts_python.json" },
  },
  {
    id: "go",
    label: "Go",
    repo: "a2a-go",
    files: { itk: "itk_go.json", acts: "acts_go.json" },
  },
  {
    id: "rust",
    label: "Rust",
    repo: "a2a-rs",
    files: { itk: "itk_rust.json", acts: "acts_rust.json" },
  },
  {
    id: "dotnet",
    label: ".NET",
    repo: "a2a-dotnet",
    files: { itk: "itk_dotnet.json", acts: "acts_dotnet.json" },
  },
  {
    id: "ts",
    label: "TypeScript",
    repo: "a2a-js",
    files: { itk: "itk_ts.json", acts: "acts_ts.json" },
  },
  {
    id: "java",
    label: "Java",
    repo: "a2a-java",
    files: { itk: "itk_java.json", acts: "acts_java.json" },
  },
];

export const findSdk = (id: string | undefined): SdkTarget | undefined =>
  SDKS.find((s) => s.id === id);

export const metricsFile = (sdk: SdkTarget, domain: Domain): string =>
  sdk.files[domain];

export const commitUrl = (sdk: SdkTarget, sha: string): string =>
  `https://github.com/a2aproject/${sdk.repo}/commit/${sha}`;

/** Newest run first. Does not mutate the input. */
export const sortRuns = <T extends { timestamp: string }>(runs: T[]): T[] =>
  [...runs].sort((a, b) => Date.parse(b.timestamp) - Date.parse(a.timestamp));
