"""
Fraud Detection Service — First National Bank
Simulates a FICO Falcon-style real-time fraud scoring engine.

Scores transactions on amount, destination country, velocity, and
randomly flags ~5% as high-risk to generate interesting trace data.
"""

import time
import random
import logging
import os
import uuid
from datetime import datetime
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

OTLP_ENDPOINT = os.getenv("OTLP_ENDPOINT", "http://otel-collector:4317")

resource = Resource.create({
    "service.name": "fraud-detection-svc",
    "service.version": "3.1.0",
    "service.namespace": "fnb-banking",
    "deployment.environment": "production",
    "host.name": "fnb-fraud-prod-01",
    "system.vendor": "FICO",
    "system.product": "Falcon",
})

tracer_provider = TracerProvider(resource=resource)
tracer_provider.add_span_processor(
    BatchSpanProcessor(OTLPSpanExporter(endpoint=OTLP_ENDPOINT, insecure=True))
)
trace.set_tracer_provider(tracer_provider)
tracer = trace.get_tracer("fnb.fraud-detection")

metric_reader = PeriodicExportingMetricReader(
    OTLPMetricExporter(endpoint=OTLP_ENDPOINT, insecure=True),
    export_interval_millis=15000,
)
meter_provider = MeterProvider(resource=resource, metric_readers=[metric_reader])
metrics.set_meter_provider(meter_provider)
meter = metrics.get_meter("fnb.fraud-detection.metrics")

fraud_checks     = meter.create_counter("fraud.checks.total",       unit="1",  description="Total fraud checks")
fraud_flags      = meter.create_counter("fraud.flags.total",        unit="1",  description="Transactions flagged as suspicious")
fraud_score_hist = meter.create_histogram("fraud.score",            unit="1",  description="Fraud score distribution (0-100)")
fraud_latency    = meter.create_histogram("fraud.check.duration",   unit="ms", description="Fraud check duration")

logger_provider = LoggerProvider(resource=resource)
logger_provider.add_log_record_processor(
    BatchLogRecordProcessor(OTLPLogExporter(endpoint=OTLP_ENDPOINT, insecure=True))
)
set_logger_provider(logger_provider)
logging.basicConfig(level=logging.INFO, format='%(asctime)s %(levelname)-8s [fraud-detection-svc] %(message)s')
logger = logging.getLogger("com.fnb.fraud")
logger.addHandler(LoggingHandler(level=logging.DEBUG, logger_provider=logger_provider))

app = Flask(__name__)
FlaskInstrumentor().instrument_app(app)

HIGH_RISK_COUNTRIES = {"IR", "KP", "SY", "CU", "VE", "MM"}
ELEVATED_RISK_COUNTRIES = {"NG", "PK", "AF", "IQ", "LY", "YE"}


def compute_fraud_score(amount, destination_country, account_id):
    """Compute a fraud risk score 0-100."""
    score = random.gauss(15, 8)  # baseline: most transactions are low-risk

    # Amount-based risk
    if amount > 100000:
        score += random.uniform(20, 35)
    elif amount > 50000:
        score += random.uniform(10, 20)
    elif amount > 10000:
        score += random.uniform(5, 10)

    # Country risk
    if destination_country in HIGH_RISK_COUNTRIES:
        score += random.uniform(30, 50)
    elif destination_country in ELEVATED_RISK_COUNTRIES:
        score += random.uniform(15, 25)

    # Random spike (~5% of transactions get flagged)
    if random.random() < 0.05:
        score += random.uniform(25, 40)

    return max(0, min(100, score))


@app.route("/fraud/check", methods=["POST"])
def fraud_check():
    """Real-time fraud scoring for a transaction."""
    correlation_id = request.headers.get("X-Correlation-ID", str(uuid.uuid4()))
    body = request.get_json(silent=True) or {}
    amount = body.get("amount", 0)
    destination_country = body.get("destinationCountry", "US")
    account_id = body.get("sourceAccount", "UNKNOWN")

    with tracer.start_as_current_span("fraud.scoreTransaction",
        attributes={
            "fraud.correlation_id": correlation_id,
            "fraud.amount": amount,
            "fraud.destination_country": destination_country,
            "fraud.account_id": account_id,
        }) as span:

        start = time.time()

        # Simulate model inference
        with tracer.start_as_current_span("fraud.modelInference",
            attributes={"fraud.model": "falcon-v3.1", "fraud.model_type": "gradient_boost"}):
            time.sleep(random.uniform(0.03, 0.08))

        # Simulate velocity check (recent transaction lookup)
        with tracer.start_as_current_span("fraud.velocityCheck",
            attributes={"fraud.lookback_hours": 24}):
            time.sleep(random.uniform(0.01, 0.03))

        score = compute_fraud_score(amount, destination_country, account_id)
        risk_level = "LOW" if score < 30 else "MEDIUM" if score < 60 else "HIGH"
        flagged = score >= 70

        elapsed = (time.time() - start) * 1000
        fraud_checks.add(1, {"fraud.risk_level": risk_level})
        fraud_score_hist.record(score, {"fraud.risk_level": risk_level})
        fraud_latency.record(elapsed, {"fraud.risk_level": risk_level})

        span.set_attribute("fraud.score", round(score, 1))
        span.set_attribute("fraud.risk_level", risk_level)
        span.set_attribute("fraud.flagged", flagged)

        if flagged:
            fraud_flags.add(1, {"fraud.destination_country": destination_country})
            logger.warning(
                f"FRAUD ALERT | score={score:.1f} risk={risk_level} amount={amount} "
                f"country={destination_country} account={account_id} correlationId={correlation_id}"
            )
        else:
            logger.info(
                f"Fraud check passed | score={score:.1f} risk={risk_level} "
                f"amount={amount} correlationId={correlation_id}"
            )

        return jsonify({
            "fraudScore": round(score, 1),
            "riskLevel": risk_level,
            "flagged": flagged,
            "recommendation": "BLOCK" if score >= 85 else "REVIEW" if flagged else "APPROVE",
            "modelVersion": "falcon-v3.1",
            "checkDurationMs": round(elapsed, 1),
            "correlationId": correlation_id,
        })


@app.route("/health")
def health():
    return jsonify({"status": "UP", "service": "fraud-detection-svc", "system": "FICO Falcon 3.1"})


if __name__ == "__main__":
    logger.info("Fraud Detection Service starting | system=FICO-Falcon version=3.1.0 port=9002")
    app.run(host="0.0.0.0", port=9002, threaded=True)
