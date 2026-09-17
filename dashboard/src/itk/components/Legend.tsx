import type { CellStatus } from "../types.ts";

const ITEMS: [CellStatus, string, string][] = [
  ["pass", "Pass", "every scenario passed"],
  ["mixed", "Mixed", "some passed, some failed"],
  ["fail", "Fail", "every scenario failed"],
  ["none", "—", "not covered by this run"],
];

export default function Legend() {
  return (
    <dl className="legend" aria-label="Matrix legend">
      {ITEMS.map(([status, label, meaning]) => (
        <div key={status}>
          <dt>
            {status === "none" ? (
              <span className="cell-empty">{label}</span>
            ) : (
              <span className={`chip chip-${status}`}>{label}</span>
            )}
          </dt>
          <dd>{meaning}</dd>
        </div>
      ))}
    </dl>
  );
}
