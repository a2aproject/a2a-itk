import { useEffect } from "react";
import { Link, useLocation } from "react-router-dom";
import { SDKS } from "../lib.ts";
import { sdkPath } from "../routes.tsx";

export default function NotFound() {
  const { pathname } = useLocation();

  useEffect(() => {
    document.title = "Not found — A2A Integration Test Kit";
  }, []);

  return (
    <section className="not-found">
      <h2>No such page</h2>
      <p className="muted">
        Nothing is routed at <code className="mono">#{pathname}</code>. Pick an SDK:
      </p>
      <ul className="not-found-links">
        {SDKS.map((sdk) => (
          <li key={sdk.id}>
            <Link to={sdkPath(sdk.id)}>{sdk.label}</Link>
          </li>
        ))}
      </ul>
    </section>
  );
}
