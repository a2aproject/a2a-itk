#!/usr/bin/env node
/**
 * Coordinator Agent: real @a2a-js/sdk@1.3.0 server (toward the Test Client)
 * AND real @a2a-js/sdk client (toward the Python Expense Agent).
 *
 * Responsibility (H08 requirement): faithfully propagate the Expense Agent's
 * AUTH_REQUIRED interrupted state up to the Test Client as an observable
 * task-state transition on the COORDINATOR's own task -- not silently
 * absorbed or misrepresented as a different state.
 *
 * Correlation model: the coordinator creates its OWN parent task_id/context_id
 * for the Test Client conversation, and delegates to the Expense Agent which
 * creates its OWN child task_id under a child context_id. The mapping
 * parent task_id -> {childTaskId, childContextId, parentContextId} is kept
 * in-memory so a resume message on the parent task is correctly forwarded
 * to the correct child task.
 */
import crypto from 'node:crypto';
import express from 'express';
import fs from 'node:fs';
import path from 'node:path';
import { fileURLToPath } from 'node:url';

import {
  A2A_PROTOCOL_VERSION,
  AGENT_CARD_PATH,
  TaskState,
  Role,
} from '@a2a-js/sdk';
import {
  InMemoryTaskStore,
  DefaultRequestHandler,
  AgentEvent,
} from '@a2a-js/sdk/server';
import { agentCardHandler, restHandler, UserBuilder } from '@a2a-js/sdk/server/express';
import {
  ClientFactory,
  ClientFactoryOptions,
  DefaultAgentCardResolver,
  JsonRpcTransportFactory,
  RestTransportFactory,
} from '@a2a-js/sdk/client';

const __dirname = path.dirname(fileURLToPath(import.meta.url));

const HOST = process.env.COORDINATOR_HOST || '127.0.0.1';
const PORT = Number(process.env.COORDINATOR_PORT || 8301);
const EXPENSE_AGENT_URL = process.env.EXPENSE_AGENT_URL || 'http://127.0.0.1:8201';
const EVIDENCE_PATH =
  process.env.COORDINATOR_WIRE_EVIDENCE ||
  path.join(__dirname, '..', 'evidence', 'coordinator-wire.jsonl');

function trace(record) {
  fs.mkdirSync(path.dirname(EVIDENCE_PATH), { recursive: true });
  fs.appendFileSync(EVIDENCE_PATH, JSON.stringify(record, (k, v) => (typeof v === 'bigint' ? v.toString() : v)) + '\n');
}

// --- parent task_id -> child correlation map ------------------------------
/** @type {Map<string, {childTaskId: string, childContextId: string}>} */
const correlation = new Map();

// --- Expense Agent client (coordinator acting as client) ------------------
const expenseClientFactory = new ClientFactory(
  ClientFactoryOptions.createFrom(ClientFactoryOptions.default, {
    cardResolver: new DefaultAgentCardResolver(),
    transports: [new JsonRpcTransportFactory(), new RestTransportFactory()],
  })
);

let expenseClientPromise = null;
async function getExpenseClient() {
  if (!expenseClientPromise) {
    expenseClientPromise = expenseClientFactory.createFromUrl(EXPENSE_AGENT_URL);
  }
  return expenseClientPromise;
}

function textPart(value) {
  return { content: { $case: 'text', value }, metadata: undefined, filename: '', mediaType: 'text/plain' };
}

function childMessage(text, taskId, contextId) {
  return {
    role: Role.ROLE_USER,
    messageId: crypto.randomUUID(),
    parts: [textPart(text)],
    taskId: taskId || '',
    contextId: contextId || '',
    extensions: [],
    metadata: {},
    referenceTaskIds: [],
  };
}

/** Sends a message to the Expense Agent and drains the stream, returning the
 * last Task snapshot observed (the terminal-or-interrupted state). */
async function callExpenseAgent(text, taskId, contextId) {
  const client = await getExpenseClient();
  let lastTask = null;
  const req = {
    message: childMessage(text, taskId, contextId),
    configuration: { returnImmediately: false, acceptedOutputModes: [], historyLength: undefined },
  };
  trace({ kind: 'coordinator_to_expense_request', at: Date.now(), request: req });
  // client.sendMessageStream yields raw StreamResponse (wire/protobuf shape:
  // event.payload.$case / event.payload.value), NOT the server-side
  // AgentExecutionEvent discriminated union (that type is only used inside
  // AgentExecutor/ExecutionEventBus on the server side).
  for await (const event of client.sendMessageStream(req)) {
    trace({ kind: 'coordinator_to_expense_event', at: Date.now(), event });
    const payloadCase = event.payload?.$case;
    const payloadValue = event.payload?.value;
    if (payloadCase === 'task') {
      lastTask = payloadValue;
    } else if (payloadCase === 'statusUpdate' && lastTask) {
      lastTask = { ...lastTask, status: payloadValue.status };
    }
  }
  return lastTask;
}

async function getExpenseTask(taskId) {
  const client = await getExpenseClient();
  return client.getTask({ id: taskId, tenant: '' });
}

async function cancelExpenseTask(taskId) {
  const client = await getExpenseClient();
  return client.cancelTask({ id: taskId, tenant: '', metadata: {} });
}

// --- Coordinator AgentExecutor (coordinator acting as server) -------------

class CoordinatorExecutor {
  cancelTask = async (taskId, eventBus) => {
    const corr = correlation.get(taskId);
    trace({ kind: 'coordinator_cancel_requested', at: Date.now(), parentTaskId: taskId, correlation: corr });
    if (corr) {
      try {
        await cancelExpenseTask(corr.childTaskId);
      } catch (err) {
        trace({ kind: 'coordinator_cancel_child_error', at: Date.now(), error: String(err) });
      }
    }
    const statusUpdate = {
      taskId,
      contextId: corr?.parentContextId || '',
      status: { state: TaskState.TASK_STATE_CANCELED, timestamp: new Date().toISOString(), message: undefined },
      metadata: {},
    };
    eventBus.publish(AgentEvent.statusUpdate(statusUpdate));
  };

  async execute(requestContext, eventBus) {
    const userMessage = requestContext.userMessage;
    const taskId = requestContext.taskId;
    const contextId = requestContext.contextId;
    const existingTask = requestContext.task;
    const userText = (userMessage.parts || [])
      .map((p) => (p.content?.$case === 'text' ? p.content.value : ''))
      .join('\n');

    trace({
      kind: 'coordinator_execute',
      at: Date.now(),
      parentTaskId: taskId,
      parentContextId: contextId,
      hasExistingTask: !!existingTask,
      userText,
    });

    // 1. Every turn must begin with a Task or Message event.
    const taskSnapshot = existingTask ?? {
      id: taskId,
      contextId,
      status: { state: TaskState.TASK_STATE_SUBMITTED, timestamp: new Date().toISOString(), message: undefined },
      artifacts: [],
      history: [userMessage],
      metadata: userMessage.metadata,
    };
    eventBus.publish(AgentEvent.task(taskSnapshot));

    eventBus.publish(
      AgentEvent.statusUpdate({
        taskId,
        contextId,
        status: { state: TaskState.TASK_STATE_WORKING, timestamp: new Date().toISOString(), message: undefined },
        metadata: {},
      })
    );

    let corr = correlation.get(taskId);
    let childTask;
    try {
      if (!corr) {
        // First turn: delegate a NEW expense request to the child agent.
        childTask = await callExpenseAgent(userText, undefined, undefined);
        if (!childTask) throw new Error('Expense agent returned no task snapshot');
        corr = { childTaskId: childTask.id, childContextId: childTask.contextId, parentContextId: contextId };
        correlation.set(taskId, corr);
      } else {
        // Follow-up turn (e.g. resume after approval/denial): forward on the
        // SAME child task_id/context_id so the expense agent's execute() is
        // invoked again for that task per the A2A in-bound auth model.
        childTask = await callExpenseAgent(userText, corr.childTaskId, corr.childContextId);
        if (!childTask) {
          // Streaming may not re-emit a Task on a resume in some transports;
          // fall back to an explicit get_task to observe current state.
          childTask = await getExpenseTask(corr.childTaskId);
        }
      }
    } catch (err) {
      trace({ kind: 'coordinator_delegate_error', at: Date.now(), error: String(err) });
      eventBus.publish(
        AgentEvent.statusUpdate({
          taskId,
          contextId,
          status: {
            state: TaskState.TASK_STATE_FAILED,
            timestamp: new Date().toISOString(),
            message: {
              role: Role.ROLE_AGENT,
              messageId: crypto.randomUUID(),
              parts: [textPart(`Delegation to expense agent failed: ${err}`)],
              taskId,
              contextId,
              extensions: [],
              metadata: {},
              referenceTaskIds: [],
            },
          },
          metadata: {},
        })
      );
      return;
    }

    // 2. Faithfully propagate the child's state as the PARENT's observable
    //    state (H08 requirement) -- no state invention, no silent absorption.
    const childState = childTask.status.state;
    trace({
      kind: 'coordinator_propagate',
      at: Date.now(),
      parentTaskId: taskId,
      childTaskId: corr.childTaskId,
      childState: childState,
    });

    const propagatedMessage = childTask.status.message
      ? {
          ...childTask.status.message,
          taskId,
          contextId,
          messageId: crypto.randomUUID(),
          metadata: {
            ...(childTask.status.message.metadata || {}),
            'coordinator.childTaskId': corr.childTaskId,
            'coordinator.childContextId': corr.childContextId,
          },
        }
      : undefined;

    eventBus.publish(
      AgentEvent.statusUpdate({
        taskId,
        contextId,
        status: {
          state: childState,
          timestamp: new Date().toISOString(),
          message: propagatedMessage,
        },
        metadata: { 'coordinator.childTaskId': corr.childTaskId, 'coordinator.childContextId': corr.childContextId },
      })
    );
  }
}

// --- Server bootstrap ------------------------------------------------------

const coordinatorAgentCard = {
  name: 'Coordinator Agent (real SDK)',
  description: 'Delegates expense reimbursement requests to the Expense Agent, propagating HIL approval interruptions.',
  supportedInterfaces: [
    {
      url: `http://${HOST}:${PORT}/a2a/rest`,
      protocolBinding: 'HTTP+JSON',
      tenant: '',
      protocolVersion: A2A_PROTOCOL_VERSION,
    },
  ],
  provider: { organization: 'A2A Conformance Testing', url: 'https://example.invalid' },
  version: '1.0.0',
  capabilities: { streaming: true, pushNotifications: false, extensions: [], extendedAgentCard: false },
  securitySchemes: {},
  securityRequirements: [],
  defaultInputModes: ['text'],
  defaultOutputModes: ['text', 'task-status'],
  skills: [
    {
      id: 'coordinate_expense',
      name: 'Coordinate Expense Reimbursement',
      description: 'Delegates to the Expense Agent and surfaces HIL approval interruptions.',
      tags: ['coordinator', 'expense', 'hil'],
      examples: ['{"amountMinor": 15000, "currency": "USD", "beneficiary": "acme"}'],
      inputModes: ['text'],
      outputModes: ['text', 'task-status'],
      securityRequirements: [],
    },
  ],
  documentationUrl: '',
  signatures: [],
};

async function main() {
  const taskStore = new InMemoryTaskStore();
  const agentExecutor = new CoordinatorExecutor();
  const requestHandler = new DefaultRequestHandler(coordinatorAgentCard, taskStore, agentExecutor);

  const app = express();
  app.use(express.json());

  // Wire-capture middleware (logs raw request/response at the transport
  // boundary for the Test-Client -> Coordinator hop).
  app.use((req, res, next) => {
    const chunks = [];
    const originalWrite = res.write.bind(res);
    const originalEnd = res.end.bind(res);
    res.write = (chunk, ...args) => {
      if (chunk) chunks.push(Buffer.isBuffer(chunk) ? chunk : Buffer.from(chunk));
      return originalWrite(chunk, ...args);
    };
    res.end = (chunk, ...args) => {
      if (chunk) chunks.push(Buffer.isBuffer(chunk) ? chunk : Buffer.from(chunk));
      trace({
        kind: 'wire_http',
        server: 'coordinator',
        method: req.method,
        path: req.path,
        request_body: req.body,
        status_code: res.statusCode,
        response_body: Buffer.concat(chunks).toString('utf8').slice(0, 8000),
        at: Date.now(),
      });
      return originalEnd(chunk, ...args);
    };
    next();
  });

  app.use(`/${AGENT_CARD_PATH}`, agentCardHandler({ agentCardProvider: requestHandler }));
  app.use('/a2a/rest', restHandler({ requestHandler, userBuilder: UserBuilder.noAuthentication }));

  app.get('/__health__', (_req, res) => res.json({ ok: true }));

  app.listen(PORT, HOST, () => {
    console.log(`[Coordinator] listening on http://${HOST}:${PORT}`);
    console.log(`[Coordinator] agent card: http://${HOST}:${PORT}/${AGENT_CARD_PATH}`);
  });
}

main().catch((err) => {
  console.error(err);
  process.exit(1);
});
