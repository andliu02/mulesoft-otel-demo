# FNB MuleSoft OTel Demo

End-to-end observability demo for a fictional bank (**First National Bank**) showing how Elastic + OpenTelemetry provides full visibility across a MuleSoft integration platform and the downstream banking systems it connects.

## The Story

> *"Your team runs MuleSoft as the integration backbone — connecting your core banking system, fraud detection, AML screening, and CRM. Anypoint Monitoring tells you your flows ran. Elastic tells you what every system did in response. When a core banking slow query backs up your entire payment pipeline, you go from a 4-hour investigation to a 30-second trace."*

## Architecture

```
[Browser / Teller]
      │
      ▼
[FNB Portal]  ──── OTel instrumented (Python/Flask)
      │
      │ HTTP + W3C traceparent
      ▼
[MuleSoft Anypoint Runtime]  ──── EDOT Java agent
   ├── payment-processing-flow        (wire/ACH)
   ├── customer-360-flow              (account + profile aggregation)
   ├── account-opening-kyc-flow       (onboarding)
   └── trade-reconciliation-batch     (nightly batch)
      │
      │ HTTP + W3C traceparent propagated
      ▼
[Banking Backend Services]  ──── OTel instrumented (Python/Flask)
   ├── core-banking-svc       (Temenos/FIS mock)   :9001
   ├── fraud-detection-svc    (real-time scoring)  :9002
   ├── aml-screening-svc      (OFAC/sanctions)     :9003
   ├── customer-profile-svc   (Salesforce/CRM)     :9004
   └── notification-svc       (SMS/email gateway)  :9005
      │
      ▼
[OTel Collector]  ──── routes all signals to Elastic Cloud
      │
      ▼
[Elastic Cloud]
   ├── APM → Service Map (full topology)
   ├── APM → Traces (end-to-end waterfalls)
   ├── Dashboards → MuleSoft runtime metrics
   └── Logs → correlated across all services
```

## Key Demo Scenario

**Core banking slow query backing up the payment flow:**

```
Normal payment [420ms]              Slow query payment [4,800ms]
  ├── fraud-detection  [80ms]  ✅     ├── fraud-detection  [80ms]   ✅
  ├── aml-screening    [120ms] ✅     ├── aml-screening    [120ms]  ✅
  ├── core-banking     [180ms] ✅     ├── core-banking     [4,500ms] ⚠️
  └── notification     [40ms]  ✅     └── notification     [40ms]   ✅
```

Alert fires on p99 → click to trace → slow span visible instantly → `db.sql.table: accounts_ledger`, `slow_query: true` in span attributes.

## Infrastructure

Two GCP VMs on a shared VPC:

| VM | Type | Runs |
|---|---|---|
| `aliu-fnb-integration-vm` | e2-standard-2 | MuleSoft + OTel Collector |
| `aliu-fnb-app-vm` | e2-standard-4 | FNB Portal + 5 backend services |

## Quick Start

```bash
# 1. Clone
git clone https://github.com/YOUR_ORG/fnb-mulesoft-otel-demo
cd fnb-mulesoft-otel-demo

# 2. Configure
cd terraform
cp terraform.tfvars.example terraform.tfvars
# edit terraform.tfvars with your GCP project + Elastic credentials

# 3. Deploy
terraform init
terraform apply

# 4. Watch startup
gcloud compute ssh aliu-fnb-integration-vm --zone=us-central1-a -- \
  'tail -f /var/log/demo-startup.log'
```

After ~3 minutes both VMs are live and generating traffic automatically.

## Repository Structure

```
fnb-mulesoft-otel-demo/
├── README.md
├── .gitignore
├── docker-compose.yml          # app-vm: portal + backend services
├── docs/
│   ├── architecture.md         # detailed architecture decisions
│   └── demo-script.md          # 15-minute demo walkthrough
├── terraform/
│   ├── main.tf                 # two-VM GCP infrastructure
│   ├── variables.tf
│   ├── outputs.tf
│   ├── startup-integration-vm.sh.tpl
│   ├── startup-app-vm.sh.tpl
│   └── terraform.tfvars.example
├── mulesoft/
│   ├── flows/
│   │   ├── payment-processing-flow.xml
│   │   ├── customer-360-flow.xml
│   │   ├── account-opening-kyc-flow.xml
│   │   └── trade-reconciliation-batch-flow.xml
│   └── edot-setup.md           # how EDOT Java agent attaches to Mule
├── fnb-portal/
│   ├── app.py                  # bank portal (Tier 1)
│   ├── requirements.txt
│   └── Dockerfile
├── backend-services/
│   ├── core-banking-svc/       # Temenos/FIS mock
│   ├── fraud-detection-svc/    # real-time fraud scoring
│   ├── aml-screening-svc/      # OFAC/sanctions
│   ├── customer-profile-svc/   # Salesforce/CRM mock
│   └── notification-svc/       # SMS/email gateway
└── otel-collector/
    └── otel-collector-config.yml
```

## Signals in Elastic

| Signal | Source | Key fields |
|---|---|---|
| Traces | EDOT Java (Mule) + OTel (all services) | `mule.flow.name`, `mule.correlation.id`, `mule.error.type` |
| Metrics | EDOT Java (Mule JVM) + OTel (services) | `mule.flow.processing.time`, `jvm.heap.used`, `http.client.request.duration` |
| Logs | All services via OTel Collector | Correlated by `trace.id` across all 7 services |

## Load Pattern

```
00:00 - 08:00  Overnight   Low volume + trade reconciliation batch at 02:00
09:00 - 17:00  Business    High volume, burst at open (09:00) and close (16:00)
17:00 - 00:00  After hours Medium volume, account opening drops off
```

## Requirements

- GCP project with billing enabled
- Elastic Cloud deployment (free trial works)
- Terraform >= 1.5
- gcloud CLI authenticated
- Mac/Linux for running Terraform locally
