"""
FNB Portal — First National Bank Internal Portal
Tier 1: The bank teller / internal operations portal.

This is what a bank teller or back-office operator uses.
It calls MuleSoft for everything — it has no direct knowledge
of core banking, fraud, or AML systems.

OTel instrumented so traces connect:
  fnb-portal → mulesoft-anypoint-runtime → [core-banking, fraud, aml, crm, notification]

Includes a burst-pattern load generator:
  - Overnight (00-08): low volume
  - Business hours (09-17): high volume, burst at 09:00 and 16:00
  - After hours (17-00): medium volume
"""

import time
import random
import logging
import threading
import os
import uuid
import requests
from datetime import datetime
from flask import Flask, request, jsonify, render_template_string

from opentelemetry import trace, metrics, propagate
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor
from opentelemetry.sdk.metrics import MeterProvider
from opentelemetry.sdk.metrics.export import PeriodicExportingMetricReader
from opentelemetry.exporter.otlp.proto.grpc.trace_exporter import OTLPSpanExporter
from opentelemetry.exporter.otlp.proto.grpc.metric_exporter import OTLPMetricExporter
from opentelemetry.sdk.resources import Resource
from opentelemetry.instrumentation.flask import FlaskInstrumentor
from opentelemetry.instrumentation.requests import RequestsInstrumentor
from opentelemetry._logs import set_logger_provider
from opentelemetry.sdk._logs import LoggerProvider, LoggingHandler
from opentelemetry.sdk._logs.export import BatchLogRecordProcessor
from opentelemetry.exporter.otlp.proto.grpc._log_exporter import OTLPLogExporter

OTLP_ENDPOINT   = os.getenv("OTLP_ENDPOINT",   "http://otel-collector:4317")
MULESOFT_URL    = os.getenv("MULESOFT_URL",     "http://integration-vm:8081")

resource = Resource.create({
    "service.name": "fnb-portal",
    "service.version": "4.2.1",
    "service.namespace": "fnb-banking",
    "deployment.environment": "production",
    "host.name": "fnb-portal-prod-01",
    "application.name": "First National Bank Portal",
})

tracer_provider = TracerProvider(resource=resource)
tracer_provider.add_span_processor(
    BatchSpanProcessor(OTLPSpanExporter(endpoint=OTLP_ENDPOINT, insecure=True))
)
trace.set_tracer_provider(tracer_provider)
tracer = trace.get_tracer("fnb.portal")

metric_reader = PeriodicExportingMetricReader(
    OTLPMetricExporter(endpoint=OTLP_ENDPOINT, insecure=True),
    export_interval_millis=15000,
)
meter_provider = MeterProvider(resource=resource, metric_readers=[metric_reader])
metrics.set_meter_provider(meter_provider)
meter = metrics.get_meter("fnb.portal.metrics")

portal_requests  = meter.create_counter("portal.requests.total",        unit="1",  description="Portal operations by type")
portal_errors    = meter.create_counter("portal.errors.total",          unit="1",  description="Portal errors")
portal_latency   = meter.create_histogram("portal.operation.duration",  unit="ms", description="Portal operation duration")
mulesoft_calls   = meter.create_counter("portal.mulesoft.calls",        unit="1",  description="Calls to MuleSoft")
mulesoft_errors  = meter.create_counter("portal.mulesoft.errors",       unit="1",  description="MuleSoft errors from portal")
mulesoft_latency = meter.create_histogram("portal.mulesoft.latency",    unit="ms", description="MuleSoft response time from portal")
active_tellers   = meter.create_up_down_counter("portal.tellers.active",unit="1",  description="Active teller sessions")

logger_provider = LoggerProvider(resource=resource)
logger_provider.add_log_record_processor(
    BatchLogRecordProcessor(OTLPLogExporter(endpoint=OTLP_ENDPOINT, insecure=True))
)
set_logger_provider(logger_provider)
logging.basicConfig(level=logging.INFO, format='%(asctime)s %(levelname)-8s [fnb-portal] %(message)s')
logger = logging.getLogger("com.fnb.portal")
logger.addHandler(LoggingHandler(level=logging.DEBUG, logger_provider=logger_provider))

app = Flask(__name__)
FlaskInstrumentor().instrument_app(app)
RequestsInstrumentor().instrument()

def call_mulesoft(path: str, method: str = "GET", payload: dict = None,
                  correlation_id: str = None, operation: str = "unknown") -> dict:
    """Call MuleSoft with W3C trace context propagation."""
    url = f"{MULESOFT_URL}{path}"
    correlation_id = correlation_id or str(uuid.uuid4())
    headers = {
        "Content-Type": "application/json",
        "X-Correlation-ID": correlation_id,
        "X-Source-System": "fnb-portal",
        "X-Teller-ID": f"TLR{random.randint(100,999)}",
        "X-Branch-Code": f"BR{random.randint(1,50):03d}",
    }
    # Inject W3C traceparent — this is what connects portal traces to MuleSoft traces
    propagate.inject(headers)

    start = time.time()
    mulesoft_calls.add(1, {"mulesoft.operation": operation})

    try:
        if method == "POST":
            resp = requests.post(url, json=payload, headers=headers, timeout=15)
        else:
            resp = requests.get(url, headers=headers, timeout=15)

        elapsed = (time.time() - start) * 1000
        mulesoft_latency.record(elapsed, {"mulesoft.operation": operation, "http.status_code": str(resp.status_code)})

        if resp.status_code >= 400:
            mulesoft_errors.add(1, {"mulesoft.operation": operation, "http.status_code": str(resp.status_code)})
            logger.warning(f"MuleSoft error | op={operation} status={resp.status_code} latency={elapsed:.1f}ms correlationId={correlation_id}")
            return {"success": False, "status_code": resp.status_code, "error": resp.text}

        logger.info(f"MuleSoft ok | op={operation} status={resp.status_code} latency={elapsed:.1f}ms correlationId={correlation_id}")
        return {"success": True, "body": resp.json(), "latency_ms": elapsed}

    except requests.exceptions.Timeout:
        mulesoft_errors.add(1, {"mulesoft.operation": operation, "http.status_code": "timeout"})
        logger.error(f"MuleSoft timeout | op={operation} correlationId={correlation_id}")
        return {"success": False, "error": "MuleSoft timeout"}

    except Exception as e:
        mulesoft_errors.add(1, {"mulesoft.operation": operation, "http.status_code": "error"})
        logger.error(f"MuleSoft call failed | op={operation} error={e}")
        return {"success": False, "error": str(e)}

# ── Portal API Endpoints ────────────────────────────────────────────────────

@app.route("/portal/payments/wire", methods=["POST"])
def initiate_wire_transfer():
    """Teller initiates a wire transfer → MuleSoft payment-processing-flow."""
    correlation_id = str(uuid.uuid4())
    body = request.get_json(silent=True) or {}
    amount = body.get("amount", random.uniform(1000, 500000))

    active_tellers.add(1)
    portal_requests.add(1, {"portal.operation": "wire_transfer"})
    logger.info(f"Wire transfer initiated | amount={amount:.2f} from={body.get('sourceAccount')} correlationId={correlation_id}")

    with tracer.start_as_current_span("portal.initiateWireTransfer",
        attributes={
            "portal.operation": "wire_transfer",
            "payment.amount": amount,
            "payment.currency": body.get("currency", "USD"),
            "payment.source_account": body.get("sourceAccount", ""),
            "payment.destination": body.get("destinationAccount", ""),
            "portal.correlation_id": correlation_id,
        }):

        start = time.time()
        result = call_mulesoft("/api/payments/wire", method="POST",
            payload={**body, "correlationId": correlation_id, "amount": amount},
            correlation_id=correlation_id, operation="payment-processing-flow")

        elapsed = (time.time() - start) * 1000
        portal_latency.record(elapsed, {"portal.operation": "wire_transfer"})
        active_tellers.add(-1)

        if not result["success"]:
            portal_errors.add(1, {"portal.operation": "wire_transfer"})
            return jsonify({"error": "Payment processing failed", "correlationId": correlation_id}), 502

        return jsonify({**result["body"], "correlationId": correlation_id, "portalLatencyMs": elapsed})

@app.route("/portal/customers/<customer_id>/360", methods=["GET"])
def customer_360(customer_id):
    """Pull full customer 360 view → MuleSoft customer-360-flow."""
    correlation_id = str(uuid.uuid4())
    portal_requests.add(1, {"portal.operation": "customer_360"})

    with tracer.start_as_current_span("portal.getCustomer360",
        attributes={"portal.operation": "customer_360", "customer.id": customer_id}):

        start = time.time()
        result = call_mulesoft(f"/api/customers/{customer_id}/360",
            correlation_id=correlation_id, operation="customer-360-flow")

        elapsed = (time.time() - start) * 1000
        portal_latency.record(elapsed, {"portal.operation": "customer_360"})

        if not result["success"]:
            portal_errors.add(1, {"portal.operation": "customer_360"})
            return jsonify({"error": "Customer lookup failed", "correlationId": correlation_id}), 502

        return jsonify({**result["body"], "correlationId": correlation_id})

@app.route("/portal/accounts/open", methods=["POST"])
def open_account():
    """Open a new account + KYC → MuleSoft account-opening-kyc-flow."""
    correlation_id = str(uuid.uuid4())
    body = request.get_json(silent=True) or {}
    portal_requests.add(1, {"portal.operation": "account_opening"})

    with tracer.start_as_current_span("portal.openAccount",
        attributes={
            "portal.operation": "account_opening",
            "account.type": body.get("accountType", "CHECKING"),
        }):

        start = time.time()
        result = call_mulesoft("/api/accounts/open", method="POST",
            payload={**body, "correlationId": correlation_id},
            correlation_id=correlation_id, operation="account-opening-kyc-flow")

        elapsed = (time.time() - start) * 1000
        portal_latency.record(elapsed, {"portal.operation": "account_opening"})

        if not result["success"]:
            portal_errors.add(1, {"portal.operation": "account_opening"})
            return jsonify({"error": "Account opening failed", "correlationId": correlation_id}), 502

        return jsonify({**result["body"], "correlationId": correlation_id}), 201

@app.route("/portal/payments/ach", methods=["POST"])
def initiate_ach():
    """ACH payment → MuleSoft payment-processing-flow."""
    correlation_id = str(uuid.uuid4())
    body = request.get_json(silent=True) or {}
    amount = body.get("amount", random.uniform(50, 10000))
    portal_requests.add(1, {"portal.operation": "ach_payment"})

    with tracer.start_as_current_span("portal.initiateACH",
        attributes={"portal.operation": "ach_payment", "payment.amount": amount}):

        start = time.time()
        result = call_mulesoft("/api/payments/ach", method="POST",
            payload={**body, "correlationId": correlation_id, "amount": amount},
            correlation_id=correlation_id, operation="payment-processing-flow")

        elapsed = (time.time() - start) * 1000
        portal_latency.record(elapsed, {"portal.operation": "ach_payment"})

        if not result["success"]:
            portal_errors.add(1, {"portal.operation": "ach_payment"})
            return jsonify({"error": "ACH failed", "correlationId": correlation_id}), 502

        return jsonify({**result["body"], "correlationId": correlation_id})

@app.route("/portal/reconciliation/status", methods=["GET"])
def reconciliation_status():
    """Check trade reconciliation status → MuleSoft batch flow."""
    correlation_id = str(uuid.uuid4())
    portal_requests.add(1, {"portal.operation": "reconciliation_status"})

    with tracer.start_as_current_span("portal.getReconciliationStatus"):
        result = call_mulesoft("/api/reconciliation/status",
            correlation_id=correlation_id, operation="trade-reconciliation-batch")

        if not result["success"]:
            return jsonify({"error": "Reconciliation status unavailable"}), 502
        return jsonify(result["body"])

@app.route("/health")
def health():
    return jsonify({"status": "UP", "service": "fnb-portal", "version": "4.2.1"})

# ── Simple status UI ────────────────────────────────────────────────────────
PORTAL_UI = """<!DOCTYPE html>
<html>
<head>
  <title>First National Bank — Internal Portal</title>
  <style>
    body { font-family: Arial, sans-serif; background: #1a2744; color: #fff; padding: 40px; }
    h1 { color: #c8a96e; } h2 { color: #8ab4d4; font-size: 14px; }
    .card { background: #243060; border-radius: 8px; padding: 20px; margin: 10px 0; }
    .status { color: #4caf50; font-weight: bold; }
    table { width: 100%; border-collapse: collapse; }
    td { padding: 8px; border-bottom: 1px solid #344070; font-size: 13px; }
    .badge { padding: 2px 8px; border-radius: 4px; font-size: 11px; }
    .green { background: #1b5e20; } .blue { background: #0d47a1; }
  </style>
</head>
<body>
  <h1>🏦 First National Bank — Internal Operations Portal</h1>
  <div class="card">
    <h2>SYSTEM STATUS</h2>
    <table>
      <tr><td>MuleSoft Integration Platform</td><td><span class="status">● OPERATIONAL</span></td></tr>
      <tr><td>Core Banking (Temenos T24)</td><td><span class="status">● OPERATIONAL</span></td></tr>
      <tr><td>Fraud Detection (FICO Falcon)</td><td><span class="status">● OPERATIONAL</span></td></tr>
      <tr><td>AML Screening (Dow Jones RC)</td><td><span class="status">● OPERATIONAL</span></td></tr>
      <tr><td>CRM (Salesforce FSC)</td><td><span class="status">● OPERATIONAL</span></td></tr>
      <tr><td>Notification Gateway</td><td><span class="status">● OPERATIONAL</span></td></tr>
    </table>
  </div>
  <div class="card">
    <h2>OBSERVABILITY</h2>
    <p>All services instrumented with <span class="badge blue">EDOT Java (MuleSoft)</span>
    and <span class="badge blue">OpenTelemetry</span> — traces, metrics, and logs
    streaming to <strong>Elastic Cloud</strong>.</p>
  </div>
</body>
</html>"""

@app.route("/")
def ui():
    return PORTAL_UI

# ── Burst Load Generator ────────────────────────────────────────────────────
ACCOUNTS = [f"ACC{i:08d}" for i in range(1, 101)]
CUSTOMERS = [f"CUST{i:06d}" for i in range(1, 101)]

def get_request_interval():
    """Return sleep interval based on simulated time of day (wall clock hours mapped to bank hours)."""
    hour = datetime.now().hour
    if 9 <= hour < 17:      # Business hours: high volume
        if hour == 9 or hour == 16:  # Burst at open/close
            return random.uniform(0.3, 0.8)
        return random.uniform(1.0, 3.0)
    elif 0 <= hour < 8:     # Overnight: low volume
        return random.uniform(15.0, 30.0)
    else:                   # After hours: medium
        return random.uniform(5.0, 12.0)

def generate_wire_transfer():
    requests.post("http://localhost:8080/portal/payments/wire", json={
        "sourceAccount": random.choice(ACCOUNTS),
        "destinationAccount": f"EXT{random.randint(10000000,99999999)}",
        "amount": round(random.uniform(1000, 250000), 2),
        "currency": "USD",
        "paymentType": "WIRE",
        "destinationCountry": random.choice(["US", "GB", "DE", "SG", "JP", "CA"]),
        "purpose": random.choice(["TRADE", "INVESTMENT", "PERSONAL", "PAYROLL"]),
    }, timeout=20)

def generate_ach_payment():
    requests.post("http://localhost:8080/portal/payments/ach", json={
        "sourceAccount": random.choice(ACCOUNTS),
        "destinationRouting": f"{random.randint(100000000,999999999)}",
        "destinationAccount": f"{random.randint(10000000,99999999)}",
        "amount": round(random.uniform(50, 10000), 2),
        "currency": "USD",
        "paymentType": "ACH",
        "secCode": random.choice(["PPD", "CCD", "CTX"]),
    }, timeout=20)

def generate_customer_360():
    customer_id = random.choice(CUSTOMERS)
    requests.get(f"http://localhost:8080/portal/customers/{customer_id}/360", timeout=20)

def generate_account_opening():
    requests.post("http://localhost:8080/portal/accounts/open", json={
        "firstName": random.choice(["James", "Sarah", "Michael", "Emily"]),
        "lastName": random.choice(["Smith", "Johnson", "Williams", "Brown"]),
        "dateOfBirth": f"{random.randint(1950,2000)}-{random.randint(1,12):02d}-{random.randint(1,28):02d}",
        "ssn": f"{random.randint(100,999)}-{random.randint(10,99)}-{random.randint(1000,9999)}",
        "accountType": random.choice(["CHECKING", "SAVINGS"]),
        "initialDeposit": round(random.uniform(500, 10000), 2),
        "branchCode": f"BR{random.randint(1,50):03d}",
        "customerType": "INDIVIDUAL",
    }, timeout=30)

OPERATIONS = [
    (generate_wire_transfer,    0.35),   # 35% wire transfers
    (generate_ach_payment,      0.30),   # 30% ACH
    (generate_customer_360,     0.25),   # 25% customer lookups
    (generate_account_opening,  0.10),   # 10% account opening (drops off after hours)
]

def load_generator():
    """Burst-pattern load generator."""
    logger.info("Load generator starting in 20 seconds...")
    time.sleep(20)
    logger.info("Load generator active | pattern=business-hours-burst")

    while True:
        try:
            hour = datetime.now().hour
            # Account opening only during business hours
            ops = OPERATIONS if 9 <= hour < 17 else OPERATIONS[:3]
            weights = [w for _, w in ops]
            total = sum(weights)
            normalized = [w/total for w in weights]

            fn = random.choices([f for f, _ in ops], weights=normalized)[0]
            fn()
        except Exception as e:
            logger.debug(f"Load gen: {e}")

        time.sleep(get_request_interval())

threading.Thread(target=load_generator, daemon=True).start()

if __name__ == "__main__":
    logger.info("FNB Portal starting | version=4.2.1 port=8080")
    app.run(host="0.0.0.0", port=8080, threaded=True)
