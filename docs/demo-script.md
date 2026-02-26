# FNB MuleSoft OTel Demo Script — 15 Minutes

## Setup (before the call)
- Both VMs running, all services healthy
- Elastic Kibana open in browser
- APM → Service Map loaded
- Have a terminal ready with the test curl commands from Terraform outputs

---

## Act 1 — "Here's your MuleSoft" (3 min)

**Open: APM → Service Map**

> "This is your integration platform at a glance. MuleSoft is right in the center where you'd expect it — everything flows through it. The FNB portal on the left is your internal operations app. On the right are the five systems MuleSoft orchestrates: core banking, fraud detection, AML screening, your CRM, and the notification gateway."

Point to the node colors/sizes:
> "Node size is throughput, color is error rate. You can already see at a glance which systems are healthy and which are degraded — without clicking into a single log."

**Open: APM → Services → mulesoft-anypoint-runtime**

> "This is what EDOT gives you on the Mule JVM — not just that flows ran, but how long they took, what the error breakdown looks like, and JVM internals like heap and GC pauses. This is everything Anypoint Monitoring shows you, but it's now sitting alongside your logs, traces, and every other system in one place."

Show the latency distribution chart. Point out p99 vs p50:
> "Notice the gap between median and p99 latency. That gap is the story — sometimes payments run at 400ms, sometimes they're hitting 5 seconds. Let's find out why."

---

## Act 2 — "But MuleSoft doesn't live in isolation" (4 min)

**Open: APM → Transactions → payment-processing-wire-flow**

> "Let's look at wire transfers specifically. Here's every transaction the system has processed."

Sort by duration descending. Click the slowest one:

> "This is a payment that took 4.8 seconds. Your customer felt that. Now watch what happens when we open the trace."

**Open the trace waterfall**

Walk through it top to bottom:
> "The FNB portal initiated the payment — that's the root span. It called MuleSoft's payment processing flow here. MuleSoft then called fraud detection — 80ms, totally fine. AML screening — 120ms, cleared. Now look at this."

Point to the core-banking GET /balance span highlighted in red/orange:
> "Core banking. Balance validation. 4,500 milliseconds. That's where your 4.8 seconds went."

Expand the core-banking span, show the attributes:
> "And here's why — `db.sql.table: accounts_ledger`, `slow_query: true`, `slow_query.reason: table_lock_contention`. Your DBA caused a table lock on the accounts ledger during peak hours. Every payment flow was queueing behind it."

> "Without this, you'd have seen a slow payment, correlated timestamps manually across Anypoint Monitoring, core banking Oracle logs, and probably spent four hours before you even looked at the right table."

---

## Act 3 — "Now you can see everything" (4 min)

**Open: APM → Services → core-banking-svc**

> "Switch to the core banking service. From here you can see the problem from the other side — how it looks to Temenos itself, not just how MuleSoft experienced it."

Show the `db.SELECT accounts_ledger` transaction:
> "The slow query is visible as its own span with full DB attributes. You know the table, you know the operation, you know the duration. Your DBA gets a Slack alert with a direct link to this view."

**Open: Dashboards → MuleSoft Runtime Metrics** (or Metrics Explorer)

Build a quick visualization:
- `mule.flow.processing.time` p99 by `mule.flow.name` → show payment flow spike
- `jvm.gc.duration` → show GC pauses correlating with slow queries
- `mulesoft.api.latency` from fnb-portal → "This is what the portal measured — always higher than what MuleSoft reports for itself because it includes network overhead"

> "Three different perspectives — the portal's view, MuleSoft's view, and core banking's view — all in one dashboard, same time axis, correlated by trace ID."

---

## Act 4 — "The compliance angle" (2 min)

**Open: APM → Traces, filter by `aml.ofac_hit: true`**

> "For your compliance team — every AML OFAC hit is a traceable event. You can filter traces to show only payments that hit the OFAC watchlist, see the exact amount, the destination country, the screening ID. When a regulator asks for evidence that you screened a specific transaction, it's a 10-second query instead of a 3-hour document hunt."

**Open: Logs → Log Explorer**

Filter by a specific `correlationId` from one of the traces:
> "That correlation ID from the trace? Paste it into log explorer. You get every log line across every service — MuleSoft, fraud, AML, core banking — for that single transaction, in chronological order. Regulators call this an audit trail. You now have it for free."

---

## Wrap / Next Steps (2 min)

> "What you've seen today is three things: MuleSoft metrics and logs in Elastic — the observability you already wanted. End-to-end distributed traces connecting MuleSoft to every system it touches — the visibility you didn't know you were missing. And a correlated log and trace trail that makes compliance reporting a query instead of a project."

> "The path here is straightforward — attach the EDOT Java agent to your Mule JVM, no code changes, no Anypoint project modifications. You get metrics and logs in a day. Add distributed tracing and you get the full waterfall. We can start with your highest-priority flow — payments, KYC, reconciliation — whichever is causing the most noise today."

---

## Demo URLs (fill in after terraform apply)

| URL | Purpose |
|-----|---------|
| http://APP_VM_IP:8080 | FNB Portal UI |
| http://APP_VM_IP:8080/health | Portal health |
| http://INTEGRATION_VM_IP:8081/health | MuleSoft health |

## Quick Test Commands

```bash
# Wire transfer (triggers full payment flow with fraud + AML + core banking)
curl -X POST http://APP_VM_IP:8080/portal/payments/wire \
  -H "Content-Type: application/json" \
  -d '{"sourceAccount":"ACC00000001","destinationAccount":"EXT12345678","amount":50000,"currency":"USD","paymentType":"WIRE","destinationCountry":"US","purpose":"TRADE"}'

# Customer 360
curl http://APP_VM_IP:8080/portal/customers/CUST000042/360

# Account opening
curl -X POST http://APP_VM_IP:8080/portal/accounts/open \
  -H "Content-Type: application/json" \
  -d '{"firstName":"James","lastName":"Smith","dateOfBirth":"1975-06-15","accountType":"CHECKING","initialDeposit":5000,"branchCode":"BR001","customerType":"INDIVIDUAL"}'

# Trigger reconciliation batch
curl -X POST http://INTEGRATION_VM_IP:8081/api/reconciliation/trigger
```
