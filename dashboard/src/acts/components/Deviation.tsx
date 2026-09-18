import { TRANSPORT_LABELS } from "../lib.ts";
import type { Deviation as DeviationData } from "../lib.ts";
import type { TestResult } from "../types.ts";

const FIELDS: [keyof NonNullable<TestResult["failure"]>, string][] = [
  ["step_id", "Step"],
  ["expected", "Expected"],
  ["actual", "Actual"],
  ["assertion_path", "Path"],
];

/**
 * One reason a test did not pass, and every transport that hit it. Grouping
 * happens in `deviationsOf`; an identical failure on all three transports
 * arrives here once.
 */
export default function Deviation({ deviation }: { deviation: DeviationData }) {
  const { test, transports } = deviation;
  const { failure } = test;

  return (
    <div className="deviation">
      <p className="deviation-head">
        <span className={`chip chip-${test.result}`}>{test.result}</span>
        <span className="deviation-transport">
          {transports.map((t) => TRANSPORT_LABELS[t]).join(" · ")}
        </span>
      </p>

      {test.result === "skip" ? (
        <p className="deviation-message">{test.skip_reason || "No reason given."}</p>
      ) : (
        <>
          <p className="deviation-message">
            {failure?.message || "No failure message was recorded."}
          </p>
          {failure && (
            <dl className="deviation-fields">
              {FIELDS.map(([key, label]) => {
                const value = failure[key];
                if (!value) return null;
                return (
                  <div key={key}>
                    <dt>{label}</dt>
                    <dd className="mono">{value}</dd>
                  </div>
                );
              })}
            </dl>
          )}
        </>
      )}
    </div>
  );
}
