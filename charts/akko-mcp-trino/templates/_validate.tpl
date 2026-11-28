{{/*
Settings that cannot work, the ones the server would refuse at start included, refused at
render time with a message that names the value to change. A setting that works but leaves
something open renders, and NOTES.txt says what it leaves open (mcp-trino.warnings below).
*/}}
{{- define "mcp-trino.validate" -}}
{{- $v := .Values }}
{{- $mode := $v.trino.identityMode }}
{{- if eq $v.transport "stdio" }}
{{- fail "transport=stdio cannot be served by a pod: use streamable-http (the default) or sse." }}
{{- end }}
{{- if eq $mode "oauth2" }}
{{- fail "trino.identityMode=oauth2 is a native browser login over stdio only: use jwt (the default), impersonation or service." }}
{{- end }}
{{- if not $v.trino.host }}
{{- fail "trino.host is required: the Trino coordinator, such as trino.example.internal." }}
{{- end }}
{{- if not $v.trino.catalog }}
{{- fail "trino.catalog is required: a data catalog where names without a catalog are read." }}
{{- end }}
{{- if and (ne $mode "service") (ne $v.trino.httpScheme "https") }}
{{- fail (printf "trino.identityMode=%s requires trino.httpScheme=https: a token or a credential never travels over http." $mode) }}
{{- end }}

{{- /* The budget of profile_table's aggregate query, against the budgets set beside it. */}}
{{- $profile := $v.limits.profileTimeoutSeconds }}
{{- if and $profile (gt (float64 $profile) 0.0) }}
{{- with $v.trino.queryTimeoutSeconds }}
{{- if and (gt (float64 .) 0.0) (gt (float64 $profile) (float64 .)) }}
{{- fail (printf "limits.profileTimeoutSeconds=%v is past trino.queryTimeoutSeconds=%v: the server refuses it at start; set it lower, or raise trino.queryTimeoutSeconds." $profile .) }}
{{- end }}
{{- end }}
{{- with $v.limits.callTimeoutSeconds }}
{{- if and (gt (float64 .) 0.0) (gt (float64 $profile) (float64 .)) }}
{{- fail (printf "limits.profileTimeoutSeconds=%v is past limits.callTimeoutSeconds=%v: the server refuses it at start; set it lower, or raise limits.callTimeoutSeconds." $profile .) }}
{{- end }}
{{- end }}
{{- end }}

{{- /* The tools exposed: the server's own rules, at render time. */}}
{{- $sets := include "mcp-trino.toolsets" . | fromJson }}
{{- range ($v.server.toolsets | default list) }}
{{- if not (hasKey $sets .) }}
{{- fail (printf "server.toolsets names %q, which is no group of tools; the groups are discovery, profile, query." .) }}
{{- end }}
{{- end }}
{{- $builtin := list }}
{{- range (list "discovery" "profile" "query") }}{{- $builtin = concat $builtin (get $sets .) }}{{- end }}
{{- range $v.server.tools }}
{{- if not (has . $builtin) }}
{{- fail (printf "server.tools names %q, which is no tool of the server; the tools are %s (a plugin's tools are exposed unless server.excludeTools names them)." . (join ", " $builtin)) }}
{{- end }}
{{- end }}
{{- if and (not (include "mcp-trino.exposedTools" . | fromJsonArray)) (not $v.server.toolPlugins) (not $v.server.extensions) }}
{{- fail "server.toolsets, server.tools and server.excludeTools leave no tool exposed: name a group (discovery, profile, query) or a tool." }}
{{- end }}

{{- /* The door. */}}
{{- if and $v.auth.required (not $v.auth.enabled) }}
{{- fail "auth.required=true with auth.enabled=false: a door that requires a token it cannot verify, which the server refuses at start. Enable auth, or turn both off (service mode only)." }}
{{- end }}
{{- if not (and $v.auth.enabled $v.auth.required) }}
{{- if ne $mode "service" }}
{{- fail (printf "trino.identityMode=%s requires auth.enabled=true and auth.required=true: the caller is known only from a verified token." $mode) }}
{{- end }}
{{- end }}
{{- if $v.auth.enabled }}
{{- if not $v.auth.jwksUrl }}
{{- fail "auth.jwksUrl is required when auth is enabled: the identity provider's JWKS endpoint." }}
{{- end }}
{{- if not $v.auth.issuer }}
{{- fail "auth.issuer is required when auth is enabled: the issuer the identity provider puts in its tokens (iss). Empty, the server refuses to start." }}
{{- end }}
{{- if not (or $v.auth.audience $v.auth.resourceUrl) }}
{{- fail "auth.audience or auth.resourceUrl is required when auth is enabled: the audience of the tokens issued for this server." }}
{{- end }}
{{- end }}
{{- with $v.auth.resourceUrl }}
{{- if not $v.auth.enabled }}
{{- fail "auth.resourceUrl requires auth.enabled=true: protected resource metadata from a server that verifies no token." }}
{{- end }}
{{- $endpoint := include "mcp-trino.endpoint" $ }}
{{- $path := (urlParse .).path | trimSuffix "/" }}
{{- if ne $path $endpoint }}
{{- fail (printf "auth.resourceUrl has the path %q, but transport=%s serves %s: the published resource must be the URL hosts call." $path $v.transport $endpoint) }}
{{- end }}
{{- end }}

{{- /* The identity mode's credentials. */}}
{{- if eq $mode "jwt" }}
{{- $te := $v.trino.tokenExchange }}
{{- if not (hasPrefix "https://" ($te.url | default "")) }}
{{- fail "trino.identityMode=jwt requires trino.tokenExchange.url, the provider's token endpoint, over https." }}
{{- end }}
{{- if not $te.clientId }}
{{- fail "trino.identityMode=jwt requires trino.tokenExchange.clientId: this server's confidential client at the provider." }}
{{- end }}
{{- if not $te.audience }}
{{- fail "trino.identityMode=jwt requires trino.tokenExchange.audience: the audience Trino's required-audience names." }}
{{- end }}
{{- if has $te.audience (list $v.auth.audience $v.auth.resourceUrl) }}
{{- fail "trino.tokenExchange.audience must differ from the audience this server accepts (auth.audience, auth.resourceUrl): a token issued for Trino is refused at the door." }}
{{- end }}
{{- if not $v.existingSecret.name }}
{{- fail (printf "trino.identityMode=jwt requires existingSecret.name: a Secret whose key %q holds the token exchange client's secret." $v.existingSecret.keys.tokenExchangeClientSecret) }}
{{- end }}
{{- else }}
{{- if not $v.trino.user }}
{{- fail (printf "trino.identityMode=%s requires trino.user: the server's own Trino account." $mode) }}
{{- end }}
{{- $cred := $v.trino.credential }}
{{- if and (eq $mode "impersonation") (eq $cred "none") }}
{{- fail "trino.identityMode=impersonation requires trino.credential=password or clientCertificate: Trino must authenticate the account that impersonates." }}
{{- end }}
{{- if and (ne $cred "none") (ne $v.trino.httpScheme "https") }}
{{- fail (printf "trino.credential=%s requires trino.httpScheme=https." $cred) }}
{{- end }}
{{- if and (eq $cred "password") (not $v.existingSecret.name) }}
{{- fail (printf "trino.credential=password requires existingSecret.name: a Secret whose key %q holds the password." $v.existingSecret.keys.trinoPassword) }}
{{- end }}
{{- if and (eq $cred "clientCertificate") (not $v.trino.clientCertificate.secretName) }}
{{- fail "trino.credential=clientCertificate requires trino.clientCertificate.secretName: the Secret of the certificate and its key." }}
{{- end }}
{{- end }}
{{- if and $v.ca.configMap $v.ca.secret }}
{{- fail "ca.configMap and ca.secret are both set: name one." }}
{{- end }}

{{- /* Exposure. */}}
{{- if $v.route.enabled }}
{{- if eq $v.route.tls.termination "reencrypt" }}
{{- if or (not $v.route.targetPort) (eq (toString $v.route.targetPort) "mcp") (eq (toString $v.route.targetPort) (toString $v.ports.mcp)) }}
{{- fail "route.tls.termination=reencrypt: the server listens in plain HTTP. Add a TLS listener (extraContainers), list its port in service.extraPorts and name it in route.targetPort, or use termination=edge." }}
{{- end }}
{{- end }}
{{- end }}
{{- if and $v.ingress.enabled (not $v.ingress.hosts) }}
{{- fail "ingress.enabled=true requires ingress.hosts." }}
{{- end }}
{{- if and $v.tests.enabled (not $v.service.exposeHealthPort) }}
{{- fail "tests.enabled=true calls the health Service: keep service.exposeHealthPort=true, or set tests.enabled=false." }}
{{- end }}
{{- /* A variable the chart already puts in env: a duplicate key the API server rejects. */}}
{{- $chartEnv := list }}
{{- if eq $mode "jwt" }}
{{- $chartEnv = append $chartEnv "TRINO_TOKEN_EXCHANGE_CLIENT_SECRET" }}
{{- end }}
{{- if include "mcp-trino.passwordEnabled" . }}
{{- $chartEnv = append $chartEnv "TRINO_PASSWORD" }}
{{- end }}
{{- range $v.extraEnv }}
{{- if has (.name | default "") $chartEnv }}
{{- fail (printf "extraEnv sets %s, which the chart already puts in the container's env from existingSecret: two entries of one name, which the API server rejects (duplicate entries for key). Set existingSecret.name and existingSecret.keys instead." .name) }}
{{- end }}
{{- end }}
{{- if $v.networkPolicy.enabled }}
{{- if and (or $v.ingress.enabled (and $v.route.enabled (not $v.networkPolicy.ingress.openshiftRouter))) (not $v.networkPolicy.ingress.from) }}
{{- fail "networkPolicy.enabled=true with an Ingress (or a Route without networkPolicy.ingress.openshiftRouter) requires networkPolicy.ingress.from: the ingress controller's peers; without them the MCP port admits no one." }}
{{- end }}
{{- if not $v.networkPolicy.egress.trino }}
{{- fail "networkPolicy.enabled=true requires networkPolicy.egress.trino: the peers of Trino (an ipBlock, or a namespace and pod selector)." }}
{{- end }}
{{- if and $v.auth.enabled (not $v.networkPolicy.egress.identityProvider.to) }}
{{- fail "networkPolicy.enabled=true with auth enabled requires networkPolicy.egress.identityProvider.to: the peers of the JWKS and token endpoints." }}
{{- end }}
{{- end }}
{{- if $v.podDisruptionBudget.enabled }}
{{- $pdb := $v.podDisruptionBudget }}
{{- if and (not (kindIs "invalid" $pdb.minAvailable)) (not (kindIs "invalid" $pdb.maxUnavailable)) }}
{{- fail "podDisruptionBudget: set minAvailable or maxUnavailable, not both." }}
{{- end }}
{{- end }}
{{- if $v.autoscaling.enabled }}
{{- $a := $v.autoscaling }}
{{- if gt (int $a.minReplicas) (int $a.maxReplicas) }}
{{- fail (printf "autoscaling.minReplicas=%v is above autoscaling.maxReplicas=%v." $a.minReplicas $a.maxReplicas) }}
{{- end }}
{{- if and (kindIs "invalid" $a.targetCPUUtilizationPercentage) (kindIs "invalid" $a.targetMemoryUtilizationPercentage) }}
{{- fail "autoscaling.enabled=true needs a target: autoscaling.targetCPUUtilizationPercentage or autoscaling.targetMemoryUtilizationPercentage." }}
{{- end }}
{{- $requests := (($v.resources | default dict).requests | default dict) }}
{{- if and (not (kindIs "invalid" $a.targetCPUUtilizationPercentage)) (not $requests.cpu) }}
{{- fail "autoscaling.targetCPUUtilizationPercentage is a share of resources.requests.cpu, which is not set: set it, or the HorizontalPodAutoscaler has nothing to measure against." }}
{{- end }}
{{- if and (not (kindIs "invalid" $a.targetMemoryUtilizationPercentage)) (not $requests.memory) }}
{{- fail "autoscaling.targetMemoryUtilizationPercentage is a share of resources.requests.memory, which is not set: set it, or the HorizontalPodAutoscaler has nothing to measure against." }}
{{- end }}
{{- end }}
{{- end }}

{{/*
Settings that work but leave something open, one line each: the risk and the safe setting.
NOTES.txt prints them at install and upgrade; the server says its own at start and in --check.
The acknowledge* values of earlier versions are accepted and have no effect.
*/}}
{{- define "mcp-trino.warnings" -}}
{{- $v := .Values }}
{{- if not (and $v.auth.enabled $v.auth.required) }}
WARNING: the door is open (auth.enabled={{ $v.auth.enabled }}, auth.required={{ $v.auth.required }}): every caller reaches Trino as {{ $v.trino.user }} without a verified token. Safe setting: auth.enabled=true and auth.required=true.
{{ end }}
{{- if and $v.auth.enabled (not (hasPrefix "https://" $v.auth.jwksUrl)) }}
WARNING: the JWKS is fetched over http ({{ $v.auth.jwksUrl }}): anyone on the path could substitute keys and mint accepted tokens. Safe setting: an https auth.jwksUrl.
{{ end }}
{{- $exposed := include "mcp-trino.exposedTools" . | fromJsonArray }}
{{- if and $exposed (not (has "list_tables" $exposed)) (not (has "describe_table" $exposed)) (not (has "search_columns" $exposed)) (not (has "list_catalogs" $exposed)) (not (has "list_schemas" $exposed)) (not (has "describe_schema" $exposed)) }}
WARNING: server.toolsets leaves out every discovery tool: the agent writes its queries without listing or describing the tables it may read. Safe setting: add discovery.
{{ end }}
{{- if ne $v.service.type "ClusterIP" }}
WARNING: service.type={{ $v.service.type }} serves the MCP port in plain HTTP outside the cluster, bearer tokens in clear. Safe setting: a ClusterIP Service behind an Ingress or a Route with TLS, or a TLS-terminating load balancer in front.
{{ end }}
{{- if and (not $v.trino.verify) (eq $v.trino.httpScheme "https") }}
WARNING: trino.verify=false: Trino's certificate is not verified, so the server cannot know whom it sends its credentials to. Safe setting: trino.verify=true, with ca.configMap or ca.secret for an internal certificate authority.
{{ end }}
{{- if and (eq $v.trino.identityMode "jwt") (not $v.trino.tokenExchange.verify) }}
WARNING: trino.tokenExchange.verify=false: the token endpoint's certificate is not verified, so the server cannot know whom it sends its client secret and the callers' tokens to. Safe setting: trino.tokenExchange.verify=true, with ca.configMap or ca.secret for an internal certificate authority.
{{ end }}
{{- if eq (lower $v.trino.catalog) "system" }}
WARNING: trino.catalog=system: names without a catalog are refused, so every query must name its catalog. Safe setting: a data catalog.
{{ end }}
{{- $managed := include "mcp-trino.managedEnv" . | fromJson }}
{{- range $v.extraEnv }}
{{- if hasKey $managed (.name | default "") }}
WARNING: extraEnv sets {{ .name }}, which the chart manages: it overrides the chart's value, out of reach of its render-time checks. Safe setting: {{ get $managed .name }}.
{{ end }}
{{- end }}
{{- if include "mcp-trino.multiReplica" . }}
{{- $replicas := ternary (printf "up to %v replicas (autoscaling)" $v.autoscaling.maxReplicas) (printf "replicaCount=%v" $v.replicaCount) $v.autoscaling.enabled }}
{{- $affinity := eq $v.service.sessionAffinity "ClientIP" }}
{{- if and (eq $v.transport "streamable-http") (not $v.server.statelessHttp) (not $affinity) }}
WARNING: {{ $replicas }} with server.statelessHttp=false and no affinity: a client of a 2025 protocol revision whose next request reaches another replica gets 404 "Session not found", and the official client does not open a new session. Safe setting: server.statelessHttp=true; or service.sessionAffinity=ClientIP, with a Route also route.annotations haproxy.router.openshift.io/balance=source (its cookie holds only for hosts that send it back).
{{ end }}
{{- if and (eq $v.transport "sse") (not $affinity) }}
WARNING: {{ $replicas }} with transport=sse and no affinity: an SSE session lives in one replica, and a message posted to another one is refused. Safe setting: transport=streamable-http (any replica answers); or service.sessionAffinity=ClientIP, with a Route also route.annotations haproxy.router.openshift.io/balance=source.
{{ end }}
{{- if not $v.podDisruptionBudget.enabled }}
WARNING: {{ $replicas }} without a disruption budget: a node drain may evict every replica at once. Safe setting: podDisruptionBudget.enabled=true with podDisruptionBudget.maxUnavailable=1.
{{ end }}
{{- end }}
{{- $needed := include "mcp-trino.neededGrace" . | int64 }}
{{- if lt (include "mcp-trino.gracePeriod" . | int64) $needed }}
WARNING: terminationGracePeriodSeconds={{ $v.terminationGracePeriodSeconds }} is below the {{ $needed }} s a clean stop needs (shutdown.delaySeconds, the shutdown timeout of {{ include "mcp-trino.shutdownTimeout" . }} s, one request to Trino, shutdown.marginSeconds): a call still running when the pod is killed leaves its query running in Trino. Safe setting: terminationGracePeriodSeconds=null (computed), or a lower shutdown.timeoutSeconds.
{{ end }}
{{- if $v.podDisruptionBudget.enabled }}
{{- $pdb := $v.podDisruptionBudget }}
{{- $min := ternary 1 $pdb.minAvailable (and (kindIs "invalid" $pdb.minAvailable) (kindIs "invalid" $pdb.maxUnavailable)) }}
{{- if and (not (kindIs "invalid" $min)) (not (kindIs "string" $min)) (ge (int $min) (int $v.replicaCount)) }}
WARNING: podDisruptionBudget.minAvailable={{ $min }} with replicaCount={{ $v.replicaCount }} blocks every node drain. Safe setting: a higher replicaCount, a lower minAvailable, or maxUnavailable.
{{ end }}
{{- end }}
{{- end }}
