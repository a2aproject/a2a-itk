# Wire-format conformance vectors

Draft slice for a2aproject/a2a-itk#32. Not ready to merge, and the two questions on
that issue are still open.

## What this is for

The traversal scenarios check that agents built on different SDKs can talk to each other.
They do not check that those SDKs agree on the bytes, because a traversal only carries one
Part shape and verifies by substring containment, so any divergence that leaves the trace
tokens readable passes.

These vectors check the other thing. Each one is an input, the message type it belongs to,
and what the canonical proto at `specification/a2a.proto` says should come back out under
the proto3 JSON mapping.

## Layout

```
conformance/
  vectors/            language-neutral JSON, one file per axis
  runners/python/     a2a-python
  runners/ts/         @a2a-js/sdk
  run.py              drives the runners and adjudicates
```

## The runner contract

Every runner reads one JSON object per line on stdin and writes one per line on stdout:

```
in   {"id": "...", "message": "TaskStatus", "input": {...}}
out  {"id": "...", "output": {...}}
out  {"id": "...", "rejected": true, "error": "..."}
```

A runner never decides whether a result is right. It reports what its SDK did. All
adjudication lives in `run.py`, so adding an SDK means writing a runner, not re-implementing
the comparison. A runner is about forty lines.

## Two modes

The choice between these is question 2 on #32. The corpus supports both without change, so
the decision is a flag rather than a rewrite.

```bash
python conformance/run.py --mode agreement
python conformance/run.py --mode strict
python conformance/run.py --mode strict --runner python
```

`agreement` requires every runner to produce the same result. It is cheap and needs nobody
to adjudicate, but it cannot see the case where every SDK agrees and all of them are wrong.
That case is real: see a2aproject/A2A#2122, where neither Python nor JS reproduces the
§8.4.1 worked example and they fail it identically.

`strict` requires every runner to match the `expected` value recorded in the vector. Those
values are derived from the canonical proto, which `specification/json/README.md` confirms
is normative while `a2a.json` is a non-normative build artifact. This catches the unanimous
case, at the cost of maintaining expectations.

## Current results

Two axes, 13 vectors, `a2a-python` at v1.0.3 and `@a2a-js/sdk` at 1.0.1.

```
mode=strict    runners=python,ts  vectors=13  checks=26  failures=6
mode=agreement runners=python,ts  vectors=13  checks=13  failures=6
mode=strict    runners=python     vectors=13  checks=13  failures=0
```

The six are the same six in both modes, all on the JS side, and each maps to a filed issue:

| Vector | Expected | `@a2a-js/sdk` 1.0.1 | Issue |
| --- | --- | --- | --- |
| `enum.unknown_number` | `{"state": 99}` | `{"state": "UNRECOGNIZED"}` | a2aproject/a2a-js#640 |
| `enum.role_unknown_number` | `{"role": 77}` | `{"role": "UNRECOGNIZED"}` | a2aproject/a2a-js#640 |
| `timestamp.offset_utc` | `...T00:00:00Z` | `...T00:00:00+00:00` | a2aproject/a2a-js#641 |
| `timestamp.offset_non_utc` | `...T00:00:00Z` | `...T05:30:00+05:30` | a2aproject/a2a-js#641 |
| `timestamp.not_a_timestamp` | rejection | accepted and forwarded | a2aproject/a2a-js#641 |
| `timestamp.number_input` | rejection | coerced to `"12345"` | a2aproject/a2a-js#641 |

The Python-only run failing nothing is the control. It shows the expectations are
achievable rather than a wish list, so a failure means an SDK diverged rather than that the
vector was unreasonable.

`enum.role_unknown_number` is new. #640 was filed against `TaskState`, and `Role` behaves
the same way, which is what a shared corpus is supposed to surface.

## Adding an axis

Drop a JSON file in `vectors/`. `run.py` reads every file in that directory, so no
registration step. Each vector carries a `rationale` saying what it is testing and why,
because a vector nobody can explain is a vector nobody will maintain.

## Not covered yet

Deliberately narrow, per the offer on #32: enums and Timestamp only, Python and TS only.
The axes that would follow are oneof arm selection, bytes and base64, `Struct` and `Value`
including null, numeric formatting, and non-ASCII text. Go, Java and Rust runners are the
same forty lines each against the agents already in `agents/`.
