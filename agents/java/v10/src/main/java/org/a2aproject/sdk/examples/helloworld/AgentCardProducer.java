package org.a2aproject.sdk.examples.helloworld;

import org.a2aproject.sdk.server.PublicAgentCard;
import org.a2aproject.sdk.spec.AgentCapabilities;
import org.a2aproject.sdk.spec.AgentCard;
import org.a2aproject.sdk.spec.AgentInterface;
import org.a2aproject.sdk.spec.AgentSkill;
import jakarta.enterprise.context.ApplicationScoped;
import jakarta.enterprise.inject.Produces;
import org.eclipse.microprofile.config.inject.ConfigProperty;

import java.util.List;

@ApplicationScoped
public class AgentCardProducer {

    @ConfigProperty(name = "quarkus.http.port", defaultValue = "10102")
    int httpPort;

    @ConfigProperty(name = "quarkus.grpc.server.port", defaultValue = "11002")
    int grpcPort;

    @Produces
    @PublicAgentCard
    public AgentCard agentCard() {
        return AgentCard.builder()
                .name("ITK v10 Agent")
                .description("Java agent using A2A SDK 1.0.")
                .version("1.0.0")
                .supportedInterfaces(List.of(
                        new AgentInterface("JSONRPC", "http://127.0.0.1:" + httpPort),
                        new AgentInterface("HTTP+JSON", "http://127.0.0.1:" + httpPort),
                        new AgentInterface("GRPC", "127.0.0.1:" + grpcPort)
                ))
                .capabilities(AgentCapabilities.builder()
                        .streaming(true)
                        .pushNotifications(true)
                        .build())
                .defaultInputModes(List.of("text"))
                .defaultOutputModes(List.of("text"))
                .skills(List.of(AgentSkill.builder()
                        .id("itk_v10")
                        .name("ITK v10")
                        .description("Processes ITK instruction traversals")
                        .tags(List.of("itk"))
                        .examples(List.of())
                        .build()))
                .build();
    }
}
