"""
AML Screening Service — First National Bank
Simulates OFAC / Dow Jones Risk & Compliance sanctions screening.

Checks names and entities against simulated watchlists.
~2% of checks return a partial match to generate interesting trace data.
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
    "service.name": "aml-screening-svc",
    "service.version": "2.8.0",
    "service.namespace": "fnb-banking",
    "deployment.environment": "production",
    "host.name": "fnb-aml-prod-01",
    "system.vendor": "Dow Jones",
    "system.product": "Risk & Compliance",
})

tracer_provider = TracerProvider(resource=resource)
tracer_provider.add_span_processor(
    BatchSpanProcessor(OTLPSpanExporter(endpoint=OTLP_ENDPOINT, insecure=True))
)
trace.set_tracer_provider(tracer_provider)
tracer = trace.get_tracer("fnb.aml-screening")

metric_reader = PeriodicExportingMetricReader(
    OTLPMetricExporter(endpoint=OTLP_ENDPOINT, insecure=True),
    export_interval_millis=15000,
)
meter_provider = MeterProvider(resource=resource, metric_readers=[metric_reader])
metrics.set_meter_provider(meter_provider)
meter = metrics.get_meter("fnb.aml-screening.metrics")

aml_checks      = meter.create_counter("aml.checks.total",       unit="1", description="Total AML screenings")
aml_hits         = meter.create_counter("aml.hits.total",         unit="1", description="Watchlist hits (partial+exact)")
aml_latency      = meter.create_histogram("aml.check.duration",   unit="ms", description="AML screening duration")
watchlist_size   = meter.create_up_down_counter("aml.watchlist.size", unit="1", description="Active watchlist entries")

logger_provider = LoggerProvider(resource=resource)
logger_provider.add_log_record_processor(
    BatchLogRecordProcessor(OTLPLogExporter(endpoint=OTLP_ENDPOINT, insecure=True))
)
set_logger_provider(logger_provider)
logging.basicConfig(level=logging.INFO, format='%(asctime)s %(levelname)-8s [aml-screening-svc] %(message)s')
logger = logging.getLogger("com.fnb.aml")
logger.addHandler(LoggingHandler(level=logging.DEBUG, logger_provider=logger_provider))

app = Flask(__name__)
FlaskInstrumentor().instrument_app(app)

WATCHLISTS = ["OFAC-SDN", "OFAC-SSI", "EU-SANCTIONS", "UN-CONSOLIDATED", "PEP-DATABASE"]


@app.route("/aml/screen", methods=["POST"])
def aml_screen():
    """Screen a name/entity against sanctions watchlists."""
    correlation_id = request.headers.get("X-Correlation-ID", str(uuid.uuid4()))
    body = request.get_json(silent=True) or {}
    name = body.get("name", body.get("firstName", "Unknown") + " " + body.get("lastName", ""))
    country = body.get("destinationCountry", body.get("country", "US"))
    amount = body.get("amount", 0)

    with tracer.start_as_current_span("aml.screenEntity",
        attributes={
            "aml.correlation_id": correlation_id,
            "aml.entity_name": name,
            "aml.country": country,
            "aml.amount": amount,
            "aml.screening_type": "TRANSACTION",
        }) as span:

        start = time.time()
        hits = []

        # Screen against each watchlist
        for watchlist in WATCHLISTS:
            with tracer.start_as_current_span(f"aml.checkWatchlist.{watchlist}",
                attributes={"aml.watchlist": watchlist}) as wl_span:

                time.sleep(random.uniform(0.015, 0.035))

                # ~2% chance of partial match per watchlist
                if random.random() < 0.02:
                    match_score = random.uniform(0.65, 0.95)
                    hits.append({
                        "watchlist": watchlist,
                        "matchScore": round(match_score, 2),
                        "matchType": "EXACT" if match_score > 0.90 else "PARTIAL",
                        "matchedName": name,
                        "listingDate": "2023-06-15",
                    })
                    wl_span.set_attribute("aml.match_found", True)
                    wl_span.set_attribute("aml.match_score", match_score)

        elapsed = (time.time() - start) * 1000
        status = "CLEAR" if not hits else "HIT"
        risk_level = "NONE" if not hits else ("HIGH" if any(h["matchType"] == "EXACT" for h in hits) else "MEDIUM")

        aml_checks.add(1, {"aml.status": status, "aml.country": country})
        aml_latency.record(elapsed, {"aml.status": status})

        span.set_attribute("aml.status", status)
        span.set_attribute("aml.hit_count", len(hits))
        span.set_attribute("aml.risk_level", risk_level)
        span.set_attribute("aml.watchlists_checked", len(WATCHLISTS))

        if hits:
            aml_hits.add(len(hits), {"aml.risk_level": risk_level})
            logger.warning(
                f"AML HIT | name={name} hits={len(hits)} risk={risk_level} "
                f"country={country} correlationId={correlation_id}"
            )
        else:
            logger.info(f"AML clear | name={name} country={country} correlationId={correlation_id}")

        return jsonify({
            "status": status,
            "riskLevel": risk_level,
            "hits": hits,
            "hitCount": len(hits),
            "watchlistsChecked": WATCHLISTS,
            "screeningDurationMs": round(elapsed, 1),
            "correlationId": correlation_id,
            "recommendation": "BLOCK" if risk_level == "HIGH" else "REVIEW" if hits else "PROCEED",
        })


@app.route("/aml/screen/kyc", methods=["POST"])
def aml_screen_kyc():
    """Enhanced screening for KYC / account opening — checks more lists."""
    correlation_id = request.headers.get("X-Correlation-ID", str(uuid.uuid4()))
    body = request.get_json(silent=True) or {}
    name = f"{body.get('firstName', 'Unknown')} {body.get('lastName', '')}"

    with tracer.start_as_current_span("aml.kycScreening",
        attributes={
            "aml.correlation_id": correlation_id,
            "aml.entity_name": name,
            "aml.screening_type": "KYC",
        }) as span:

        start = time.time()

        # KYC screening is more thorough — additional PEP and adverse media checks
        with tracer.start_as_current_span("aml.pepCheck"):
            time.sleep(random.uniform(0.02, 0.05))
            pep_match = random.random() < 0.01

        with tracer.start_as_current_span("aml.adverseMediaCheck"):
            time.sleep(random.uniform(0.03, 0.06))
            media_hits = random.randint(0, 1) if random.random() < 0.03 else 0

        # Standard watchlist check
        with tracer.start_as_current_span("aml.watchlistScan"):
            time.sleep(random.uniform(0.05, 0.10))
            watchlist_clear = random.random() > 0.02

        elapsed = (time.time() - start) * 1000
        overall_clear = watchlist_clear and not pep_match and media_hits == 0

        span.set_attribute("aml.kyc.pep_match", pep_match)
        span.set_attribute("aml.kyc.adverse_media_hits", media_hits)
        span.set_attribute("aml.kyc.watchlist_clear", watchlist_clear)
        span.set_attribute("aml.kyc.overall_clear", overall_clear)

        aml_checks.add(1, {"aml.status": "CLEAR" if overall_clear else "HIT", "aml.screening_type": "KYC"})
        aml_latency.record(elapsed, {"aml.screening_type": "KYC"})

        logger.info(f"KYC screening | name={name} clear={overall_clear} correlationId={correlation_id}")

        return jsonify({
            "status": "CLEAR" if overall_clear else "REQUIRES_REVIEW",
            "pepMatch": pep_match,
            "adverseMediaHits": media_hits,
            "watchlistClear": watchlist_clear,
            "screeningDurationMs": round(elapsed, 1),
            "correlationId": correlation_id,
        })


@app.route("/health")
def health():
    return jsonify({"status": "UP", "service": "aml-screening-svc", "system": "Dow Jones RC 2.8"})


if __name__ == "__main__":
    logger.info("AML Screening Service starting | system=DowJones-RC version=2.8.0 port=9003")
    app.run(host="0.0.0.0", port=9003, threaded=True)
