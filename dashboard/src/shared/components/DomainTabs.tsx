import { NavLink } from "react-router-dom";
import { sdkPath } from "../../routes.tsx";
import type { Domain } from "../types.ts";

const SUITES: { domain: Domain; label: string; blurb: string }[] = [
  {
    domain: "itk",
    label: "Interoperability",
    blurb: "Every SDK against its peers",
  },
  {
    domain: "acts",
    label: "Conformance",
    blurb: "Each SDK against the specification",
  },
];

/**
 * Suite switch. Keeps the current SDK when moving between suites, so the
 * question changes but the subject does not.
 */
export default function DomainTabs({
  active,
  sdkId,
}: {
  active: Domain;
  sdkId: string;
}) {
  return (
    <nav className="suites" aria-label="Test suite">
      {SUITES.map(({ domain, label, blurb }) => (
        <NavLink
          key={domain}
          to={sdkPath(domain, sdkId)}
          className={({ isActive }) => (isActive ? "suite suite-active" : "suite")}
          aria-current={domain === active ? "page" : undefined}
        >
          <span className="suite-label">{label}</span>
          <span className="suite-blurb">{blurb}</span>
        </NavLink>
      ))}
    </nav>
  );
}
