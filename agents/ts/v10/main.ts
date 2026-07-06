/**
 * ITK TypeScript v1.0 baseline agent.
 */
import express from 'express';
import * as grpc from '@grpc/grpc-js';
import process from 'process';

import {
  AGENT_CARD_PATH,
  type AgentCard,
  type Message,
  Role,
  StreamResponse,
  SubscribeToTaskRequest,
  CancelTaskRequest,
  type Task,
  type TaskPushNotificationConfig,
  TaskState,
} from '@a2a-js/sdk';
import {
  ClientFactory,
  ClientFactoryOptions,
  DefaultAgentCardResolver,
  JsonRpcTransportFactory,
  RestTransportFactory,
} from '@a2a-js/sdk/client';
import { GrpcTransportFactory } from '@a2a-js/sdk/client/grpc';
import {
  AgentEvent,
  type AgentExecutor,
  DefaultRequestHandler,
  type ExecutionEventBus,
  InMemoryTaskStore,
  type RequestContext,
  type TaskStore,
} from '@a2a-js/sdk/server';
import {
  agentCardHandler,
  jsonRpcHandler,
  restHandler,
  UserBuilder,
} from '@a2a-js/sdk/server/express';
import {
  A2AService,
  grpcService,
  UserBuilder as GrpcUserBuilder,
} from '@a2a-js/sdk/server/grpc';
import {
  legacyGrpcService,
  LegacyA2AService,
} from '@a2a-js/sdk/compat/v0_3/server/grpc';

import { Instruction, CallAgent } from './pb/instruction.js';

const HOLD_TASK_TICK_MS = 2000;
const HOLD_TASK_TICK_COUNT = 5;

export class ItkAgentExecutor implements AgentExecutor {
  private holdCancellers = new Map<string, AbortController>();

  async execute(context: RequestContext, eventBus: ExecutionEventBus): Promise<void> {
    console.log(`Executing task ${context.taskId}`);

    eventBus.publish(
      AgentEvent.task({
        id: context.taskId,
        contextId: context.contextId,
        status: {
          state: TaskState.TASK_STATE_SUBMITTED,
          message: undefined,
          timestamp: new Date().toISOString(),
        },
        artifacts: [],
        history: [context.userMessage],
        metadata: {},
      })
    );

    eventBus.publish(
      AgentEvent.statusUpdate({
        taskId: context.taskId,
        contextId: context.contextId,
        status: {
          state: TaskState.TASK_STATE_WORKING,
          message: undefined,
          timestamp: new Date().toISOString(),
        },
        metadata: undefined,
      })
    );

    const instruction = this.extractInstruction(context.userMessage);
    if (!instruction) {
      const errorMsg = 'No valid instruction found in request';
      console.error(errorMsg);
      this.publishStatus(eventBus, context, TaskState.TASK_STATE_FAILED, errorMsg);
      return;
    }

    try {
      const results = await this.handleInstruction(instruction);
      const responseText = results.join('\n');
      console.log('Response:', responseText);

      if (this.shouldHold(instruction)) {
        console.log(`[ItkAgent] Holding task ${context.taskId}`);
        const cancelled = await this.holdTask(
          eventBus,
          context,
          responseText + '\ntask-finished'
        );
        if (!cancelled) {
          this.publishStatus(eventBus, context, TaskState.TASK_STATE_COMPLETED, responseText);
        }
      } else {
        this.publishStatus(eventBus, context, TaskState.TASK_STATE_COMPLETED, responseText);
      }
    } catch (error) {
      console.error('Error handling instruction:', error);
      this.publishStatus(eventBus, context, TaskState.TASK_STATE_FAILED, String(error));
    }
  }

  async cancelTask(taskId: string, eventBus: ExecutionEventBus): Promise<void> {
    console.log(`Cancel requested for task ${taskId}`);
    const canceller = this.holdCancellers.get(taskId);
    if (canceller) {
      canceller.abort();
      this.holdCancellers.delete(taskId);
    }
    eventBus.publish(
      AgentEvent.statusUpdate({
        taskId,
        contextId: '',
        status: {
          state: TaskState.TASK_STATE_CANCELED,
          message: undefined,
          timestamp: new Date().toISOString(),
        },
        metadata: undefined,
      })
    );
  }

  private shouldHold(inst: Instruction): boolean {
    if (!inst.step) return false;
    if (inst.step.$case === 'returnResponse') {
      return inst.step.value.holdTask === true;
    }
    if (inst.step.$case === 'steps') {
      return inst.step.value.instructions.some((s) => this.shouldHold(s));
    }
    return false;
  }

  private async holdTask(
    eventBus: ExecutionEventBus,
    context: RequestContext,
    finishedText: string
  ): Promise<boolean> {
    const canceller = new AbortController();
    this.holdCancellers.set(context.taskId, canceller);
    try {
      const finishedMessage: Message = {
        messageId: 'task-finished',
        contextId: context.contextId,
        taskId: context.taskId,
        role: Role.ROLE_AGENT,
        parts: [
          {
            content: { $case: 'text', value: finishedText },
            mediaType: 'text/plain',
            filename: '',
            metadata: {},
          },
        ],
        extensions: [],
        referenceTaskIds: [],
        metadata: {},
      };
      eventBus.publish(
        AgentEvent.statusUpdate({
          taskId: context.taskId,
          contextId: context.contextId,
          status: {
            state: TaskState.TASK_STATE_WORKING,
            message: finishedMessage,
            timestamp: new Date().toISOString(),
          },
          metadata: undefined,
        })
      );

      for (let i = 0; i < HOLD_TASK_TICK_COUNT; i++) {
        try {
          await this.sleep(HOLD_TASK_TICK_MS, canceller.signal);
        } catch (e) {
          if (canceller.signal.aborted) return true;
          console.error('[ItkAgent] holdTask sleep failed unexpectedly:', e);
          throw e;
        }
        eventBus.publish(
          AgentEvent.statusUpdate({
            taskId: context.taskId,
            contextId: context.contextId,
            status: {
              state: TaskState.TASK_STATE_WORKING,
              message: undefined,
              timestamp: new Date().toISOString(),
            },
            metadata: undefined,
          })
        );
      }
      return false;
    } finally {
      this.holdCancellers.delete(context.taskId);
    }
  }

  private sleep(ms: number, signal: AbortSignal): Promise<void> {
    return new Promise<void>((resolve, reject) => {
      if (signal.aborted) {
        reject(new Error('aborted'));
        return;
      }
      const timer = setTimeout(() => {
        signal.removeEventListener('abort', onAbort);
        resolve();
      }, ms);
      const onAbort = () => {
        clearTimeout(timer);
        reject(new Error('aborted'));
      };
      signal.addEventListener('abort', onAbort, { once: true });
    });
  }

  private publishStatus(
    eventBus: ExecutionEventBus,
    context: RequestContext,
    state: TaskState,
    text: string
  ): void {
    eventBus.publish(
      AgentEvent.statusUpdate({
        taskId: context.taskId,
        contextId: context.contextId,
        status: {
          state,
          message: {
            messageId: state === TaskState.TASK_STATE_COMPLETED ? 'done' : 'fail',
            parts: [
              {
                content: { $case: 'text', value: text },
                mediaType: 'text/plain',
                filename: '',
                metadata: {},
              },
            ],
            role: Role.ROLE_AGENT,
            metadata: {},
            contextId: context.contextId,
            taskId: context.taskId,
            extensions: [],
            referenceTaskIds: [],
          },
          timestamp: new Date().toISOString(),
        },
        metadata: undefined,
      })
    );
  }

  private extractInstruction(message: Message): Instruction | null {
    if (!message || !message.parts) return null;

    for (const part of message.parts) {
      if (part.mediaType === 'application/x-protobuf' || part.filename === 'instruction.bin') {
        if (part.content?.$case === 'raw') {
          try {
            return Instruction.decode(part.content.value);
          } catch (e) {
            console.debug('[ItkAgent] raw Instruction.decode failed:', e);
          }
        } else if (part.content?.$case === 'text') {
          try {
            return Instruction.decode(Buffer.from(part.content.value, 'base64'));
          } catch (e) {
            console.debug('[ItkAgent] x-protobuf text/base64 Instruction.decode failed:', e);
          }
        }
      }

      if (part.content?.$case === 'text') {
        try {
          return Instruction.decode(Buffer.from(part.content.value, 'base64'));
        } catch (e) {
          console.debug('[ItkAgent] fallback text/base64 Instruction.decode failed:', e);
        }
      }
    }
    return null;
  }

  private async handleInstruction(inst: Instruction): Promise<string[]> {
    if (!inst.step) throw new Error('Unknown instruction type');

    switch (inst.step.$case) {
      case 'returnResponse':
        return [inst.step.value.response];
      case 'callAgent':
        return await this.handleCallAgent(inst.step.value);
      case 'steps': {
        const allResults: string[] = [];
        for (const step of inst.step.value.instructions) {
          const results = await this.handleInstruction(step);
          allResults.push(...results);
        }
        return allResults;
      }
      default:
        throw new Error('Unknown instruction type');
    }
  }

  private async handleCallAgent(call: CallAgent): Promise<string[]> {
    console.log(`Calling agent ${call.agentCardUri} via ${call.transport}`);

    const transportMap: Record<string, string> = {
      JSONRPC: 'JSONRPC',
      'HTTP+JSON': 'HTTP+JSON',
      HTTP_JSON: 'HTTP+JSON',
      REST: 'HTTP+JSON',
      GRPC: 'GRPC',
    };

    const selectedTransport = transportMap[call.transport.toUpperCase()];
    if (!selectedTransport) {
      throw new Error(`Unsupported transport: ${call.transport}`);
    }

    const legacyCompat = { enabled: true };
    const factory = new ClientFactory(
      ClientFactoryOptions.createFrom(ClientFactoryOptions.default, {
        transports: [
          new JsonRpcTransportFactory({ legacyCompat }),
          new RestTransportFactory({ legacyCompat }),
          new GrpcTransportFactory({ legacyCompat }),
        ],
        preferredTransports: [selectedTransport as 'JSONRPC' | 'HTTP+JSON' | 'GRPC'],
        cardResolver: new DefaultAgentCardResolver({ legacyCompat }),
      })
    );

    let pushNotificationConfig: TaskPushNotificationConfig | undefined;
    if (call.behavior?.$case === 'pushNotification') {
      let url = call.behavior.value?.url;
      if (!url) throw new Error('URL not specified in push_notification behavior');
      if (!url.startsWith('http://') && !url.startsWith('https://')) {
        url = `http://${url}`;
      }
      pushNotificationConfig = {
        url: `${url}/notifications`,
        token: 'itk-token',
        id: '',
        taskId: '',
        tenant: '',
        authentication: undefined,
      };
    }

    try {
      const baseUri = call.agentCardUri.endsWith('/')
        ? call.agentCardUri
        : call.agentCardUri + '/';
      const client = await factory.createFromUrl(baseUri);

      if (!call.instruction) {
        throw new Error('Instruction missing in callAgent step');
      }
      const instBytes = Buffer.from(Instruction.encode(call.instruction).finish());
      const nestedMsg: Message = {
        messageId: Math.random().toString(36).substring(2),
        contextId: '',
        taskId: '',
        role: Role.ROLE_USER,
        parts: [
          {
            content: { $case: 'raw', value: instBytes },
            filename: 'instruction.bin',
            mediaType: 'application/x-protobuf',
            metadata: {},
          },
        ],
        extensions: [],
        referenceTaskIds: [],
        metadata: {},
      };

      const results: string[] = [];

      const processMessage = (msg: Message | undefined) => {
        if (!msg?.parts) return;
        for (const part of msg.parts) {
          if (part.content?.$case === 'text' && part.content.value) {
            results.push(part.content.value);
          }
        }
      };

      const request = {
        tenant: '',
        message: nestedMsg,
        configuration: pushNotificationConfig
          ? {
              acceptedOutputModes: [],
              taskPushNotificationConfig: pushNotificationConfig,
              returnImmediately: false,
            }
          : undefined,
        metadata: {},
      };

      if (call.behavior?.$case === 'resubscribe') {
        const resubscribeResults = await this.callAgentWithResubscribe(client, request);
        results.length = 0;
        results.push(...resubscribeResults);
      } else if (call.streaming) {
        for await (const event of client.sendMessageStream(request)) {
          const msg = this.extractMessageFromStreamResponse(event);
          processMessage(msg);
        }
      } else {
        const response = await client.sendMessage(request);
        if (response && 'parts' in response) {
          processMessage(response as Message);
        } else if (response && 'status' in response) {
          const task = response as Task;
          processMessage(task.status?.message);
          task.history?.forEach(processMessage);
        }
      }

      return results;
    } catch (e) {
      console.error('Failed to call outbound agent', e);
      throw new Error(`Outbound call to ${call.agentCardUri} failed: ${e}`);
    }
  }

  private async callAgentWithResubscribe(
    client: Awaited<ReturnType<ClientFactory['createFromUrl']>>,
    request: Parameters<Awaited<ReturnType<ClientFactory['createFromUrl']>>['sendMessage']>[0]
  ): Promise<string[]> {
    const initController = new AbortController();
    let taskId: string | undefined;
    try {
      for await (const event of client.sendMessageStream(request, {
        signal: initController.signal,
      })) {
        taskId = this.extractTaskIdFromStreamResponse(event);
        if (taskId) break;
      }
    } catch (e) {
      if (!initController.signal.aborted) {
        console.error('[ItkAgent] resubscribe init sendMessageStream failed:', e);
        throw e;
      }
    } finally {
      initController.abort();
    }

    if (!taskId) {
      throw new Error('Resubscribe: initial send_message did not yield a task_id');
    }

    const responses: string[] = [];
    let finished = false;

    const collect = (msg: Message | undefined): boolean => {
      if (!msg?.parts) return false;
      for (const part of msg.parts) {
        if (part.content?.$case === 'text' && part.content.value) {
          const text = part.content.value.replace(/task-finished/g, '').trim();
          if (text) responses.push(text);
          if (part.content.value.includes('task-finished')) return true;
        }
      }
      return false;
    };

    const resubRequest: SubscribeToTaskRequest = { tenant: '', id: taskId };
    for await (const event of client.resubscribeTask(resubRequest)) {
      if (!event.payload) continue;
      if (event.payload.$case === 'task') {
        const task = event.payload.value;
        for (const histMsg of task.history ?? []) {
          if (histMsg.role === Role.ROLE_AGENT && collect(histMsg)) {
            finished = true;
            break;
          }
        }
        if (!finished && collect(task.status?.message)) finished = true;
      } else if (event.payload.$case === 'statusUpdate') {
        if (collect(event.payload.value.status?.message)) finished = true;
      } else if (event.payload.$case === 'message') {
        if (collect(event.payload.value)) finished = true;
      }
      if (finished) break;
    }

    try {
      const cancelReq: CancelTaskRequest = { tenant: '', id: taskId, metadata: undefined };
      await client.cancelTask(cancelReq);
    } catch (e) {
      console.warn(`[ItkAgent] Cancel after resubscribe failed (non-fatal):`, e);
    }

    return responses;
  }

  private extractTaskIdFromStreamResponse(event: StreamResponse): string | undefined {
    if (!event.payload) return undefined;
    switch (event.payload.$case) {
      case 'task':
        return event.payload.value.id || undefined;
      case 'statusUpdate':
        return event.payload.value.taskId || undefined;
      case 'artifactUpdate':
        return event.payload.value.taskId || undefined;
      default:
        return undefined;
    }
  }

  private extractMessageFromStreamResponse(event: StreamResponse): Message | undefined {
    if (!event.payload) return undefined;
    switch (event.payload.$case) {
      case 'message':
        return event.payload.value;
      case 'statusUpdate':
        return event.payload.value.status?.message;
      case 'task':
        return event.payload.value.status?.message;
      default:
        return undefined;
    }
  }
}

async function main() {
  const args = process.argv.slice(2);
  let httpPort = 10102;
  let grpcPort = 11002;

  for (let i = 0; i < args.length; i++) {
    if (args[i] === '--httpPort' && i + 1 < args.length) {
      httpPort = parseInt(args[i + 1], 10);
      i++;
    } else if (args[i].startsWith('--httpPort=')) {
      httpPort = parseInt(args[i].split('=')[1], 10);
    } else if (args[i] === '--grpcPort' && i + 1 < args.length) {
      grpcPort = parseInt(args[i + 1], 10);
      i++;
    } else if (args[i].startsWith('--grpcPort=')) {
      grpcPort = parseInt(args[i].split('=')[1], 10);
    }
  }

  console.log(`Starting ITK TS v1.0 baseline agent on HTTP ${httpPort} / gRPC ${grpcPort}`);

  const agentCard: AgentCard = {
    name: 'ITK TS v1.0 Baseline Agent',
    description: 'TypeScript baseline agent for ITK interoperability tests.',
    version: '1.0.0',
    capabilities: {
      streaming: true,
      pushNotifications: true,
      extensions: [],
      extendedAgentCard: true,
    },
    supportedInterfaces: [
      {
        url: `http://127.0.0.1:${httpPort}/jsonrpc`,
        protocolBinding: 'JSONRPC',
        tenant: '',
        protocolVersion: '1.0',
      },
      {
        url: `http://127.0.0.1:${httpPort}/jsonrpc`,
        protocolBinding: 'JSONRPC',
        tenant: '',
        protocolVersion: '0.3',
      },
      {
        url: `127.0.0.1:${grpcPort}`,
        protocolBinding: 'GRPC',
        tenant: '',
        protocolVersion: '1.0',
      },
      {
        url: `127.0.0.1:${grpcPort}`,
        protocolBinding: 'GRPC',
        tenant: '',
        protocolVersion: '0.3',
      },
      {
        url: `http://127.0.0.1:${httpPort}/rest`,
        protocolBinding: 'HTTP+JSON',
        tenant: '',
        protocolVersion: '1.0',
      },
      {
        url: `http://127.0.0.1:${httpPort}/rest`,
        protocolBinding: 'HTTP+JSON',
        tenant: '',
        protocolVersion: '0.3',
      },
    ],
    provider: { organization: 'A2A Samples', url: 'https://example.com/a2a-samples' },
    securitySchemes: {},
    securityRequirements: [],
    defaultInputModes: ['text/plain', 'application/x-protobuf'],
    defaultOutputModes: ['text/plain'],
    skills: [],
    signatures: [],
  };

  const taskStore: TaskStore = new InMemoryTaskStore();
  const agentExecutor: AgentExecutor = new ItkAgentExecutor();
  const requestHandler = new DefaultRequestHandler(agentCard, taskStore, agentExecutor);

  const app = express();
  const jsonRpcPath = '/jsonrpc';
  const restPath = '/rest';
  const legacyCompat = { enabled: true };

  app.use(
    `/${AGENT_CARD_PATH}`,
    agentCardHandler({ agentCardProvider: requestHandler, legacyCompat })
  );
  app.use(
    `${jsonRpcPath}/${AGENT_CARD_PATH}`,
    agentCardHandler({ agentCardProvider: requestHandler, legacyCompat })
  );
  app.use(
    `${restPath}/${AGENT_CARD_PATH}`,
    agentCardHandler({ agentCardProvider: requestHandler, legacyCompat })
  );
  app.use(jsonRpcPath, express.json());
  app.use(
    jsonRpcPath,
    jsonRpcHandler({ requestHandler, userBuilder: UserBuilder.noAuthentication, legacyCompat })
  );
  app.use(
    restPath,
    restHandler({ requestHandler, userBuilder: UserBuilder.noAuthentication, legacyCompat })
  );

  app.listen(httpPort, () => {
    console.log(`[ItkAgent] Server started on http://localhost:${httpPort}`);
  });

  const grpcServer = new grpc.Server();
  grpcServer.addService(
    A2AService,
    grpcService({
      requestHandler,
      userBuilder: GrpcUserBuilder.noAuthentication,
    })
  );
  grpcServer.addService(
    LegacyA2AService,
    legacyGrpcService({
      requestHandler,
      userBuilder: GrpcUserBuilder.noAuthentication,
    })
  );

  grpcServer.bindAsync(
    `0.0.0.0:${grpcPort}`,
    grpc.ServerCredentials.createInsecure(),
    (err, port) => {
      if (err) {
        console.error(`Failed to bind gRPC server: ${err.message}`);
        return;
      }
      console.log(`gRPC server listening on port ${port}`);
    }
  );
}

main().catch(console.error);
