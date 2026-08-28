{{/*
Expand the name of the chart.
*/}}
{{- define "preloop.name" -}}
{{- default .Chart.Name .Values.nameOverride | replace "." "-" | trunc 63 | trimSuffix "-" }}
{{- end }}

{{/*
Create a default fully qualified app name.
We truncate at 63 chars because some Kubernetes name fields are limited to this (by the DNS naming spec).
If release name contains chart name it will be used as a full name.
*/}}
{{- define "preloop.fullname" -}}
{{- if .Values.fullnameOverride }}
{{- .Values.fullnameOverride | replace "." "-" | trunc 63 | trimSuffix "-" }}
{{- else }}
{{- $name := default .Chart.Name .Values.nameOverride }}
{{- if contains $name .Release.Name }}
{{- .Release.Name | replace "." "-" | trunc 63 | trimSuffix "-" }}
{{- else }}
{{- printf "%s-%s" .Release.Name $name | replace "." "-" | trunc 63 | trimSuffix "-" }}
{{- end }}
{{- end }}
{{- end }}

{{/*
Create chart name and version as used by the chart label.
*/}}
{{- define "preloop.chart" -}}
{{- printf "%s-%s" .Chart.Name .Chart.Version | replace "+" "_" | trunc 63 | trimSuffix "-" }}
{{- end }}

{{/*
Common labels
*/}}
{{- define "preloop.labels" -}}
helm.sh/chart: {{ include "preloop.chart" . }}
{{ include "preloop.selectorLabels" . }}
{{- if .Chart.AppVersion }}
app.kubernetes.io/version: {{ .Chart.AppVersion | quote }}
{{- end }}
app.kubernetes.io/managed-by: {{ .Release.Service }}
{{- end }}

{{/*
Selector labels
*/}}
{{- define "preloop.selectorLabels" -}}
app.kubernetes.io/name: {{ include "preloop.name" . }}
app.kubernetes.io/instance: {{ .Release.Name }}
{{- end }}

{{/*
Create the name of the service account to use
*/}}
{{- define "preloop.serviceAccountName" -}}
{{- if .Values.serviceAccount.create }}
{{- default (include "preloop.fullname" .) .Values.serviceAccount.name }}
{{- else }}
{{- default "default" .Values.serviceAccount.name }}
{{- end }}
{{- end }}

{{/*
Create the database connection URL
*/}}
{{- define "preloop.databaseUrl" -}}
{{- if .Values.database.enabled -}}
{{- if .Values.database.external -}}
{{- $ssl := .Values.database.externalDatabase.sslMode -}}
postgresql://{{ .Values.database.externalDatabase.user }}:{{ .Values.database.externalDatabase.password }}@{{ .Values.database.externalDatabase.host }}:{{ .Values.database.externalDatabase.port }}/{{ .Values.database.externalDatabase.database }}{{- if $ssl }}?sslmode={{ $ssl }}{{- end }}
{{- else -}}
{{- if .Values.database.cnpg.name -}}
postgresql://{{ .Values.database.cnpg.auth.username | default "postgres" }}:{{ .Values.database.cnpg.auth.password | default "" }}@{{ .Values.database.cnpg.name }}-rw:5432/{{ .Values.database.cnpg.auth.database }}
{{- else -}}
postgresql://{{ .Values.database.cnpg.auth.username | default "postgres" }}:{{ .Values.database.cnpg.auth.password | default "" }}@{{ include "preloop.fullname" . }}-db-rw:5432/{{ .Values.database.cnpg.auth.database }}
{{- end -}}
{{- end -}}
{{- else -}}
{{ .Values.environment.databaseUrl }}
{{- end -}}
{{- end }}

{{/*
Name of the application Secret. When existingSecret is set, pods read
jwt-secret (and other keys) from that Secret instead of the chart-generated one.
*/}}
{{- define "preloop.secretName" -}}
{{- if .Values.existingSecret -}}
{{- .Values.existingSecret -}}
{{- else -}}
{{- include "preloop.fullname" . -}}
{{- end -}}
{{- end }}

{{/*
DATABASE_URL env entry. Prefer urlFromSecret so the URL is not stored in
values committed to git.
*/}}
{{- define "preloop.databaseUrlEnv" -}}
- name: DATABASE_URL
{{- if and .Values.database.urlFromSecret .Values.database.urlFromSecret.name }}
  valueFrom:
    secretKeyRef:
      name: {{ .Values.database.urlFromSecret.name | quote }}
      key: {{ default "database-url" .Values.database.urlFromSecret.key | quote }}
{{- else }}
  value: {{ include "preloop.databaseUrl" . | quote }}
{{- end }}
{{- end }}

{{/*
Operator-supplied extra env vars (API, gateway, workers, jobs).
*/}}
{{- define "preloop.extraEnv" -}}
{{- with .Values.extraEnv -}}
{{- toYaml . -}}
{{- end -}}
{{- end }}
