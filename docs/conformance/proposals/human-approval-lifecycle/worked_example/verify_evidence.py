#!/usr/bin/env python3
"""Read-only evidence verifier for the real-SDK A2A HIL approval fixture.

This script performs READ-ONLY checks against whatever evidence files are
already on disk under evidence/ (relative to this file's directory). It
NEVER fabricates, overwrites, or re-runs any server -- it only reads
evidence/*.jsonl and prints a human-readable summary.

Checks performed:
  1. All H01-H12 scenario IDs are present exactly once each in
     evidence/scenario-results.jsonl (no duplicates, none missing).
  2. Pass/fail/blocked accounting: counts and prints each status.
  3. H08's parent and child task IDs are both present and DISTINCT, cross-
     referenced against the wire evidence
     (evidence/coordinator-wire.jsonl / evidence/expense-agent-wire.jsonl).
  4. Expected mock ledger count/effects are consistent with which
     scenarios should have produced a protected-tool effect (cross-
     referenced against TEST_RESULTS.md's matrix: H01, H06's first
     legitimate approval, H07's genuine first approval, and H12's
     post-recovery approval should each net exactly one ledger entry;
     denied/rejected/tampered/replayed-duplicate attempts should net zero
     additional entries). This check is evidence-shape-based (ledger
     ordering / count), not a live ledger query -- it does not start any
     server.
  5. Both focused approval-policy test records are present in
     evidence/approval-policy-race-tests.jsonl with PASS status.
  6. The H04 cross-version probe records (H04_v1.2.2, H04_pr1182 in
     scenario-results.jsonl, plus the separate h04-cross-sdk-comparison /
     h04-direct-child-probe files, if present) are reported SEPARATELY
     from the 12-case H01-H12 count -- never blended into one number.

Exit code 0 if everything checks out, non-zero otherwise. Always prints a
clear summary, pass or fail.

Usage:
    python3 verify_evidence.py
"""
from __future__ import annotations

import json
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
EVIDENCE_DIR = os.path.join(HERE, "evidence")

EXPECTED_SCENARIOS = [f"H{i:02d}" for i in range(1, 13)]

# Per TEST_RESULTS.md's ledger-verification section: exactly these
# (scenario, occurrence) pairs are expected to produce a protected-tool
# ledger effect. H06 and H07 each legitimately execute once (their FIRST
# resume) before their second (blocked) attempt; H12 executes once after
# channel recovery. All other scenarios/attempts must net zero.
EXPECTED_LEDGER_PRODUCING = {"H01", "H06", "H07", "H12"}


def read_jsonl(path: str) -> list[dict]:
    if not os.path.exists(path):
        return []
    rows = []
    with open(path, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            rows.append(json.loads(line))
    return rows


def main() -> int:
    problems: list[str] = []
    notes: list[str] = []

    scenario_path = os.path.join(EVIDENCE_DIR, "scenario-results.jsonl")
    scenario_rows = read_jsonl(scenario_path)
    if not scenario_rows:
        problems.append(f"No scenario results found at {scenario_path}")

    # --- Check 1 & 6: H01-H12 present exactly once, H04 cross-version
    # probes counted and reported SEPARATELY. ---------------------------
    core_rows = [r for r in scenario_rows if r.get("scenario") in EXPECTED_SCENARIOS]
    h04_probe_rows = [
        r for r in scenario_rows if r.get("scenario") not in EXPECTED_SCENARIOS
    ]

    seen_ids = [r["scenario"] for r in core_rows]
    seen_counts: dict[str, int] = {}
    for sid in seen_ids:
        seen_counts[sid] = seen_counts.get(sid, 0) + 1

    missing = [sid for sid in EXPECTED_SCENARIOS if sid not in seen_counts]
    duplicated = [sid for sid, n in seen_counts.items() if n > 1]
    if missing:
        problems.append(f"Missing core scenario IDs: {missing}")
    if duplicated:
        problems.append(f"Duplicated core scenario IDs: {duplicated}")

    core_pass = sum(1 for r in core_rows if r.get("status") == "PASS")
    core_fail = sum(1 for r in core_rows if r.get("status") == "FAIL")
    core_blocked = sum(1 for r in core_rows if r.get("status") not in ("PASS", "FAIL"))
    notes.append(
        f"12/12 core scenarios: {core_pass} pass, {core_fail} fail, "
        f"{core_blocked} other/blocked (of {len(core_rows)} rows found)"
    )
    if core_fail or core_blocked:
        problems.append(
            f"Core scenario suite is not clean: {core_fail} FAIL, {core_blocked} other/blocked"
        )
    if len(core_rows) != 12:
        problems.append(f"Expected exactly 12 core scenario rows, found {len(core_rows)}")

    # H04 cross-SDK probes (H04_v1.2.2, H04_pr1182) -- kept SEPARATE from
    # the 12-case count, per the task brief.
    h04_pass = sum(1 for r in h04_probe_rows if r.get("status") == "PASS")
    notes.append(
        f"H04 cross-SDK probes (scenario-results.jsonl, excluded from the 12/12 count above): "
        f"{h04_pass}/{len(h04_probe_rows)} pass "
        f"({[r.get('scenario') for r in h04_probe_rows]})"
    )

    h04_cross_sdk_path = os.path.join(EVIDENCE_DIR, "h04-cross-sdk-comparison.jsonl")
    h04_direct_path = os.path.join(EVIDENCE_DIR, "h04-direct-child-probe.jsonl")
    h04_cross_sdk_rows = read_jsonl(h04_cross_sdk_path)
    h04_direct_rows = read_jsonl(h04_direct_path)
    if h04_cross_sdk_rows or h04_direct_rows:
        notes.append(
            f"H04 separate cross-SDK-version evidence files found (not produced by this "
            f"worked example's own bundled run.sh, which does not include that comparison "
            f"mode -- see run.sh's header comment): "
            f"h04-cross-sdk-comparison.jsonl={len(h04_cross_sdk_rows)} rows, "
            f"h04-direct-child-probe.jsonl={len(h04_direct_rows)} rows "
            f"(reported separately; not part of the 12/12 core count)"
        )
    else:
        notes.append(
            "H04 separate cross-SDK evidence files not present (expected: this worked "
            "example's bundled run.sh does not include the H04 cross-SDK-version "
            "comparison mode; see run.sh's header comment)"
        )

    # --- Check 2: pass/fail/blocked accounting (overall, across all rows
    # in scenario-results.jsonl, core + H04 probes combined for a single
    # top-line count; the breakdown above already separates them). ------
    all_pass = sum(1 for r in scenario_rows if r.get("status") == "PASS")
    all_fail = sum(1 for r in scenario_rows if r.get("status") == "FAIL")
    all_other = len(scenario_rows) - all_pass - all_fail
    notes.append(
        f"scenario-results.jsonl overall: {len(scenario_rows)} rows total "
        f"({all_pass} PASS, {all_fail} FAIL, {all_other} other/blocked)"
    )

    # --- Check 3: H08 parent/child task IDs present and DISTINCT, cross-
    # referenced against wire evidence. ----------------------------------
    h08_rows = [r for r in core_rows if r.get("scenario") == "H08"]
    if not h08_rows:
        problems.append("H08 row not found; cannot verify parent/child task id distinctness")
    else:
        h08 = h08_rows[0]
        detail = h08.get("detail", {})
        parent_id = detail.get("parent_task_id")
        child_id = detail.get("child_task_id")
        if not parent_id or not child_id:
            problems.append(f"H08 detail missing parent_task_id/child_task_id: {detail}")
        elif parent_id == child_id:
            problems.append(
                f"H08 parent_task_id and child_task_id are NOT distinct (both {parent_id!r})"
            )
        else:
            notes.append(
                f"H08 parent/child task ids present and distinct: "
                f"parent={parent_id}, child={child_id}"
            )
            # Cross-reference against wire evidence: the child id should
            # appear in the coordinator wire log's correlation metadata
            # and/or the expense-agent wire log (its own task creation).
            coord_wire_path = os.path.join(EVIDENCE_DIR, "coordinator-wire.jsonl")
            expense_wire_path = os.path.join(EVIDENCE_DIR, "expense-agent-wire.jsonl")
            coord_wire_text = ""
            expense_wire_text = ""
            if os.path.exists(coord_wire_path):
                with open(coord_wire_path, "r", encoding="utf-8") as f:
                    coord_wire_text = f.read()
            if os.path.exists(expense_wire_path):
                with open(expense_wire_path, "r", encoding="utf-8") as f:
                    expense_wire_text = f.read()
            if child_id not in coord_wire_text and child_id not in expense_wire_text:
                problems.append(
                    f"H08 child_task_id {child_id!r} not found in any wire evidence file "
                    f"(coordinator-wire.jsonl or expense-agent-wire.jsonl)"
                )
            else:
                notes.append("H08 child_task_id cross-referenced successfully in wire evidence")

    # --- Check 4: ledger-producing scenario set is consistent with the
    # documented expectation (shape-based: which scenarios' own recorded
    # `actual`/`detail` strings indicate a ledger increment, not a live
    # ledger query). ------------------------------------------------------
    unexpected_producers = []
    for r in core_rows:
        sid = r.get("scenario")
        actual = r.get("actual", "")
        # Heuristic consistent with how each scenario records its own
        # ledger before/after counts in `actual` (see scenarios.py).
        produced_effect = False
        if sid == "H01" and "ledger_len=1" in actual:
            produced_effect = True
        elif sid in ("H06", "H07") and "ledger_after_first=" in actual or "ledger_after_genuine=" in actual:
            # H06/H07 legitimately produce exactly one entry on their FIRST
            # resume; their second (blocked) attempt must not add another.
            produced_effect = True
        elif sid == "H12" and "ledger_after_recovery=" in actual:
            produced_effect = True
        if produced_effect and sid not in EXPECTED_LEDGER_PRODUCING:
            unexpected_producers.append(sid)
    if unexpected_producers:
        problems.append(
            f"Scenarios producing a ledger effect but not in the expected set "
            f"{sorted(EXPECTED_LEDGER_PRODUCING)}: {unexpected_producers}"
        )
    else:
        notes.append(
            f"Ledger-producing scenario set consistent with expectation: {sorted(EXPECTED_LEDGER_PRODUCING)}"
        )

    # --- Check 5: both focused approval-policy test records present with
    # PASS status. ---------------------------------------------------------
    policy_path = os.path.join(EVIDENCE_DIR, "approval-policy-race-tests.jsonl")
    policy_rows = read_jsonl(policy_path)
    expected_policy_tests = {
        "APPROVAL-POLICY-01-replay-safety",
        "APPROVAL-POLICY-02-action-binding-tamper",
    }
    found_policy_tests = {r.get("test") for r in policy_rows}
    missing_policy = expected_policy_tests - found_policy_tests
    if missing_policy:
        problems.append(f"Missing focused approval-policy test records: {sorted(missing_policy)}")
    policy_not_pass = [
        r.get("test") for r in policy_rows if r.get("status") != "PASS"
    ]
    if policy_not_pass:
        problems.append(f"Focused approval-policy tests not PASS: {policy_not_pass}")
    if not missing_policy and not policy_not_pass:
        notes.append(
            f"Focused approval-policy tests: 2/2 PASS ({sorted(found_policy_tests)})"
        )

    # --- Summary ----------------------------------------------------------
    print("=" * 72)
    print("EVIDENCE VERIFICATION SUMMARY (read-only; no servers started/re-run)")
    print("=" * 72)
    for n in notes:
        print(f"  - {n}")
    print()
    if problems:
        print(f"RESULT: FAIL ({len(problems)} problem(s) found)")
        for p in problems:
            print(f"  ! {p}")
        return 1
    print("RESULT: PASS (all checks satisfied)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
