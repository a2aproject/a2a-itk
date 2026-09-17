// Node-link layout geometry for the topology diagrams. Pure maths, no DOM.

const BASE_HEIGHT = 300;
/** Vertical distance the peers spread over at the classic 300-unit height. */
const SPREAD = 200;
/** Smallest gap allowed between two peer pills before the canvas grows. */
const MIN_GAP = 16;
/** Breathing room kept above the first peer and below the last. */
const PADDING = 36;
const ARROW_LEN = 9;
const ARROW_HALF_W = 4.5;

interface Point {
  x: number;
  y: number;
}

interface Size {
  w: number;
  h: number;
}

export interface LayoutNode {
  sdk: string;
  index: number;
  pos: Point;
  root: boolean;
}

export interface LayoutLine {
  key: string;
  from: number;
  to: number;
  start: Point;
  end: Point;
  bidirectional: boolean;
}

export interface Layout {
  nodes: LayoutNode[];
  lines: LayoutLine[];
  /** Arrowheads as ready-to-render SVG `points` strings. */
  arrows: string[];
  box: Size;
  viewBox: string;
}

/**
 * Where an edge meets `from`'s pill: the midpoint of the face turned towards
 * `to`.
 *
 * A true ray/rectangle intersection would leave through whichever face the
 * ray happens to hit — for the steep angles in a wide star that is the pill's
 * top or bottom edge, which drops the line straight onto the neighbouring
 * pill. Since the layout always runs root-left to peers-right, anchoring on
 * the vertical face keeps every edge inside the empty column between them.
 */
function attachPoint(from: Point, to: Point, half: Size): Point {
  const dx = to.x - from.x;
  if (dx === 0) {
    const dy = to.y - from.y;
    return { x: from.x, y: from.y + Math.sign(dy) * half.h };
  }
  return { x: from.x + Math.sign(dx) * half.w, y: from.y };
}

/** Triangle whose tip sits at (x,y) pointing along the unit vector (vx,vy). */
function arrowhead(x: number, y: number, vx: number, vy: number): string {
  const bx = x - ARROW_LEN * vx;
  const by = y - ARROW_LEN * vy;
  return [
    [x, y],
    [bx + ARROW_HALF_W * vy, by - ARROW_HALF_W * vx],
    [bx - ARROW_HALF_W * vy, by + ARROW_HALF_W * vx],
  ]
    .map(([px, py]) => `${px},${py}`)
    .join(" ");
}

/**
 * Lay out `sdks` as a star: the `current` root on the left, peers stacked down
 * the right. Returns everything the SVG needs, already positioned.
 */
export function layoutTopology(sdks: string[], edges: string[]): Layout {
  const pairwise = sdks.length === 2;
  const box: Size = pairwise ? { w: 140, h: 34 } : { w: 110, h: 28 };
  const half: Size = { w: box.w / 2, h: box.h / 2 };

  let rootIdx = sdks.findIndex((s) => (s || "").toLowerCase() === "current");
  if (rootIdx === -1) rootIdx = 0;

  // Peers stack down the right. Small topologies keep the roomy classic
  // spread; past ~6 peers `SPREAD / (n - 1)` drops below the pill height, so
  // the canvas grows instead of letting the pills touch.
  const children = sdks.map((_, i) => i).filter((i) => i !== rootIdx);
  const step =
    children.length > 1
      ? Math.max(SPREAD / (children.length - 1), box.h + MIN_GAP)
      : 0;
  const span = step * Math.max(0, children.length - 1);
  const height = Math.max(BASE_HEIGHT, span + PADDING * 2);

  const positions: Record<number, Point> = { [rootIdx]: { x: 80, y: height / 2 } };
  children.forEach((idx, i) => {
    positions[idx] = { x: 320, y: height / 2 - span / 2 + i * step };
  });

  const lines: LayoutLine[] = [];
  const arrows: string[] = [];
  const drawn = new Set<string>();

  for (const edge of edges) {
    const [from, to] = edge.split("->").map((n) => Number.parseInt(n, 10));
    if (Number.isNaN(from) || Number.isNaN(to) || from === to) continue;

    const key = `${Math.min(from, to)}-${Math.max(from, to)}`;
    if (drawn.has(key)) continue;
    drawn.add(key);

    const a = positions[from];
    const b = positions[to];
    if (!a || !b) continue;

    const start = attachPoint(a, b, half);
    const end = attachPoint(b, a, half);
    const bidirectional = edges.includes(`${to}->${from}`);
    lines.push({ key, from, to, start, end, bidirectional });

    const dx = end.x - start.x;
    const dy = end.y - start.y;
    const len = Math.hypot(dx, dy);
    if (len <= 20) continue;

    const ux = dx / len;
    const uy = dy / len;
    const at = (f: number): Point => ({ x: start.x + f * dx, y: start.y + f * dy });

    if (bidirectional) {
      // One arrowhead per direction, parked clear of both node pills. Every
      // star edge shares the root's attach point, so heads placed too near it
      // overlap each other; 0.35/0.65 keeps a wide fan legible and still reads
      // symmetrically on a two-node card.
      const fwd = at(0.65);
      const rev = at(0.35);
      arrows.push(
        arrowhead(fwd.x + (ARROW_LEN / 2) * ux, fwd.y + (ARROW_LEN / 2) * uy, ux, uy),
        arrowhead(rev.x - (ARROW_LEN / 2) * ux, rev.y - (ARROW_LEN / 2) * uy, -ux, -uy),
      );
    } else {
      const mid = at(0.5);
      arrows.push(
        arrowhead(mid.x + (ARROW_LEN / 2) * ux, mid.y + (ARROW_LEN / 2) * uy, ux, uy),
      );
    }
  }

  const nodes: LayoutNode[] = sdks
    .map((sdk, index) => ({ sdk, index, pos: positions[index], root: index === rootIdx }))
    .filter((n) => n.pos);

  return {
    nodes,
    lines,
    arrows,
    box,
    // Pairwise cards only need the middle band of the canvas.
    viewBox: pairwise ? "0 105 400 90" : `0 0 400 ${height}`,
  };
}
