// Pure derivation helpers for the ITK compatibility dashboard.
// Kept free of React/DOM so `node --test src/lib.test.ts` can exercise them.

import type { Behavior, CellStatus, Run, Scenario, SdkTarget, Topology } from "./types.ts";

export const SDKS: SdkTarget[] = [
  { id: "python", label: "Python", file: "itk_python.json", repo: "a2a-python" },
  { id: "go", label: "Go", file: "itk_go.json", repo: "a2a-go" },
  { id: "rust", label: "Rust", file: "itk_rust.json", repo: "a2a-rs" },
  { id: "dotnet", label: ".NET", file: "itk_dotnet.json", repo: "a2a-dotnet" },
  { id: "ts", label: "TypeScript", file: "itk_ts.json", repo: "a2a-js" },
  { id: "java", label: "Java", file: "itk_java.json", repo: "a2a-java" },
];

export const PROTOCOLS = ["jsonrpc", "grpc", "http_json"] as const;

export const BEHAVIORS: Behavior[] = [
  { name: "send_message", streaming: false, label: "Send Message" },
  { name: "send_message", streaming: true, label: "Send Message (Streaming)" },
  { name: "push_notification", streaming: false, label: "Push Notification" },
  { name: "resubscribe", streaming: true, label: "Resubscribe" },
];

const lower = (s: string | undefined): string => (s || "").toLowerCase();

/** Languages whose display name is not just the capitalised token. */
const LANG_NAMES: Record<string, string> = { dotnet: ".NET", ts: "TypeScript" };

/** Display name: `python_v10` -> `Python v1.0`, `current` -> `Current`. */
export function formatSdkName(sdk: string): string {
  if (!sdk) return "";
  if (lower(sdk) === "current") return "Current";

  const [head, ver] = sdk.split("_");
  const lang = LANG_NAMES[lower(head)] ?? head.charAt(0).toUpperCase() + head.slice(1);
  if (!ver) return lang;
  // `v10` is a compacted `v1.0`; anything else passes through verbatim.
  return `${lang} ${ver.startsWith("v") && ver.length === 3 ? `v${ver[1]}.${ver[2]}` : ver}`;
}

/** Language token used to colour graph nodes via CSS (`.node-python`, ...). */
export function sdkKind(sdk: string): string {
  const s = lower(sdk);
  if (s === "current") return "current";
  const known = ["python", "go", "rust", "java", "dotnet", "ts"];
  return known.find((k) => s.startsWith(k)) || "other";
}

export const runPassed = (run: Run): boolean =>
  run.all_passed ?? (run.scenarios || []).every((s) => s.passed);

export const failedScenarios = (run: Run): Scenario[] =>
  (run.scenarios || []).filter((s) => !s.passed);

/** Newest run first. Does not mutate the input. */
export const sortRuns = (runs: Run[]): Run[] =>
  [...runs].sort((a, b) => Date.parse(b.timestamp) - Date.parse(a.timestamp));

export function scenarioIncludesSdk(scenario: Scenario, activeSdk: string): boolean {
  const sdks = (scenario.sdks || []).map(lower);
  return sdks.includes("current") || sdks.some((s) => s.startsWith(lower(activeSdk)));
}

/**
 * Build the star topology shown in the summary card.
 *
 * Two run shapes exist in the wild:
 *   (a) one giant star scenario per (protocol x behavior) listing every peer;
 *   (b) only pairwise (2-node) scenarios, with no giant star scenario at all.
 *
 * Taking a single scenario's SDK list works for (a) but degrades to "just one
 * pair" for (b). So: synthesise `current` plus the union of every other SDK
 * seen across all scenarios. That reduces to the giant star for (a) and yields
 * a proper star-of-all-peers for (b). When a genuine giant-star scenario does
 * exist its ordering wins, keeping historical dashboards visually identical.
 */
export function summaryTopology(scenarios: Scenario[]): Topology {
  const currentLabel =
    scenarios.flatMap((s) => s.sdks || []).find((n) => lower(n) === "current") ||
    "current";

  const peers: string[] = [];
  for (const scenario of scenarios) {
    for (const sdk of scenario.sdks || []) {
      if (sdk && lower(sdk) !== "current" && !peers.includes(sdk)) peers.push(sdk);
    }
  }

  let biggest: string[] = [];
  for (const scenario of scenarios) {
    if ((scenario.sdks || []).length > biggest.length) biggest = [...scenario.sdks];
  }
  const useBiggest =
    biggest.length === peers.length + 1 && biggest.some((n) => lower(n) === "current");

  const sdks = useBiggest ? biggest : [currentLabel, ...peers];

  const rootIdx = Math.max(
    0,
    sdks.findIndex((n) => lower(n) === "current"),
  );
  const edges: string[] = [];
  sdks.forEach((_, idx) => {
    if (idx === rootIdx) return;
    edges.push(`${rootIdx}->${idx}`, `${idx}->${rootIdx}`);
  });

  return { sdks, edges };
}

/** `[A,B]` and `[B,A]` must land in the same bucket. */
const pairKey = (sdks: string[]): string =>
  [...(sdks || [])]
    .map(lower)
    .sort()
    .join(",");

export interface PairwiseGroup extends Topology {
  key: string;
  scenarios: Scenario[];
}

/**
 * Group every 2-node scenario into one card per unordered SDK pair, ordered to
 * match the summary star so the page reads top-to-bottom consistently.
 */
export function pairwiseGroups(
  scenarios: Scenario[],
  summarySdks: string[],
): PairwiseGroup[] {
  const rank = (sdk: string): number => {
    const i = summarySdks.findIndex((s) => lower(s) === lower(sdk));
    return i === -1 ? 999 : i;
  };

  const keys: string[] = [];
  for (const scenario of scenarios) {
    if ((scenario.sdks || []).length !== 2) continue;
    const key = pairKey(scenario.sdks);
    if (!keys.includes(key)) keys.push(key);
  }

  const ranksOf = (key: string): number[] =>
    key
      .split(",")
      .map(rank)
      .sort((a, b) => a - b);

  keys.sort((a, b) => {
    const [a0, a1] = ranksOf(a);
    const [b0, b1] = ranksOf(b);
    return a0 !== b0 ? a0 - b0 : a1 - b1;
  });

  return keys.map((key) => {
    const group = scenarios.filter(
      (s) => (s.sdks || []).length === 2 && pairKey(s.sdks) === key,
    );
    const sdks = key.split(",").sort((a, b) => rank(a) - rank(b));

    // Prefer a scenario already listed in our display order; otherwise take any
    // and flip its edge indices, or the arrows would point the wrong way.
    const matched = group.find(
      (s) => lower(s.sdks[0]) === sdks[0] && lower(s.sdks[1]) === sdks[1],
    );
    const source = matched || group[0];
    let edges = source.edges || [];
    if (!matched && lower(source.sdks[0]) !== sdks[0]) {
      const flip = (n: string) => (n === "0" ? "1" : "0");
      edges = edges.map((e) =>
        e.replace(/(\d+)->(\d+)/, (_, from: string, to: string) => `${flip(from)}->${flip(to)}`),
      );
    }

    return { key, sdks, edges, scenarios: group };
  });
}

/** Status of one behaviour x protocol cell across a group of scenarios. */
export function cellStatus(
  scenarios: Scenario[],
  behavior: Behavior,
  protocol: string,
): CellStatus {
  const hits = scenarios.filter(
    (s) =>
      (s.protocols || []).includes(protocol) &&
      s.behavior === behavior.name &&
      (s.streaming === true) === behavior.streaming,
  );
  if (hits.length === 0) return "none";
  const passed = hits.some((s) => s.passed);
  const failed = hits.some((s) => !s.passed);
  if (passed && failed) return "mixed";
  return passed ? "pass" : "fail";
}
