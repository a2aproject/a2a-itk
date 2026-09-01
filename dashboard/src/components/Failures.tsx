import { formatSdkName } from "../lib.ts";
import type { Scenario } from "../types.ts";

export default function Failures({ failures }: { failures: Scenario[] }) {
  return (
    <details className="failures" open>
      <summary>Failed scenarios ({failures.length})</summary>
      <ul>
        {failures.map((scenario, index) => (
          <li key={`${scenario.name}-${index}`}>
            <strong>{scenario.name}</strong>
            <span className="muted">
              {" "}
              {(scenario.sdks || []).map(formatSdkName).join(" \u2194 ")} &middot;{" "}
              {scenario.streaming
                ? `${scenario.behavior} (streaming)`
                : scenario.behavior}{" "}
              &middot; {(scenario.protocols || []).join(", ")}
            </span>
          </li>
        ))}
      </ul>
    </details>
  );
}
