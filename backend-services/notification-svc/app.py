"""
Notification Service — First National Bank
Simulates an SMS/email gateway (Twilio/SendGrid style).

Sends transaction confirmations, fraud alerts, and account notifications.
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
    "service.name": "notification-svc",
    "service.version": "1.5.0",
    "service.namespace": "fnb-banking",
    "deployment.environment": "production",
    "host.name": "fnb-notify-prod-01",
    "system.vendor": "Internal",
    "system.product": "FNB Notification Gateway",
})

tracer_provider = TracerProvider(resource=resource)
tracer_provider.add_span_processor(
    BatchSpanProcessor(OTLPSpanExporter(endpoint=OTLP_ENDPOINT, insecure=True))
)
trace.set_tracer_provider(tracer_provider)
tracer = trace.get_tracer("fnb.notification")

metric_reader = PeriodicExportingMetricReader(
    OTLPMetricExporter(endpoint=OTLP_ENDPOINT, insecure=True),
    export_interval_millis=15000,
)
meter_provider = MeterProvider(resource=resource, metric_readers=[metric_reader])
metrics.set_meter_provider(meter_provider)
meter = metrics.get_meter("fnb.notification.metrics")

notifications_sent   = meter.create_counter("notification.sent.total",     unit="1", description="Notifications sent")
notifications_failed = meter.create_counter("notification.failed.total",   unit="1", description="Notification failures")
notification_latency = meter.create_histogram("notification.send.duration",unit="ms", description="Notification send duration")

logger_provider = LoggerProvider(resource=resource)
logger_provider.add_log_record_processor(
    BatchLogRecordProcessor(OTLPLogExporter(endpoint=OTLP_ENDPOINT, insecure=True))
)
set_logger_provider(logger_provider)
logging.basicConfig(level=logging.INFO, format='%(asctime)s %(levelname)-8s [notification-svc] %(message)s')
logger = logging.getLogger("com.fnb.notification")
logger.addHandler(LoggingHandler(level=logging.DEBUG, logger_provider=logger_provider))

app = Flask(__name__)
FlaskInstrumentor().instrument_app(app)


@app.route("/notify/transaction", methods=["POST"])
def notify_transaction():
    """Send transaction confirmation via SMS + email."""
    correlation_id = request.headers.get("X-Correlation-ID", str(uuid.uuid4()))
    body = request.get_json(silent=True) or {}

    with tracer.start_as_current_span("notification.sendTransactionAlert",
        attributes={
            "notification.correlation_id": correlation_id,
            "notification.type": "TRANSACTION",
            "notification.account": body.get("accountNumber", ""),
            "notification.amount": body.get("amount", 0),
        }) as span:

        start = time.time()
        channels_sent = []

        # Send SMS
        with tracer.start_as_current_span("notification.sendSMS",
            attributes={"notification.channel": "SMS", "notification.provider": "twilio"}) as sms_span:
            time.sleep(random.uniform(0.015, 0.035))
            sms_success = random.random() > 0.02  # 2% failure rate
            sms_span.set_attribute("notification.delivered", sms_success)
            if sms_success:
                channels_sent.append("SMS")
            else:
                notifications_failed.add(1, {"notification.channel": "SMS"})

        # Send email
        with tracer.start_as_current_span("notification.sendEmail",
            attributes={"notification.channel": "EMAIL", "notification.provider": "sendgrid"}) as email_span:
            time.sleep(random.uniform(0.010, 0.025))
            email_success = random.random() > 0.01  # 1% failure rate
            email_span.set_attribute("notification.delivered", email_success)
            if email_success:
                channels_sent.append("EMAIL")
            else:
                notifications_failed.add(1, {"notification.channel": "EMAIL"})

        elapsed = (time.time() - start) * 1000
        notifications_sent.add(len(channels_sent), {"notification.type": "TRANSACTION"})
        notification_latency.record(elapsed, {"notification.type": "TRANSACTION"})

        span.set_attribute("notification.channels_sent", len(channels_sent))

        logger.info(
            f"Transaction notification sent | channels={channels_sent} "
            f"account={body.get('accountNumber')} amount={body.get('amount')} "
            f"correlationId={correlation_id}"
        )

        return jsonify({
            "notificationId": f"NTF{uuid.uuid4().hex[:12].upper()}",
            "type": "TRANSACTION",
            "channels": channels_sent,
            "status": "DELIVERED" if channels_sent else "FAILED",
            "timestamp": datetime.now().isoformat(),
            "correlationId": correlation_id,
        })


@app.route("/notify/fraud-alert", methods=["POST"])
def notify_fraud_alert():
    """Send urgent fraud alert — SMS + email + push."""
    correlation_id = request.headers.get("X-Correlation-ID", str(uuid.uuid4()))
    body = request.get_json(silent=True) or {}

    with tracer.start_as_current_span("notification.sendFraudAlert",
        attributes={
            "notification.type": "FRAUD_ALERT",
            "notification.priority": "HIGH",
            "notification.correlation_id": correlation_id,
        }):

        start = time.time()

        for channel in ["SMS", "EMAIL", "PUSH"]:
            with tracer.start_as_current_span(f"notification.send{channel.title()}",
                attributes={"notification.channel": channel, "notification.priority": "HIGH"}):
                time.sleep(random.uniform(0.010, 0.025))

        elapsed = (time.time() - start) * 1000
        notifications_sent.add(3, {"notification.type": "FRAUD_ALERT"})
        notification_latency.record(elapsed, {"notification.type": "FRAUD_ALERT"})

        logger.warning(f"Fraud alert sent | account={body.get('accountNumber')} correlationId={correlation_id}")

        return jsonify({
            "notificationId": f"NTF{uuid.uuid4().hex[:12].upper()}",
            "type": "FRAUD_ALERT",
            "channels": ["SMS", "EMAIL", "PUSH"],
            "status": "DELIVERED",
            "priority": "HIGH",
            "correlationId": correlation_id,
        })


@app.route("/notify/account-opened", methods=["POST"])
def notify_account_opened():
    """Send welcome notification for new account."""
    correlation_id = request.headers.get("X-Correlation-ID", str(uuid.uuid4()))
    body = request.get_json(silent=True) or {}

    with tracer.start_as_current_span("notification.sendWelcome",
        attributes={"notification.type": "ACCOUNT_OPENED", "notification.correlation_id": correlation_id}):

        start = time.time()

        with tracer.start_as_current_span("notification.sendEmail",
            attributes={"notification.channel": "EMAIL", "notification.template": "welcome_kit"}):
            time.sleep(random.uniform(0.020, 0.040))

        elapsed = (time.time() - start) * 1000
        notifications_sent.add(1, {"notification.type": "ACCOUNT_OPENED"})
        notification_latency.record(elapsed, {"notification.type": "ACCOUNT_OPENED"})

        logger.info(f"Welcome notification sent | correlationId={correlation_id}")

        return jsonify({
            "notificationId": f"NTF{uuid.uuid4().hex[:12].upper()}",
            "type": "ACCOUNT_OPENED",
            "channels": ["EMAIL"],
            "status": "DELIVERED",
            "correlationId": correlation_id,
        })


@app.route("/health")
def health():
    return jsonify({"status": "UP", "service": "notification-svc", "system": "FNB Notification Gateway 1.5"})


if __name__ == "__main__":
    logger.info("Notification Service starting | version=1.5.0 port=9005")
    app.run(host="0.0.0.0", port=9005, threaded=True)
