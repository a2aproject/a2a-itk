# ACTS corpus provenance

The `*.acts.yaml` files in this directory are a **byte-identical mirror** of
`tests/acts/*.acts.yaml` in the A2A repository:

    a2aproject/A2A @ 7bae1d8f46c1d7e94e94ffe46b1237460111a4ec
    branch: conformance-spec-adjustments

- [#2227](https://github.com/a2aproject/A2A/pull/2227) — the commit above, which
  brings the corpus into line with the normative A2A sources.
- [#1882](https://github.com/a2aproject/A2A/pull/1882) — where the ACTS
  specification and this corpus originate. #2227 targets its branch.

Fifteen files, 111 tests, loaded strictly with no errors and no rewriting.

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
   SHA=7bae1d8
   for f in scenarios/acts/*.acts.yaml; do
     git -C ../A2A show "$SHA:tests/acts/$(basename "$f")" \
       | cmp -s - "$f" || echo "DRIFT: $f"
   done
   ```

   Check against the commit rather than against `../A2A/tests/acts/`: the two
   agree only while that working tree is clean, and a copy taken from an
   uncommitted tree pins nothing.

5. Update the SHA, the branch and the PR links above to what you copied from.
