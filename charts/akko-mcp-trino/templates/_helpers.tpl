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
- configMapRef:
    name: {{ include "mcp-trino.fullname" . }}
{{- with .Values.extraEnvFrom }}
{{ toYaml . }}
{{- end }}
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
