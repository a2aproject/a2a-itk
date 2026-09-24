import { outcomeLabel } from "../lib.ts";
import type { TestResult } from "../types.ts";

/**
 * One test/transport cell. A pass is deliberately almost invisible: the table
 * is read to find deviations, so only those get ink.
 */
export default function ResultCell({ test }: { test: TestResult | undefined }) {
  if (!test) {
    return (
      <span className="cell-empty">
        <span aria-hidden="true">—</span>
        <span className="sr-only">not run</span>
      </span>
    );
  }

  if (test.result === "pass") {
    return (
      <span className="cell-pass">
        <span aria-hidden="true">✓</span>
        <span className="sr-only">passed</span>
      </span>
    );
  }

  return (
    <span className={`chip chip-${test.result}`}>
      {test.result}
      <span className="sr-only"> — {outcomeLabel[test.result]}</span>
    </span>
  );
}
