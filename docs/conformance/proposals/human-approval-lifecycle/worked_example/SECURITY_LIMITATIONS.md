# Security limitations (read before reusing any of this)

This worked example is a synthetic, local-only, defensive/conformance-testing
fixture. It is **not** a reference security design and must never be treated
as one. Specifically:

- **No real principal authentication.** The out-of-band `/__human__/decision`
  endpoint accepts a `principal` field as a plain string with no
  verification whatsoever — no session, no token, no certificate, no
  cryptographic signature. Whichever HTTP caller supplies the exact
  allowlisted string is treated as "the human," full stop.
- **Single hardcoded allowlist string.** `TRUSTED_PRINCIPAL = "human-auditor"`
  in `expense_agent/approval_gate.py` is the entire "trust" mechanism: a
  Python `==` string comparison. There is no notion of multiple principals,
  roles, or revocation.
- **In-memory, non-durable state.** `ApprovalGate` keeps all challenges and
  the mock ledger in process memory (`dict`/`list` behind a `threading.RLock`).
  A process restart loses everything. There is no persistence, no
  replication, no crash recovery.
- **No transport security considerations addressed.** The example runs over
  plain HTTP on localhost. TLS, mutual TLS, network segmentation, and
  secrets management are all out of scope and unaddressed.
- **Synthetic, local-only.** Every scenario in this example runs against
  `localhost` processes spawned and torn down by `run.sh`. Nothing here has
  been exercised against a networked, multi-tenant, or production-shaped
  deployment.
- **The one real SDK defect encountered along the way (not fixed here).**
  Building this example surfaced a confirmed `a2a-python` SDK defect
  (`TaskManager.save_task_event()` can overwrite a terminal task's status
  from a late/stale event with no transition guard) that is already being
  fixed upstream in `a2a-python` pull request #1182. That PR is existing,
  separate, already in flight work — it is referenced here for context only.
  This proposal does not duplicate it, does not re-describe it as something
  to fix in this repository, and does not depend on it landing.

A production implementation of human-approval gating on top of
`AUTH_REQUIRED` would need, at minimum: a genuinely authenticated
human-approval channel (signed callback, OAuth-authenticated session, or
equivalent), durable and auditable decision storage, proper secrets handling,
and a real multi-principal authorization model. Building any of that is
explicitly out of scope for this worked example, whose only purpose is to
demonstrate the *shape* of the application-level invariants (exact-action
binding, single-use, trusted-principal check, expiry, retry-safety, and
honest representation of out-of-band channel failure) that a real
implementation would still need to get right, regardless of how it
implements the authentication/authorization underneath.
