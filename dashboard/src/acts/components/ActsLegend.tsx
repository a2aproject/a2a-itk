const OUTCOMES: [string, string, string][] = [
  ["pass", "✓", "the SDK met the assertion"],
  ["fail", "fail", "the SDK did something the specification forbids"],
  ["skip", "skip", "the harness could not run it; not evidence either way"],
  ["empty", "—", "out of scope: the test targets another transport"],
];

const LEVELS: [string, string][] = [
  ["must", "a failure makes the run non-conformant"],
  ["should", "recommended; a failure is a quality signal"],
  ["may", "optional behaviour"],
];

export default function ActsLegend() {
  return (
    <div className="legend-group">
      <dl className="legend" aria-label="Result legend">
        {OUTCOMES.map(([id, label, meaning]) => (
          <div key={id}>
            <dt>
              {id === "pass" || id === "empty" ? (
                <span className={id === "pass" ? "cell-pass" : "cell-empty"}>
                  {label}
                </span>
              ) : (
                <span className={`chip chip-${id}`}>{label}</span>
              )}
            </dt>
            <dd>{meaning}</dd>
          </div>
        ))}
      </dl>
      <dl className="legend" aria-label="Requirement level legend">
        {LEVELS.map(([id, meaning]) => (
          <div key={id}>
            <dt>
              <span className={`level level-${id}`}>{id}</span>
            </dt>
            <dd>{meaning}</dd>
          </div>
        ))}
      </dl>
    </div>
  );
}
