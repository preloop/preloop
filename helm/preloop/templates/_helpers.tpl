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
Label key for a Cilium endpoint selector inside an ingress or egress rule.
Cilium reads a bare key there as any:, which matches the label from any
source; k8s: names the pod label and is the form its documentation uses.
A key that already carries a source (contains a colon) is left alone.
*/}}
{{- define "preloop.ciliumLabelKey" -}}
{{- if contains ":" . }}{{ . }}{{ else }}k8s:{{ . }}{{ end -}}
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
Name of the Secret holding credentials the chart derives from values
(DATABASE_URL, SMTP password). Separate from preloop.secretName so that an
operator-supplied existingSecret does not have to carry these keys.
*/}}
{{- define "preloop.credentialsSecretName" -}}
{{- printf "%s-credentials" (include "preloop.fullname" .) -}}
{{- end }}

{{/*
DATABASE_URL env entry. Always a secretKeyRef: either the operator's own
Secret (database.urlFromSecret, which also keeps the URL out of values) or
the chart-managed credentials Secret. Never a literal in the pod spec.
*/}}
{{- define "preloop.databaseUrlEnv" -}}
- name: DATABASE_URL
  valueFrom:
    secretKeyRef:
{{- if and .Values.database.urlFromSecret .Values.database.urlFromSecret.name }}
      name: {{ .Values.database.urlFromSecret.name | quote }}
      key: {{ default "database-url" .Values.database.urlFromSecret.key | quote }}
{{- else }}
      name: {{ include "preloop.credentialsSecretName" . | quote }}
      key: "database-url"
{{- end }}
{{- end }}

{{/*
In-cluster model gateway URL.

Agent Jobs, the API and the workers resolve model traffic to this URL when a
model row carries no explicit meta_data.gateway.url. It must be the gateway
Service: the API pods run PRELOOP_SERVICE_ROLE=api and never mount
/openai/v1, so pointing it at the API Service answers 404 Not Found for every
model call.
*/}}
{{- define "preloop.modelGatewayUrlK8s" -}}
{{- if .Values.gateway.inClusterUrl -}}
{{- .Values.gateway.inClusterUrl -}}
{{- else -}}
http://{{ include "preloop.fullname" . }}-gateway:80/openai/v1
{{- end -}}
{{- end }}

{{/*
SMTP_PASSWORD env entry, same rule as DATABASE_URL. Call with the root
context ($) so it also works inside a range.
*/}}
{{- define "preloop.smtpPasswordEnv" -}}
{{- $smtpSecret := .Values.config.smtp.passwordSecret | default dict -}}
- name: SMTP_PASSWORD
  valueFrom:
    secretKeyRef:
{{- if $smtpSecret.name }}
      name: {{ $smtpSecret.name | quote }}
      key: {{ default "smtp-password" $smtpSecret.key | quote }}
{{- else }}
      name: {{ include "preloop.credentialsSecretName" . | quote }}
      key: "smtp-password"
{{- end }}
{{- end }}

{{/*
Annotation that rolls pods when a chart-managed credential changes. Env
literals used to do this implicitly; a Secret reference does not, so the
checksum has to be carried on the pod template. Call with the root context.
*/}}
{{- define "preloop.credentialsChecksum" -}}
checksum/credentials: {{ include (print $.Template.BasePath "/secret-credentials.yaml") . | sha256sum }}
{{- end }}

{{/*
Operator-supplied extra env vars (API, gateway, workers, jobs).
*/}}
{{- define "preloop.extraEnv" -}}
{{- with .Values.extraEnv -}}
{{- toYaml . -}}
{{- end -}}
{{- end }}

{{/*
Shared OTLP env for API and gateway (one values schema).
*/}}
{{- define "preloop.otlpEnv" -}}
{{- if .Values.otlp.enabled }}
- name: OTLP_ENABLED
  value: "true"
- name: OTLP_ENDPOINT
  value: {{ .Values.otlp.endpoint | quote }}
- name: OTLP_PROTOCOL
  value: {{ .Values.otlp.protocol | quote }}
- name: OTLP_SERVICE_NAME
  value: {{ .Values.otlp.resource.serviceName | quote }}
{{- if .Values.otlp.resource.serviceNamespace }}
- name: OTLP_SERVICE_NAMESPACE
  value: {{ .Values.otlp.resource.serviceNamespace | quote }}
{{- end }}
{{- if .Values.otlp.resource.deploymentEnvironment }}
- name: OTLP_DEPLOYMENT_ENVIRONMENT
  value: {{ .Values.otlp.resource.deploymentEnvironment | quote }}
{{- end }}
- name: OTLP_SAMPLER_RATIO
  value: {{ .Values.otlp.samplerRatio | quote }}
{{- $headersSecretName := .Values.otlp.headersSecret.name | default "" }}
{{- $headersSecretKey := .Values.otlp.headersSecret.key | default "otlp-headers" }}
{{- if $headersSecretName }}
- name: OTLP_HEADERS
  valueFrom:
    secretKeyRef:
      name: {{ $headersSecretName | quote }}
      key: {{ $headersSecretKey | quote }}
{{- else if .Values.otlp.headers }}
- name: OTLP_HEADERS
  valueFrom:
    secretKeyRef:
      name: {{ include "preloop.fullname" . }}
      key: otlp-headers
{{- end }}
{{- end }}
{{- end }}

{{/* Execution-only settings; never change control-plane Kubernetes credentials. */}}
{{- define "preloop.executionEnv" -}}
{{- if .Values.agentExecution.kubeconfigSecret }}
- name: PRELOOP_EXECUTION_KUBECONFIG
  value: /var/run/preloop/execution/kubeconfig
{{- end }}
{{- if .Values.agentExecution.priorityClassName }}
- name: PRELOOP_EXECUTION_PRIORITY_CLASS
  value: {{ .Values.agentExecution.priorityClassName | quote }}
{{- end }}
{{- if .Values.agentExecution.apiUrl }}
- name: PRELOOP_EXECUTION_API_URL
  value: {{ .Values.agentExecution.apiUrl | quote }}
{{- end }}
{{- if .Values.agentExecution.gatewayUrl }}
- name: PRELOOP_EXECUTION_GATEWAY_URL
  value: {{ .Values.agentExecution.gatewayUrl | quote }}
{{- end }}
{{- end -}}

{{/*
CODEX_IMAGE for processes that launch hosted Codex (API and workers).
Empty leaves the variable unset so the process uses the digest pinned in
the application. Call with the root context.
*/}}
{{- define "preloop.codexImageEnv" -}}
{{- if .Values.agents.codexImage }}
- name: CODEX_IMAGE
  value: {{ .Values.agents.codexImage | quote }}
{{- end }}
{{- end -}}

{{/* namespace.create governs local provisioning, not a remote destination. */}}
{{- define "preloop.executionNamespace" -}}
{{- if or .Values.agentExecution.kubeconfigSecret .Values.agentExecution.namespace.create -}}
{{- .Values.agentExecution.namespace.name -}}
{{- else -}}
{{- .Release.Namespace -}}
{{- end -}}
{{- end -}}

{{/*
Governance review env for the API, workers and scheduler. Each variable is
rendered only when its value is set, so the application defaults apply
otherwise: deterministic reports are on and model analysis defaults off.
Account overrides can enable analysis independently of the global default.
*/}}
{{- define "preloop.governanceReviewEnv" -}}
{{- $gr := .Values.governanceReview | default dict }}
{{- if kindIs "bool" $gr.enabled }}
- name: GOVERNANCE_REVIEW_ENABLED
  value: {{ $gr.enabled | quote }}
{{- end }}
{{- if kindIs "bool" $gr.modelAnalysisEnabled }}
- name: GOVERNANCE_REVIEW_MODEL_ANALYSIS_ENABLED
  value: {{ $gr.modelAnalysisEnabled | quote }}
{{- end }}
{{- if $gr.analysisModel }}
- name: PRELOOP_GOVERNANCE_ANALYSIS_MODEL
  value: {{ $gr.analysisModel | quote }}
{{- end }}
{{- end }}

{{/*
Enterprise single sign-on settings. ENTERPRISE_SSO_ENABLED and SSO_PUBLIC_URL
are read only by the Preloop Cloud and Preloop Enterprise images.
GATEWAY_IDP_ALLOW_PRIVATE_ISSUERS is a core setting shared with gateway identity
providers (see values.yaml).
Rendered on the API, which serves discovery, start, callback, SAML metadata and
ACS. Disabled renders ENTERPRISE_SSO_ENABLED=false so an Enterprise image does
not mount the SSO routes.
*/}}
{{- define "preloop.ssoEnv" -}}
{{- $sso := .Values.sso | default dict -}}
- name: ENTERPRISE_SSO_ENABLED
  value: {{ ternary "true" "false" (eq (toString $sso.enabled) "true") | quote }}
{{- if eq (toString $sso.enabled) "true" }}
{{- with $sso.publicUrl }}
{{- if not (regexMatch "^https://[A-Za-z0-9.-]+(:[0-9]{1,5})?$" (trimSuffix "/" .)) }}
{{- fail "sso.publicUrl must be an https origin such as https://preloop.example.com, without a path" }}
{{- end }}
- name: SSO_PUBLIC_URL
  value: {{ trimSuffix "/" . | quote }}
{{- end }}
{{- if eq (toString $sso.allowPrivateIssuers) "true" }}
- name: GATEWAY_IDP_ALLOW_PRIVATE_ISSUERS
  value: "true"
{{- end }}
{{- end }}
{{- end }}

{{/*
Console nginx security headers (#2278, #2279). nginx drops server-level
add_header directives in any block that declares its own add_header, so
configmap-nginx.yaml includes this at server level AND in every location/if
block that sets headers. Keep it identical to frontend/docker/
security-headers.conf (backend/tests/test_nginx_security_headers.py checks
parity). $preloop_hsts comes from preloop.nginxHstsMap.
*/}}
{{- define "preloop.nginxSecurityHeaders" -}}
add_header X-Frame-Options "SAMEORIGIN" always;
add_header X-Content-Type-Options "nosniff" always;
add_header Referrer-Policy $preloop_referrer_policy always;
add_header Permissions-Policy "camera=(), geolocation=(), payment=(), usb=(), microphone=(self)" always;
add_header Content-Security-Policy $preloop_csp always;
add_header Content-Security-Policy-Report-Only $preloop_csp_report_only always;
add_header Strict-Transport-Security $preloop_hsts always;
{{- end }}

{{/*
Space-separated extra CSP sources from a values key, validated and normalised
(#2280). Only host sources are accepted (https://cdn.example.com,
*.example.com, 127.0.0.1:4000): keywords, quotes, bare schemes such as
"https:" and a bare "*" would reopen what the policy closes or break out of
the header value. Same rule as frontend/docker/15-preloop-csp.envsh.
Call with (list "<values key>" <value>).
*/}}
{{- define "preloop.cspSources" -}}
{{- $key := index . 0 -}}
{{- $raw := index . 1 | default "" | toString -}}
{{- $source := `([a-z][a-z0-9+.-]*://)?(\*\.)?[A-Za-z0-9-]+(\.[A-Za-z0-9-]+)*(:([0-9]+|\*))?(/[A-Za-z0-9._~%/-]*)?` -}}
{{- $sources := list -}}
{{- range splitList " " (regexReplaceAll `\s+` $raw " ") -}}
{{- if . -}}
{{- if not (regexMatch (printf "^%s$" $source) .) -}}
{{- fail (printf "%s must be space-separated host sources such as https://cdn.example.com, got %q" $key $raw) -}}
{{- end -}}
{{- $sources = append $sources . -}}
{{- end -}}
{{- end -}}
{{- join " " $sources -}}
{{- end }}

{{/*
Origin of analytics.scriptUrl for the CSP, or "" when analytics is off, a
customSnippet replaces the built-in tag, or the URL is same-origin. The
built-in tag loads its script from this origin and the Plausible-style
script posts events back to it, so it goes into both script-src and
connect-src. https://traffic.example.com/js/script.js gives
https://traffic.example.com; a protocol-relative //cdn.example.com/s.js
gives the host source cdn.example.com; a relative /js/script.js (a tracker
proxied under the console's own domain) gives "", since 'self' covers it.
*/}}
{{- define "preloop.analyticsOrigin" -}}
{{- $a := .Values.analytics -}}
{{- if and $a.enabled (not $a.customSnippet) $a.scriptUrl -}}
{{- $url := urlParse $a.scriptUrl -}}
{{- if and $url.scheme $url.host -}}
{{- printf "%s://%s" $url.scheme $url.host -}}
{{- else if $url.host -}}
{{- $url.host -}}
{{- end -}}
{{- end -}}
{{- end }}

{{/*
http-context maps for the console Content-Security-Policy (#2280), from
.Values.securityHeaders.csp. Keep the policy identical to the one in
frontend/docker/nginx.conf.template (backend/tests check parity).

  report-only  legacy Content-Security-Policy plus the new policy as
               Content-Security-Policy-Report-Only (default)
  enforce      the new policy as Content-Security-Policy, nothing else
  legacy       the legacy Content-Security-Policy only

The new policy covers what this nginx serves from the console build.
Responses proxied from the API ($upstream_addr is set) and the separate
admin SPA under /admin keep the legacy header in every mode: the console's
violation sweep loads neither.
*/}}
{{- define "preloop.nginxCspMap" -}}
{{- $csp := .Values.securityHeaders.csp | default dict -}}
{{- $mode := $csp.mode | default "report-only" | toString -}}
{{- if not (has $mode (list "report-only" "enforce" "legacy")) -}}
{{- fail (printf "securityHeaders.csp.mode must be one of report-only, enforce, legacy, got %q" $mode) -}}
{{- end -}}
{{- $analytics := include "preloop.cspSources" (list "analytics.scriptUrl" (include "preloop.analyticsOrigin" .)) -}}
{{- $scriptSrc := list "'self'" -}}
{{- $connectSrc := list "'self'" "ws:" "wss:" "data:" -}}
{{- if $analytics -}}
{{- $scriptSrc = append $scriptSrc $analytics -}}
{{- $connectSrc = append $connectSrc $analytics -}}
{{- end -}}
{{- with include "preloop.cspSources" (list "securityHeaders.csp.extraScriptSrc" $csp.extraScriptSrc) -}}
{{- $scriptSrc = append $scriptSrc . -}}
{{- end -}}
{{- with include "preloop.cspSources" (list "securityHeaders.csp.extraConnectSrc" $csp.extraConnectSrc) -}}
{{- $connectSrc = append $connectSrc . -}}
{{- end -}}
map "" $preloop_csp_policy {
    default "default-src 'self'; script-src {{ join " " $scriptSrc }}; style-src 'self' 'unsafe-inline'; img-src 'self' data: blob: https:; font-src 'self' data:; connect-src {{ join " " $connectSrc }}; media-src 'self' blob:; worker-src 'self' blob:; frame-src https://www.youtube-nocookie.com; object-src 'none'; base-uri 'self'; form-action 'self'; frame-ancestors 'self'";
}

# 1 for what nginx serves from the console build, 0 for proxied responses
# ($upstream_addr set) and the separate admin SPA under /admin, which the
# console's violation sweep does not load.
map "$upstream_addr:$uri" $preloop_csp_console {
    "~^:/admin(/|$)" 0;
    "~^:" 1;
    default 0;
}

map "{{ $mode }}:$preloop_csp_console" $preloop_csp {
    "enforce:1" $preloop_csp_policy;
    default "default-src 'self' http: https: data: blob: 'unsafe-inline'";
}

map "{{ $mode }}:$preloop_csp_console" $preloop_csp_report_only {
    "report-only:1" $preloop_csp_policy;
    default "";
}
{{- end }}

{{/*
Validated Strict-Transport-Security value from .Values.securityHeaders.hsts.
Browsers reject a preload policy without includeSubDomains or with a
max-age below one year, so the chart refuses to render one.
*/}}
{{- define "preloop.hstsValue" -}}
{{- $hsts := .Values.securityHeaders.hsts -}}
{{- /* Sprig int64 turns unparsable input into 0, which would silently
       render max-age=0; reject anything that is not a whole number >= 0. */ -}}
{{- $rawMaxAge := $hsts.maxAge -}}
{{- $validMaxAge := false -}}
{{- if kindIs "string" $rawMaxAge -}}
{{- $validMaxAge = regexMatch "^[0-9]+$" $rawMaxAge -}}
{{- else if or (kindIs "float64" $rawMaxAge) (kindIs "int64" $rawMaxAge) (kindIs "int" $rawMaxAge) -}}
{{- $validMaxAge = and (ge (float64 $rawMaxAge) 0.0) (eq (float64 $rawMaxAge) (float64 (int64 $rawMaxAge))) -}}
{{- end -}}
{{- if not $validMaxAge -}}
{{- fail (printf "securityHeaders.hsts.maxAge must be a whole number of seconds >= 0, got %v" $rawMaxAge) -}}
{{- end -}}
{{- $maxAge := int64 $hsts.maxAge -}}
{{- if and $hsts.preload (or (not $hsts.includeSubDomains) (lt $maxAge 31536000)) -}}
{{- fail "securityHeaders.hsts.preload=true requires securityHeaders.hsts.includeSubDomains=true and securityHeaders.hsts.maxAge >= 31536000 (HSTS_PRELOAD needs HSTS_INCLUDE_SUBDOMAINS and HSTS_MAX_AGE >= 31536000)" -}}
{{- end -}}
max-age={{ $maxAge }}{{ if $hsts.includeSubDomains }}; includeSubDomains{{ end }}{{ if $hsts.preload }}; preload{{ end }}
{{- end }}

{{/*
http-context map that yields the HSTS value for requests that arrived over
HTTPS (X-Forwarded-Proto: https, or an https scheme) and "" otherwise. nginx
omits a header whose value is empty.
*/}}
{{- define "preloop.nginxHstsMap" -}}
{{- $value := include "preloop.hstsValue" . -}}
# Referrer-Policy default for the shared header set. When a proxied upstream
# already sent one (the API uses no-referrer where the URL carries an OAuth
# code or a signed token), yield "" so nginx adds no second, weaker policy.
map $upstream_http_referrer_policy $preloop_referrer_policy {
    "" "strict-origin-when-cross-origin";
    default "";
}

map "$http_x_forwarded_proto:$scheme" $preloop_hsts {
{{- if .Values.securityHeaders.hsts.enabled }}
    "~*^https" "{{ $value }}";
    "~*^[^:]*:https$" "{{ $value }}";
{{- end }}
    default "";
}
{{- end }}

{{/* HSTS settings for the API and gateway middleware (same names as Compose). */}}
{{- define "preloop.hstsEnv" -}}
{{- $_ := include "preloop.hstsValue" . -}}
- name: HSTS_ENABLED
  value: {{ .Values.securityHeaders.hsts.enabled | toString | quote }}
- name: HSTS_MAX_AGE
  value: {{ int64 .Values.securityHeaders.hsts.maxAge | toString | quote }}
- name: HSTS_INCLUDE_SUBDOMAINS
  value: {{ .Values.securityHeaders.hsts.includeSubDomains | toString | quote }}
- name: HSTS_PRELOAD
  value: {{ .Values.securityHeaders.hsts.preload | toString | quote }}
{{- end }}
