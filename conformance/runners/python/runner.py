"""Conformance runner for a2a-python.

Runner contract, shared by every language:

    stdin   one JSON object per line: {"id", "message", "input"}
    stdout  one JSON object per line: {"id", "output"} on success,
            {"id", "rejected": true, "error"} when the SDK refuses the input

A runner never decides whether a result is correct. It reports what its SDK
did and nothing else. Adjudication belongs to run.py so that adding an SDK
does not mean re-implementing the comparison.
"""

import json
import sys

from a2a import types
from google.protobuf.json_format import MessageToDict, ParseDict


def main() -> None:
    for raw in sys.stdin:
        line = raw.strip()
        if not line:
            continue
        case = json.loads(line)
        out: dict = {'id': case['id']}

        cls = getattr(types, case['message'], None)
        if cls is None:
            out['rejected'] = True
            out['error'] = f'unknown message type {case["message"]}'
        else:
            try:
                msg = cls()
                ParseDict(case['input'], msg)
                out['output'] = MessageToDict(msg)
            # Deliberately broad: however the SDK refuses the input, the runner
            # reports it as a rejection and lets run.py decide if that is right.
            except Exception as exc:  # noqa: BLE001
                out['rejected'] = True
                out['error'] = f'{type(exc).__name__}: {exc}'

        sys.stdout.write(json.dumps(out, ensure_ascii=False) + '\n')


if __name__ == '__main__':
    main()
