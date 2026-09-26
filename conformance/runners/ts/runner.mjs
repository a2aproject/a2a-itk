// Conformance runner for @a2a-js/sdk.
//
// Runner contract, shared by every language:
//
//   stdin   one JSON object per line: {"id", "message", "input"}
//   stdout  one JSON object per line: {"id", "output"} on success,
//           {"id", "rejected": true, "error"} when the SDK refuses the input
//
// A runner never decides whether a result is correct. It reports what its SDK
// did and nothing else. Adjudication belongs to run.py.

import { createInterface } from 'node:readline';
import * as sdk from '@a2a-js/sdk';

const rl = createInterface({ input: process.stdin, crlfDelay: Infinity });

for await (const line of rl) {
  if (!line.trim()) continue;

  const testCase = JSON.parse(line);
  const out = { id: testCase.id };
  const codec = sdk[testCase.message];

  if (!codec || typeof codec.fromJSON !== 'function') {
    out.rejected = true;
    out.error = `unknown message type ${testCase.message}`;
  } else {
    try {
      out.output = codec.toJSON(codec.fromJSON(testCase.input));
    } catch (err) {
      out.rejected = true;
      out.error = String((err && err.message) || err);
    }
  }

  process.stdout.write(JSON.stringify(out) + '\n');
}
