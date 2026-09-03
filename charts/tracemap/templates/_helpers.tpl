{{/*
Shared naming, labels and the probe block.

The probes live here rather than in each Deployment for one reason: M17 is a
rule about a shape, and a rule copied into eight files is a rule that will be
wrong in one of them. Defining it once means "did someone put a dependency
check in a liveness probe" is a question about eleven lines, not about the
whole chart.
*/}}

{{- define "tracemap.name" -}}
{{- default .Chart.Name .Values.nameOverride | trunc 63 | trimSuffix "-" -}}
{{- end -}}

{{- define "tracemap.fullname" -}}
{{- if .Values.fullnameOverride -}}
{{- .Values.fullnameOverride | trunc 63 | trimSuffix "-" -}}
{{- else -}}
{{- printf "%s-%s" .Release.Name (include "tracemap.name" .) | trunc 63 | trimSuffix "-" -}}
{{- end -}}
{{- end -}}

{{- define "tracemap.labels" -}}
helm.sh/chart: {{ printf "%s-%s" .Chart.Name .Chart.Version | replace "+" "_" }}
app.kubernetes.io/name: {{ include "tracemap.name" . }}
app.kubernetes.io/instance: {{ .Release.Name }}
app.kubernetes.io/version: {{ .Chart.AppVersion | quote }}
app.kubernetes.io/managed-by: {{ .Release.Service }}
app.kubernetes.io/part-of: tracemap
{{- end -}}

{{/*
Per-pod labels.

`git-sha` is not decoration. The deploy watcher lists Deployments and records a
marker keyed on this label, and that marker is what lets an anomaly name the
release that probably caused it. A pod without it is a pod whose regressions
can never be attributed (M24), and backfilling leaves a window of traces that
can never be correlated to anything.
*/}}
{{- define "tracemap.podLabels" -}}
app.kubernetes.io/name: {{ include "tracemap.name" .root }}
app.kubernetes.io/instance: {{ .root.Release.Name }}
app.kubernetes.io/component: {{ .name }}
app.kubernetes.io/part-of: tracemap
git-sha: {{ .root.Values.global.gitSha | quote }}
{{- end -}}

{{- define "tracemap.image" -}}
{{- $g := .root.Values.global -}}
{{- printf "%s/%s/%s:%s" $g.imageRegistry $g.imageRepository .image $g.gitSha -}}
{{- end -}}

{{/*
The probe block. Takes a dict of (root, port).

Read the three together, because the distinction between them is the whole
point:

  startupProbe    "is it up yet?"        buys a slow boot 60s before liveness
                                          starts counting failures against it
  livenessProbe   "should I restart?"    /healthz -- PROCESS ONLY. A dependency
                                          check here restarts the fleet when one
                                          database gets slow
  readinessProbe  "should I get traffic?" /readyz -- dependencies belong HERE.
                                          Failing readiness sheds traffic, which
                                          is the correct response to a broken
                                          dependency; restarting is not.
*/}}
{{- define "tracemap.probes" -}}
{{- $p := .root.Values.probes -}}
startupProbe:
  httpGet:
    path: {{ $p.startup.path }}
    port: {{ .port }}
  periodSeconds: {{ $p.startup.periodSeconds }}
  failureThreshold: {{ $p.startup.failureThreshold }}
livenessProbe:
  httpGet:
    path: {{ $p.liveness.path }}
    port: {{ .port }}
  periodSeconds: {{ $p.liveness.periodSeconds }}
  timeoutSeconds: {{ $p.liveness.timeoutSeconds }}
  failureThreshold: {{ $p.liveness.failureThreshold }}
readinessProbe:
  httpGet:
    path: {{ $p.readiness.path }}
    port: {{ .port }}
  periodSeconds: {{ $p.readiness.periodSeconds }}
  timeoutSeconds: {{ $p.readiness.timeoutSeconds }}
  failureThreshold: {{ $p.readiness.failureThreshold }}
{{- end -}}

{{/*
Telemetry environment shared by every traced service.

Head sampling is OFF: the collector's tail sampler decides once it can see the
whole trace. Turning both on without meaning to samples twice, and the second
multiplication is invisible -- the traces that survive look perfectly normal.
*/}}
{{- define "tracemap.otelEnv" -}}
- name: OTEL_SERVICE_NAME
  value: {{ .name }}
- name: SERVICE_NAME
  value: {{ .name }}
- name: SERVICE_VERSION
  value: {{ .root.Values.global.gitSha | quote }}
- name: OTEL_RESOURCE_ATTRIBUTES
  value: "service.version={{ .root.Values.global.gitSha }},deployment.environment={{ .root.Values.global.environment }}"
- name: OTEL_EXPORTER_OTLP_ENDPOINT
  value: {{ .root.Values.otel.collectorEndpoint | quote }}
- name: OTEL_EXPORTER_OTLP_PROTOCOL
  value: grpc
- name: OTEL_TRACES_SAMPLER
  value: parentbased_always_on
- name: OTEL_METRICS_EXPORTER
  value: none
- name: OTEL_LOGS_EXPORTER
  value: none
{{- end -}}

{{/*
Datastore environment. Credentials come from existing Secrets, never from
values -- a password in a values file ends up in `helm get values`, in CI
logs, and in the repository.
*/}}
{{- define "tracemap.datastoreEnv" -}}
{{- $d := .Values.datastores -}}
- name: POSTGRES_HOST
  value: {{ $d.postgres.host | quote }}
- name: POSTGRES_PORT
  value: {{ $d.postgres.port | quote }}
- name: POSTGRES_DB
  value: {{ $d.postgres.database | quote }}
- name: POSTGRES_USER
  valueFrom:
    secretKeyRef: { name: {{ $d.postgres.existingSecret }}, key: username }
- name: POSTGRES_PASSWORD
  valueFrom:
    secretKeyRef: { name: {{ $d.postgres.existingSecret }}, key: password }
{{- end -}}

{{- define "tracemap.clickhouseEnv" -}}
{{- $d := .Values.datastores -}}
- name: CLICKHOUSE_HOST
  value: {{ $d.clickhouse.host | quote }}
- name: CLICKHOUSE_PORT
  value: {{ $d.clickhouse.port | quote }}
- name: CLICKHOUSE_DB
  value: {{ $d.clickhouse.database | quote }}
- name: CLICKHOUSE_USER
  valueFrom:
    secretKeyRef: { name: {{ $d.clickhouse.existingSecret }}, key: username }
- name: CLICKHOUSE_PASSWORD
  valueFrom:
    secretKeyRef: { name: {{ $d.clickhouse.existingSecret }}, key: password }
- name: REDIS_URL
  value: {{ $d.redis.url | quote }}
{{- end -}}

{{/*
A writable /tmp for a read-only root filesystem. Cheap, and it is what stops
`readOnlyRootFilesystem: true` from being reverted the first time something
needs to write a temp file.
*/}}
{{- define "tracemap.tmpVolume" -}}
- name: tmp
  emptyDir: {}
{{- end -}}

{{- define "tracemap.tmpMount" -}}
- name: tmp
  mountPath: /tmp
{{- end -}}
