import { useMemo, useState } from "react";
import TestRow from "./TestRow.tsx";
import {
  TRANSPORT_LABELS,
  buildRows,
  filterCounts,
  groupBySuite,
  hasDetail,
  rowMatches,
  runTransports,
} from "../lib.ts";
import type { ActsRun, RowFilter } from "../types.ts";

const FILTERS: { id: RowFilter; label: string }[] = [
  { id: "all", label: "All" },
  { id: "failing", label: "Failing" },
  { id: "skipped", label: "Skipped" },
];

const EMPTY: Record<RowFilter, string> = {
  all: "This run executed no tests.",
  failing: "Nothing failed. Every test either passed or was skipped.",
  skipped: "Nothing was skipped: every test ran on every transport.",
};

/** The full corpus as test x transport, with reasons behind a disclosure. */
export default function ConformanceTable({ run }: { run: ActsRun }) {
  const transports = useMemo(() => runTransports(run), [run]);
  const rows = useMemo(() => buildRows(run), [run]);
  const counts = useMemo(() => filterCounts(rows), [rows]);

  const [filter, setFilter] = useState<RowFilter>("all");
  const [open, setOpen] = useState<ReadonlySet<string>>(() => new Set());

  const groups = useMemo(
    () => groupBySuite(rows.filter((row) => rowMatches(row, filter))),
    [rows, filter],
  );

  // "Expand all" acts on what is currently on screen, not the whole corpus.
  const expandableIds = useMemo(
    () => groups.flatMap((g) => g.rows).filter(hasDetail).map((r) => r.id),
    [groups],
  );
  const allOpen =
    expandableIds.length > 0 && expandableIds.every((id) => open.has(id));

  const toggleRow = (id: string) =>
    setOpen((prev) => {
      const next = new Set(prev);
      if (!next.delete(id)) next.add(id);
      return next;
    });

  const toggleAll = () =>
    setOpen((prev) => {
      const next = new Set(prev);
      for (const id of expandableIds) {
        if (allOpen) next.delete(id);
        else next.add(id);
      }
      return next;
    });

  const shown = groups.reduce((n, g) => n + g.rows.length, 0);

  return (
    <section className="card" aria-labelledby="corpus-heading">
      <div className="card-head">
        <h2 id="corpus-heading">Test corpus</h2>
        <span className="pill">
          {shown === counts.all ? `${counts.all} tests` : `${shown} of ${counts.all}`}
        </span>
      </div>

      <div className="table-controls">
        <div className="segmented" role="group" aria-label="Filter tests">
          {FILTERS.map(({ id, label }) => (
            <button
              key={id}
              type="button"
              className={id === filter ? "segment segment-on" : "segment"}
              aria-pressed={id === filter}
              onClick={() => setFilter(id)}
            >
              {label}
              <span className="segment-count">{counts[id]}</span>
            </button>
          ))}
        </div>

        {expandableIds.length > 0 && (
          <button type="button" className="ghost-button" onClick={toggleAll}>
            {allOpen ? "Collapse all" : `Expand all (${expandableIds.length})`}
          </button>
        )}
      </div>

      {shown === 0 ? (
        <p className="notice">{EMPTY[filter]}</p>
      ) : (
        <table className="matrix conformance">
          <caption className="sr-only">
            Every ACTS test for this run, by transport. Rows that did not pass can
            be expanded for the reason.
          </caption>
          <thead>
            <tr>
              <th scope="col">Test</th>
              {transports.map((transport) => (
                <th scope="col" key={transport}>
                  {TRANSPORT_LABELS[transport]}
                </th>
              ))}
            </tr>
          </thead>
          {groups.map((group) => (
            <tbody key={group.suite}>
              <tr className="suite-row">
                <th scope="colgroup" colSpan={transports.length + 1}>
                  {group.suite}
                  <span className="suite-count">
                    {group.rows.length} {group.rows.length === 1 ? "test" : "tests"}
                  </span>
                </th>
              </tr>
              {group.rows.map((row) => (
                <TestRow
                  key={row.id}
                  row={row}
                  transports={transports}
                  open={open.has(row.id)}
                  onToggle={() => toggleRow(row.id)}
                />
              ))}
            </tbody>
          ))}
        </table>
      )}
    </section>
  );
}
