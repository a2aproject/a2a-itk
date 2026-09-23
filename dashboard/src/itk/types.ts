/** Shape of the nightly interoperability metrics published by each SDK repo. */

import type { RunMeta } from "../shared/types.ts";

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

export interface Run extends RunMeta {
  all_passed?: boolean;
  scenarios?: Scenario[];
}

export interface Behavior {
  name: string;
  streaming: boolean;
  label: string;
}

export type CellStatus = "pass" | "fail" | "mixed" | "none";

export interface Topology {
  sdks: string[];
  edges: string[];
}
