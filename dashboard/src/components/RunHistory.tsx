import { runPassed } from "../lib.ts";
import type { Run } from "../types.ts";

interface Props {
  runs: Run[];
  activeIndex: number;
  onSelect: (index: number) => void;
}

export default function RunHistory({ runs, activeIndex, onSelect }: Props) {
  return (
    <aside className="card history" aria-label="Nightly run history">
      <div className="card-head">
        <h2>Nightly runs</h2>
        <span className="pill">{runs.length}</span>
      </div>
      <ol className="run-list">
        {runs.map((run, index) => {
          const ok = runPassed(run);
          return (
            <li key={`${run.timestamp}-${run.commit_sha}`}>
              <button
                type="button"
                className={`run-item${index === activeIndex ? " run-active" : ""}`}
                aria-current={index === activeIndex ? "true" : undefined}
                onClick={() => onSelect(index)}
              >
                <span
                  className={`dot ${ok ? "dot-pass" : "dot-fail"}`}
                  aria-hidden="true"
                />
                <span className="run-meta">
                  <time dateTime={run.timestamp}>
                    {new Date(run.timestamp).toLocaleString()}
                  </time>
                  <span className="mono">{run.commit_sha.slice(0, 7)}</span>
                </span>
                <span className="sr-only">{ok ? "all passed" : "has failures"}</span>
              </button>
            </li>
          );
        })}
      </ol>
    </aside>
  );
}
