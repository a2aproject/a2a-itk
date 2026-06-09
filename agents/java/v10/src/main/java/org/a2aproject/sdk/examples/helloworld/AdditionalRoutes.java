package org.a2aproject.sdk.examples.helloworld;

import io.vertx.ext.web.Router;
import jakarta.enterprise.context.ApplicationScoped;
import jakarta.enterprise.event.Observes;
import org.slf4j.Logger;
import org.slf4j.LoggerFactory;

@ApplicationScoped
public class AdditionalRoutes {

    private static final Logger log = LoggerFactory.getLogger(AdditionalRoutes.class);

    void addPrefixedRoutes(@Observes Router router) {
        router.route("/jsonrpc/*").handler(ctx -> {
            log.info("Rerouting /jsonrpc -> /");
            ctx.reroute("/");
        });
        router.route("/rest/*").handler(ctx -> {
            log.info("Rerouting POST /rest -> /");
            ctx.reroute("/");
        });

        router.route().order(Integer.MIN_VALUE)
                .handler(ctx -> {
                    String method = ctx.request().method().name();
                    String path = ctx.request().path();
                    String contentType = ctx.request().getHeader("Content-Type");
                    log.info("Incoming {} {} Content-Type={}", method, path, contentType);
                    if ("POST".equals(method)) {
                        ctx.request().body().onSuccess(buffer -> {
                            if (buffer != null) {
                                log.debug("POST {} payload: {}", path, buffer.toString());
                            }
                        });
                    }
                    ctx.next();
                });
    }
}
