package org.a2aproject.sdk.examples.helloworld;

import java.util.ArrayList;
import java.util.Base64;
import java.util.Collections;
import java.util.List;
import java.util.UUID;
import java.util.concurrent.CompletableFuture;
import java.util.concurrent.ExecutionException;
import java.util.concurrent.TimeUnit;
import java.util.concurrent.TimeoutException;

import jakarta.enterprise.context.ApplicationScoped;
import jakarta.enterprise.inject.Produces;

import org.a2aproject.sdk.A2A;
import org.a2aproject.sdk.client.Client;
import org.a2aproject.sdk.client.ClientEvent;
import org.a2aproject.sdk.client.MessageEvent;
import org.a2aproject.sdk.client.TaskEvent;
import org.a2aproject.sdk.client.TaskUpdateEvent;
import org.a2aproject.sdk.client.config.ClientConfig;
import org.a2aproject.sdk.client.transport.grpc.GrpcTransport;
import org.a2aproject.sdk.client.transport.grpc.GrpcTransportConfigBuilder;
import org.a2aproject.sdk.client.transport.jsonrpc.JSONRPCTransport;
import org.a2aproject.sdk.client.transport.jsonrpc.JSONRPCTransportConfigBuilder;
import org.a2aproject.sdk.client.transport.rest.RestTransport;
import org.a2aproject.sdk.client.transport.rest.RestTransportConfigBuilder;
import org.a2aproject.sdk.server.agentexecution.AgentExecutor;
import org.a2aproject.sdk.server.agentexecution.RequestContext;
import org.a2aproject.sdk.server.tasks.AgentEmitter;
import org.a2aproject.sdk.spec.A2AError;
import org.a2aproject.sdk.spec.AgentCard;
import org.a2aproject.sdk.spec.FileWithBytes;
import org.a2aproject.sdk.spec.FilePart;
import org.a2aproject.sdk.spec.Message;
import org.a2aproject.sdk.spec.Part;
import org.a2aproject.sdk.spec.TaskPushNotificationConfig;
import org.a2aproject.sdk.spec.TaskState;
import org.a2aproject.sdk.spec.TextPart;

import io.grpc.ManagedChannelBuilder;

import itk.InstructionOuterClass.CallAgent;
import itk.InstructionOuterClass.Instruction;

import org.slf4j.Logger;
import org.slf4j.LoggerFactory;

@ApplicationScoped
public class AgentExecutorProducer {

    private static final Logger log = LoggerFactory.getLogger(AgentExecutorProducer.class);

    @Produces
    public AgentExecutor agentExecutor() {
        return new V10AgentExecutor();
    }

    static class V10AgentExecutor implements AgentExecutor {

        private static final int HOLD_ITERATIONS = 5;
        private static final long HOLD_INTERVAL_MS = 2000;
        private static final long TASK_TIMEOUT_SECONDS = 60;

        @Override
        public void execute(RequestContext context, AgentEmitter emitter) throws A2AError {
            log.info("Executing task {}", emitter.getTaskId());

            emitter.startWork();

            Instruction instruction = extractInstruction(context.getMessage());
            if (instruction == null) {
                log.error("No valid instruction found in request");
                emitter.sendMessage("No valid instruction found in request");
                emitter.fail();
                return;
            }

            try {
                List<String> results = handleInstruction(instruction);
                String response = String.join("\n", results);
                log.info("Response: {}", response);

                if (shouldHold(instruction)) {
                    log.info("Holding task {} as requested", emitter.getTaskId());

                    Message holdMsg = emitter.newAgentMessage(
                            List.of(new TextPart(response + "\ntask-finished")), null);
                    emitter.updateStatus(TaskState.TASK_STATE_WORKING, holdMsg);

                    for (int i = 0; i < HOLD_ITERATIONS; i++) {
                        log.info("Emitting periodic status update for held task {}", emitter.getTaskId());
                        try {
                            Thread.sleep(HOLD_INTERVAL_MS);
                        } catch (InterruptedException e) {
                            Thread.currentThread().interrupt();
                            log.info("Task {} interrupted during hold", emitter.getTaskId());
                            return;
                        }
                    }
                    log.info("Held task {} timed out, auto-completing", emitter.getTaskId());
                    Message completeMsg = emitter.newAgentMessage(
                            List.of(new TextPart(response + "\ntask-finished")), null);
                    emitter.complete(completeMsg);
                } else {
                    Message completeMsg = emitter.newAgentMessage(
                            List.of(new TextPart(response)), null);
                    emitter.complete(completeMsg);
                }
            } catch (TimeoutException e) {
                log.error("Timed out waiting for remote agent response", e);
                emitter.fail();
            } catch (InterruptedException e) {
                Thread.currentThread().interrupt();
                log.error("Task {} interrupted while calling remote agent", emitter.getTaskId(), e);
                emitter.fail();
            } catch (ExecutionException e) {
                log.error("Remote agent call failed for task {}: {}",
                        emitter.getTaskId(), e.getCause().getMessage(), e.getCause());
                emitter.fail();
            } catch (Exception e) {
                log.error("Unexpected error handling instruction for task {}",
                        emitter.getTaskId(), e);
                emitter.fail();
            }
        }

        @Override
        public void cancel(RequestContext context, AgentEmitter emitter) throws A2AError {
            log.info("Cancel requested for task {}", emitter.getTaskId());
            emitter.cancel();
        }

        private Instruction extractInstruction(Message message) {
            if (message == null || message.parts() == null) {
                return null;
            }

            for (Part<?> part : message.parts()) {
                if (part instanceof FilePart filePart) {
                    var file = filePart.file();
                    if ("application/x-protobuf".equals(file.mimeType())
                            || "instruction.bin".equals(file.name())) {
                        try {
                            if (file instanceof FileWithBytes fwb) {
                                byte[] raw = Base64.getDecoder().decode(fwb.bytes());
                                return Instruction.parseFrom(raw);
                            }
                        } catch (Exception e) {
                            log.debug("Failed to parse instruction from file part", e);
                        }
                    }
                }

                if (part instanceof TextPart textPart) {
                    try {
                        byte[] raw = Base64.getDecoder().decode(textPart.text());
                        return Instruction.parseFrom(raw);
                    } catch (Exception e) {
                        log.debug("Failed to parse instruction from text part", e);
                    }
                }
            }
            return null;
        }

        private List<String> handleInstruction(Instruction inst) throws Exception {
            if (inst.hasCallAgent()) {
                return handleCallAgent(inst.getCallAgent());
            }
            if (inst.hasReturnResponse()) {
                return List.of(inst.getReturnResponse().getResponse());
            }
            if (inst.hasSteps()) {
                List<String> allResults = new ArrayList<>();
                for (Instruction step : inst.getSteps().getInstructionsList()) {
                    allResults.addAll(handleInstruction(step));
                }
                return allResults;
            }
            throw new IllegalStateException("Unknown instruction type");
        }

        private List<String> handleCallAgent(CallAgent call) throws Exception {
            log.info("Calling agent {} via {}", call.getAgentCardUri(), call.getTransport());
            AgentCard remoteCard = A2A.getAgentCard(call.getAgentCardUri());

            ClientConfig.Builder configBuilder = new ClientConfig.Builder()
                    .setStreaming(call.getStreaming() || isGrpc(call.getTransport()))
                    .setUseClientPreference(true);

            if (call.hasPushNotification()) {
                String url = call.getPushNotification().getUrl();
                if (url.isEmpty()) {
                    throw new IllegalArgumentException("URL not specified in push_notification behavior");
                }
                if (!url.startsWith("http://") && !url.startsWith("https://")) {
                    url = "http://" + url;
                }
                configBuilder.setTaskPushNotificationConfig(
                        TaskPushNotificationConfig.builder()
                                .id(UUID.randomUUID().toString())
                                .url(url + "/notifications")
                                .token("itk-token")
                                .build());
            }

            var clientBuilder = Client.builder(remoteCard)
                    .clientConfig(configBuilder.build());

            addTransport(clientBuilder, call.getTransport());

            byte[] instBytes = call.getInstruction().toByteArray();
            Message wrappedMsg = Message.builder()
                    .role(Message.Role.ROLE_USER)
                    .parts(List.of(new FilePart(
                            new FileWithBytes("application/x-protobuf", "instruction.bin", instBytes))))
                    .build();

            CompletableFuture<List<String>> resultFuture = new CompletableFuture<>();
            List<String> responses = Collections.synchronizedList(new ArrayList<>());

            clientBuilder.addConsumer((event, card) -> {
                List<String> texts = extractTextFromEvent(event);
                if (call.hasResubscribe()) {
                    String finished = findTaskFinishedText(texts);
                    if (finished != null) {
                        responses.add(finished);
                        if (!resultFuture.isDone()) {
                            resultFuture.complete(responses);
                        }
                        return;
                    }
                }
                responses.addAll(texts);
                if (!resultFuture.isDone() && isTerminalEvent(event)) {
                    resultFuture.complete(responses);
                }
            });
            clientBuilder.streamingErrorHandler(error -> {
                log.error("Streaming error calling {}", call.getAgentCardUri(), error);
                if (!resultFuture.isDone()) {
                    resultFuture.completeExceptionally(error);
                }
            });

            try (Client client = clientBuilder.build()) {
                client.sendMessage(wrappedMsg, (TaskPushNotificationConfig) null, null, null);
                log.info("Received responses from {}", call.getAgentCardUri());
                return resultFuture.get(TASK_TIMEOUT_SECONDS, TimeUnit.SECONDS);
            }
        }

        private List<String> extractTextFromMessage(Message message) {
            List<String> texts = new ArrayList<>();
            if (message != null && message.parts() != null) {
                for (Part<?> part : message.parts()) {
                    if (part instanceof TextPart tp && tp.text() != null && !tp.text().isEmpty()) {
                        texts.add(tp.text());
                    }
                }
            }
            return texts;
        }

        private List<String> extractTextFromEvent(ClientEvent event) {
            Message message = null;

            if (event instanceof MessageEvent me) {
                message = me.getMessage();
            } else if (event instanceof TaskEvent te) {
                if (te.getTask().status() != null && te.getTask().status().message() != null) {
                    message = te.getTask().status().message();
                }
            } else if (event instanceof TaskUpdateEvent tue) {
                if (tue.getTask().status() != null && tue.getTask().status().message() != null) {
                    message = tue.getTask().status().message();
                }
            }

            return extractTextFromMessage(message);
        }

        private String findTaskFinishedText(List<String> texts) {
            for (String text : texts) {
                if (text.contains("task-finished")) {
                    return text.replace("task-finished", "");
                }
            }
            return null;
        }

        private boolean shouldHold(Instruction inst) {
            if (inst.hasReturnResponse() && inst.getReturnResponse().getHoldTask()) {
                return true;
            }
            if (inst.hasSteps()) {
                for (Instruction step : inst.getSteps().getInstructionsList()) {
                    if (shouldHold(step)) {
                        return true;
                    }
                }
            }
            return false;
        }

        private boolean isTerminalEvent(ClientEvent event) {
            if (event instanceof MessageEvent) {
                return true;
            }
            if (event instanceof TaskEvent te) {
                return te.getTask().status() != null && te.getTask().status().state().isFinal();
            }
            if (event instanceof TaskUpdateEvent tue) {
                return tue.getTask().status() != null && tue.getTask().status().state().isFinal();
            }
            return false;
        }

        private boolean isGrpc(String transport) {
            return "GRPC".equalsIgnoreCase(transport);
        }

        @SuppressWarnings("unchecked")
        private void addTransport(org.a2aproject.sdk.client.ClientBuilder builder, String transport) {
            switch (transport.toUpperCase()) {
                case "GRPC" -> builder.withTransport(GrpcTransport.class,
                        new GrpcTransportConfigBuilder()
                                .channelFactory(url -> ManagedChannelBuilder.forTarget(url)
                                        .usePlaintext()
                                        .build()));
                case "REST", "HTTP_JSON", "HTTP+JSON" -> builder.withTransport(
                        RestTransport.class, new RestTransportConfigBuilder());
                default -> builder.withTransport(JSONRPCTransport.class,
                        new JSONRPCTransportConfigBuilder());
            }
        }
    }
}
