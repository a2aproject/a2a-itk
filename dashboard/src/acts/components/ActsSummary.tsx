import {
  LEVELS,
  TRANSPORT_LABELS,
  formatDuration,
  levelTotals,
  passRate,
  runTransports,
} from "../lib.ts";
import { commitUrl } from "../../shared/sdks.ts";
import type { SdkTarget } from "../../shared/types.ts";
import type { ActsRun, Summary } from "../types.ts";

const LEVEL_NOTE: Record<string, string> = {
  must: "required by the specification",
  should: "recommended",
  may: "optional",
};

/**
 * Proportion of pass/fail/skip in one transport's run. The same numbers are
 * spelled out beside it, so this is aria-hidden rather than a second label.
 */
function OutcomeBar({ counts }: { counts: Summary }) {
  const total = counts.total || 1;
  const pct = (n: number) => `${(n / total) * 100}%`;
  return (
    <span className="bar" aria-hidden="true">
      <span className="bar-pass" style={{ width: pct(counts.passed) }} />
      <span
        className="bar-fail"
        style={{ width: pct(counts.failed + counts.errors) }}
      />
      <span className="bar-skip" style={{ width: pct(counts.skipped) }} />
    </span>
  );
}

/** Verdict, provenance and the per-transport split for one conformance run. */
export default function ActsSummary({ run, sdk }: { run: ActsRun; sdk: SdkTarget }) {
  const transports = runTransports(run);
  const levels = levelTotals(run);
  const { summary } = run;

  return (
    <section className="card" aria-labelledby="acts-summary-heading">
      <div className="card-head">
        <h2 id="acts-summary-heading">Conformance</h2>
        <span
          className={run.conformant ? "status status-pass" : "status status-fail"}
        >
          {run.conformant ? "Conformant" : "Not conformant"}
        </span>
      </div>

      <dl className="run-facts">
        <div>
          <dt>Run</dt>
          <dd>
            <time dateTime={run.timestamp}>
              {new Date(run.timestamp).toLocaleString()}
            </time>
          </dd>
        </div>
        <div>
          <dt>Commit</dt>
          <dd>
            <a className="mono" href={commitUrl(sdk, run.commit_sha)}>
              {run.commit_sha.slice(0, 7)}
            </a>
          </dd>
        </div>
        <div>
          <dt>Specification</dt>
          <dd>v{run.spec_version}</dd>
        </div>
        <div>
          <dt>ACTS corpus</dt>
          <dd>v{run.acts_version}</dd>
        </div>
        <div>
          <dt>Duration</dt>
          <dd>{formatDuration(summary.duration_ms)}</dd>
        </div>
        <div>
          <dt>Assertions</dt>
          <dd>
            {summary.passed} of {summary.total} passed
          </dd>
        </div>
      </dl>

      <h3 className="minor-heading">By transport</h3>
      <ul className="transport-list">
        {transports.map((transport) => {
          const result = run.results[transport];
          if (!result) return null;
          const counts = result.summary;
          return (
            <li key={transport} className="transport-row">
              <span className="transport-name">
                {TRANSPORT_LABELS[transport]}
                <span
                  className={
                    result.conformant ? "tick tick-pass" : "tick tick-fail"
                  }
                >
                  {result.conformant ? "conformant" : "not conformant"}
                </span>
              </span>
              <OutcomeBar counts={counts} />
              <span className="transport-counts">
                <span className="count-pass">{counts.passed} passed</span>
                {counts.failed + counts.errors > 0 && (
                  <span className="count-fail">
                    {counts.failed + counts.errors} failed
                  </span>
                )}
                {counts.skipped > 0 && (
                  <span className="count-skip">{counts.skipped} skipped</span>
                )}
                <span className="count-rate">
                  {passRate(counts.passed, counts.total)}%
                </span>
              </span>
            </li>
          );
        })}
      </ul>

      <h3 className="minor-heading">By requirement level</h3>
      <ul className="level-list">
        {LEVELS.map((level) => {
          const counts = levels[level];
          const broken = counts.failed + counts.errors;
          return (
            <li key={level} className="level-row">
              <span className={`level level-${level}`}>{level}</span>
              <span className="level-note">{LEVEL_NOTE[level]}</span>
              <span className="level-counts">
                {counts.passed}/{counts.total}
                {broken > 0 && <span className="count-fail">{broken} failed</span>}
              </span>
            </li>
          );
        })}
      </ul>
      <p className="muted footnote">
        Conformance is decided by the <strong>must</strong> level: a single failed
        MUST assertion on any transport makes the run non-conformant.
      </p>
    </section>
  );
}
