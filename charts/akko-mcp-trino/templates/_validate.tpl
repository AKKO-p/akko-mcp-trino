{{/*
Settings the server would refuse at start, or that would leave something open, refused at
render time with a message that names the value to change.
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
{{- fail "trino.catalog is required: a data catalog where names without a catalog are read (never system)." }}
{{- end }}
{{- if eq (lower $v.trino.catalog) "system" }}
{{- fail "trino.catalog=system: set a data catalog; with system, names without a catalog are refused." }}
{{- end }}
{{- if and (ne $mode "service") (ne $v.trino.httpScheme "https") }}
{{- fail (printf "trino.identityMode=%s requires trino.httpScheme=https: a token or a credential never travels over http." $mode) }}
{{- end }}

{{- /* The door. */}}
{{- if and $v.auth.required (not $v.auth.enabled) }}
{{- fail "auth.required=true with auth.enabled=false: a door that requires a token it cannot verify. Enable auth, or turn both off with auth.acknowledgeNoAuth=true (service mode only)." }}
{{- end }}
{{- if not (and $v.auth.enabled $v.auth.required) }}
{{- if ne $mode "service" }}
{{- fail (printf "trino.identityMode=%s requires auth.enabled=true and auth.required=true: the caller is known only from a verified token." $mode) }}
{{- end }}
{{- if not $v.auth.acknowledgeNoAuth }}
{{- fail "auth.enabled or auth.required is false: every caller would reach Trino without a verified token. Set auth.acknowledgeNoAuth=true to render it anyway (a local test only)." }}
{{- end }}
{{- end }}
{{- if $v.auth.enabled }}
{{- if not $v.auth.jwksUrl }}
{{- fail "auth.jwksUrl is required when auth is enabled: the identity provider's JWKS endpoint." }}
{{- end }}
{{- if and (not (hasPrefix "https://" $v.auth.jwksUrl)) (not $v.auth.acknowledgeInsecureJwks) }}
{{- fail (printf "auth.jwksUrl=%s is not https: the JWKS is the door's trust anchor, and over http anyone on the path could substitute keys. Use https, or set auth.acknowledgeInsecureJwks=true (a test only)." $v.auth.jwksUrl) }}
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
{{- if and (ne $v.service.type "ClusterIP") (not $v.service.acknowledgePlainHttp) }}
{{- fail (printf "service.type=%s publishes the MCP port in plain HTTP outside the cluster, bearer tokens in clear. Use an Ingress or a Route with TLS, or set service.acknowledgePlainHttp=true when a TLS-terminating load balancer is in front." $v.service.type) }}
{{- end }}
{{- if and $v.tests.enabled (not $v.service.exposeHealthPort) }}
{{- fail "tests.enabled=true calls the health Service: keep service.exposeHealthPort=true, or set tests.enabled=false." }}
{{- end }}
{{- $managed := include "mcp-trino.managedEnv" . | fromJson }}
{{- range $v.extraEnv }}
{{- if hasKey $managed (.name | default "") }}
{{- fail (printf "extraEnv sets %s, which the chart manages: set %s instead, so that the render-time checks apply." .name (get $managed .name)) }}
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
{{- $min := ternary 1 $pdb.minAvailable (and (kindIs "invalid" $pdb.minAvailable) (kindIs "invalid" $pdb.maxUnavailable)) }}
{{- if and (not (kindIs "invalid" $min)) (not (kindIs "string" $min)) (ge (int $min) (int $v.replicaCount)) }}
{{- fail (printf "podDisruptionBudget.minAvailable=%v with replicaCount=%v blocks every node drain: raise replicaCount, lower minAvailable or set maxUnavailable." $min $v.replicaCount) }}
{{- end }}
{{- end }}
{{- end }}
