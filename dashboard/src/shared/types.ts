/** Types common to both metrics domains. */

/**
 * The two test suites the dashboard renders. They answer different questions:
 * `itk` is "do these SDKs talk to each other", `acts` is "does this SDK obey
 * the specification".
 */
export type Domain = "itk" | "acts";

/** Fields every published run record carries, whichever suite produced it. */
export interface RunMeta {
  timestamp: string;
  commit_sha: string;
  github_run_id?: number | string;
}

export interface SdkTarget {
  id: string;
  label: string;
  /** GitHub repo under a2aproject, for commit links. */
  repo: string;
  /** Metrics files served next to index.html, one per domain. */
  files: Record<Domain, string>;
}
