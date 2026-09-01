import { BEHAVIORS, PROTOCOLS, cellStatus } from "../lib.ts";
import type { CellStatus, Scenario } from "../types.ts";

const PROTOCOL_LABELS: Record<string, string> = {
  jsonrpc: "JSON-RPC",
  grpc: "gRPC",
  http_json: "HTTP+JSON",
};

const CELL_TEXT: Record<CellStatus, string> = {
  pass: "Pass",
  fail: "Fail",
  mixed: "Mixed",
  none: "—",
};

interface Props {
  scenarios: Scenario[];
  /** Screen-reader caption; the visible heading lives on the enclosing card. */
  caption: string;
}

/** Behaviour x protocol grid for one topology. */
export default function Matrix({ scenarios, caption }: Props) {
  return (
    <div className="matrix-wrap">
      <table className="matrix">
        <caption className="sr-only">{caption}</caption>
        <thead>
          <tr>
            <th scope="col">Behaviour</th>
            {PROTOCOLS.map((protocol) => (
              <th scope="col" key={protocol}>
                {PROTOCOL_LABELS[protocol] ?? protocol}
              </th>
            ))}
          </tr>
        </thead>
        <tbody>
          {BEHAVIORS.map((behavior) => (
            <tr key={behavior.label}>
              <th scope="row">{behavior.label}</th>
              {PROTOCOLS.map((protocol) => {
                const status = cellStatus(scenarios, behavior, protocol);
                return (
                  <td key={protocol} className="cell">
                    {status === "none" ? (
                      <span className="cell-empty" aria-label="Not covered">
                        {CELL_TEXT.none}
                      </span>
                    ) : (
                      <span className={`chip chip-${status}`}>{CELL_TEXT[status]}</span>
                    )}
                  </td>
                );
              })}
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}
