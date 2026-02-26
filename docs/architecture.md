# Architecture — FNB MuleSoft OTel Demo

## Overview

This demo shows how Elastic + OpenTelemetry provides end-to-end observability across a MuleSoft integration platform and the downstream banking systems it connects. The architecture is designed to be realistic enough for a convincing demo while being simple enough to deploy in minutes.

## Design Decisions

### Why Two VMs?

The two-VM architecture mirrors real enterprise topology:

- **Integration VM** — runs MuleSoft Anypoint Runtime (the integration layer). In production, this would be CloudHub or Runtime Fabric. Instrumented with **EDOT Java agent** which auto-instruments Mule's HTTP connectors, flow execution, and JVM internals.

- **App VM** — runs the bank portal (what tellers use) and all backend services (what MuleSoft calls). In production, these would be separate systems (Temenos, Salesforce, FICO, etc). Each service is independently instrumented with **OpenTelemetry Python SDK**.

This separation means traces cross a real network boundary between VMs, just like production.

### Trace Flow

```
Browser/Teller
    → FNB Portal (Python/OTel)
        → [HTTP + W3C traceparent header]
    → MuleSoft Anypoint Runtime (Java/EDOT)
        → [HTTP + W3C traceparent propagated by EDOT]
    → Backend Services (Python/OTel)
        → core-banking-svc
        → fraud-detection-svc
        → aml-screening-svc
        → customer-profile-svc
        → notification-svc
```

The W3C `traceparent` header is the glue. The FNB Portal injects it, EDOT propagates it through MuleSoft, and the backend services extract it. This gives one unbroken trace from teller click to database query.

### Signal Pipeline

All services export to a local OTel Collector via OTLP gRPC (port 4317). Each VM runs its own collector:

- **App VM collector** — receives from portal + 5 backend services (Docker network)
- **Integration VM collector** — receives from MuleSoft/EDOT (localhost)

Both collectors forward to Elastic Cloud via the `otlp/elastic` exporter.

### Slow Query Injection

The core demo scenario is a slow query in core-banking-svc:

- `SLOW_QUERY_RATE=0.10` — 10% of balance checks simulate a table lock on `accounts_ledger`
- Normal query: 150-200ms
- Slow query: 4,000-5,500ms

This is realistic (table lock contention during high-volume batch windows is a real problem in core banking). In Elastic APM, the slow span is immediately visible with attributes:
- `db.sql.table: accounts_ledger`
- `slow_query: true`
- `db.slow_query.reason: table_lock_contention`

### Load Generator

Built into the FNB Portal. Uses wall-clock hours to simulate bank traffic patterns:

| Time | Volume | Interval |
|------|--------|----------|
| 00:00-08:00 | Overnight/low | 15-30s between requests |
| 09:00-17:00 | Business/high | 1-3s between requests |
| 09:00, 16:00 | Burst (open/close) | 0.3-0.8s between requests |
| 17:00-00:00 | After hours/medium | 5-12s between requests |

Operation mix: 35% wire transfers, 30% ACH, 25% customer lookups, 10% account opening (business hours only).

## MuleSoft Flows

| Flow | Trigger | Backend Calls |
|------|---------|---------------|
| payment-processing-flow | POST /api/payments/{wire,ach} | fraud → aml → core-banking (balance+debit) → notification |
| customer-360-flow | GET /api/customers/{id}/360 | scatter-gather: (CRM profile+interactions) \|\| (core-banking balance+transactions) |
| account-opening-kyc-flow | POST /api/accounts/open | aml/kyc → CRM create → core-banking create → notification |
| trade-reconciliation-batch | Cron 02:00 + GET /api/reconciliation/status | core-banking trade positions |

## Backend Services

| Service | Port | Simulates | Key Behavior |
|---------|------|-----------|--------------|
| core-banking-svc | 9001 | Temenos T24 | **Slow query injection** on accounts_ledger (10% rate) |
| fraud-detection-svc | 9002 | FICO Falcon | Scores 0-100, flags ~5% as high-risk |
| aml-screening-svc | 9003 | Dow Jones R&C | Screens 5 watchlists, ~2% partial match rate |
| customer-profile-svc | 9004 | Salesforce FSC | CRM profile + interaction history |
| notification-svc | 9005 | Twilio/SendGrid | SMS + email with ~2% delivery failure |

## Infrastructure

### GCP Resources (Terraform)

- VPC network with /24 subnet
- 2 compute instances (preemptible by default)
- Firewall rules: SSH, portal (8080), MuleSoft API (8081), internal VM-to-VM
- Service account with logging write access

### Container Stack (App VM)

All services run as Docker containers via docker-compose:
- OTel Collector (contrib distribution)
- FNB Portal
- 5 backend services
- Shared Docker network for inter-service communication
