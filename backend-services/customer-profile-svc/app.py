"""
Customer Profile Service — First National Bank
Simulates Salesforce Financial Services Cloud (CRM).

Returns customer profiles, account summaries, and interaction history
for the customer-360 flow.
"""

import time
import random
import logging
import os
import uuid
from datetime import datetime, timedelta
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
    "service.name": "customer-profile-svc",
    "service.version": "5.2.0",
    "service.namespace": "fnb-banking",
    "deployment.environment": "production",
    "host.name": "fnb-crm-prod-01",
    "system.vendor": "Salesforce",
    "system.product": "Financial Services Cloud",
})

tracer_provider = TracerProvider(resource=resource)
tracer_provider.add_span_processor(
    BatchSpanProcessor(OTLPSpanExporter(endpoint=OTLP_ENDPOINT, insecure=True))
)
trace.set_tracer_provider(tracer_provider)
tracer = trace.get_tracer("fnb.customer-profile")

metric_reader = PeriodicExportingMetricReader(
    OTLPMetricExporter(endpoint=OTLP_ENDPOINT, insecure=True),
    export_interval_millis=15000,
)
meter_provider = MeterProvider(resource=resource, metric_readers=[metric_reader])
metrics.set_meter_provider(meter_provider)
meter = metrics.get_meter("fnb.customer-profile.metrics")

profile_lookups   = meter.create_counter("crm.profile.lookups",     unit="1",  description="Profile lookups")
profile_creates   = meter.create_counter("crm.profile.creates",     unit="1",  description="Profile creations")
profile_latency   = meter.create_histogram("crm.query.duration",    unit="ms", description="CRM query duration")

logger_provider = LoggerProvider(resource=resource)
logger_provider.add_log_record_processor(
    BatchLogRecordProcessor(OTLPLogExporter(endpoint=OTLP_ENDPOINT, insecure=True))
)
set_logger_provider(logger_provider)
logging.basicConfig(level=logging.INFO, format='%(asctime)s %(levelname)-8s [customer-profile-svc] %(message)s')
logger = logging.getLogger("com.fnb.crm")
logger.addHandler(LoggingHandler(level=logging.DEBUG, logger_provider=logger_provider))

app = Flask(__name__)
FlaskInstrumentor().instrument_app(app)

FIRST_NAMES = ["James", "Sarah", "Michael", "Emily", "Robert", "Jennifer", "David", "Lisa",
               "William", "Maria", "Richard", "Patricia", "Charles", "Linda", "Thomas"]
LAST_NAMES = ["Smith", "Johnson", "Williams", "Brown", "Jones", "Garcia", "Miller",
              "Davis", "Rodriguez", "Martinez", "Anderson", "Taylor", "Thomas", "Moore"]
SEGMENTS = ["PREMIER", "PREFERRED", "STANDARD"]
RELATIONSHIP_MANAGERS = [f"RM{i:03d}" for i in range(1, 21)]


@app.route("/customers/<customer_id>/profile", methods=["GET"])
def get_profile(customer_id):
    """Get customer profile from CRM."""
    correlation_id = request.headers.get("X-Correlation-ID", str(uuid.uuid4()))

    with tracer.start_as_current_span("crm.getProfile",
        attributes={
            "crm.customer_id": customer_id,
            "crm.operation": "getProfile",
            "crm.correlation_id": correlation_id,
        }) as span:

        start = time.time()

        # Simulate SOQL query to Salesforce
        with tracer.start_as_current_span("crm.soqlQuery",
            attributes={"crm.query_type": "SOQL", "crm.object": "Account__c"}):
            time.sleep(random.uniform(0.04, 0.09))

        # Simulate related contacts lookup
        with tracer.start_as_current_span("crm.relatedContacts",
            attributes={"crm.object": "Contact__c"}):
            time.sleep(random.uniform(0.02, 0.05))

        elapsed = (time.time() - start) * 1000
        profile_lookups.add(1)
        profile_latency.record(elapsed, {"crm.operation": "getProfile"})

        # Generate deterministic-ish profile from customer_id
        seed = hash(customer_id) % 1000
        random.seed(seed)
        segment = random.choice(SEGMENTS)
        first = random.choice(FIRST_NAMES)
        last = random.choice(LAST_NAMES)
        random.seed()  # reset

        logger.info(f"Profile lookup | customerId={customer_id} segment={segment} correlationId={correlation_id}")

        return jsonify({
            "customerId": customer_id,
            "firstName": first,
            "lastName": last,
            "email": f"{first.lower()}.{last.lower()}@email.com",
            "phone": f"+1-555-{random.randint(100,999)}-{random.randint(1000,9999)}",
            "segment": segment,
            "relationshipManager": random.choice(RELATIONSHIP_MANAGERS),
            "kycStatus": "VERIFIED",
            "onboardDate": (datetime.now() - timedelta(days=random.randint(90, 3650))).strftime("%Y-%m-%d"),
            "totalRelationshipValue": round(random.uniform(10000, 2000000), 2),
            "products": random.sample(["CHECKING", "SAVINGS", "MORTGAGE", "CREDIT_CARD",
                                       "INVESTMENT", "INSURANCE", "AUTO_LOAN"], k=random.randint(1, 4)),
            "lastInteraction": (datetime.now() - timedelta(days=random.randint(0, 30))).strftime("%Y-%m-%d"),
        })


@app.route("/customers/<customer_id>/interactions", methods=["GET"])
def get_interactions(customer_id):
    """Get recent customer interactions."""
    correlation_id = request.headers.get("X-Correlation-ID", str(uuid.uuid4()))

    with tracer.start_as_current_span("crm.getInteractions",
        attributes={"crm.customer_id": customer_id}):

        start = time.time()
        with tracer.start_as_current_span("crm.soqlQuery",
            attributes={"crm.object": "Interaction__c"}):
            time.sleep(random.uniform(0.03, 0.07))

        elapsed = (time.time() - start) * 1000
        profile_latency.record(elapsed, {"crm.operation": "getInteractions"})

        interactions = []
        for _ in range(random.randint(3, 10)):
            interactions.append({
                "date": (datetime.now() - timedelta(days=random.randint(0, 90))).isoformat(),
                "channel": random.choice(["BRANCH", "PHONE", "ONLINE", "MOBILE", "ATM"]),
                "type": random.choice(["INQUIRY", "TRANSACTION", "COMPLAINT", "SERVICE_REQUEST"]),
                "summary": random.choice([
                    "Balance inquiry", "Wire transfer assistance", "Card replacement",
                    "Account statement request", "Loan inquiry", "Investment consultation",
                ]),
            })

        return jsonify({
            "customerId": customer_id,
            "interactions": interactions,
            "count": len(interactions),
        })


@app.route("/customers", methods=["POST"])
def create_profile():
    """Create new customer profile — used in account opening/KYC flow."""
    correlation_id = request.headers.get("X-Correlation-ID", str(uuid.uuid4()))
    body = request.get_json(silent=True) or {}

    with tracer.start_as_current_span("crm.createProfile",
        attributes={
            "crm.operation": "createProfile",
            "crm.correlation_id": correlation_id,
        }) as span:

        start = time.time()

        with tracer.start_as_current_span("crm.insertRecord",
            attributes={"crm.object": "Account__c"}):
            time.sleep(random.uniform(0.05, 0.10))

        with tracer.start_as_current_span("crm.insertRecord",
            attributes={"crm.object": "Contact__c"}):
            time.sleep(random.uniform(0.03, 0.06))

        elapsed = (time.time() - start) * 1000
        profile_creates.add(1)
        profile_latency.record(elapsed, {"crm.operation": "createProfile"})

        customer_id = f"CUST{random.randint(100000, 999999)}"
        logger.info(f"Profile created | customerId={customer_id} correlationId={correlation_id}")

        return jsonify({
            "customerId": customer_id,
            "firstName": body.get("firstName"),
            "lastName": body.get("lastName"),
            "segment": "STANDARD",
            "kycStatus": "PENDING",
            "createdDate": datetime.now().isoformat(),
        }), 201


@app.route("/health")
def health():
    return jsonify({"status": "UP", "service": "customer-profile-svc", "system": "Salesforce FSC 5.2"})


if __name__ == "__main__":
    logger.info("Customer Profile Service starting | system=Salesforce-FSC version=5.2.0 port=9004")
    app.run(host="0.0.0.0", port=9004, threaded=True)
