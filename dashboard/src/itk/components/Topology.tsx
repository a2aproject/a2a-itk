import { formatSdkName, sdkKind } from "../lib.ts";
import { layoutTopology } from "../topology.ts";

interface Props {
  sdks: string[];
  edges: string[];
}

/** Node-link diagram of one scenario topology. */
export default function Topology({ sdks, edges }: Props) {
  const { nodes, lines, arrows, box, viewBox } = layoutTopology(sdks, edges);

  return (
    <svg
      className="topology"
      viewBox={viewBox}
      role="img"
      aria-label={`Topology: ${sdks.map(formatSdkName).join(" and ")}`}
    >
      {lines.map(({ key, start, end, bidirectional }) => (
        <line
          key={key}
          className={bidirectional ? "edge edge-bi" : "edge"}
          x1={start.x}
          y1={start.y}
          x2={end.x}
          y2={end.y}
        />
      ))}
      {arrows.map((points) => (
        <polygon key={points} className="arrow" points={points} />
      ))}
      {nodes.map(({ sdk, index, pos, root }) => (
        <g key={index} className={`node node-${sdkKind(sdk)}${root ? " node-root" : ""}`}>
          <rect
            x={pos.x - box.w / 2}
            y={pos.y - box.h / 2}
            width={box.w}
            height={box.h}
            rx="6"
          />
          <text x={pos.x} y={pos.y} fontSize={sdks.length === 2 ? 14 : 11}>
            {formatSdkName(sdk)}
          </text>
        </g>
      ))}
    </svg>
  );
}
