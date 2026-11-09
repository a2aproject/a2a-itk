import { NavLink } from "react-router-dom";
import { DEFAULT_SDK, SUMMARY_PATH, sdkPath } from "../../routes.tsx";
import type { Domain } from "../types.ts";

export type SuiteTab = "summary" | Domain;

const SUITES: {
  id: SuiteTab;
  label: string;
  blurb: string;
  to: (sdkId: string) => string;
}[] = [
  {
    id: "summary",
    label: "Summary",
    blurb: "All SDKs across both suites",
    to: () => SUMMARY_PATH,
  },
  {
    id: "itk",
    label: "Interoperability",
    blurb: "Every SDK against its peers",
    to: (sdkId) => sdkPath("itk", sdkId),
  },
  {
    id: "acts",
    label: "Conformance",
    blurb: "Each SDK against the specification",
    to: (sdkId) => sdkPath("acts", sdkId),
  },
];

/**
 * Suite switch. Keeps the current SDK when moving between per-SDK suites, so
 * the question changes but the subject does not.
 */
export default function DomainTabs({
  active,
  sdkId = DEFAULT_SDK,
}: {
  active: SuiteTab;
  sdkId?: string;
}) {
  return (
    <nav className="suites" aria-label="Test suite">
      {SUITES.map(({ id, label, blurb, to }) => (
        <NavLink
          key={id}
          to={to(sdkId)}
          className={() => (id === active ? "suite suite-active" : "suite")}
          aria-current={id === active ? "page" : undefined}
        >
          <span className="suite-label">{label}</span>
          <span className="suite-blurb">{blurb}</span>
        </NavLink>
      ))}
    </nav>
  );
}
