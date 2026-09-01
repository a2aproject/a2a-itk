import { useMemo } from "react";
import Failures from "./Failures.tsx";
import Legend from "./Legend.tsx";
import Matrix from "./Matrix.tsx";
import Topology from "./Topology.tsx";
import {
  failedScenarios,
  formatSdkName,
  pairwiseGroups,
  runPassed,
  scenarioIncludesSdk,
  summaryTopology,
} from "../lib.ts";
import type { Run, SdkTarget } from "../types.ts";

interface Props {
  run: Run;
  sdk: SdkTarget;
}

/** Everything about one nightly run: summary star, pairwise grids, failures. */
export default function RunDetail({ run, sdk }: Props) {
  const scenarios = useMemo(() => run.scenarios ?? [], [run]);

  const { summary, pairs, failures, summaryScenarios } = useMemo(() => {
    const topology = summaryTopology(scenarios);
    return {
      summary: topology,
      pairs: pairwiseGroups(scenarios, topology.sdks),
      failures: failedScenarios(run),
      summaryScenarios: scenarios.filter((s) => scenarioIncludesSdk(s, sdk.id)),
    };
  }, [run, scenarios, sdk.id]);

  const ok = runPassed(run);
  const commitUrl = `https://github.com/a2aproject/${sdk.repo}/commit/${run.commit_sha}`;

  return (
    <div className="detail">
      <section className="card" aria-labelledby="summary-heading">
        <div className="card-head">
          <h2 id="summary-heading">Summary</h2>
          <span className={ok ? "status status-pass" : "status status-fail"}>
            {ok ? "All scenarios passed" : `${failures.length} failing`}
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
              <a className="mono" href={commitUrl}>
                {run.commit_sha.slice(0, 7)}
              </a>
            </dd>
          </div>
          <div>
            <dt>Scenarios</dt>
            <dd>{scenarios.length}</dd>
          </div>
        </dl>

        {scenarios.length === 0 ? (
          <p className="notice">No test scenarios were executed in this run.</p>
        ) : summary.sdks.length > 1 ? (
          <div className="split">
            <Topology sdks={summary.sdks} edges={summary.edges} />
            <Matrix
              scenarios={summaryScenarios}
              caption={`Aggregate results for ${sdk.label} against all peer SDKs`}
            />
          </div>
        ) : (
          <p className="notice">No peer SDKs were found in this run.</p>
        )}

        {failures.length > 0 && <Failures failures={failures} />}
      </section>

      <section aria-labelledby="pairwise-heading">
        <h2 id="pairwise-heading" className="section-heading">
          Pairwise interoperability
        </h2>
        {pairs.length === 0 ? (
          <p className="notice">No pairwise scenarios were executed in this run.</p>
        ) : (
          <div className="pair-grid">
            {pairs.map((pair) => {
              const names = pair.sdks.map(formatSdkName);
              return (
                <div className="card pair" key={pair.key}>
                  <h3>{names.join(" ↔ ")}</h3>
                  <Topology sdks={pair.sdks} edges={pair.edges} />
                  <Matrix
                    scenarios={pair.scenarios}
                    caption={`Results for ${names.join(" and ")}`}
                  />
                </div>
              );
            })}
          </div>
        )}
      </section>

      <Legend />
    </div>
  );
}
