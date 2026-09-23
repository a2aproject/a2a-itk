import { NavLink } from "react-router-dom";
import { sdkPath } from "../../routes.tsx";
import { SDKS } from "../sdks.ts";
import type { Domain } from "../types.ts";

/** Tab bar; NavLink supplies the active state straight from the route. */
export default function SdkTabs({
  domain,
  active,
}: {
  domain: Domain;
  active: string;
}) {
  return (
    <nav className="tabs" aria-label="Select SDK">
      {SDKS.map((sdk) => (
        <NavLink
          key={sdk.id}
          to={sdkPath(domain, sdk.id)}
          className={({ isActive }) => (isActive ? "tab tab-active" : "tab")}
          aria-current={sdk.id === active ? "page" : undefined}
        >
          {sdk.label}
        </NavLink>
      ))}
    </nav>
  );
}
