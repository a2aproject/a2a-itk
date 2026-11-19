import { NavLink } from "react-router-dom";
import { sdkPath } from "../../routes.tsx";
import { SDKS } from "../sdks.ts";
import type { Domain } from "../types.ts";

/**
 * Tab bar. `pathFor` gives the link for each SDK, so a view can keep what else
 * it shows, such as an A2A version, when the SDK changes.
 */
export default function SdkTabs({
  domain,
  active,
  pathFor = (sdkId) => sdkPath(domain, sdkId),
}: {
  domain: Domain;
  active: string;
  pathFor?: (sdkId: string) => string;
}) {
  return (
    <nav className="tabs" aria-label="Select SDK">
      {SDKS.map((sdk) => (
        <NavLink
          key={sdk.id}
          to={pathFor(sdk.id)}
          className={() => (sdk.id === active ? "tab tab-active" : "tab")}
          aria-current={sdk.id === active ? "page" : undefined}
        >
          {sdk.label}
        </NavLink>
      ))}
    </nav>
  );
}
