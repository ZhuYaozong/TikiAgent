package com.tikiagent.spike;

import java.time.Duration;
import java.util.Map;
import java.util.Set;
import org.springframework.boot.SpringApplication;
import org.springframework.boot.autoconfigure.SpringBootApplication;
import org.springframework.context.annotation.Bean;
import org.springframework.security.config.annotation.web.builders.HttpSecurity;
import org.springframework.security.config.http.SessionCreationPolicy;
import org.springframework.security.oauth2.core.*;
import org.springframework.security.oauth2.jwt.*;
import org.springframework.security.oauth2.jose.jws.SignatureAlgorithm;
import org.springframework.security.core.annotation.AuthenticationPrincipal;
import org.springframework.security.web.SecurityFilterChain;
import org.springframework.web.bind.annotation.*;
import org.springframework.http.HttpStatus;
import org.springframework.web.server.ResponseStatusException;
import org.springframework.web.client.RestClient;
import org.springframework.util.LinkedMultiValueMap;
import org.springframework.http.MediaType;
import org.springframework.http.client.SimpleClientHttpRequestFactory;

/** 独立协议探针，只有虚构资源，不接入现有平台或领域数据库。 */
@SpringBootApplication
@RestController
public class IdentitySpike {
    public static final String ISSUER = "https://localhost:38443/realms/b03-spike";
    public static void main(String[] args) {
        SpringApplication app = new SpringApplication(IdentitySpike.class);
        app.setDefaultProperties(Map.of("server.address", "127.0.0.1", "server.port", "38444",
            "spring.main.banner-mode", "off", "logging.level.root", "WARN"));
        app.run(args);
    }
    @Bean
    JwtDecoder decoder() {
        NimbusJwtDecoder decoder = NimbusJwtDecoder.withJwkSetUri(ISSUER + "/protocol/openid-connect/certs")
            .jwsAlgorithm(SignatureAlgorithm.RS256).build();
        OAuth2TokenValidator<Jwt> binding = jwt -> {
            boolean valid = jwt.getAudience().contains("jobpilot")
                && "tiki".equals(jwt.getClaimAsString("azp"))
                && jwt.getSubject() != null && jwt.getExpiresAt() != null
                && jwt.getClaimAsString("scope") != null
                && java.util.Arrays.asList(jwt.getClaimAsString("scope").split(" ")).contains("resume.read");
            return valid ? OAuth2TokenValidatorResult.success() : OAuth2TokenValidatorResult.failure(
                new OAuth2Error("invalid_token", "身份或权限绑定不满足", null));
        };
        decoder.setJwtValidator(new DelegatingOAuth2TokenValidator<>(new JwtTimestampValidator(Duration.ZERO),
            new JwtIssuerValidator(ISSUER), binding));
        return decoder;
    }
    @Bean
    SecurityFilterChain security(HttpSecurity http) throws Exception {
        // 探针只接受 Bearer，不使用会话 Cookie；生产 BFF 的 CSRF 配置不由此推断。
        return http.csrf(csrf -> csrf.disable()).sessionManagement(s -> s.sessionCreationPolicy(SessionCreationPolicy.STATELESS))
            .authorizeHttpRequests(a -> a.requestMatchers("/health").permitAll().anyRequest().authenticated())
            .oauth2ResourceServer(o -> o.jwt(j -> {})).build();
    }
    @GetMapping("/health")
    Map<String,String> health() { return Map.of("status", "ready", "java", System.getProperty("java.version"),
        "springBoot", org.springframework.boot.SpringBootVersion.getVersion(),
        "springSecurity", org.springframework.security.core.SpringSecurityCoreVersion.getVersion()); }

    @GetMapping("/probe/wrong-issuer")
    Map<String,String> wrongIssuer(@AuthenticationPrincipal Jwt jwt) {
        if (new JwtIssuerValidator(ISSUER + "-wrong").validate(jwt).hasErrors())
            throw new ResponseStatusException(HttpStatus.UNAUTHORIZED);
        return Map.of("status", "unexpectedly-accepted");
    }

    @GetMapping("/resource/{owner}")
    Map<String,String> resource(@AuthenticationPrincipal Jwt jwt, @PathVariable String owner) {
        if (!jwt.getSubject().equals(owner)) throw new ResponseStatusException(HttpStatus.FORBIDDEN);
        return Map.of("subject", jwt.getSubject(), "actor", jwt.getClaimAsString("azp"), "audience", "jobpilot");
    }

    @GetMapping("/online-resource/{owner}")
    Map<String,String> onlineResource(@AuthenticationPrincipal Jwt jwt, @PathVariable String owner) {
        var identity = resource(jwt, owner);
        var form = new LinkedMultiValueMap<String,String>();
        form.add("client_id", "jobpilot");
        form.add("client_secret", "disposable-b03-client");
        form.add("token", jwt.getTokenValue());
        // 探针使用固定临时凭据；生产服务必须从受保护凭据存储注入。
        Map<?,?> result;
        try {
            var transport = new SimpleClientHttpRequestFactory();
            transport.setConnectTimeout(3000);
            transport.setReadTimeout(3000);
            result = RestClient.builder().requestFactory(transport).build().post().uri(ISSUER + "/protocol/openid-connect/token/introspect")
                .contentType(MediaType.APPLICATION_FORM_URLENCODED).body(form).retrieve().body(Map.class);
        } catch (Exception failure) {
            throw new ResponseStatusException(HttpStatus.SERVICE_UNAVAILABLE, "身份状态无法核对");
        }
        if (result == null || !Boolean.TRUE.equals(result.get("active")))
            throw new ResponseStatusException(HttpStatus.UNAUTHORIZED);
        return identity;
    }
}
