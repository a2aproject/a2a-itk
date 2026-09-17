#!/usr/bin/env bash
#
# Sync the rolling nightly metrics into public/, where Vite copies them
# verbatim into dist/ and the app fetches them from the site root.
#
# Both `npm run fetch-metrics` and .github/workflows/deploy_dashboard.yml call
# this, so local dev and the deployed site always render the same set of files.
#
# An SDK that publishes no metrics yet is not an error: the asset is replaced
# with an empty array, which the dashboard renders as "no data".

set -uo pipefail

base=https://github.com/a2aproject
public="$(cd "$(dirname "${BASH_SOURCE[0]}")/../public" && pwd)"
missing=0

# fetch <local name> <repo> <remote asset>
#
# Local names use the dashboard's SDK ids (SDKS in src/lib.ts), which differ
# from the publishing repo's own asset name for TypeScript: js -> ts.
fetch() {
  local out=$1 repo=$2 asset=$3 code
  curl -sL -f -o "$public/$out" \
    "$base/$repo/releases/download/nightly-metrics/$asset"
  code=$?
  if [ "$code" -eq 0 ]; then
    printf '  %-18s <- %s\n' "$out" "$repo/$asset"
    return
  fi
  echo "[]" > "$public/$out"
  missing=$((missing + 1))
  # 22 is curl's "server returned an HTTP error" under -f: the asset simply is
  # not published. Any other code is a real transport failure worth reporting.
  if [ "$code" -eq 22 ]; then
    printf '  %-18s -- not published yet, wrote []\n' "$out"
  else
    printf '  %-18s !! curl exit %s, wrote []\n' "$out" "$code"
  fi
}

echo "Interop (ITK) metrics:"
fetch itk_python.json a2a-python itk_python.json
fetch itk_go.json     a2a-go     itk_go.json
fetch itk_rust.json   a2a-rs     itk_rust.json
fetch itk_dotnet.json a2a-dotnet itk_dotnet.json
fetch itk_ts.json     a2a-js     itk_js.json
fetch itk_java.json   a2a-java   itk_java.json

echo "Conformance (ACTS) metrics:"
fetch acts_python.json a2a-python acts_python.json
fetch acts_go.json     a2a-go     acts_go.json
fetch acts_rust.json   a2a-rs     acts_rust.json
fetch acts_dotnet.json a2a-dotnet acts_dotnet.json
fetch acts_ts.json     a2a-js     acts_js.json
fetch acts_java.json   a2a-java   acts_java.json

echo "Done: 12 files in public/, $missing not published yet."
