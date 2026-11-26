# ACTS corpus provenance

The `*.acts.yaml` files in this directory are a **byte-identical mirror** of
`tests/acts/*.acts.yaml` in the A2A repository:

    a2aproject/A2A @ 3252da724681a50daa15b0f7c414564974b1aa06
    branch: conformance-spec-1.1

- [#2468](https://github.com/a2aproject/A2A/pull/2468) — the commit above, which
  adds the tests for A2A 1.1 and the version range a test may declare. It
  targets the branch of #2227.
- [#2227](https://github.com/a2aproject/A2A/pull/2227) — brings the corpus into
  line with the normative A2A sources. It targets the branch of #1882.
- [#1882](https://github.com/a2aproject/A2A/pull/1882) — where the ACTS
  specification and this corpus originate.

None of the three is merged, so the specification and the tests can still
change under review.

Nineteen files, 173 tests, loaded strictly with no errors and no rewriting.

One corpus serves A2A 1.0 and A2A 1.1. A run at 1.0 takes 113 tests, the same
test ids as the last pin that had only tests for 1.0; a run at 1.1 takes 172.
What changed in the 113: a raw step that wrote `A2A-Version: "1.0"` now writes
the run version as `{{specVersion}}`, `REST-SUB-POST-001` ends at 1.0,
`PUSH-DELIV-003` accepts an artifact update as a delivered payload, and two
names and one description were reworded.

## Refreshing

The corpus is authored and reviewed in A2A, so changes go **upstream first** and
arrive here as a copy.

1. Make the change in `A2A/tests/acts/` and commit it.
2. Copy the files across. Copy, do not merge — a three-way merge between two
   copies of the same file is how they drift.

   ```bash
   cp ../A2A/tests/acts/*.acts.yaml scenarios/acts/
   ```

3. Run `uv run pytest tests/test_acts_corpus.py`. It pins the shape of the
   corpus, so anything that moved shows up as a named failure rather than as
   silent drift.
4. Verify against the commit, from the repo root:

   ```bash
   SHA=3252da724681a50daa15b0f7c414564974b1aa06
   for f in scenarios/acts/*.acts.yaml; do
     git -C ../A2A show "$SHA:tests/acts/$(basename "$f")" \
       | cmp -s - "$f" || echo "DRIFT: $f"
   done
   ```

   Check against the commit rather than against `../A2A/tests/acts/`: the two
   agree only while that working tree is clean, and a copy taken from an
   uncommitted tree pins nothing.

5. Update the SHA, the branch and the PR links above to what you copied from.
