import { Link } from "react-router-dom";
import { TRANSPORT_LABELS } from "../../acts/lib.ts";
import { sdkPath } from "../../routes.tsx";
import { commitUrl } from "../../shared/sdks.ts";
import { ACTS_TRANSPORTS, ITK_PROTOCOLS, ITK_PROTOCOL_LABELS } from "../lib.ts";
import type { SdkOverview } from "../types.ts";

function latestRunMeta(row: SdkOverview): { sha: string; timestamp: string } | undefined {
  const candidates = [row.itk, row.acts].filter(
    (r): r is NonNullable<typeof r> => r !== undefined,
  );
  if (candidates.length === 0) return undefined;
  candidates.sort((a, b) => Date.parse(b.timestamp) - Date.parse(a.timestamp));
  return { sha: candidates[0].commit_sha, timestamp: candidates[0].timestamp };
}

export default function SummaryTable({ rows }: { rows: SdkOverview[] }) {
  const interopPassing = rows.filter((r) => r.itk?.allPassed).length;
  const conformantCount = rows.filter((r) => r.acts?.conformant).length;

  return (
    <section className="card" aria-labelledby="summary-overview-heading">
      <div className="card-head">
        <h2 id="summary-overview-heading">SDK compatibility overview</h2>
        <div className="summary-pills">
          <span className="pill">
            {interopPassing}/{rows.length} interop passing
          </span>
          <span className="pill">
            {conformantCount}/{rows.length} conformant
          </span>
        </div>
      </div>

      <div className="matrix-wrap">
        <table className="matrix summary-table">
          <caption className="sr-only">
            Latest interoperability and conformance state for every A2A SDK
          </caption>
          <thead>
            <tr className="summary-group-row">
              <th scope="col" rowSpan={2}>
                SDK
              </th>
              <th scope="col" rowSpan={2}>
                Latest Run
              </th>
              <th scope="colgroup" colSpan={1 + ITK_PROTOCOLS.length} className="group-col">
                Interoperability (ITK)
              </th>
              <th scope="colgroup" colSpan={2 + ACTS_TRANSPORTS.length} className="group-col">
                Conformance (ACTS)
              </th>
            </tr>
            <tr>
              <th scope="col" className="group-start">
                Status
              </th>
              {ITK_PROTOCOLS.map((protocol) => (
                <th scope="col" key={`itk-${protocol}`}>
                  {ITK_PROTOCOL_LABELS[protocol]}
                </th>
              ))}
              <th scope="col" className="group-start">
                Status
              </th>
              <th scope="col" title="Specification MUST requirements">
                MUST
              </th>
              {ACTS_TRANSPORTS.map((transport) => (
                <th scope="col" key={`acts-${transport}`}>
                  {TRANSPORT_LABELS[transport]}
                </th>
              ))}
            </tr>
          </thead>
          <tbody>
            {rows.map((row) => {
              const { sdk, itk, acts } = row;
              const meta = latestRunMeta(row);
              const itkHref = sdkPath("itk", sdk.id);
              const actsHref = sdkPath("acts", sdk.id);

              return (
                <tr key={sdk.id} className="summary-row">
                  <th scope="row">
                    <div className="summary-sdk">
                      <span className="summary-sdk-name">{sdk.label}</span>
                      <a
                        className="summary-sdk-repo mono"
                        href={`https://github.com/a2aproject/${sdk.repo}`}
                      >
                        {sdk.repo}
                      </a>
                    </div>
                  </th>

                  <td>
                    {meta ? (
                      <div className="summary-meta">
                        <a className="mono" href={commitUrl(sdk, meta.sha)}>
                          {meta.sha.slice(0, 7)}
                        </a>
                        <time className="summary-date" dateTime={meta.timestamp}>
                          {new Date(meta.timestamp).toLocaleDateString(undefined, {
                            month: "short",
                            day: "numeric",
                          })}
                        </time>
                      </div>
                    ) : (
                      <span className="cell-empty">—</span>
                    )}
                  </td>

                  {/* Interoperability (ITK) */}
                  <td className="group-start">
                    {itk ? (
                      <Link
                        to={itkHref}
                        className="summary-cell-link"
                        aria-label={`${sdk.label} interoperability details: ${itk.passed} of ${itk.total} scenarios passed`}
                      >
                        <span
                          className={
                            itk.allPassed ? "chip chip-pass" : "chip chip-fail"
                          }
                        >
                          {itk.allPassed ? "Pass" : "Fail"}
                        </span>
                        <span className="summary-fraction">
                          {itk.passed}/{itk.total}
                        </span>
                      </Link>
                    ) : (
                      <span className="cell-empty">—</span>
                    )}
                  </td>

                  {ITK_PROTOCOLS.map((protocol) => {
                    const p = itk?.byProtocol[protocol];
                    return (
                      <td key={`itk-${protocol}`} className="summary-num-cell">
                        {!p || p.status === "none" ? (
                          <span className="cell-empty">—</span>
                        ) : (
                          <Link to={itkHref} className="summary-metric-link">
                            {p.status === "pass" ? (
                              <>
                                <span className="cell-pass" aria-hidden="true">
                                  ✓{" "}
                                </span>
                                <span className="summary-num">
                                  {p.passed}/{p.total}
                                </span>
                              </>
                            ) : (
                              <span className="count-fail">
                                <span aria-hidden="true">✗ </span>
                                {p.passed}/{p.total}
                              </span>
                            )}
                          </Link>
                        )}
                      </td>
                    );
                  })}

                  {/* Conformance (ACTS) */}
                  <td className="group-start">
                    {acts ? (
                      <Link
                        to={actsHref}
                        className="summary-cell-link"
                        aria-label={`${sdk.label} conformance details: ${acts.passed} of ${acts.total} assertions passed`}
                      >
                        <span
                          className={
                            acts.conformant ? "chip chip-pass" : "chip chip-fail"
                          }
                        >
                          {acts.conformant ? "Conformant" : "Not conformant"}
                        </span>
                        <span className="summary-fraction">
                          {acts.passed}/{acts.total}
                        </span>
                      </Link>
                    ) : (
                      <span className="cell-empty">—</span>
                    )}
                  </td>

                  <td className="summary-num-cell">
                    {acts ? (
                      <Link to={actsHref} className="summary-metric-link">
                        {acts.mustPassed === acts.mustTotal ? (
                          <>
                            <span className="cell-pass" aria-hidden="true">
                              ✓{" "}
                            </span>
                            <span className="summary-num">
                              {acts.mustPassed}/{acts.mustTotal}
                            </span>
                          </>
                        ) : (
                          <span className="count-fail">
                            <span aria-hidden="true">✗ </span>
                            {acts.mustPassed}/{acts.mustTotal}
                          </span>
                        )}
                      </Link>
                    ) : (
                      <span className="cell-empty">—</span>
                    )}
                  </td>

                  {ACTS_TRANSPORTS.map((transport) => {
                    const t = acts?.byTransport[transport];
                    return (
                      <td key={`acts-${transport}`} className="summary-num-cell">
                        {!t ? (
                          <span className="cell-empty">—</span>
                        ) : (
                          <Link to={actsHref} className="summary-metric-link">
                            {t.conformant ? (
                              <>
                                <span className="cell-pass" aria-hidden="true">
                                  ✓{" "}
                                </span>
                                <span className="summary-num">
                                  {t.passed}/{t.total}
                                </span>
                              </>
                            ) : (
                              <span className="count-fail">
                                <span aria-hidden="true">✗ </span>
                                {t.passed}/{t.total}
                              </span>
                            )}
                          </Link>
                        )}
                      </td>
                    );
                  })}
                </tr>
              );
            })}
          </tbody>
        </table>
      </div>

      <p className="muted footnote">
        Select any <strong>Interoperability</strong> or <strong>Conformance</strong>{" "}
        result to open that SDK&apos;s detailed report and run history. Conformance is
        decided by the <strong>MUST</strong> requirement level across all transports;
        total assertion counts also include <strong>SHOULD</strong> and{" "}
        <strong>MAY</strong> checks.
      </p>
    </section>
  );
}
