{{/*
Expand the name of the chart.
*/}}
{{- define "metis-gateway.name" -}}
{{- default .Chart.Name .Values.nameOverride | trunc 63 | trimSuffix "-" -}}
{{- end -}}

{{/*
Create a default fully qualified app name.
Truncated at 63 chars because some Kubernetes name fields are limited to that.
*/}}
{{- define "metis-gateway.fullname" -}}
{{- if .Values.fullnameOverride -}}
{{- .Values.fullnameOverride | trunc 63 | trimSuffix "-" -}}
{{- else -}}
{{- $name := default .Chart.Name .Values.nameOverride -}}
{{- if contains $name .Release.Name -}}
{{- .Release.Name | trunc 63 | trimSuffix "-" -}}
{{- else -}}
{{- printf "%s-%s" .Release.Name $name | trunc 63 | trimSuffix "-" -}}
{{- end -}}
{{- end -}}
{{- end -}}

{{/*
Chart label (chart name + version, sanitized).
*/}}
{{- define "metis-gateway.chart" -}}
{{- printf "%s-%s" .Chart.Name .Chart.Version | replace "+" "_" | trunc 63 | trimSuffix "-" -}}
{{- end -}}

{{/*
Common labels stamped on every resource.
*/}}
{{- define "metis-gateway.labels" -}}
helm.sh/chart: {{ include "metis-gateway.chart" . }}
{{ include "metis-gateway.selectorLabels" . }}
{{- if .Chart.AppVersion }}
app.kubernetes.io/version: {{ .Chart.AppVersion | quote }}
{{- end }}
app.kubernetes.io/managed-by: {{ .Release.Service }}
{{- end -}}

{{/*
Selector labels — kept stable across upgrades (used by Deployment matchLabels,
which is immutable post-create).
*/}}
{{- define "metis-gateway.selectorLabels" -}}
app.kubernetes.io/name: {{ include "metis-gateway.name" . }}
app.kubernetes.io/instance: {{ .Release.Name }}
{{- end -}}

{{/*
ServiceAccount name to use.
*/}}
{{- define "metis-gateway.serviceAccountName" -}}
{{- if .Values.serviceAccount.create -}}
{{- default (include "metis-gateway.fullname" .) .Values.serviceAccount.name -}}
{{- else -}}
{{- default "default" .Values.serviceAccount.name -}}
{{- end -}}
{{- end -}}

{{/*
Name of the chart-managed provider-keys Secret.
*/}}
{{- define "metis-gateway.providerSecretName" -}}
{{- if .Values.provider.existingSecret -}}
{{- .Values.provider.existingSecret -}}
{{- else -}}
{{- printf "%s-providers" (include "metis-gateway.fullname" .) -}}
{{- end -}}
{{- end -}}

{{/*
Name of the chart-managed keystore ConfigMap (only used when
.Values.keystore.existingSecret is empty).
*/}}
{{- define "metis-gateway.keystoreConfigMapName" -}}
{{- printf "%s-keystore" (include "metis-gateway.fullname" .) -}}
{{- end -}}

{{/*
Name of the PersistentVolumeClaim for the trace DB.
*/}}
{{- define "metis-gateway.pvcName" -}}
{{- printf "%s-data" (include "metis-gateway.fullname" .) -}}
{{- end -}}
