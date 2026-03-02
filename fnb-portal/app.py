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
ELASTIC_APM_URL = os.getenv("ELASTIC_APM_URL",  "https://mulesoft-otel-demo-55170e.apm.us-central1.gcp.cloud.es.io")

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

    source_account = body.get("sourceAccount", "ACC00000001")
    # Derive customer ID from account number (ACC00000042 → CUST000042)
    try:
        acct_num = int(source_account.replace("ACC", ""))
        customer_id = f"CUST{acct_num:06d}"
    except ValueError:
        customer_id = "CUST000001"

    with tracer.start_as_current_span("portal.initiateWireTransfer",
        attributes={
            "portal.operation": "wire_transfer",
            "customer.id": customer_id,
            "payment.amount": amount,
            "payment.currency": body.get("currency", "USD"),
            "payment.source_account": source_account,
            "payment.destination_account": body.get("destinationAccount", ""),
            "payment.destination_country": body.get("destinationCountry", "US"),
            "payment.purpose": body.get("purpose", "TRADE"),
            "payment.type": "WIRE",
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
            "account.initial_deposit": body.get("initialDeposit", 0),
            "customer.first_name": body.get("firstName", ""),
            "customer.last_name": body.get("lastName", ""),
            "customer.type": body.get("customerType", "INDIVIDUAL"),
            "customer.branch_code": body.get("branchCode", "BR001"),
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

    source_account = body.get("sourceAccount", "ACC00000001")
    try:
        acct_num = int(source_account.replace("ACC", ""))
        customer_id = f"CUST{acct_num:06d}"
    except ValueError:
        customer_id = "CUST000001"

    with tracer.start_as_current_span("portal.initiateACH",
        attributes={
            "portal.operation": "ach_payment",
            "customer.id": customer_id,
            "payment.amount": amount,
            "payment.currency": "USD",
            "payment.source_account": source_account,
            "payment.destination_account": body.get("destinationAccount", ""),
            "payment.destination_routing": body.get("destinationRouting", ""),
            "payment.type": "ACH",
            "payment.sec_code": body.get("secCode", "PPD"),
        }):

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

# ── Interactive Portal UI with Elastic RUM Agent ───────────────────────────
PORTAL_UI = """<!DOCTYPE html>
<html>
<head>
  <title>First National Bank — Internal Portal</title>
  <style>
    * { box-sizing: border-box; margin: 0; padding: 0; }
    body { font-family: 'Segoe UI', Arial, sans-serif; background: #0f1b33; color: #e0e6f0; }
    .topbar { background: #162040; padding: 12px 40px; display: flex; align-items: center;
              border-bottom: 2px solid #c8a96e; }
    .topbar h1 { color: #c8a96e; font-size: 18px; flex: 1; }
    .topbar .user { color: #8ab4d4; font-size: 13px; }
    .container { display: grid; grid-template-columns: 220px 1fr; min-height: calc(100vh - 50px); }
    .sidebar { background: #162040; padding: 20px 0; }
    .sidebar a { display: block; padding: 10px 24px; color: #8ab4d4; text-decoration: none;
                 font-size: 13px; border-left: 3px solid transparent; }
    .sidebar a:hover, .sidebar a.active { background: #1d2d55; color: #fff; border-left-color: #c8a96e; }
    .main { padding: 30px; }
    h2 { color: #c8a96e; font-size: 16px; margin-bottom: 16px; }
    .cards { display: grid; grid-template-columns: repeat(auto-fit, minmax(200px, 1fr)); gap: 16px; margin-bottom: 30px; }
    .card { background: #1a2744; border-radius: 8px; padding: 18px; border: 1px solid #2a3a60; }
    .card .label { color: #7a8aaa; font-size: 11px; text-transform: uppercase; letter-spacing: 1px; }
    .card .value { color: #fff; font-size: 28px; font-weight: 700; margin: 6px 0; }
    .card .sub { color: #4caf50; font-size: 12px; }
    .section { background: #1a2744; border-radius: 8px; padding: 24px; margin-bottom: 24px; border: 1px solid #2a3a60; }
    .form-row { display: flex; gap: 12px; margin-bottom: 12px; flex-wrap: wrap; }
    .form-row label { color: #7a8aaa; font-size: 12px; display: block; margin-bottom: 4px; }
    .form-row input, .form-row select { background: #0f1b33; border: 1px solid #2a3a60; color: #fff;
      padding: 8px 12px; border-radius: 4px; font-size: 13px; width: 180px; }
    .btn { padding: 9px 20px; border: none; border-radius: 4px; cursor: pointer; font-size: 13px; font-weight: 600; }
    .btn-primary { background: #c8a96e; color: #0f1b33; }
    .btn-secondary { background: #2a3a60; color: #8ab4d4; }
    .btn:hover { opacity: 0.85; }
    .btn:disabled { opacity: 0.5; cursor: not-allowed; }
    .status-dot { color: #4caf50; }
    .status-row { display: flex; justify-content: space-between; padding: 8px 0;
                  border-bottom: 1px solid #2a3a60; font-size: 13px; }
    .result { margin-top: 12px; padding: 12px; background: #0f1b33; border-radius: 4px;
              font-family: monospace; font-size: 12px; max-height: 200px; overflow-y: auto;
              white-space: pre-wrap; display: none; }
    .result.show { display: block; }
    .result.error { border-left: 3px solid #f44336; }
    .result.success { border-left: 3px solid #4caf50; }
    .badge { padding: 2px 8px; border-radius: 4px; font-size: 11px; background: #0d47a1; }
  </style>
</head>
<body>
  <div class="topbar">
    <h1>First National Bank — Internal Operations Portal</h1>
    <div class="user">Teller: TLR-{{TELLER_ID}} | Branch: BR-001</div>
  </div>
  <div class="container">
    <div class="sidebar">
      <a href="#" class="active" onclick="showTab('dashboard')">Dashboard</a>
      <a href="#" onclick="showTab('wire')">Wire Transfer</a>
      <a href="#" onclick="showTab('ach')">ACH Payment</a>
      <a href="#" onclick="showTab('customer')">Customer 360</a>
      <a href="#" onclick="showTab('account')">Open Account</a>
      <a href="#" onclick="showTab('status')">System Status</a>
    </div>
    <div class="main">

      <!-- Dashboard Tab -->
      <div id="tab-dashboard" class="tab">
        <h2>Operations Dashboard</h2>
        <div class="cards">
          <div class="card"><div class="label">Wire Transfers Today</div><div class="value" id="stat-wire">—</div><div class="sub">Processing</div></div>
          <div class="card"><div class="label">ACH Payments</div><div class="value" id="stat-ach">—</div><div class="sub">Settled</div></div>
          <div class="card"><div class="label">Customer Lookups</div><div class="value" id="stat-lookup">—</div><div class="sub">Completed</div></div>
          <div class="card"><div class="label">Accounts Opened</div><div class="value" id="stat-acct">—</div><div class="sub">This session</div></div>
        </div>
        <div class="section">
          <h2>Quick Actions</h2>
          <div class="form-row">
            <button class="btn btn-primary" onclick="showTab('wire')">New Wire Transfer</button>
            <button class="btn btn-secondary" onclick="showTab('ach')">New ACH Payment</button>
            <button class="btn btn-secondary" onclick="showTab('customer')">Customer Lookup</button>
            <button class="btn btn-secondary" onclick="showTab('account')">Open Account</button>
          </div>
        </div>
      </div>

      <!-- Wire Transfer Tab -->
      <div id="tab-wire" class="tab" style="display:none">
        <h2>Initiate Wire Transfer</h2>
        <div class="section">
          <div class="form-row">
            <div><label>Source Account</label><input id="wire-src" placeholder="ACC00000001"></div>
            <div><label>Destination Account</label><input id="wire-dst" placeholder="EXT12345678"></div>
            <div><label>Amount (USD)</label><input id="wire-amt" type="number" placeholder="10000.00"></div>
          </div>
          <div class="form-row">
            <div><label>Currency</label><select id="wire-ccy"><option>USD</option><option>EUR</option><option>GBP</option></select></div>
            <div><label>Destination Country</label><select id="wire-country"><option>US</option><option>GB</option><option>DE</option><option>SG</option><option>JP</option></select></div>
            <div><label>Purpose</label><select id="wire-purpose"><option>TRADE</option><option>INVESTMENT</option><option>PERSONAL</option><option>PAYROLL</option></select></div>
          </div>
          <button class="btn btn-primary" id="wire-btn" onclick="submitWire()">Submit Wire Transfer</button>
          <div id="wire-result" class="result"></div>
        </div>
      </div>

      <!-- ACH Payment Tab -->
      <div id="tab-ach" class="tab" style="display:none">
        <h2>Initiate ACH Payment</h2>
        <div class="section">
          <div class="form-row">
            <div><label>Source Account</label><input id="ach-src" placeholder="ACC00000001"></div>
            <div><label>Routing Number</label><input id="ach-routing" placeholder="021000021"></div>
            <div><label>Destination Account</label><input id="ach-dst" placeholder="12345678"></div>
          </div>
          <div class="form-row">
            <div><label>Amount (USD)</label><input id="ach-amt" type="number" placeholder="500.00"></div>
            <div><label>SEC Code</label><select id="ach-sec"><option>PPD</option><option>CCD</option><option>CTX</option></select></div>
          </div>
          <button class="btn btn-primary" id="ach-btn" onclick="submitACH()">Submit ACH Payment</button>
          <div id="ach-result" class="result"></div>
        </div>
      </div>

      <!-- Customer 360 Tab -->
      <div id="tab-customer" class="tab" style="display:none">
        <h2>Customer 360 Lookup</h2>
        <div class="section">
          <div class="form-row">
            <div><label>Customer ID</label><input id="cust-id" placeholder="CUST000001"></div>
            <button class="btn btn-primary" id="cust-btn" onclick="lookupCustomer()" style="align-self:end">Search</button>
          </div>
          <div id="cust-result" class="result"></div>
        </div>
      </div>

      <!-- Open Account Tab -->
      <div id="tab-account" class="tab" style="display:none">
        <h2>Open New Account</h2>
        <div class="section">
          <div class="form-row">
            <div><label>First Name</label><input id="acct-fname" placeholder="James"></div>
            <div><label>Last Name</label><input id="acct-lname" placeholder="Smith"></div>
            <div><label>Date of Birth</label><input id="acct-dob" type="date"></div>
          </div>
          <div class="form-row">
            <div><label>Account Type</label><select id="acct-type"><option>CHECKING</option><option>SAVINGS</option></select></div>
            <div><label>Initial Deposit (USD)</label><input id="acct-deposit" type="number" placeholder="1000.00"></div>
          </div>
          <button class="btn btn-primary" id="acct-btn" onclick="openAccount()">Open Account</button>
          <div id="acct-result" class="result"></div>
        </div>
      </div>

      <!-- System Status Tab -->
      <div id="tab-status" class="tab" style="display:none">
        <h2>System Status</h2>
        <div class="section">
          <div class="status-row"><span>MuleSoft Integration Platform</span><span class="status-dot">● OPERATIONAL</span></div>
          <div class="status-row"><span>Core Banking (Temenos T24)</span><span class="status-dot">● OPERATIONAL</span></div>
          <div class="status-row"><span>Fraud Detection (FICO Falcon)</span><span class="status-dot">● OPERATIONAL</span></div>
          <div class="status-row"><span>AML Screening (Dow Jones RC)</span><span class="status-dot">● OPERATIONAL</span></div>
          <div class="status-row"><span>CRM (Salesforce FSC)</span><span class="status-dot">● OPERATIONAL</span></div>
          <div class="status-row"><span>Notification Gateway</span><span class="status-dot">● OPERATIONAL</span></div>
        </div>
        <div class="section">
          <h2>Observability</h2>
          <p>All services instrumented with <span class="badge">EDOT Java (MuleSoft)</span>
          and <span class="badge">OpenTelemetry</span> — traces, metrics, and logs
          streaming to <strong>Elastic Cloud</strong>.</p>
          <p style="margin-top:8px">RUM agent active — capturing page loads, user interactions, and web vitals.</p>
        </div>
      </div>

    </div>
  </div>

  <!-- Elastic APM RUM Agent -->
  <script src="https://unpkg.com/@elastic/apm-rum@5/dist/bundles/elastic-apm-rum.umd.min.js" crossorigin></script>
  <script>
    var apmServerUrl = '{{APM_SERVER_URL}}';
    if (apmServerUrl) {
      var apm = elasticApm.init({
        serviceName: 'fnb-portal-rum',
        serverUrl: apmServerUrl,
        serviceVersion: '4.2.1',
        environment: 'production',
        distributedTracingOrigins: [window.location.origin],
        transactionSampleRate: 1.0,
        captureBody: 'all',
      });
    }

    var stats = { wire: 0, ach: 0, lookup: 0, acct: 0 };

    function showTab(name) {
      document.querySelectorAll('.tab').forEach(t => t.style.display = 'none');
      document.getElementById('tab-' + name).style.display = 'block';
      document.querySelectorAll('.sidebar a').forEach(a => a.classList.remove('active'));
      event.target.classList.add('active');
    }

    function showResult(id, data, isError) {
      var el = document.getElementById(id);
      el.textContent = JSON.stringify(data, null, 2);
      el.className = 'result show ' + (isError ? 'error' : 'success');
    }

    async function submitWire() {
      var btn = document.getElementById('wire-btn');
      btn.disabled = true; btn.textContent = 'Processing...';
      try {
        var resp = await fetch('/portal/payments/wire', {
          method: 'POST', headers: {'Content-Type':'application/json'},
          body: JSON.stringify({
            sourceAccount: document.getElementById('wire-src').value || 'ACC00000001',
            destinationAccount: document.getElementById('wire-dst').value || 'EXT' + Math.floor(Math.random()*90000000+10000000),
            amount: parseFloat(document.getElementById('wire-amt').value) || Math.round(Math.random()*100000),
            currency: document.getElementById('wire-ccy').value,
            destinationCountry: document.getElementById('wire-country').value,
            purpose: document.getElementById('wire-purpose').value,
          })
        });
        var data = await resp.json();
        showResult('wire-result', data, resp.status >= 400);
        stats.wire++; document.getElementById('stat-wire').textContent = stats.wire;
      } catch(e) { showResult('wire-result', {error: e.message}, true); }
      btn.disabled = false; btn.textContent = 'Submit Wire Transfer';
    }

    async function submitACH() {
      var btn = document.getElementById('ach-btn');
      btn.disabled = true; btn.textContent = 'Processing...';
      try {
        var resp = await fetch('/portal/payments/ach', {
          method: 'POST', headers: {'Content-Type':'application/json'},
          body: JSON.stringify({
            sourceAccount: document.getElementById('ach-src').value || 'ACC00000001',
            destinationRouting: document.getElementById('ach-routing').value || '021000021',
            destinationAccount: document.getElementById('ach-dst').value || '12345678',
            amount: parseFloat(document.getElementById('ach-amt').value) || Math.round(Math.random()*5000),
            currency: 'USD',
            secCode: document.getElementById('ach-sec').value,
          })
        });
        var data = await resp.json();
        showResult('ach-result', data, resp.status >= 400);
        stats.ach++; document.getElementById('stat-ach').textContent = stats.ach;
      } catch(e) { showResult('ach-result', {error: e.message}, true); }
      btn.disabled = false; btn.textContent = 'Submit ACH Payment';
    }

    async function lookupCustomer() {
      var btn = document.getElementById('cust-btn');
      btn.disabled = true; btn.textContent = 'Searching...';
      try {
        var custId = document.getElementById('cust-id').value || 'CUST000001';
        var resp = await fetch('/portal/customers/' + custId + '/360');
        var data = await resp.json();
        showResult('cust-result', data, resp.status >= 400);
        stats.lookup++; document.getElementById('stat-lookup').textContent = stats.lookup;
      } catch(e) { showResult('cust-result', {error: e.message}, true); }
      btn.disabled = false; btn.textContent = 'Search';
    }

    async function openAccount() {
      var btn = document.getElementById('acct-btn');
      btn.disabled = true; btn.textContent = 'Processing...';
      try {
        var resp = await fetch('/portal/accounts/open', {
          method: 'POST', headers: {'Content-Type':'application/json'},
          body: JSON.stringify({
            firstName: document.getElementById('acct-fname').value || 'James',
            lastName: document.getElementById('acct-lname').value || 'Smith',
            dateOfBirth: document.getElementById('acct-dob').value || '1990-01-15',
            accountType: document.getElementById('acct-type').value,
            initialDeposit: parseFloat(document.getElementById('acct-deposit').value) || 1000,
            branchCode: 'BR001',
            customerType: 'INDIVIDUAL',
          })
        });
        var data = await resp.json();
        showResult('acct-result', data, resp.status >= 400);
        stats.acct++; document.getElementById('stat-acct').textContent = stats.acct;
      } catch(e) { showResult('acct-result', {error: e.message}, true); }
      btn.disabled = false; btn.textContent = 'Open Account';
    }
  </script>
</body>
</html>"""

@app.route("/")
def ui():
    teller_id = random.randint(100, 999)
    html = PORTAL_UI.replace("{{TELLER_ID}}", str(teller_id))
    html = html.replace("{{APM_SERVER_URL}}", ELASTIC_APM_URL)
    return html

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
    source_acct = random.choice(ACCOUNTS)
    requests.post("http://localhost:8080/portal/payments/wire", json={
        "sourceAccount": source_acct,
        "destinationAccount": f"EXT{random.randint(10000000,99999999)}",
        "amount": round(random.uniform(1000, 250000), 2),
        "currency": random.choice(["USD", "USD", "USD", "EUR", "GBP"]),
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
