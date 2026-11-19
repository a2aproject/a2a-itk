import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import test from "node:test";
import { fileURLToPath } from "node:url";
import {
  ACTS_VERSIONS,
  SDKS,
  actsFile,
  findSdk,
  isActsVersion,
} from "./sdks.ts";

const fetchScript = readFileSync(
  fileURLToPath(new URL("../../scripts/fetch-metrics.sh", import.meta.url)),
  "utf8",
);

/** `fetch <local name> <repo> <asset>` lines of fetch-metrics.sh. */
const fetched = [...fetchScript.matchAll(/^fetch\s+(\S+)\s+(\S+)\s+(\S+)\s*$/gm)].map(
  ([, local, repo, asset]) => ({ local, repo, asset }),
);

const python = findSdk("python")!;
const ts = findSdk("ts")!;

test("the 1.0 conformance file keeps the name it always had", () => {
  assert.equal(actsFile(python, "1.0"), "acts_python.json");
});

test("another version gets a suffix", () => {
  assert.equal(actsFile(python, "1.1"), "acts_python_v11.json");
  assert.equal(actsFile(ts, "1.1"), "acts_ts_v11.json");
});

test("only the versions the dashboard knows are accepted", () => {
  for (const version of ACTS_VERSIONS) assert.ok(isActsVersion(version));
  for (const value of ["1", "1.2", "v1.1", "", undefined]) {
    assert.equal(isActsVersion(value), false, String(value));
  }
});

test("every file the dashboard reads is fetched, from the repo of its SDK", () => {
  const wanted = SDKS.flatMap((sdk) => [
    { local: sdk.files.itk, repo: sdk.repo },
    ...ACTS_VERSIONS.map((version) => ({ local: actsFile(sdk, version), repo: sdk.repo })),
  ]);
  for (const { local, repo } of wanted) {
    const line = fetched.find((f) => f.local === local);
    assert.ok(line, `fetch-metrics.sh does not fetch ${local}`);
    assert.equal(line.repo, repo, `${local} comes from the wrong repo`);
  }
  assert.equal(fetched.length, wanted.length, "fetch-metrics.sh fetches a file no SDK reads");
});

test("a conformance asset at another version is named as the driver publishes it", () => {
  // The driver appends the version suffix to the SDK's own metrics name,
  // which is `js` for the SDK this dashboard calls `ts`.
  for (const sdk of SDKS) {
    const at10 = fetched.find((f) => f.local === actsFile(sdk, "1.0"))!;
    const at11 = fetched.find((f) => f.local === actsFile(sdk, "1.1"))!;
    assert.equal(at11.asset, at10.asset.replace(/\.json$/, "_v11.json"), sdk.id);
  }
});
