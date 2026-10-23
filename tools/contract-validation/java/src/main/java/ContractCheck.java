import com.fasterxml.jackson.core.JsonParser;
import com.fasterxml.jackson.databind.JsonNode;
import com.fasterxml.jackson.databind.ObjectMapper;
import com.networknt.schema.*;
import java.nio.file.*;
import java.time.Instant;
import java.util.*;

/** 契约规范的独立 Java 参考校验器，不接入生产服务。 */
public final class ContractCheck {
    private static final ObjectMapper JSON = new ObjectMapper().enable(JsonParser.Feature.STRICT_DUPLICATE_DETECTION);
    private static JsonNode read(Path p) throws Exception { return JSON.readTree(p.toFile()); }
    private static String s(JsonNode n, String key) { return n.required(key).asText(); }
    private static boolean eq(JsonNode a, JsonNode b, String k) { return a.required(k).equals(b.required(k)); }
    private static boolean has(JsonNode array, String value) { for (JsonNode v : array) if (v.asText().equals(value)) return true; return false; }
    private static boolean expired(String now, String expiry) { return !Instant.parse(now).isBefore(Instant.parse(expiry)); }
    private static void localReferences(JsonNode n) {
        if (n.isObject() && n.has("$ref") && !s(n, "$ref").startsWith("#/$defs/")) throw new IllegalArgumentException("Nonlocal reference");
        for (JsonNode child : n) localReferences(child);
    }
    private static String semantic(String rule, JsonNode x, JsonNode policy) {
        switch (rule) {
            case "transition": {
                if (!s(policy.required("owners"), s(x,"kind")).equals(s(x,"writer"))) return "FORBIDDEN";
                if (!x.required("revision").equals(x.required("expected_revision"))) return "REVISION_CONFLICT";
                JsonNode allowed = policy.required("transitions").required(s(x,"before"));
                if (allowed.isEmpty()) return "ALREADY_TERMINAL";
                if (s(x,"effect_status").equals("unknown") && Set.of("COMPLETED","FAILED","CANCELED","REJECTED").contains(s(x,"after"))) return "RECONCILE";
                return has(allowed, s(x,"after")) ? "APPLY" : "INVALID_TRANSITION";
            }
            case "idempotency": {
                JsonNode old = x.required("stored"), next = x.required("incoming");
                for (String k : List.of("principal","caller","kind","key")) if (!eq(old,next,k)) return "NEW";
                return eq(old,next,"hash") ? "DUPLICATE" : "IDEMPOTENCY_CONFLICT";
            }
            case "projection": {
                JsonNode e = x.required("event"), snap = e.required("snapshot");
                if (!s(e,"principal_id").equals(s(x,"expected_principal")) || !s(snap,"principal_id").equals(s(x,"expected_principal"))) return "FORBIDDEN";
                if (!s(e,"source").equals(s(snap,"owner")) || !s(e,"source_task_id").equals(s(snap,"task_id"))
                    || !e.required("source_revision").equals(snap.required("revision"))
                    || !s(e,"platform_task_id").equals(s(x,"expected_platform_task"))
                    || !s(snap,"platform_task_id").equals(s(x,"expected_platform_task"))
                    || !s(snap,"run_id").equals(s(x,"root_run_id"))
                    || (s(snap,"task_kind").equals("runtime") && !s(snap,"task_id").equals(s(x,"root_run_id")))) return "INVALID_BINDING";
                String key = s(e,"source") + "/" + s(e,"source_task_id");
                if (has(x.required("seen_event_ids"), s(e,"source") + "/" + s(e,"event_id"))
                    || e.required("source_revision").asLong() <= x.required("source_revisions").path(key).asLong(0)) return "IGNORE";
                return s(snap,"task_kind").equals("runtime") ? "APPLY_ROOT" : "APPLY_CHILD";
            }
            case "input": {
                JsonNode req = x.required("request"), response = x.required("response"), consumed = x.required("consumed_response");
                if (!s(req,"principal_id").equals(s(x,"principal"))) return "FORBIDDEN";
                if (!consumed.isNull()) return consumed.equals(response) ? "DUPLICATE" : "INPUT_CONFLICT";
                if (!eq(req,response,"input_request_id") || !eq(req,response,"expected_revision")
                    || !req.required("expected_revision").equals(x.required("current_revision")) || !eq(req,response,"response_type")
                    || expired(s(x,"now"),s(req,"expires_at"))) return "INPUT_CONFLICT";
                return "ACCEPT";
            }
            case "approval": {
                JsonNode p = x.required("proposal"), d = x.required("decision");
                if (!s(p,"principal_id").equals(s(x,"authenticated_principal")) || !s(d,"principal_id").equals(s(x,"authenticated_principal"))) return "FORBIDDEN";
                for (String k : List.of("proposal_id","proposal_revision","operation_id","payload_hash")) if (!eq(p,d,k)) return "APPROVAL_CONFLICT";
                if (!s(p,"executor").equals(s(x,"executor")) || !s(p,"audience").equals(s(x,"executor"))
                    || !s(p,"payload_hash").equals(s(x,"actual_payload_hash")) || !p.required("resources").equals(x.required("current_resources"))
                    || expired(s(x,"now"),s(p,"expires_at"))) return "APPROVAL_CONFLICT";
                for (JsonNode r : p.required("resources")) if (!eq(r,p,"principal_id")) return "APPROVAL_CONFLICT";
                if (s(d,"decision").equals("reject")) return "DENIED";
                return x.required("grant_active").asBoolean() ? "AUTHORIZED" : "APPROVAL_CONFLICT";
            }
            case "budget": {
                JsonNode p = x.required("parent"), c = x.required("child"), reserved = x.required("already_reserved");
                if (!eq(p,c,"principal_id")) return "FORBIDDEN";
                if (!s(c,"parent_reservation_id").equals(s(p,"reservation_id")) || !eq(c,p,"platform_task_id")
                    || Instant.parse(s(c,"deadline")).isAfter(Instant.parse(s(p,"deadline")))) return "BUDGET_EXHAUSTED";
                for (String k : List.of("max_calls","max_input_tokens","max_output_tokens"))
                    if (c.required(k).asLong() + reserved.required(k).asLong() > p.required(k).asLong()) return "BUDGET_EXHAUSTED";
                return "RESERVE";
            }
            default: throw new IllegalArgumentException("Unknown rule: " + rule);
        }
    }
    public static void main(String[] args) throws Exception {
        Path root = args.length == 0 ? Path.of("../../../docs/contracts") : Path.of(args[0]);
        var factory = JsonSchemaFactory.getInstance(SpecVersion.VersionFlag.V202012);
        var config = SchemaValidatorsConfig.builder().build();
        Map<String,JsonSchema> schemas = new TreeMap<>();
        Map<String,JsonNode> definitions = new HashMap<>();
        try (var paths = Files.newDirectoryStream(root, "*-v1.schema.json")) {
            for (Path path : paths) {
                JsonNode definition = read(path);
                localReferences(definition);
                var fields = definition.required("$defs").fields();
                while (fields.hasNext()) {
                    var field = fields.next();
                    JsonNode prior = definitions.putIfAbsent(field.getKey(), field.getValue());
                    if (prior != null && !prior.equals(field.getValue())) throw new IllegalArgumentException("Definition drift: " + field.getKey());
                }
                var meta = factory.getSchema(SchemaLocation.of("https://json-schema.org/draft/2020-12/schema"), config);
                if (!meta.validate(definition).isEmpty()) throw new IllegalArgumentException("Invalid schema: " + path);
                schemas.put(path.getFileName().toString(), factory.getSchema(definition, config));
            }
        }
        if (schemas.isEmpty()) throw new IllegalArgumentException("No schemas");
        JsonNode policy = read(root.resolve("ownership-and-transitions.json"));
        Set<String> seen = new HashSet<>(), covered = new HashSet<>();
        for (String category : List.of("schema","semantic")) {
            JsonNode cases = read(root.resolve("fixtures/" + category + "-cases.json"));
            if (!cases.isArray() || cases.isEmpty()) throw new IllegalArgumentException("Empty test suite");
            for (JsonNode c : cases) {
                String id = s(c,"id"), actual, expected;
                if (!seen.add(id)) throw new IllegalArgumentException("Duplicate case: " + id);
                if (category.equals("schema")) {
                    covered.add(s(c,"schema"));
                    if (!c.required("valid").isBoolean()) throw new IllegalArgumentException("valid must be boolean");
                    JsonSchema schema = Objects.requireNonNull(schemas.get(s(c,"schema")));
                    actual = Boolean.toString(schema.validate(c.required("input"), context -> context.getExecutionConfig().setFormatAssertionsEnabled(true)).isEmpty());
                    expected = s(c,"valid");
                } else {
                    actual = semantic(s(c,"rule"), c.required("input"), policy);
                    expected = s(c,"expected");
                }
                if (!actual.equals(expected)) throw new AssertionError(id + ": " + actual + " != " + expected);
                System.out.println(id + "=" + actual);
            }
        }
        if (!covered.equals(schemas.keySet())) throw new IllegalArgumentException("Uncovered schema");
        System.out.println("PASS schemas=" + schemas.size() + " cases=" + seen.size());
    }
}
