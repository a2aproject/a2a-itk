import type { RunMeta } from "../types.ts";

interface Props<T extends RunMeta> {
  runs: T[];
  activeIndex: number;
  onSelect: (index: number) => void;
  /** Whether a run counts as clean; each domain decides for itself. */
  isOk: (run: T) => boolean;
  /** Screen-reader wording for the green/red dot, e.g. "conformant". */
  okLabel: string;
  failLabel: string;
}

/** The run picker both domains share: newest first, one row per nightly run. */
export default function RunHistory<T extends RunMeta>({
  runs,
  activeIndex,
  onSelect,
  isOk,
  okLabel,
  failLabel,
}: Props<T>) {
  return (
    <aside className="card history" aria-label="Nightly run history">
      <div className="card-head">
        <h2>Nightly runs</h2>
        <span className="pill">{runs.length}</span>
      </div>
      <ol className="run-list">
        {runs.map((run, index) => {
          const ok = isOk(run);
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
                <span className="sr-only">{ok ? okLabel : failLabel}</span>
              </button>
            </li>
          );
        })}
      </ol>
    </aside>
  );
}
