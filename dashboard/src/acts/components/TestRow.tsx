import Deviation from "./Deviation.tsx";
import ResultCell from "./ResultCell.tsx";
import { deviationsOf, hasDetail } from "../lib.ts";
import type { TestRow as Row, Transport } from "../types.ts";

interface Props {
  row: Row;
  transports: Transport[];
  open: boolean;
  onToggle: () => void;
}

/**
 * One test across every transport, plus the disclosure row holding its
 * reasons. Rows that passed everywhere have nothing to disclose, so they get
 * plain text instead of a button and no second row at all.
 */
export default function TestRow({ row, transports, open, onToggle }: Props) {
  const expandable = hasDetail(row);
  const detailId = `detail-${row.id}`;

  const label = (
    <>
      <code className="mono test-id">{row.id}</code>
      <span className={`level level-${row.level}`}>{row.level}</span>
    </>
  );

  return (
    <>
      <tr className={open ? "test-row test-row-open" : "test-row"}>
        <th scope="row">
          {expandable ? (
            <button
              type="button"
              className="row-toggle"
              aria-expanded={open}
              aria-controls={detailId}
              onClick={onToggle}
            >
              <span className="chevron" aria-hidden="true" />
              {label}
              <span className="sr-only">, show why it did not pass</span>
            </button>
          ) : (
            <span className="row-static">{label}</span>
          )}
        </th>
        {transports.map((transport) => (
          <td key={transport} className="cell">
            <ResultCell test={row.cells[transport]} />
          </td>
        ))}
      </tr>

      {expandable && (
        <tr className="detail-row" id={detailId} hidden={!open}>
          <td colSpan={transports.length + 1}>
            <div className="detail-body">
              {deviationsOf(row, transports).map((deviation) => (
                <Deviation key={deviation.transports.join()} deviation={deviation} />
              ))}
            </div>
          </td>
        </tr>
      )}
    </>
  );
}
