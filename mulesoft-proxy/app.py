"""
MuleSoft Anypoint Runtime Mock — First National Bank Integration Layer
Simulates MuleSoft Mule 4.6 Community Runtime with EDOT Java agent.

This proxy mimics MuleSoft's API-led connectivity:
  Experience API (portal-facing) → Process API (orchestration) → System APIs (backends)

Implements the three MuleSoft flows:
  1. payment-processing-flow   — wire/ACH payments
  2. customer-360-flow         — scatter-gather customer data
  3. account-opening-kyc-flow  — sequential KYC + account creation

Instrumented to look like a real EDOT-instrumented Mule runtime:
  - service.name: mulesoft-anypoint-runtime
  - Span names match Mule flow/processor patterns
  - Attributes include mule.flow.name, mule.processor, etc.
"""

import time
import random
import logging
import os
import uuid
import requests
from datetime import datetime
from flask import Flask, request, jsonify

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

OTLP_ENDPOINT = os.getenv("OTLP_ENDPOINT", "http://otel-collector:4317")
BACKEND_HOST  = os.getenv("BACKEND_HOST", "http://localhost")

# Backend service URLs (on same Docker network or localhost)
CORE_BANKING_URL = os.getenv("CORE_BANKING_URL", f"{BACKEND_HOST}:9001")
FRAUD_URL        = os.getenv("FRAUD_URL",        f"{BACKEND_HOST}:9002")
AML_URL          = os.getenv("AML_URL",          f"{BACKEND_HOST}:9003")
CRM_URL          = os.getenv("CRM_URL",          f"{BACKEND_HOST}:9004")
NOTIFICATION_URL = os.getenv("NOTIFICATION_URL",  f"{BACKEND_HOST}:9005")

resource = Resource.create({
    "service.name": "mulesoft-anypoint-runtime",
    "service.version": "4.6.1",
    "service.namespace": "fnb-integration",
    "deployment.environment": "production",
    "host.name": "fnb-mule-prod-01",
    "mule.app.name": "fnb-integration",
    "mule.runtime.version": "4.6.1",
    "mule.region": "us-central1",
    "telemetry.sdk.name": "elastic-otel-java",
    "telemetry.sdk.version": "1.0.0",
})

# Traces
tracer_provider = TracerProvider(resource=resource)
tracer_provider.add_span_processor(
    BatchSpanProcessor(OTLPSpanExporter(endpoint=OTLP_ENDPOINT, insecure=True))
)
trace.set_tracer_provider(tracer_provider)
tracer = trace.get_tracer("mule.flow.processor")

# Metrics
metric_reader = PeriodicExportingMetricReader(
    OTLPMetricExporter(endpoint=OTLP_ENDPOINT, insecure=True),
    export_interval_millis=15000,
)
meter_provider = MeterProvider(resource=resource, metric_readers=[metric_reader])
metrics.set_meter_provider(meter_provider)
meter = metrics.get_meter("mule.runtime.metrics")

flow_executions   = meter.create_counter("mule.flow.executions",       unit="1",  description="Mule flow executions")
flow_errors       = meter.create_counter("mule.flow.errors",           unit="1",  description="Mule flow errors")
flow_duration     = meter.create_histogram("mule.flow.duration",       unit="ms", description="Mule flow execution time")
http_requests     = meter.create_counter("mule.http.requests",         unit="1",  description="HTTP connector requests")
backend_latency   = meter.create_histogram("mule.backend.latency",     unit="ms", description="Backend system call latency")
active_flows      = meter.create_up_down_counter("mule.flows.active",  unit="1",  description="Currently executing flows")
message_count     = meter.create_counter("mule.messages.processed",    unit="1",  description="Messages processed")

# Logs
logger_provider = LoggerProvider(resource=resource)
logger_provider.add_log_record_processor(
    BatchLogRecordProcessor(OTLPLogExporter(endpoint=OTLP_ENDPOINT, insecure=True))
)
set_logger_provider(logger_provider)
logging.basicConfig(level=logging.INFO, format='%(asctime)s %(levelname)-8s [mulesoft-runtime] %(message)s')
logger = logging.getLogger("org.mule.runtime")
logger.addHandler(LoggingHandler(level=logging.DEBUG, logger_provider=logger_provider))

app = Flask(__name__)
FlaskInstrumentor().instrument_app(app)
RequestsInstrumentor().instrument()


def call_backend(service_name, url, method="GET", payload=None, headers=None, correlation_id=""):
    """Call a backend system API with Mule-style span."""
    hdrs = {"Content-Type": "application/json", "X-Correlation-ID": correlation_id}
    if headers:
        hdrs.update(headers)
    propagate.inject(hdrs)

    with tracer.start_as_current_span(f"mule:http:request/{service_name}",
        attributes={
            "mule.processor": "http:request",
            "mule.processor.namespace": "http",
            "http.url": url,
            "http.method": method,
            "mule.correlation_id": correlation_id,
        }) as span:

        start = time.time()
        http_requests.add(1, {"mule.backend": service_name})

        try:
            if method == "POST":
                resp = requests.post(url, json=payload, headers=hdrs, timeout=12)
            else:
                resp = requests.get(url, headers=hdrs, timeout=12)

            elapsed = (time.time() - start) * 1000
            backend_latency.record(elapsed, {"mule.backend": service_name})
            span.set_attribute("http.status_code", resp.status_code)
            span.set_attribute("mule.backend.latency_ms", round(elapsed))

            if resp.status_code >= 400:
                logger.warning(f"Backend error | system={service_name} status={resp.status_code} latency={elapsed:.0f}ms")
                return None, resp.status_code

            return resp.json(), resp.status_code

        except Exception as e:
            elapsed = (time.time() - start) * 1000
            span.set_attribute("error", True)
            span.set_attribute("error.message", str(e))
            logger.error(f"Backend call failed | system={service_name} error={e}")
            return None, 503


# ═══════════════════════════════════════════════════════════════════════════
# Flow 1: Payment Processing Flow (wire + ACH)
# ═══════════════════════════════════════════════════════════════════════════
@app.route("/api/payments/wire", methods=["POST"])
@app.route("/api/payments/ach", methods=["POST"])
def payment_processing_flow():
    """MuleSoft payment-processing-flow: validate → fraud check → debit → notify."""
    correlation_id = request.headers.get("X-Correlation-ID", str(uuid.uuid4()))
    body = request.get_json(silent=True) or {}
    amount = body.get("amount", random.uniform(1000, 50000))
    payment_type = "WIRE" if "wire" in request.path else "ACH"

    active_flows.add(1)
    flow_executions.add(1, {"mule.flow.name": "payment-processing-flow", "payment.type": payment_type})
    message_count.add(1, {"mule.flow.name": "payment-processing-flow"})

    with tracer.start_as_current_span("mule:flow/payment-processing-flow",
        attributes={
            "mule.flow.name": "payment-processing-flow",
            "mule.app.name": "fnb-integration",
            "mule.correlation_id": correlation_id,
            "payment.type": payment_type,
            "payment.amount": amount,
            "payment.currency": body.get("currency", "USD"),
        }) as flow_span:

        start = time.time()
        source_account = body.get("sourceAccount", f"ACC{random.randint(1,100):08d}")

        logger.info(f"Flow started | flow=payment-processing-flow type={payment_type} amount={amount:.2f} correlationId={correlation_id}")

        # Step 1: Validate — DataWeave transform + balance check
        with tracer.start_as_current_span("mule:dw:transform/validate-payment-request",
            attributes={"mule.processor": "ee:transform", "mule.processor.namespace": "ee"}):
            time.sleep(random.uniform(0.005, 0.015))  # DataWeave transform

        balance_data, status = call_backend(
            "core-banking/balance-check",
            f"{CORE_BANKING_URL}/accounts/{source_account}/balance",
            correlation_id=correlation_id)

        if not balance_data:
            flow_errors.add(1, {"mule.flow.name": "payment-processing-flow", "error.step": "balance-check"})
            active_flows.add(-1)
            return jsonify({"error": "Balance check failed", "correlationId": correlation_id}), 502

        # Step 2: Fraud check (parallel in real Mule, sequential here for simplicity)
        with tracer.start_as_current_span("mule:flow-ref/fraud-screening-subflow",
            attributes={"mule.processor": "flow-ref", "mule.flow.ref": "fraud-screening-subflow"}):

            fraud_result, _ = call_backend(
                "fraud-detection/check",
                f"{FRAUD_URL}/fraud/check",
                method="POST",
                payload={
                    "transactionId": f"TXN{uuid.uuid4().hex[:12].upper()}",
                    "accountNumber": source_account,
                    "amount": amount,
                    "currency": body.get("currency", "USD"),
                    "destinationCountry": body.get("destinationCountry", "US"),
                    "paymentType": payment_type,
                },
                correlation_id=correlation_id)

        if fraud_result and fraud_result.get("flagged"):
            logger.warning(f"Transaction flagged by fraud detection | correlationId={correlation_id} score={fraud_result.get('score')}")
            flow_span.set_attribute("payment.fraud_flagged", True)
            flow_span.set_attribute("payment.fraud_score", fraud_result.get("score", 0))

        # Step 3: Debit account
        debit_result, _ = call_backend(
            "core-banking/debit",
            f"{CORE_BANKING_URL}/accounts/{source_account}/debit",
            method="POST",
            payload={"amount": amount, "currency": body.get("currency", "USD"),
                     "reference": correlation_id, "paymentType": payment_type},
            correlation_id=correlation_id)

        if not debit_result:
            flow_errors.add(1, {"mule.flow.name": "payment-processing-flow", "error.step": "debit"})
            active_flows.add(-1)
            return jsonify({"error": "Debit failed", "correlationId": correlation_id}), 502

        # Step 4: Send notification (async in real Mule — fire and forget)
        with tracer.start_as_current_span("mule:async/send-notification",
            attributes={"mule.processor": "async"}):
            call_backend(
                "notification/transaction",
                f"{NOTIFICATION_URL}/notify/transaction",
                method="POST",
                payload={
                    "transactionId": debit_result.get("transactionId", ""),
                    "accountNumber": source_account,
                    "amount": amount,
                    "currency": body.get("currency", "USD"),
                    "type": payment_type,
                    "customerId": f"CUST{random.randint(1,100):06d}",
                },
                correlation_id=correlation_id)

        elapsed = (time.time() - start) * 1000
        flow_duration.record(elapsed, {"mule.flow.name": "payment-processing-flow"})
        flow_span.set_attribute("mule.flow.duration_ms", round(elapsed))
        active_flows.add(-1)

        logger.info(f"Flow completed | flow=payment-processing-flow type={payment_type} duration={elapsed:.0f}ms correlationId={correlation_id}")

        return jsonify({
            "transactionId": debit_result.get("transactionId"),
            "status": "COMPLETED",
            "amount": amount,
            "currency": body.get("currency", "USD"),
            "paymentType": payment_type,
            "fraudScore": fraud_result.get("score") if fraud_result else None,
            "fraudFlagged": fraud_result.get("flagged") if fraud_result else False,
            "timestamp": datetime.now().isoformat(),
            "correlationId": correlation_id,
        })


# ═══════════════════════════════════════════════════════════════════════════
# Flow 2: Customer 360 Flow (scatter-gather)
# ═══════════════════════════════════════════════════════════════════════════
@app.route("/api/customers/<customer_id>/360", methods=["GET"])
def customer_360_flow(customer_id):
    """MuleSoft customer-360-flow: scatter-gather from CRM + core banking."""
    correlation_id = request.headers.get("X-Correlation-ID", str(uuid.uuid4()))

    active_flows.add(1)
    flow_executions.add(1, {"mule.flow.name": "customer-360-flow"})
    message_count.add(1, {"mule.flow.name": "customer-360-flow"})

    with tracer.start_as_current_span("mule:flow/customer-360-flow",
        attributes={
            "mule.flow.name": "customer-360-flow",
            "mule.app.name": "fnb-integration",
            "mule.correlation_id": correlation_id,
            "customer.id": customer_id,
        }) as flow_span:

        start = time.time()
        logger.info(f"Flow started | flow=customer-360-flow customerId={customer_id} correlationId={correlation_id}")

        # Scatter-gather: call CRM + Core Banking in parallel
        # (In real Mule this is a scatter-gather router — here we call sequentially
        #  but wrap in a scatter-gather span for realistic trace shape)
        with tracer.start_as_current_span("mule:scatter-gather",
            attributes={"mule.processor": "scatter-gather"}):

            # Route 1: CRM profile
            profile_data, _ = call_backend(
                "crm/customer-profile",
                f"{CRM_URL}/customers/{customer_id}/profile",
                correlation_id=correlation_id)

            # Route 2: CRM interactions
            interactions_data, _ = call_backend(
                "crm/customer-interactions",
                f"{CRM_URL}/customers/{customer_id}/interactions",
                correlation_id=correlation_id)

            # Route 3: Core banking — account + transactions
            account_id = f"ACC{customer_id.replace('CUST', ''):0>8}"
            balance_data, _ = call_backend(
                "core-banking/balance",
                f"{CORE_BANKING_URL}/accounts/{account_id}/balance",
                correlation_id=correlation_id)

            transactions_data, _ = call_backend(
                "core-banking/transactions",
                f"{CORE_BANKING_URL}/accounts/{account_id}/transactions?days=30",
                correlation_id=correlation_id)

        # DataWeave: merge results
        with tracer.start_as_current_span("mule:dw:transform/merge-customer-360",
            attributes={"mule.processor": "ee:transform", "mule.processor.namespace": "ee"}):
            time.sleep(random.uniform(0.003, 0.010))

        elapsed = (time.time() - start) * 1000
        flow_duration.record(elapsed, {"mule.flow.name": "customer-360-flow"})
        flow_span.set_attribute("mule.flow.duration_ms", round(elapsed))
        active_flows.add(-1)

        logger.info(f"Flow completed | flow=customer-360-flow customerId={customer_id} duration={elapsed:.0f}ms correlationId={correlation_id}")

        return jsonify({
            "customerId": customer_id,
            "profile": profile_data,
            "interactions": interactions_data,
            "accounts": {
                "primary": balance_data,
                "recentTransactions": transactions_data.get("transactions", [])[:10] if transactions_data else [],
            },
            "correlationId": correlation_id,
            "assembledAt": datetime.now().isoformat(),
        })


# ═══════════════════════════════════════════════════════════════════════════
# Flow 3: Account Opening + KYC Flow (sequential)
# ═══════════════════════════════════════════════════════════════════════════
@app.route("/api/accounts/open", methods=["POST"])
def account_opening_kyc_flow():
    """MuleSoft account-opening-kyc-flow: AML/KYC → CRM → core banking → notify."""
    correlation_id = request.headers.get("X-Correlation-ID", str(uuid.uuid4()))
    body = request.get_json(silent=True) or {}

    active_flows.add(1)
    flow_executions.add(1, {"mule.flow.name": "account-opening-kyc-flow"})
    message_count.add(1, {"mule.flow.name": "account-opening-kyc-flow"})

    with tracer.start_as_current_span("mule:flow/account-opening-kyc-flow",
        attributes={
            "mule.flow.name": "account-opening-kyc-flow",
            "mule.app.name": "fnb-integration",
            "mule.correlation_id": correlation_id,
            "account.type": body.get("accountType", "CHECKING"),
        }) as flow_span:

        start = time.time()
        full_name = f"{body.get('firstName', 'John')} {body.get('lastName', 'Doe')}"
        logger.info(f"Flow started | flow=account-opening-kyc-flow applicant={full_name} correlationId={correlation_id}")

        # Step 1: AML/KYC Screening
        with tracer.start_as_current_span("mule:flow-ref/kyc-screening-subflow",
            attributes={"mule.processor": "flow-ref", "mule.flow.ref": "kyc-screening-subflow"}):

            aml_result, _ = call_backend(
                "aml-screening/kyc",
                f"{AML_URL}/aml/screen/kyc",
                method="POST",
                payload={
                    "fullName": full_name,
                    "dateOfBirth": body.get("dateOfBirth", "1990-01-01"),
                    "nationality": body.get("nationality", "US"),
                    "ssn": body.get("ssn", ""),
                    "customerType": body.get("customerType", "INDIVIDUAL"),
                },
                correlation_id=correlation_id)

        if aml_result and aml_result.get("overallResult") == "MATCH":
            logger.warning(f"KYC screening matched | applicant={full_name} correlationId={correlation_id}")
            flow_span.set_attribute("kyc.matched", True)
            flow_errors.add(1, {"mule.flow.name": "account-opening-kyc-flow", "error.step": "kyc-match"})
            active_flows.add(-1)
            return jsonify({
                "status": "REJECTED",
                "reason": "KYC screening flagged",
                "correlationId": correlation_id,
            }), 422

        # Step 2: Create CRM profile
        crm_result, _ = call_backend(
            "crm/create-customer",
            f"{CRM_URL}/customers",
            method="POST",
            payload={
                "firstName": body.get("firstName", "John"),
                "lastName": body.get("lastName", "Doe"),
                "dateOfBirth": body.get("dateOfBirth"),
                "email": body.get("email", f"{body.get('firstName', 'john').lower()}.{body.get('lastName', 'doe').lower()}@example.com"),
                "phone": body.get("phone", "+1-555-0100"),
                "customerType": body.get("customerType", "INDIVIDUAL"),
            },
            correlation_id=correlation_id)

        customer_id = crm_result.get("customerId") if crm_result else f"CUST{random.randint(1,100):06d}"

        # Step 3: Create account in core banking
        account_result, _ = call_backend(
            "core-banking/create-account",
            f"{CORE_BANKING_URL}/accounts",
            method="POST",
            payload={
                "customerId": customer_id,
                "accountType": body.get("accountType", "CHECKING"),
                "initialDeposit": body.get("initialDeposit", 0),
                "branchCode": body.get("branchCode", "BR001"),
            },
            correlation_id=correlation_id)

        # Step 4: Send welcome notification
        with tracer.start_as_current_span("mule:async/send-welcome-notification",
            attributes={"mule.processor": "async"}):
            call_backend(
                "notification/account-opened",
                f"{NOTIFICATION_URL}/notify/account-opened",
                method="POST",
                payload={
                    "customerId": customer_id,
                    "accountNumber": account_result.get("accountNumber") if account_result else "PENDING",
                    "accountType": body.get("accountType", "CHECKING"),
                    "customerName": full_name,
                },
                correlation_id=correlation_id)

        elapsed = (time.time() - start) * 1000
        flow_duration.record(elapsed, {"mule.flow.name": "account-opening-kyc-flow"})
        flow_span.set_attribute("mule.flow.duration_ms", round(elapsed))
        active_flows.add(-1)

        logger.info(f"Flow completed | flow=account-opening-kyc-flow customerId={customer_id} duration={elapsed:.0f}ms correlationId={correlation_id}")

        return jsonify({
            "status": "APPROVED",
            "customerId": customer_id,
            "accountNumber": account_result.get("accountNumber") if account_result else "PENDING",
            "accountType": body.get("accountType", "CHECKING"),
            "kycStatus": "CLEAR",
            "correlationId": correlation_id,
            "timestamp": datetime.now().isoformat(),
        }), 201


# ═══════════════════════════════════════════════════════════════════════════
# Flow 4: Trade Reconciliation Status
# ═══════════════════════════════════════════════════════════════════════════
@app.route("/api/reconciliation/status", methods=["GET"])
def reconciliation_status():
    """MuleSoft trade-reconciliation-batch: status check."""
    correlation_id = request.headers.get("X-Correlation-ID", str(uuid.uuid4()))

    flow_executions.add(1, {"mule.flow.name": "trade-reconciliation-batch"})

    with tracer.start_as_current_span("mule:flow/trade-reconciliation-batch",
        attributes={
            "mule.flow.name": "trade-reconciliation-batch",
            "mule.app.name": "fnb-integration",
        }):

        positions_data, _ = call_backend(
            "core-banking/trade-positions",
            f"{CORE_BANKING_URL}/trade-positions",
            correlation_id=correlation_id)

        total = positions_data.get("count", 0) if positions_data else 0
        matched = int(total * random.uniform(0.92, 0.98))
        breaks = total - matched

        return jsonify({
            "status": "COMPLETED",
            "lastRun": datetime.now().isoformat(),
            "totalPositions": total,
            "matched": matched,
            "breaks": breaks,
            "matchRate": round(matched / max(total, 1) * 100, 1),
            "nextScheduledRun": "T+1 06:00 UTC",
        })


@app.route("/health")
def health():
    return jsonify({
        "status": "UP",
        "service": "mulesoft-anypoint-runtime",
        "version": "4.6.1",
        "agent": "elastic-otel-java",
    })


if __name__ == "__main__":
    logger.info("MuleSoft Anypoint Runtime starting | version=4.6.1 agent=EDOT-Java port=8081")
    app.run(host="0.0.0.0", port=8081, threaded=True)
