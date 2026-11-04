{{/* The chart name, possibly overridden. */}}
{{- define "mcp-trino.name" -}}
{{- default .Chart.Name .Values.nameOverride | trunc 63 | trimSuffix "-" }}
{{- end }}

{{/* The full name of the release's objects. */}}
{{- define "mcp-trino.fullname" -}}
{{- if .Values.fullnameOverride }}
{{- .Values.fullnameOverride | trunc 63 | trimSuffix "-" }}
{{- else }}
{{- $name := default .Chart.Name .Values.nameOverride }}
{{- if contains $name .Release.Name }}
{{- .Release.Name | trunc 63 | trimSuffix "-" }}
{{- else }}
{{- printf "%s-%s" .Release.Name $name | trunc 63 | trimSuffix "-" }}
{{- end }}
{{- end }}
{{- end }}

{{/* The health Service's name, within the 63 characters a Service name may have. */}}
{{- define "mcp-trino.healthName" -}}
{{- printf "%s-health" (include "mcp-trino.fullname" . | trunc 56 | trimSuffix "-") }}
{{- end }}

{{- define "mcp-trino.chart" -}}
{{- printf "%s-%s" .Chart.Name .Chart.Version | replace "+" "_" | trunc 63 | trimSuffix "-" }}
{{- end }}

{{- define "mcp-trino.selectorLabels" -}}
app.kubernetes.io/name: {{ include "mcp-trino.name" . }}
app.kubernetes.io/instance: {{ .Release.Name }}
{{- end }}

{{- define "mcp-trino.labels" -}}
helm.sh/chart: {{ include "mcp-trino.chart" . }}
{{ include "mcp-trino.selectorLabels" . }}
app.kubernetes.io/version: {{ .Chart.AppVersion | quote }}
app.kubernetes.io/managed-by: {{ .Release.Service }}
{{- end }}

{{/* The server's pods: the release's selector labels and the server component. */}}
{{- define "mcp-trino.serverSelectorLabels" -}}
{{ include "mcp-trino.selectorLabels" . }}
app.kubernetes.io/component: server
{{- end }}

{{- define "mcp-trino.serviceAccountName" -}}
{{- if .Values.serviceAccount.create }}
{{- default (include "mcp-trino.fullname" .) .Values.serviceAccount.name }}
{{- else }}
{{- default "default" .Values.serviceAccount.name }}
{{- end }}
{{- end }}

{{- define "mcp-trino.image" -}}
{{- $repo := .Values.image.repository }}
{{- if .Values.image.digest }}
{{- printf "%s@%s" $repo .Values.image.digest }}
{{- else }}
{{- printf "%s:%s" $repo (default .Chart.AppVersion .Values.image.tag) }}
{{- end }}
{{- end }}

{{/* True when rendering for OpenShift: set in the values, or served by the cluster. */}}
{{- define "mcp-trino.openshift" -}}
{{- if or .Values.openshift (.Capabilities.APIVersions.Has "security.openshift.io/v1") }}true{{- end }}
{{- end }}

{{/* The transport's MCP endpoint. */}}
{{- define "mcp-trino.endpoint" -}}
{{- ternary "/sse" "/mcp" (eq .Values.transport "sse") }}
{{- end }}

{{/*
The variables the chart manages, and the value that sets each: refused in extraEnv, where
they would silently override the render-time checks.
*/}}
{{- define "mcp-trino.managedEnv" -}}
{{- toJson (dict
  "MCP_TRANSPORT" "transport" "MCP_PORT" "ports.mcp" "MCP_HEALTH_PORT" "ports.health"
  "MCP_SERVER_NAME" "server.name" "MCP_TOOL_PLUGINS" "server.toolPlugins" "MCP_EXTENSIONS" "server.extensions"
  "TRINO_HOST" "trino.host" "TRINO_PORT" "trino.port" "TRINO_HTTP_SCHEME" "trino.httpScheme"
  "TRINO_CATALOG" "trino.catalog" "TRINO_IDENTITY_MODE" "trino.identityMode" "TRINO_READ_ONLY" "trino.readOnly"
  "TRINO_VERIFY" "trino.verify and ca" "TRINO_SOURCE" "trino.source" "TRINO_USER" "trino.user"
  "TRINO_PASSWORD" "trino.credential and existingSecret" "TRINO_CLIENT_CERT" "trino.clientCertificate"
  "TRINO_CLIENT_KEY" "trino.clientCertificate" "TRINO_MAX_ROWS" "trino.maxRows"
  "TRINO_QUERY_TIMEOUT_SECONDS" "trino.queryTimeoutSeconds" "TRINO_REQUEST_TIMEOUT_SECONDS" "trino.requestTimeoutSeconds"
  "TRINO_TOKEN_EXCHANGE_URL" "trino.tokenExchange.url" "TRINO_TOKEN_EXCHANGE_CLIENT_ID" "trino.tokenExchange.clientId"
  "TRINO_TOKEN_EXCHANGE_CLIENT_SECRET" "existingSecret" "TRINO_TOKEN_EXCHANGE_AUDIENCE" "trino.tokenExchange.audience"
  "TRINO_TOKEN_EXCHANGE_SCOPE" "trino.tokenExchange.scope" "TRINO_TOKEN_EXCHANGE_VERIFY" "trino.tokenExchange.verify and ca"
  "MCP_AUTH_ENABLED" "auth.enabled" "MCP_AUTH_REQUIRED" "auth.required" "MCP_JWKS_URL" "auth.jwksUrl"
  "MCP_OIDC_ISSUER" "auth.issuer" "MCP_OIDC_AUDIENCE" "auth.audience" "MCP_RESOURCE_URL" "auth.resourceUrl"
  "MCP_JWT_USER_CLAIM" "auth.userClaim" "MCP_JWT_ALGORITHMS" "auth.algorithms" "MCP_JWT_LEEWAY_SECONDS" "auth.leewaySeconds"
  "MCP_ALLOWED_HOSTS" "auth.allowedHosts" "MCP_EXTRA_READ_COMMANDS" "extraReadCommands"
  "MCP_CALL_TIMEOUT_SECONDS" "limits.callTimeoutSeconds" "MCP_MAX_CATALOGS" "limits.maxCatalogs"
  "MCP_DESCRIBE_SCHEMA_TABLES" "limits.describeSchemaTables" "MCP_DESCRIBE_SCHEMA_COLUMNS" "limits.describeSchemaColumns"
  "MCP_SEARCH_MIN_LENGTH" "limits.searchMinLength" "MCP_SEARCH_MAX_WORDS" "limits.searchMaxWords"
  "MCP_SEARCH_MAX_WORD_LENGTH" "limits.searchMaxWordLength" "MCP_SEARCH_RESERVE_SECONDS" "limits.searchReserveSeconds"
  "MCP_USER_TOKEN" "nothing: stdio only" "SSL_CERT_FILE" "ca and ca.setSslCertFile" "PYTHONDONTWRITEBYTECODE" "nothing: always set"
) }}
{{- end }}

{{/* The paths an Ingress host routes by default. */}}
{{- define "mcp-trino.defaultPaths" -}}
{{- $paths := list (dict "path" (include "mcp-trino.endpoint" .) "pathType" "Prefix") }}
{{- if .Values.auth.resourceUrl }}
{{- $paths = append $paths (dict "path" "/.well-known/oauth-protected-resource" "pathType" "Prefix") }}
{{- end }}
{{- if eq .Values.transport "sse" }}
{{- $paths = append $paths (dict "path" "/messages/" "pathType" "Prefix") }}
{{- end }}
{{- toJson $paths }}
{{- end }}

{{/* Mount paths. */}}
{{- define "mcp-trino.caPath" -}}
/etc/mcp-trino/ca/{{ .Values.ca.key }}
{{- end }}
{{- define "mcp-trino.caEnabled" -}}
{{- if or .Values.ca.configMap .Values.ca.secret }}true{{- end }}
{{- end }}
{{- define "mcp-trino.clientCertEnabled" -}}
{{- if and (has .Values.trino.identityMode (list "impersonation" "service")) (eq .Values.trino.credential "clientCertificate") }}true{{- end }}
{{- end }}
{{- define "mcp-trino.passwordEnabled" -}}
{{- if and (has .Values.trino.identityMode (list "impersonation" "service")) (eq .Values.trino.credential "password") }}true{{- end }}
{{- end }}

{{/* The pod security context, with the user and group left to the SCC on OpenShift. */}}
{{- define "mcp-trino.podSecurityContext" -}}
{{- $ctx := deepCopy (.Values.podSecurityContext | default dict) }}
{{- if include "mcp-trino.openshift" . }}
{{- $ctx = omit $ctx "runAsUser" "runAsGroup" "fsGroup" }}
{{- end }}
{{- toYaml $ctx }}
{{- end }}

{{/*
The environment shared by the server and the test pod: the ConfigMap, the secrets by
secretKeyRef (the server reads no *_FILE variable), and the extra entries.
*/}}
{{- define "mcp-trino.envFrom" -}}
{{- /* The chart's ConfigMap comes last: on a duplicate key, the last source wins. */}}
{{- with .Values.extraEnvFrom }}
{{ toYaml . }}
{{- end }}
- configMapRef:
    name: {{ include "mcp-trino.fullname" . }}
{{- end }}

{{- define "mcp-trino.env" -}}
{{- if eq .Values.trino.identityMode "jwt" }}
- name: TRINO_TOKEN_EXCHANGE_CLIENT_SECRET
  valueFrom:
    secretKeyRef:
      name: {{ .Values.existingSecret.name | quote }}
      key: {{ .Values.existingSecret.keys.tokenExchangeClientSecret | quote }}
{{- end }}
{{- if include "mcp-trino.passwordEnabled" . }}
- name: TRINO_PASSWORD
  valueFrom:
    secretKeyRef:
      name: {{ .Values.existingSecret.name | quote }}
      key: {{ .Values.existingSecret.keys.trinoPassword | quote }}
{{- end }}
{{- with .Values.extraEnv }}
{{ toYaml . }}
{{- end }}
{{- end }}

{{- define "mcp-trino.volumeMounts" -}}
- name: tmp
  mountPath: /tmp
{{- if include "mcp-trino.caEnabled" . }}
- name: ca
  mountPath: /etc/mcp-trino/ca
  readOnly: true
{{- end }}
{{- if include "mcp-trino.clientCertEnabled" . }}
- name: client-certificate
  mountPath: /etc/mcp-trino/client
  readOnly: true
{{- end }}
{{- with .Values.extraVolumeMounts }}
{{ toYaml . }}
{{- end }}
{{- end }}

{{- define "mcp-trino.volumes" -}}
- name: tmp
  emptyDir:
    sizeLimit: 64Mi
{{- if .Values.ca.configMap }}
- name: ca
  configMap:
    name: {{ .Values.ca.configMap | quote }}
    items:
      - key: {{ .Values.ca.key | quote }}
        path: {{ .Values.ca.key | quote }}
{{- else if .Values.ca.secret }}
- name: ca
  secret:
    secretName: {{ .Values.ca.secret | quote }}
    items:
      - key: {{ .Values.ca.key | quote }}
        path: {{ .Values.ca.key | quote }}
{{- end }}
{{- if include "mcp-trino.clientCertEnabled" . }}
{{- $cc := .Values.trino.clientCertificate }}
- name: client-certificate
  secret:
    secretName: {{ $cc.secretName | quote }}
    defaultMode: 0440
    items:
      - key: {{ $cc.certKey | quote }}
        path: tls.crt
      - key: {{ $cc.keyKey | quote }}
        path: tls.key
{{- end }}
{{- with .Values.extraVolumes }}
{{ toYaml . }}
{{- end }}
{{- end }}
