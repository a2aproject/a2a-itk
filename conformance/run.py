"""Run the wire-format conformance vectors against one or more SDK runners.

Two modes, because the choice between them is a real question and the corpus
supports both without change:

  agreement  every runner must produce the same result. Cheap, needs nobody to
             adjudicate, and catches drift between implementations. Blind to the
             case where every SDK agrees and all of them are wrong.

  strict     every runner must match the `expected` bytes recorded in the
             vector, which are derived from the canonical proto at
             specification/a2a.proto under the proto3 JSON mapping. Catches the
             unanimous-and-wrong case, at the cost of someone maintaining the
             expectations.

Usage:
    python conformance/run.py --mode agreement
    python conformance/run.py --mode strict --runner python --runner ts
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
from pathlib import Path

_HERE = Path(__file__).parent
_VECTORS = _HERE / 'vectors'

# How to invoke each runner. Each reads the shared line protocol on stdin.
RUNNERS: dict[str, list[str]] = {
    'python': ['uv', 'run', 'python', str(_HERE / 'runners/python/runner.py')],
    'ts': ['node', str(_HERE / 'runners/ts/runner.mjs')],
}


def load_vectors() -> list[dict]:
    vectors: list[dict] = []
    for path in sorted(_VECTORS.glob('*.json')):
        doc = json.loads(path.read_text(encoding='utf-8'))
        for vector in doc['vectors']:
            vector['axis'] = doc['axis']
            vectors.append(vector)
    return vectors


def invoke(name: str, vectors: list[dict]) -> dict[str, dict]:
    """Feed every vector to one runner and index its replies by vector id."""
    payload = '\n'.join(
        json.dumps({'id': v['id'], 'message': v['message'], 'input': v['input']})
        for v in vectors
    )
    cwd = _HERE / 'runners' / name
    proc = subprocess.run(
        RUNNERS[name],
        input=payload,
        capture_output=True,
        text=True,
        cwd=cwd if cwd.is_dir() else None,
        check=False,
    )
    if proc.returncode != 0:
        sys.exit(f'runner {name!r} exited {proc.returncode}:\n{proc.stderr}')

    replies = {}
    for line in proc.stdout.splitlines():
        if line.strip():
            reply = json.loads(line)
            replies[reply['id']] = reply
    return replies


def describe(reply: dict | None) -> str:
    if reply is None:
        return '<no reply>'
    if reply.get('rejected'):
        return f'rejected ({reply.get("error", "")[:60]})'
    return json.dumps(reply.get('output'), sort_keys=True, ensure_ascii=False)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument('--mode', choices=('agreement', 'strict'), default='strict')
    parser.add_argument(
        '--runner', action='append', dest='runners', choices=sorted(RUNNERS)
    )
    args = parser.parse_args()
    names = args.runners or sorted(RUNNERS)

    vectors = load_vectors()
    results = {name: invoke(name, vectors) for name in names}

    failures: list[str] = []

    for vector in vectors:
        vid = vector['id']
        replies = {name: results[name].get(vid) for name in names}

        if args.mode == 'strict':
            wants_reject = vector.get('expect_reject', False)
            for name, reply in replies.items():
                if wants_reject:
                    ok = bool(reply and reply.get('rejected'))
                    want = 'rejection'
                else:
                    ok = bool(reply) and reply.get('output') == vector.get('expected')
                    want = json.dumps(vector.get('expected'), ensure_ascii=False)
                if not ok:
                    failures.append(
                        f'{vid} [{name}]\n    want: {want}\n    got : {describe(reply)}'
                    )
        else:
            shapes = {name: describe(reply) for name, reply in replies.items()}
            if len(set(shapes.values())) > 1:
                detail = '\n'.join(f'    {n}: {s}' for n, s in sorted(shapes.items()))
                failures.append(f'{vid} runners disagree\n{detail}')

    total = len(vectors) * (len(names) if args.mode == 'strict' else 1)
    print(
        f'mode={args.mode} runners={",".join(names)} '
        f'vectors={len(vectors)} checks={total} failures={len(failures)}'
    )
    for failure in failures:
        print(f'\nFAIL {failure}')

    return 1 if failures else 0


if __name__ == '__main__':
    sys.exit(main())
