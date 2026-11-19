import { Link } from "react-router-dom";
import { actsPath } from "../../routes.tsx";
import { ACTS_VERSIONS } from "../../shared/sdks.ts";
import type { ActsVersion } from "../../shared/sdks.ts";

/**
 * Which A2A version's conformance runs are shown. Each version is a run of
 * its own, published to a file of its own, so this switches the data and not
 * a filter on it.
 */
export default function VersionTabs({
  sdkId,
  active,
}: {
  sdkId: string;
  active: ActsVersion;
}) {
  return (
    <nav className="segmented version-tabs" aria-label="A2A version">
      {ACTS_VERSIONS.map((version) => (
        // Not a NavLink: the 1.0 path is a prefix of the 1.1 path, so a
        // NavLink would mark 1.0 as the current page on both.
        <Link
          key={version}
          to={actsPath(sdkId, version)}
          className={version === active ? "segment segment-on" : "segment"}
          aria-current={version === active ? "page" : undefined}
        >
          A2A {version}
        </Link>
      ))}
    </nav>
  );
}
