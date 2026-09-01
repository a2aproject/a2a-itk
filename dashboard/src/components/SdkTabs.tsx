import { NavLink } from "react-router-dom";
import { SDKS } from "../lib.ts";
import { sdkPath } from "../routes.tsx";

/** Tab bar; NavLink supplies the active state straight from the route. */
export default function SdkTabs({ active }: { active: string }) {
  return (
    <nav className="tabs" aria-label="Select SDK">
      {SDKS.map((sdk) => (
        <NavLink
          key={sdk.id}
          to={sdkPath(sdk.id)}
          className={({ isActive }) => (isActive ? "tab tab-active" : "tab")}
          aria-current={sdk.id === active ? "page" : undefined}
        >
          {sdk.label}
        </NavLink>
      ))}
    </nav>
  );
}
