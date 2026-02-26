"""
Core Banking Service — First National Bank
Simulates a Temenos / FIS core banking system.

This is the primary failure point in the demo:
  - 10% of account balance queries inject a slow query (simulating table lock
    on accounts_ledger during high-volume payment processing)
  - Slow queries take 4-5 seconds vs normal 150-200ms
  - This is what backs up the MuleSoft payment-processing-flow
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
SLOW_QUERY_RATE = float(os.getenv("SLOW_QUERY_RATE", "0.10"))  # 10% slow queries

resource = Resource.create({
    "service.name": "core-banking-svc",
    "service.version": "8.4.2",
    "service.namespace": "fnb-banking",
    "deployment.environment": "production",
    "host.name": "fnb-corebank-prod-01",
    "db.system": "oracle",
    "system.vendor": "Temenos",
    "system.product": "T24",
})

# Traces
tracer_provider = TracerProvider(resource=resource)
tracer_provider.add_span_processor(
    BatchSpanProcessor(OTLPSpanExporter(endpoint=OTLP_ENDPOINT, insecure=True))
)
trace.set_tracer_provider(tracer_provider)
tracer = trace.get_tracer("fnb.core-banking")

# Metrics
metric_reader = PeriodicExportingMetricReader(
    OTLPMetricExporter(endpoint=OTLP_ENDPOINT, insecure=True),
    export_interval_millis=15000,
)
meter_provider = MeterProvider(resource=resource, metric_readers=[metric_reader])
metrics.set_meter_provider(meter_provider)
meter = metrics.get_meter("fnb.core-banking.metrics")

db_queries        = meter.create_counter("db.queries.total",           unit="1",  description="Total DB queries")
db_slow_queries   = meter.create_counter("db.slow_queries.total",      unit="1",  description="Slow queries (>1s)")
db_query_duration = meter.create_histogram("db.query.duration",        unit="ms", description="Query execution time")
accounts_debited  = meter.create_counter("banking.accounts.debited",   unit="1",  description="Successful account debits")
accounts_credited = meter.create_counter("banking.accounts.credited",  unit="1",  description="Successful account credits")
balance_checks    = meter.create_counter("banking.balance.checks",     unit="1",  description="Balance checks")
txn_amount        = meter.create_histogram("banking.transaction.amount",unit="USD",description="Transaction amount distribution")
active_sessions   = meter.create_up_down_counter("db.sessions.active", unit="1",  description="Active DB sessions")

# Logs
logger_provider = LoggerProvider(resource=resource)
logger_provider.add_log_record_processor(
    BatchLogRecordProcessor(OTLPLogExporter(endpoint=OTLP_ENDPOINT, insecure=True))
)
set_logger_provider(logger_provider)

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s %(levelname)-8s [core-banking-svc] %(message)s'
)
logger = logging.getLogger("com.fnb.corebanking")
logger.addHandler(LoggingHandler(level=logging.DEBUG, logger_provider=logger_provider))

app = Flask(__name__)
FlaskInstrumentor().instrument_app(app)

# ── Simulated account data ──────────────────────────────────────────────────
ACCOUNTS = {f"ACC{i:08d}": {
    "accountNumber": f"ACC{i:08d}",
    "customerId": f"CUST{i:06d}",
    "type": random.choice(["CHECKING", "SAVINGS", "MONEY_MARKET"]),
    "balance": round(random.uniform(1000, 500000), 2),
    "currency": "USD",
    "status": "ACTIVE",
    "openDate": (datetime.now() - timedelta(days=random.randint(30, 3650))).strftime("%Y-%m-%d"),
    "branchCode": f"BR{random.randint(1, 50):03d}",
} for i in range(1, 5001)}

def simulate_db_query(operation: str, table: str, base_latency_ms: float,
                      inject_slow: bool = False, account_id: str = None):
    """Simulate a DB query with optional slow query injection."""
    is_slow = inject_slow and random.random() < SLOW_QUERY_RATE
    latency = base_latency_ms + random.gauss(0, base_latency_ms * 0.15)

    if is_slow:
        # Simulate table lock / unoptimized query
        latency = random.uniform(4000, 5500)

    with tracer.start_as_current_span(f"db.{operation}",
        attributes={
            "db.system": "oracle",
            "db.name": "FNBPROD",
            "db.operation": operation,
            "db.sql.table": table,
            "db.oracle.instance": "FNBPROD01",
            "slow_query": is_slow,
            **({"db.account_id": account_id} if account_id else {}),
        }) as span:

        active_sessions.add(1)
        time.sleep(max(10, latency) / 1000)
        active_sessions.add(-1)

        db_queries.add(1, {"db.operation": operation, "db.sql.table": table})
        db_query_duration.record(latency, {"db.operation": operation, "slow_query": str(is_slow)})

        if is_slow:
            db_slow_queries.add(1, {"db.sql.table": table})
            span.set_attribute("db.slow_query.reason", "table_lock_contention")
            span.set_attribute("db.slow_query.threshold_ms", 1000)
            span.set_attribute("db.slow_query.actual_ms", round(latency))
            logger.warning(
                f"SLOW QUERY DETECTED | table={table} operation={operation} "
                f"duration={latency:.0f}ms threshold=1000ms reason=table_lock_contention "
                f"account={account_id}"
            )

        return is_slow, latency

# ── API Endpoints ───────────────────────────────────────────────────────────
@app.route("/accounts/<account_id>/balance", methods=["GET"])
def get_balance(account_id):
    """Get account balance — primary target for slow query injection."""
    correlation_id = request.headers.get("X-Correlation-ID", str(uuid.uuid4()))

    with tracer.start_as_current_span("core-banking.getBalance",
        attributes={
            "banking.account_id": account_id,
            "banking.operation": "getBalance",
            "banking.correlation_id": correlation_id,
        }):

        logger.info(f"Balance check | account={account_id} correlationId={correlation_id}")

        # This is the slow query injection point
        is_slow, latency = simulate_db_query(
            "SELECT", "accounts_ledger",
            base_latency_ms=180,
            inject_slow=True,
            account_id=account_id
        )

        account = ACCOUNTS.get(account_id, {
            "accountNumber": account_id,
            "balance": round(random.uniform(1000, 100000), 2),
            "currency": "USD",
            "status": "ACTIVE",
        })

        balance_checks.add(1, {"account.type": account.get("type", "UNKNOWN")})

        return jsonify({
            "accountNumber": account_id,
            "balance": account["balance"],
            "availableBalance": round(account["balance"] * 0.98, 2),
            "currency": "USD",
            "asOf": datetime.now().isoformat(),
            "slowQuery": is_slow,
        })

@app.route("/accounts/<account_id>/debit", methods=["POST"])
def debit_account(account_id):
    """Debit account — used in payment processing flow."""
    correlation_id = request.headers.get("X-Correlation-ID", str(uuid.uuid4()))
    body = request.get_json(silent=True) or {}
    amount = body.get("amount", 0)

    with tracer.start_as_current_span("core-banking.debitAccount",
        attributes={
            "banking.account_id": account_id,
            "banking.operation": "debitAccount",
            "banking.amount": amount,
            "banking.currency": body.get("currency", "USD"),
            "banking.correlation_id": correlation_id,
        }):

        logger.info(f"Debit initiated | account={account_id} amount={amount} correlationId={correlation_id}")

        # Validate balance first (slow query injection here)
        simulate_db_query("SELECT", "accounts_ledger", 180, inject_slow=True, account_id=account_id)

        # Write debit transaction
        simulate_db_query("INSERT", "transactions", 80)
        simulate_db_query("UPDATE", "accounts_ledger", 60, account_id=account_id)

        txn_id = f"TXN{uuid.uuid4().hex[:12].upper()}"
        accounts_debited.add(1, {"currency": body.get("currency", "USD")})
        txn_amount.record(amount, {"transaction.type": "DEBIT"})

        logger.info(f"Debit successful | account={account_id} amount={amount} txnId={txn_id} correlationId={correlation_id}")

        return jsonify({
            "transactionId": txn_id,
            "accountNumber": account_id,
            "amount": amount,
            "currency": body.get("currency", "USD"),
            "type": "DEBIT",
            "status": "POSTED",
            "timestamp": datetime.now().isoformat(),
            "reference": body.get("reference", correlation_id),
        })

@app.route("/accounts", methods=["POST"])
def create_account():
    """Create new account — used in account opening / KYC flow."""
    correlation_id = request.headers.get("X-Correlation-ID", str(uuid.uuid4()))
    body = request.get_json(silent=True) or {}

    with tracer.start_as_current_span("core-banking.createAccount",
        attributes={
            "banking.operation": "createAccount",
            "banking.account_type": body.get("accountType", "CHECKING"),
            "banking.correlation_id": correlation_id,
        }):

        logger.info(f"Account creation | customerId={body.get('customerId')} type={body.get('accountType')} correlationId={correlation_id}")

        simulate_db_query("INSERT", "accounts", 120)
        simulate_db_query("INSERT", "accounts_ledger", 80)
        simulate_db_query("INSERT", "audit_log", 40)

        account_number = f"ACC{random.randint(10000000, 99999999)}"

        return jsonify({
            "accountNumber": account_number,
            "customerId": body.get("customerId"),
            "accountType": body.get("accountType", "CHECKING"),
            "status": "ACTIVE",
            "openDate": datetime.now().strftime("%Y-%m-%d"),
            "branchCode": body.get("branchCode", "BR001"),
            "routingNumber": "021000021",
        }), 201

@app.route("/accounts/<account_id>/transactions", methods=["GET"])
def get_transactions(account_id):
    """Get transaction history — used in customer-360 flow."""
    correlation_id = request.headers.get("X-Correlation-ID", str(uuid.uuid4()))
    days = int(request.args.get("days", 30))

    with tracer.start_as_current_span("core-banking.getTransactions",
        attributes={
            "banking.account_id": account_id,
            "banking.operation": "getTransactions",
            "banking.lookback_days": days,
        }):

        # Larger lookback = longer query
        simulate_db_query("SELECT", "transactions", base_latency_ms=50 + (days * 2))

        num_txns = random.randint(5, min(50, days * 3))
        transactions = []
        for i in range(num_txns):
            txn_date = datetime.now() - timedelta(days=random.randint(0, days))
            transactions.append({
                "transactionId": f"TXN{uuid.uuid4().hex[:12].upper()}",
                "date": txn_date.isoformat(),
                "amount": round(random.uniform(10, 5000), 2),
                "type": random.choice(["DEBIT", "CREDIT", "WIRE_IN", "WIRE_OUT", "ACH"]),
                "description": random.choice([
                    "WIRE TRANSFER", "ACH PAYMENT", "POS PURCHASE",
                    "ATM WITHDRAWAL", "DIRECT DEPOSIT", "BILL PAYMENT"
                ]),
                "balance": round(random.uniform(1000, 100000), 2),
            })

        return jsonify({
            "accountNumber": account_id,
            "transactions": transactions,
            "count": len(transactions),
            "fromDate": (datetime.now() - timedelta(days=days)).strftime("%Y-%m-%d"),
            "toDate": datetime.now().strftime("%Y-%m-%d"),
        })

@app.route("/trade-positions", methods=["GET"])
def get_trade_positions():
    """Get open trade positions — used in trade reconciliation batch."""
    correlation_id = request.headers.get("X-Correlation-ID", str(uuid.uuid4()))

    with tracer.start_as_current_span("core-banking.getTradePositions",
        attributes={"banking.operation": "getTradePositions"}):

        simulate_db_query("SELECT", "trade_positions", base_latency_ms=200)
        simulate_db_query("SELECT", "securities_master", base_latency_ms=100)

        num_positions = random.randint(200, 800)
        positions = []
        for i in range(num_positions):
            positions.append({
                "tradeId": f"TRD{uuid.uuid4().hex[:10].upper()}",
                "security": random.choice(["AAPL", "MSFT", "JPM", "BAC", "GS", "MS"]),
                "quantity": random.randint(100, 10000),
                "price": round(random.uniform(50, 500), 2),
                "tradeDate": (datetime.now() - timedelta(days=random.randint(0, 3))).strftime("%Y-%m-%d"),
                "settlementDate": (datetime.now() + timedelta(days=random.randint(1, 2))).strftime("%Y-%m-%d"),
                "status": random.choice(["PENDING", "SETTLED", "FAILED"]),
                "counterparty": random.choice(["JPMORGAN", "GOLDMAN", "CITI", "BOFA"]),
            })

        return jsonify({
            "positions": positions,
            "count": len(positions),
            "asOf": datetime.now().isoformat(),
        })

@app.route("/health")
def health():
    return jsonify({"status": "UP", "service": "core-banking-svc", "system": "Temenos T24 8.4.2"})

if __name__ == "__main__":
    logger.info("Core Banking Service starting | system=Temenos-T24 version=8.4.2 port=9001")
    app.run(host="0.0.0.0", port=9001, threaded=True)
