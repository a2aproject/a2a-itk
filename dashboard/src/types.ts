/** Shape of the nightly metrics JSON published by each SDK repo. */

export interface Scenario {
  name: string;
  /** Topology nodes, e.g. `["current", "python_v10"]`. */
  sdks: string[];
  /** Directed edges as `"<fromIndex>-><toIndex>"` into `sdks`. */
  edges?: string[];
  protocols?: string[];
  behavior: string;
  streaming?: boolean;
  traversal?: string;
  build_subtests?: boolean;
  passed: boolean;
}

export interface Run {
  timestamp: string;
  commit_sha: string;
  github_run_id?: number | string;
  all_passed?: boolean;
  scenarios?: Scenario[];
}

export interface Behavior {
  name: string;
  streaming: boolean;
  label: string;
}

export interface SdkTarget {
  id: string;
  label: string;
  /** Metrics file served next to index.html. */
  file: string;
  /** GitHub repo under a2aproject, for commit links. */
  repo: string;
}

export type CellStatus = "pass" | "fail" | "mixed" | "none";

export interface Topology {
  sdks: string[];
  edges: string[];
}
