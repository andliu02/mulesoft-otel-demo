# OpenTelemetry Instrumentation Guide

How every service in this demo is instrumented with OpenTelemetry (Python SDK), including traces, metrics, logs, and custom business attributes.

---

## Table of Contents

1. [Dependencies](#1-dependencies)
2. [Setup: Resource, Providers, Exporters](#2-setup-resource-providers-exporters)
3. [Traces: Spans & Attributes](#3-traces-spans--attributes)
4. [Metrics: Counters, Histograms, Gauges](#4-metrics-counters-histograms-gauges)
5. [Logs: Correlated Logging](#5-logs-correlated-logging)
6. [Auto-Instrumentation: Flask & Requests](#6-auto-instrumentation-flask--requests)
7. [Context Propagation (W3C Trace Context)](#7-context-propagation-w3c-trace-context)
8. [Custom Business Attributes](#8-custom-business-attributes)
9. [MuleSoft-Specific Patterns](#9-mulesoft-specific-patterns)
10. [Full Working Example](#10-full-working-example)

---

## 1. Dependencies

```
# requirements.txt
flask==3.0.3
requests==2.32.3

# OTel core
opentelemetry-api==1.26.0
opentelemetry-sdk==1.26.0

# Auto-instrumentation libraries
opentelemetry-instrumentation-flask==0.47b0
opentelemetry-instrumentation-requests==0.47b0

# OTLP exporter (gRPC)
opentelemetry-exporter-otlp-proto-grpc==1.26.0
```

Install:
```bash
pip install opentelemetry-api opentelemetry-sdk \
    opentelemetry-instrumentation-flask \
    opentelemetry-instrumentation-requests \
    opentelemetry-exporter-otlp-proto-grpc
```

---

## 2. Setup: Resource, Providers, Exporters

Every service needs three providers: traces, metrics, and logs. They all share a **Resource** that identifies the service.

```python
import os
from opentelemetry import trace, metrics
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor
from opentelemetry.sdk.metrics import MeterProvider
from opentelemetry.sdk.metrics.export import PeriodicExportingMetricReader
from opentelemetry.exporter.otlp.proto.grpc.trace_exporter import OTLPSpanExporter
from opentelemetry.exporter.otlp.proto.grpc.metric_exporter import OTLPMetricExporter
from opentelemetry.sdk.resources import Resource

OTLP_ENDPOINT = os.getenv("OTLP_ENDPOINT", "http://otel-collector:4317")

# ── Resource: identifies this service in all telemetry ────────────────────
resource = Resource.create({
    "service.name": "my-service",           # REQUIRED: unique service name
    "service.version": "1.0.0",             # version tag
    "service.namespace": "fnb-banking",     # logical grouping
    "deployment.environment": "production", # environment
})

# ── Traces ────────────────────────────────────────────────────────────────
tracer_provider = TracerProvider(resource=resource)
tracer_provider.add_span_processor(
    BatchSpanProcessor(OTLPSpanExporter(endpoint=OTLP_ENDPOINT, insecure=True))
)
trace.set_tracer_provider(tracer_provider)
tracer = trace.get_tracer("my.service")    # named tracer for creating spans

# ── Metrics ───────────────────────────────────────────────────────────────
metric_reader = PeriodicExportingMetricReader(
    OTLPMetricExporter(endpoint=OTLP_ENDPOINT, insecure=True),
    export_interval_millis=15000,           # export every 15 seconds
)
meter_provider = MeterProvider(resource=resource, metric_readers=[metric_reader])
metrics.set_meter_provider(meter_provider)
meter = metrics.get_meter("my.service.metrics")
```

**Key points:**
- `insecure=True` is for gRPC to the OTel Collector on the same network (no TLS)
- The OTel Collector then forwards to Elastic Cloud over TLS
- `service.name` is what appears in the Kibana service map and APM

---

## 3. Traces: Spans & Attributes

Spans represent units of work. Each span has a name, attributes, and a duration.

### Creating a basic span

```python
with tracer.start_as_current_span("my.operation") as span:
    # Your code here
    result = do_something()

    # Add attributes after creation
    span.set_attribute("result.status", "success")
```

### Span with attributes at creation

```python
with tracer.start_as_current_span("portal.initiateWireTransfer",
    attributes={
        "portal.operation": "wire_transfer",
        "customer.id": "CUST000042",
        "payment.amount": 50000.00,
        "payment.currency": "USD",
        "payment.type": "WIRE",
    }):
    result = call_mulesoft("/api/payments/wire", payload=body)
```

### Nested spans (parent-child)

```python
with tracer.start_as_current_span("mule:flow/payment-processing-flow") as flow_span:

    with tracer.start_as_current_span("mule:dw:transform/validate") as transform_span:
        validate_request(body)

    with tracer.start_as_current_span("mule:http:request/core-banking/debit") as http_span:
        http_span.set_attribute("http.method", "POST")
        http_span.set_attribute("http.url", "http://core-banking:9001/accounts/debit")
        response = requests.post(url, json=body)
        http_span.set_attribute("http.status_code", response.status_code)
```

### Recording errors

```python
from opentelemetry.trace import StatusCode

with tracer.start_as_current_span("my.operation") as span:
    try:
        result = risky_operation()
    except Exception as e:
        span.set_status(StatusCode.ERROR, str(e))
        span.record_exception(e)
        raise
```

### Span kinds

```python
from opentelemetry.trace import SpanKind

# SERVER span: handling an incoming request
with tracer.start_as_current_span("handle.request", kind=SpanKind.SERVER):
    pass

# CLIENT span: making an outgoing request
with tracer.start_as_current_span("call.backend", kind=SpanKind.CLIENT):
    pass

# INTERNAL span: internal processing (default)
with tracer.start_as_current_span("process.data", kind=SpanKind.INTERNAL):
    pass
```

---

## 4. Metrics: Counters, Histograms, Gauges

### Counter (monotonically increasing)
Use for: request counts, error counts, total operations

```python
# Create
request_counter = meter.create_counter(
    "portal.requests.total",
    unit="1",
    description="Total portal operations by type"
)

error_counter = meter.create_counter(
    "portal.errors.total",
    unit="1",
    description="Portal errors by operation"
)

# Record (always positive increments)
request_counter.add(1, {"portal.operation": "wire_transfer"})
error_counter.add(1, {"portal.operation": "wire_transfer", "error.type": "timeout"})
```

### Histogram (distribution of values)
Use for: latency, request size, transaction amounts

```python
# Create
latency_histogram = meter.create_histogram(
    "portal.operation.duration",
    unit="ms",
    description="Portal operation duration"
)

# Record
start = time.time()
result = process_request()
elapsed_ms = (time.time() - start) * 1000
latency_histogram.record(elapsed_ms, {"portal.operation": "wire_transfer"})
```

### UpDownCounter (can go up or down)
Use for: active sessions, queue depth, in-flight requests

```python
# Create
active_sessions = meter.create_up_down_counter(
    "portal.tellers.active",
    unit="1",
    description="Active teller sessions"
)

# Record
active_sessions.add(1)     # teller starts
# ... do work ...
active_sessions.add(-1)    # teller finishes
```

### Observable Gauge (callback-based, read on demand)
Use for: CPU usage, memory, pool sizes

```python
import psutil

def cpu_callback(options):
    yield metrics.Observation(psutil.cpu_percent(), {})

meter.create_observable_gauge(
    "system.cpu.usage",
    callbacks=[cpu_callback],
    unit="%",
    description="Current CPU usage"
)
```

### Metric attributes (dimensions)
Attributes on metrics create separate time series per unique combination:

```python
# These create 3 separate time series:
counter.add(1, {"portal.operation": "wire_transfer"})
counter.add(1, {"portal.operation": "ach_payment"})
counter.add(1, {"portal.operation": "customer_360"})
```

In Kibana, you can split/filter by these attributes in dashboards.

---

## 5. Logs: Correlated Logging

OTel logs automatically include trace context (trace_id, span_id) so you can jump from a log line to the exact trace.

```python
import logging
from opentelemetry._logs import set_logger_provider
from opentelemetry.sdk._logs import LoggerProvider, LoggingHandler
from opentelemetry.sdk._logs.export import BatchLogRecordProcessor
from opentelemetry.exporter.otlp.proto.grpc._log_exporter import OTLPLogExporter

# ── Setup ─────────────────────────────────────────────────────────────────
logger_provider = LoggerProvider(resource=resource)
logger_provider.add_log_record_processor(
    BatchLogRecordProcessor(OTLPLogExporter(endpoint=OTLP_ENDPOINT, insecure=True))
)
set_logger_provider(logger_provider)

# ── Attach to Python logging ─────────────────────────────────────────────
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("my.service")
logger.addHandler(LoggingHandler(level=logging.DEBUG, logger_provider=logger_provider))

# ── Use it ────────────────────────────────────────────────────────────────
# Inside a span, logs automatically get trace_id and span_id:
with tracer.start_as_current_span("process.payment"):
    logger.info("Wire transfer initiated | amount=50000 customer=CUST000042")
    # This log line in Elastic will have trace.id and span.id fields
    # allowing you to click from the log to the trace
```

---

## 6. Auto-Instrumentation: Flask & Requests

Auto-instrumentation creates spans automatically for incoming HTTP requests and outgoing HTTP calls.

```python
from flask import Flask
from opentelemetry.instrumentation.flask import FlaskInstrumentor
from opentelemetry.instrumentation.requests import RequestsInstrumentor

app = Flask(__name__)

# Auto-create SERVER spans for every Flask route
FlaskInstrumentor().instrument_app(app)

# Auto-create CLIENT spans for every requests.get/post/etc call
RequestsInstrumentor().instrument()
```

**What you get for free:**
- `GET /portal/customers/CUST000001/360` span with http.method, http.url, http.status_code
- `POST http://mulesoft:8081/api/payments/wire` client span
- W3C `traceparent` header automatically injected into outgoing requests
- Trace context automatically extracted from incoming requests

---

## 7. Context Propagation (W3C Trace Context)

This is how traces connect across services. The `traceparent` HTTP header carries the trace ID.

```
Service A                    Service B                    Service C
[span: portal.wire]  ──HTTP──▶ [span: mule:flow/payment] ──HTTP──▶ [span: core-banking.debit]
  traceparent: 00-abc123...     traceparent: 00-abc123...     traceparent: 00-abc123...
```

**With auto-instrumentation** (`RequestsInstrumentor`), this happens automatically. If you need manual propagation:

```python
from opentelemetry import propagate

# Inject trace context into headers (outgoing)
headers = {}
propagate.inject(headers)  # adds traceparent header
response = requests.post(url, headers=headers, json=body)

# Extract trace context from headers (incoming) — Flask does this automatically
context = propagate.extract(request.headers)
with tracer.start_as_current_span("my.span", context=context):
    pass
```

---

## 8. Custom Business Attributes

Add domain-specific attributes to spans for business observability. These become searchable/filterable in Kibana.

### Payment attributes
```python
with tracer.start_as_current_span("portal.initiateWireTransfer",
    attributes={
        "customer.id": "CUST000042",
        "payment.amount": 150000.00,
        "payment.currency": "EUR",
        "payment.type": "WIRE",
        "payment.source_account": "ACC00000042",
        "payment.destination_account": "EXT12345678",
        "payment.destination_country": "DE",
        "payment.purpose": "TRADE",
    }):
    pass
```

### Customer attributes
```python
with tracer.start_as_current_span("portal.openAccount",
    attributes={
        "customer.first_name": "James",
        "customer.last_name": "Smith",
        "customer.type": "INDIVIDUAL",
        "customer.branch_code": "BR001",
        "account.initial_deposit": 5000.00,
        "account.type": "CHECKING",
    }):
    pass
```

### Database attributes
```python
with tracer.start_as_current_span("db.SELECT",
    attributes={
        "db.system": "t24-core",
        "db.operation": "SELECT",
        "db.sql.table": "accounts_ledger",
        "db.statement": "SELECT balance FROM accounts_ledger WHERE account_id = ?",
    }):
    pass
```

### Fraud/risk attributes
```python
span.set_attribute("fraud.score", 0.85)
span.set_attribute("fraud.risk_level", "HIGH")
span.set_attribute("fraud.model_version", "falcon-v3.1")
```

### Using attributes in Kibana dashboards
These custom attributes appear as fields in the `traces-*` index:
- Filter: `customer.id: CUST000042`
- Split by: `payment.destination_country`
- Aggregate: `average(payment.amount)`

---

## 9. MuleSoft-Specific Patterns

The MuleSoft proxy uses span naming conventions that match real MuleSoft Anypoint Runtime:

### Flow spans
```python
with tracer.start_as_current_span("mule:flow/payment-processing-flow",
    attributes={
        "mule.flow.name": "payment-processing-flow",
        "mule.app.name": "fnb-integration",
        "mule.correlation_id": correlation_id,
    }):
    pass
```

### DataWeave transform spans
```python
with tracer.start_as_current_span("mule:dw:transform/validate-payment-request",
    attributes={
        "mule.processor.type": "dw:transform",
        "mule.processor.name": "validate-payment-request",
    }):
    pass
```

### Flow reference (subflow) spans
```python
with tracer.start_as_current_span("mule:flow-ref/fraud-screening-subflow"):
    # Call fraud + AML in parallel
    pass
```

### Scatter-gather spans
```python
with tracer.start_as_current_span("mule:scatter-gather",
    attributes={"mule.scatter_gather.routes": 4}):
    # Fire 4 parallel calls
    pass
```

### Async processor spans
```python
with tracer.start_as_current_span("mule:async/send-notification"):
    # Fire-and-forget
    pass
```

### HTTP connector spans
```python
with tracer.start_as_current_span(f"mule:http:request/{service}/{operation}",
    kind=SpanKind.CLIENT,
    attributes={
        "http.method": "POST",
        "http.url": url,
        "mule.connector.type": "http",
    }):
    response = requests.post(url, json=body, headers=headers)
    span.set_attribute("http.status_code", response.status_code)
```

### MuleSoft-specific custom metrics
```python
# Flow execution counter
flow_executions = meter.create_counter("mule.flow.executions")
flow_executions.add(1, {"mule.flow.name": "payment-processing-flow", "payment.type": "WIRE"})

# Active flows gauge
active_flows = meter.create_up_down_counter("mule.flows.active")
active_flows.add(1)   # flow starts
active_flows.add(-1)  # flow ends

# HTTP connector requests
http_requests = meter.create_counter("mule.http.requests")
http_requests.add(1, {"mule.backend": "core-banking-svc"})

# Messages processed
messages = meter.create_counter("mule.messages.processed")
messages.add(1, {"mule.flow.name": "payment-processing-flow"})
```

---

## 10. Full Working Example

Minimal Flask service with traces, metrics, and logs:

```python
import time, logging, os
from flask import Flask, request, jsonify
from opentelemetry import trace, metrics
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor
from opentelemetry.sdk.metrics import MeterProvider
from opentelemetry.sdk.metrics.export import PeriodicExportingMetricReader
from opentelemetry.exporter.otlp.proto.grpc.trace_exporter import OTLPSpanExporter
from opentelemetry.exporter.otlp.proto.grpc.metric_exporter import OTLPMetricExporter
from opentelemetry.sdk.resources import Resource
from opentelemetry.instrumentation.flask import FlaskInstrumentor
from opentelemetry._logs import set_logger_provider
from opentelemetry.sdk._logs import LoggerProvider, LoggingHandler
from opentelemetry.sdk._logs.export import BatchLogRecordProcessor
from opentelemetry.exporter.otlp.proto.grpc._log_exporter import OTLPLogExporter

OTLP = os.getenv("OTLP_ENDPOINT", "http://otel-collector:4317")

# ── Resource ──────────────────────────────────────────────────────────────
resource = Resource.create({
    "service.name": "my-service",
    "service.version": "1.0.0",
    "deployment.environment": "production",
})

# ── Traces ────────────────────────────────────────────────────────────────
tp = TracerProvider(resource=resource)
tp.add_span_processor(BatchSpanProcessor(OTLPSpanExporter(endpoint=OTLP, insecure=True)))
trace.set_tracer_provider(tp)
tracer = trace.get_tracer("my.service")

# ── Metrics ───────────────────────────────────────────────────────────────
mp = MeterProvider(resource=resource, metric_readers=[
    PeriodicExportingMetricReader(OTLPMetricExporter(endpoint=OTLP, insecure=True))
])
metrics.set_meter_provider(mp)
meter = metrics.get_meter("my.service.metrics")

request_count = meter.create_counter("my.requests.total", unit="1")
request_latency = meter.create_histogram("my.request.duration", unit="ms")
active_requests = meter.create_up_down_counter("my.requests.active", unit="1")

# ── Logs ──────────────────────────────────────────────────────────────────
lp = LoggerProvider(resource=resource)
lp.add_log_record_processor(BatchLogRecordProcessor(OTLPLogExporter(endpoint=OTLP, insecure=True)))
set_logger_provider(lp)
logger = logging.getLogger("my.service")
logger.addHandler(LoggingHandler(level=logging.DEBUG, logger_provider=lp))
logging.basicConfig(level=logging.INFO)

# ── Flask ─────────────────────────────────────────────────────────────────
app = Flask(__name__)
FlaskInstrumentor().instrument_app(app)

@app.route("/api/process", methods=["POST"])
def process():
    body = request.get_json() or {}
    active_requests.add(1)
    request_count.add(1, {"operation": "process"})

    with tracer.start_as_current_span("my.process",
        attributes={
            "customer.id": body.get("customerId", "unknown"),
            "order.amount": body.get("amount", 0),
        }) as span:

        start = time.time()
        logger.info(f"Processing order for {body.get('customerId')}")

        # ... your business logic ...
        result = {"status": "ok"}

        elapsed = (time.time() - start) * 1000
        request_latency.record(elapsed, {"operation": "process"})
        span.set_attribute("process.duration_ms", elapsed)

    active_requests.add(-1)
    return jsonify(result)

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=8080)
```

---

## Architecture: How Telemetry Flows

```
Your Service (OTel SDK)
    │
    │  gRPC (OTLP)
    ▼
OTel Collector (:4317)
    │
    │  OTLP over TLS
    ▼
Elastic Cloud
    ├── traces-*    (spans → APM, service map)
    ├── metrics-*   (counters/histograms → dashboards)
    └── logs-*      (correlated logs → Discover)
```

The OTel Collector config (`otel-collector/otel-collector-config.yml`) routes all three signal types to Elastic:

```yaml
receivers:
  otlp:
    protocols:
      grpc:
        endpoint: 0.0.0.0:4317
exporters:
  otlp/elastic:
    endpoint: "${ELASTIC_OTLP_ENDPOINT}"
    headers:
      Authorization: "ApiKey ${ELASTIC_API_KEY}"
service:
  pipelines:
    traces:
      receivers: [otlp]
      exporters: [otlp/elastic]
    metrics:
      receivers: [otlp]
      exporters: [otlp/elastic]
    logs:
      receivers: [otlp]
      exporters: [otlp/elastic]
```
