{{- define "ip.name" -}}insurance-platform{{- end -}}

{{- define "ip.fullname" -}}
{{- printf "%s" .Release.Name | trunc 40 | trimSuffix "-" -}}
{{- end -}}

{{- define "ip.labels" -}}
app.kubernetes.io/name: {{ include "ip.name" . }}
app.kubernetes.io/instance: {{ .Release.Name }}
app.kubernetes.io/version: {{ .Chart.AppVersion | quote }}
app.kubernetes.io/managed-by: {{ .Release.Service }}
helm.sh/chart: {{ printf "%s-%s" .Chart.Name .Chart.Version }}
{{- end -}}

{{- define "ip.selector" -}}
app.kubernetes.io/name: {{ include "ip.name" .root }}
app.kubernetes.io/instance: {{ .root.Release.Name }}
app.kubernetes.io/component: {{ .component }}
{{- end -}}

{{- define "ip.tag" -}}{{ .Values.image.tag | default .Chart.AppVersion }}{{- end -}}

{{- define "ip.apiImage" -}}
{{ printf "%s/%s/%s:%s" .Values.image.registry .Values.image.owner .Values.image.api (include "ip.tag" .) }}
{{- end -}}

{{- define "ip.webImage" -}}
{{ printf "%s/%s/%s:%s" .Values.image.registry .Values.image.owner .Values.image.web (include "ip.tag" .) }}
{{- end -}}

{{/* Runtime traffic goes through PgBouncer (transaction pooling) when enabled. */}}
{{- define "ip.dbHost" -}}
{{- if .Values.pgbouncer.enabled -}}{{ include "ip.fullname" . }}-pgbouncer{{- else -}}{{ .Values.database.host }}{{- end -}}
{{- end -}}

{{- define "ip.secretEnv" -}}
- name: {{ .name }}
  valueFrom:
    secretKeyRef:
      name: {{ .root.Values.secrets.existingSecret }}
      key: {{ .name }}
      optional: {{ .optional | default false }}
{{- end -}}

{{/* Environment shared by the API container. */}}
{{- define "ip.apiEnv" -}}
{{- $root := . -}}
{{- range $name := list "APP_DB_PASSWORD" "APP_ENCRYPTION_KEY" "APP_BLIND_INDEX_KEY" "JWT_SIGNING_KEY" "JWT_PRIVATE_KEY_B64" "JWT_PUBLIC_KEY_B64" "METRICS_TOKEN" "TURNSTILE_SITE_KEY" "TURNSTILE_SECRET_KEY" }}
{{ include "ip.secretEnv" (dict "root" $root "name" $name) }}
{{- end }}
{{- range $name := list "SENTRY_DSN" "SMTP_USERNAME" "SMTP_PASSWORD" "APP_ENCRYPTION_PREVIOUS_KEYS" "APP_IDEMPOTENCY_KEY" "JWT_PREVIOUS_PUBLIC_KEYS" }}
{{ include "ip.secretEnv" (dict "root" $root "name" $name "optional" true) }}
{{- end }}
- name: DATABASE_URL
  value: "postgresql+asyncpg://app_runtime:$(APP_DB_PASSWORD)@{{ include "ip.dbHost" . }}:{{ .Values.pgbouncer.enabled | ternary 5432 .Values.database.port }}/{{ .Values.database.name }}{{ .Values.database.urlQuery }}"
{{- if .Values.database.readHost }}
- name: DATABASE_READ_URL
  value: "postgresql+asyncpg://app_runtime:$(APP_DB_PASSWORD)@{{ .Values.database.readHost }}:{{ .Values.database.port }}/{{ .Values.database.name }}{{ .Values.database.urlQuery }}"
{{- end }}
- name: APP_RELEASE
  value: {{ include "ip.tag" . | quote }}
{{- end -}}
