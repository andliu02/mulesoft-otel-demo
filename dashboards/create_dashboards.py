#!/usr/bin/env python3
"""
Create Kibana dashboards for the FNB MuleSoft OTel Demo.

Builds 13 dashboards via the Kibana Saved Objects API:
  Phase 1 (Pure MuleSoft Metrics — EDOT out of the box):
    1. MuleSoft Runtime Metrics
  Phase 2 (OTel with MuleSoft — adding OTel instrumentation to MuleSoft):
    4. Distributed Tracing — Service Flow
    6. Key Transactions & Span Breakdown
    8. MuleSoft Anypoint Runtime
  Phase 3 (OTel with All Applications — full stack observability):
    2. Payment Operations — Business Metrics
    3. Core Banking — Database Performance
    5. Operations Command Center
    7, 9-13. Individual service dashboards

Usage:
    python create_dashboards.py --kibana-url <URL> --api-key <KEY>

Field naming (Elastic OTel passthrough mapping):
    Metric values:  mule.flow.executions      (short alias of metrics.mule.flow.executions)
    Attributes:     mule.flow.name            (short alias of attributes.mule.flow.name)
"""

import argparse
import json
import os
import sys
import requests as http_requests

METRICS_INDEX = "metrics-*"
TRACES_INDEX = "traces-*"
LOGS_INDEX = "logs-*"


class KibanaClient:
    def __init__(self, kibana_url, api_key):
        self.base = kibana_url.rstrip("/")
        self.headers = {
            "Authorization": f"ApiKey {api_key}",
            "kbn-xsrf": "true",
            "Content-Type": "application/json",
        }
        self.created = 0
        self.errors = 0

    def _request(self, method, path, body=None):
        url = f"{self.base}{path}"
        return http_requests.request(method, url, headers=self.headers,
                                      json=body, timeout=30)

    def create_data_view(self, dv_id, title, time_field="@timestamp"):
        resp = self._request("POST", "/api/data_views/data_view", {
            "data_view": {"id": dv_id, "title": title, "timeFieldName": time_field},
            "override": True,
        })
        status = "OK" if resp.status_code in (200, 201) else f"WARN ({resp.status_code})"
        print(f"    {title} → {status}")
        return resp

    def upsert(self, obj_type, obj_id, attributes, references=None):
        body = {"attributes": attributes}
        if references:
            body["references"] = references
        resp = self._request("POST", f"/api/saved_objects/{obj_type}/{obj_id}?overwrite=true", body)
        if resp.status_code == 409:
            resp = self._request("PUT", f"/api/saved_objects/{obj_type}/{obj_id}", body)
        if resp.status_code in (200, 201):
            self.created += 1
        else:
            self.errors += 1
            print(f"    ERROR {obj_type}/{obj_id}: {resp.status_code} {resp.text[:200]}")
        return resp


# ─── Human-readable field labels ────────────────────────────────────────────

FIELD_LABELS = {
    # MuleSoft
    "mule.flow.name":         "Flow Name",
    "mule.flow.executions":   "Flow Executions",
    "mule.http.requests":     "HTTP Requests",
    "mule.messages.processed": "Messages Processed",
    "mule.flows.active":      "Active Flows",
    "mule.backend":           "Backend",
    "mulesoft.operation":     "Operation",
    "payment.type":           "Payment Type",
    # Portal / Payment
    "portal.requests.total":  "Portal Requests",
    "portal.errors.total":    "Portal Errors",
    "portal.operation":       "Operation",
    "portal.mulesoft.calls":  "MuleSoft Calls",
    "portal.mulesoft.errors": "MuleSoft Errors",
    "portal.tellers.active":  "Active Tellers",
    # Fraud / Notification
    "fraud.checks.total":     "Fraud Checks",
    "fraud.risk_level":       "Risk Level",
    "notification.sent.total":"Notifications Sent",
    "notification.type":      "Type",
    # Database
    "db.queries.total":       "DB Queries",
    "db.slow_queries.total":  "Slow Queries",
    "db.sessions.active":     "Active Sessions",
    "db.operation":           "Operation",
    "db.sql.table":           "Table",
    "banking.accounts.debited":"Accounts Debited",
    "banking.balance.checks": "Balance Checks",
    "account.type":           "Account Type",
    # Tracing
    "service.name":           "Service",
    "span.name":              "Transaction",
    "span.duration.us":       "Duration",
    "trace_id":               "Trace ID",
    "span_id":                "Span ID",
    "event.outcome":          "Outcome",
    "http.status_code":       "Status Code",
    # Logs
    "log.level":              "Log Level",
    "@timestamp":             "Timestamp",
}


def _label(field):
    """Return human-readable label for a field name."""
    return FIELD_LABELS.get(field, field)


# ─── Visualization builders ─────────────────────────────────────────────────

def lens_metric(c, vid, title, field, agg="sum", dv="metrics-otel", subtitle="", query=""):
    attrs = {
        "title": title,
        "visualizationType": "lnsMetric",
        "state": {
            "visualization": {"layerId": "l1", "layerType": "data", "metricAccessor": "m",
                              **({"subtitle": subtitle} if subtitle else {})},
            "query": {"query": query, "language": "kuery"}, "filters": [],
            "datasourceStates": {"formBased": {"layers": {"l1": {
                "columnOrder": ["m"],
                "columns": {"m": {"dataType": "number", "isBucketed": False, "label": title,
                                  "operationType": agg,
                                  **({"sourceField": field} if agg != "count" else {}),
                                  "params": {}}},
                "incompleteColumns": {},
            }}}},
        },
    }
    return c.upsert("lens", vid, attrs, [
        {"id": dv, "name": "indexpattern-datasource-layer-l1", "type": "index-pattern"},
        {"id": dv, "name": "indexpattern-datasource-current-indexpattern", "type": "index-pattern"},
    ])


def _formula_metric(c, vid, title, formula, dv="traces-otel", subtitle="", query=""):
    """Lens metric using a formula (e.g. 'average(field) / 1000')."""
    attrs = {
        "title": title,
        "visualizationType": "lnsMetric",
        "state": {
            "visualization": {"layerId": "l1", "layerType": "data", "metricAccessor": "m",
                              **({"subtitle": subtitle} if subtitle else {})},
            "query": {"query": query, "language": "kuery"}, "filters": [],
            "datasourceStates": {"formBased": {"layers": {"l1": {
                "columnOrder": ["m"],
                "columns": {"m": {"dataType": "number", "isBucketed": False, "label": title,
                                  "operationType": "formula",
                                  "params": {"formula": formula, "isFormulaBroken": False},
                                  "references": []}},
                "incompleteColumns": {},
            }}}},
        },
    }
    return c.upsert("lens", vid, attrs, [
        {"id": dv, "name": "indexpattern-datasource-layer-l1", "type": "index-pattern"},
        {"id": dv, "name": "indexpattern-datasource-current-indexpattern", "type": "index-pattern"},
    ])


def _formula_xy(c, vid, title, formula, chart="line", split=None, dv="traces-otel",
                ylabel=None, query=""):
    """Lens XY chart with a formula-based y-axis."""
    cols = {
        "x": {"dataType": "date", "isBucketed": True, "label": "Time",
               "operationType": "date_histogram", "sourceField": "@timestamp",
               "params": {"interval": "auto"}},
        "y": {"dataType": "number", "isBucketed": False, "label": ylabel or title,
               "operationType": "formula",
               "params": {"formula": formula, "isFormulaBroken": False},
               "references": []},
    }
    order = ["x", "y"]
    layer = {"layerId": "l1", "layerType": "data", "seriesType": chart,
             "accessors": ["y"], "xAccessor": "x"}
    if split:
        cols["s"] = {"dataType": "string", "isBucketed": True, "label": _label(split),
                     "operationType": "terms", "sourceField": split,
                     "params": {"size": 10,
                                "orderBy": {"type": "alphabetical"},
                                "orderDirection": "asc"}}
        order = ["x", "s", "y"]
        layer["splitAccessor"] = "s"
    attrs = {
        "title": title, "visualizationType": "lnsXY",
        "state": {
            "visualization": {
                "layers": [layer],
                "preferredSeriesType": chart,
                "legend": {"isVisible": True, "position": "right"},
                "valueLabels": "hide",
            },
            "query": {"query": query, "language": "kuery"}, "filters": [],
            "datasourceStates": {"formBased": {"layers": {"l1": {
                "columnOrder": order, "columns": cols, "incompleteColumns": {},
            }}}},
        },
    }
    return c.upsert("lens", vid, attrs, [
        {"id": dv, "name": "indexpattern-datasource-layer-l1", "type": "index-pattern"},
        {"id": dv, "name": "indexpattern-datasource-current-indexpattern", "type": "index-pattern"},
    ])


def lens_xy(c, vid, title, field, agg="sum", chart="bar_stacked", split=None,
            dv="metrics-otel", ylabel=None, query=""):
    cols = {
        "x": {"dataType": "date", "isBucketed": True, "label": "Time",
               "operationType": "date_histogram", "sourceField": "@timestamp",
               "params": {"interval": "auto"}},
        "y": {"dataType": "number", "isBucketed": False, "label": ylabel or title,
               "operationType": agg, "sourceField": field,
               "params": {}},
    }
    order = ["x", "y"]
    layer = {"layerId": "l1", "layerType": "data", "seriesType": chart,
             "accessors": ["y"], "xAccessor": "x"}
    if split:
        cols["s"] = {"dataType": "string", "isBucketed": True, "label": _label(split),
                     "operationType": "terms", "sourceField": split,
                     "params": {"size": 10, "orderBy": {"type": "column", "columnId": "y"},
                                "orderDirection": "desc"}}
        order = ["x", "s", "y"]
        layer["splitAccessor"] = "s"

    attrs = {
        "title": title, "visualizationType": "lnsXY",
        "state": {
            "visualization": {
                "layers": [layer],
                "preferredSeriesType": chart,
                "legend": {"isVisible": True, "position": "right"},
                "valueLabels": "hide",
            },
            "query": {"query": query, "language": "kuery"}, "filters": [],
            "datasourceStates": {"formBased": {"layers": {"l1": {
                "columnOrder": order, "columns": cols, "incompleteColumns": {},
            }}}},
        },
    }
    return c.upsert("lens", vid, attrs, [
        {"id": dv, "name": "indexpattern-datasource-layer-l1", "type": "index-pattern"},
        {"id": dv, "name": "indexpattern-datasource-current-indexpattern", "type": "index-pattern"},
    ])


def lens_pie(c, vid, title, field, slice_field, agg="sum", dv="metrics-otel", query=""):
    cols = {
        "s": {"dataType": "string", "isBucketed": True, "label": _label(slice_field),
              "operationType": "terms", "sourceField": slice_field,
              "params": {"size": 10, "orderBy": {"type": "column", "columnId": "m"},
                         "orderDirection": "desc"}},
        "m": {"dataType": "number", "isBucketed": False, "label": title,
              "operationType": agg, "sourceField": field,
              "params": {}},
    }
    attrs = {
        "title": title, "visualizationType": "lnsPie",
        "state": {
            "visualization": {"layers": [{"layerId": "l1", "layerType": "data",
                                           "primaryGroups": ["s"], "metrics": ["m"],
                                           "numberDisplay": "percent", "categoryDisplay": "default",
                                           "legendDisplay": "default"}],
                              "shape": "donut",
                              "legend": {"isVisible": True, "position": "right"}},
            "query": {"query": query, "language": "kuery"}, "filters": [],
            "datasourceStates": {"formBased": {"layers": {"l1": {
                "columnOrder": ["s", "m"], "columns": cols, "incompleteColumns": {},
            }}}},
        },
    }
    return c.upsert("lens", vid, attrs, [
        {"id": dv, "name": "indexpattern-datasource-layer-l1", "type": "index-pattern"},
        {"id": dv, "name": "indexpattern-datasource-current-indexpattern", "type": "index-pattern"},
    ])


def lens_table(c, vid, title, col_cfgs, dv="logs-otel", query=""):
    cols = {}
    order = []
    for i, (field, label, op) in enumerate(col_cfgs):
        cid = f"c{i}"
        order.append(cid)
        if op == "terms":
            cols[cid] = {"dataType": "string", "isBucketed": True, "label": label,
                         "operationType": "terms", "sourceField": field,
                         "params": {"size": 20, "orderDirection": "desc",
                                    "orderBy": {"type": "alphabetical"}}}
        elif op == "count":
            cols[cid] = {"dataType": "number", "isBucketed": False, "label": label,
                         "operationType": "count", "sourceField": field, "params": {}}
        elif op.startswith("formula:"):
            cols[cid] = {"dataType": "number", "isBucketed": False, "label": label,
                         "operationType": "formula",
                         "params": {"formula": op[len("formula:"):], "isFormulaBroken": False},
                         "references": []}
        else:
            cols[cid] = {"dataType": "number", "isBucketed": False, "label": label,
                         "operationType": op, "sourceField": field, "params": {}}
    attrs = {
        "title": title, "visualizationType": "lnsDatatable",
        "state": {
            "visualization": {"layerId": "l1", "layerType": "data",
                              "columns": [{"columnId": c} for c in order]},
            "query": {"query": query, "language": "kuery"}, "filters": [],
            "datasourceStates": {"formBased": {"layers": {"l1": {
                "columnOrder": order, "columns": cols, "incompleteColumns": {},
            }}}},
        },
    }
    return c.upsert("lens", vid, attrs, [
        {"id": dv, "name": "indexpattern-datasource-layer-l1", "type": "index-pattern"},
        {"id": dv, "name": "indexpattern-datasource-current-indexpattern", "type": "index-pattern"},
    ])


def markdown(c, vid, title, content):
    return c.upsert("visualization", vid, {
        "title": title,
        "visState": json.dumps({"title": title, "type": "markdown", "aggs": [],
                                "params": {"markdown": content, "openLinksInNewTab": True, "fontSize": 12}}),
        "uiStateJSON": "{}",
        "description": "",
        "kibanaSavedObjectMeta": {
            "searchSourceJSON": json.dumps({"query": {"query": "", "language": "kuery"}, "filter": []})}})


def dashboard(c, did, title, desc, panels):
    plist, refs = [], []
    for i, (vid, vtype, x, y, w, h) in enumerate(panels):
        pid = f"p{i}"
        plist.append({"version": "8.15.0", "type": vtype,
                      "gridData": {"x": x, "y": y, "w": w, "h": h, "i": pid},
                      "panelIndex": pid, "embeddableConfig": {}, "panelRefName": f"r_{pid}"})
        refs.append({"id": vid, "name": f"r_{pid}", "type": vtype})
    return c.upsert("dashboard", did, {
        "title": title, "description": desc,
        "panelsJSON": json.dumps(plist),
        "optionsJSON": json.dumps({"useMargins": True, "syncColors": True, "syncCursor": True,
                                    "hidePanelTitles": False}),
        "timeRestore": True, "timeTo": "now", "timeFrom": "now-24h",
        "refreshInterval": {"pause": False, "value": 30000},
        "kibanaSavedObjectMeta": {
            "searchSourceJSON": json.dumps({"query": {"query": "", "language": "kuery"}, "filter": []})},
    }, refs)


# ─── Dashboard 1: MuleSoft Integration — Runtime Metrics ────────────────────

def build_d1(c):
    p = "fnb-m-"
    print("  Creating visualizations...")
    lens_metric(c, f"{p}exec", "Flow Executions", "mule.flow.executions", subtitle="Latest")
    lens_metric(c, f"{p}http", "HTTP Requests", "mule.http.requests", subtitle="Latest")
    lens_metric(c, f"{p}msgs", "Messages Processed", "mule.messages.processed", subtitle="Latest")
    lens_metric(c, f"{p}act",  "Active Flows", "mule.flows.active", agg="max", subtitle="Current")

    lens_xy(c, f"{p}exec-t", "Flow Executions Over Time",
            "mule.flow.executions", split="mule.flow.name")
    lens_xy(c, f"{p}http-t", "HTTP Connector Requests Over Time",
            "mule.http.requests", chart="line", split="mule.backend", ylabel="Requests")
    lens_pie(c, f"{p}http-pie", "HTTP Requests by Backend",
             "mule.http.requests", "mule.backend")
    lens_xy(c, f"{p}msgs-t", "Messages Processed Over Time",
            "mule.messages.processed", chart="area", split="mule.flow.name", ylabel="Messages")
    lens_xy(c, f"{p}exec-flow", "Executions by Flow & Payment Type",
            "mule.flow.executions", chart="bar_stacked", split="payment.type")

    print("  Creating dashboard...")
    dashboard(c, "fnb-dashboard-mulesoft-metrics",
        "[FNB] Phase 1: MuleSoft Runtime Metrics (EDOT)",
        "What you get out of the box with EDOT: MuleSoft flow executions, HTTP connector activity, and backend connectivity.",
        [(f"{p}exec",     "lens", 0,  0, 12, 8),
         (f"{p}http",     "lens", 12, 0, 12, 8),
         (f"{p}msgs",     "lens", 24, 0, 12, 8),
         (f"{p}act",      "lens", 36, 0, 12, 8),
         (f"{p}exec-t",   "lens", 0,  8, 48, 12),
         (f"{p}http-t",   "lens", 0,  20, 28, 14),
         (f"{p}http-pie", "lens", 28, 20, 20, 14),
         (f"{p}msgs-t",   "lens", 0,  34, 24, 14),
         (f"{p}exec-flow","lens", 24, 34, 24, 14)])


# ─── Dashboard 2: Payment Operations — Business Metrics ─────────────────────

def build_d2(c):
    p = "fnb-p-"
    print("  Creating visualizations...")
    lens_metric(c, f"{p}reqs",  "Portal Requests", "portal.requests.total", subtitle="Latest")
    lens_metric(c, f"{p}errs",  "Portal Errors", "portal.errors.total", subtitle="Latest")
    lens_metric(c, f"{p}fraud", "Fraud Checks", "fraud.checks.total", subtitle="Latest")
    lens_metric(c, f"{p}notif", "Notifications Sent", "notification.sent.total", subtitle="Latest")

    lens_xy(c, f"{p}vol", "Transaction Volume Over Time",
            "portal.requests.total", split="portal.operation")
    lens_xy(c, f"{p}mc", "MuleSoft Calls by Operation",
            "portal.mulesoft.calls", chart="bar", split="mulesoft.operation", ylabel="Calls")
    lens_xy(c, f"{p}me", "MuleSoft Call Errors",
            "portal.mulesoft.errors", chart="bar_stacked", split="mulesoft.operation", ylabel="Errors")
    lens_pie(c, f"{p}fr", "Fraud Checks by Risk Level",
             "fraud.checks.total", "fraud.risk_level")
    lens_pie(c, f"{p}ops", "Portal Requests by Operation",
             "portal.requests.total", "portal.operation")
    lens_xy(c, f"{p}et", "Portal Errors Over Time",
            "portal.errors.total", chart="bar_stacked", split="portal.operation")

    print("  Creating dashboard...")
    dashboard(c, "fnb-dashboard-payment-ops",
        "[FNB] Phase 3: Payment Operations — Business Metrics",
        "Business-level view: transaction volumes, fraud detection, and portal health.",
        [(f"{p}reqs",  "lens", 0,  0, 12, 8),
         (f"{p}errs",  "lens", 12, 0, 12, 8),
         (f"{p}fraud", "lens", 24, 0, 12, 8),
         (f"{p}notif", "lens", 36, 0, 12, 8),
         (f"{p}vol",   "lens", 0,  8, 48, 12),
         (f"{p}mc",    "lens", 0,  20, 24, 14),
         (f"{p}me",    "lens", 24, 20, 24, 14),
         (f"{p}fr",    "lens", 0,  34, 24, 14),
         (f"{p}ops",   "lens", 24, 34, 24, 14),
         (f"{p}et",    "lens", 0,  48, 48, 12)])


# ─── Dashboard 3: Core Banking — Database Performance ───────────────────────

def build_d3(c):
    p = "fnb-d-"
    print("  Creating visualizations...")
    lens_metric(c, f"{p}qry", "Total DB Queries", "db.queries.total", subtitle="Latest")
    lens_metric(c, f"{p}slw", "Slow Queries", "db.slow_queries.total", subtitle="Latest")
    lens_metric(c, f"{p}ses", "Active DB Sessions", "db.sessions.active", agg="max", subtitle="Current")
    lens_metric(c, f"{p}deb", "Accounts Debited", "banking.accounts.debited", subtitle="Latest")

    lens_xy(c, f"{p}qt", "DB Queries Over Time",
            "db.queries.total", chart="bar", split="db.operation", ylabel="Queries")
    lens_xy(c, f"{p}st", "Slow Queries Over Time (Smoking Gun)",
            "db.slow_queries.total", chart="bar", split="db.sql.table", ylabel="Slow Queries")
    lens_pie(c, f"{p}tbl", "Queries by Table",
             "db.queries.total", "db.sql.table")
    lens_xy(c, f"{p}bal", "Balance Checks by Account Type",
            "banking.balance.checks", chart="bar_stacked", split="account.type")
    lens_xy(c, f"{p}sess-t", "Active DB Sessions Over Time",
            "db.sessions.active", agg="max", chart="line", ylabel="Active Sessions")
    lens_table(c, f"{p}log", "Slow Query Log",
               [("service.name", "Service", "terms"), ("@timestamp", "Count", "count")],
               dv="logs-otel", query="SLOW QUERY")

    print("  Creating dashboard...")
    dashboard(c, "fnb-dashboard-core-banking-db",
        "[FNB] Phase 3: Core Banking — Database Performance",
        "Core banking DB health: query volumes, slow query detection (the demo's smoking gun), and transaction activity.",
        [(f"{p}qry",    "lens", 0,  0, 12, 8),
         (f"{p}slw",    "lens", 12, 0, 12, 8),
         (f"{p}ses",    "lens", 24, 0, 12, 8),
         (f"{p}deb",    "lens", 36, 0, 12, 8),
         (f"{p}qt",     "lens", 0,  8, 48, 14),
         (f"{p}st",     "lens", 0,  22, 28, 14),
         (f"{p}tbl",    "lens", 28, 22, 20, 14),
         (f"{p}bal",    "lens", 0,  36, 24, 12),
         (f"{p}sess-t", "lens", 24, 36, 24, 12),
         (f"{p}log",    "lens", 0,  48, 48, 14)])


# ─── Dashboard 4: Distributed Tracing — Service Flow ────────────────────────

def build_d4(c):
    p = "fnb-t-"
    print("  Creating visualizations...")

    markdown(c, f"{p}md", "Service Map & APM",
        "## Distributed Tracing\n\n"
        "Explore full traces in Elastic APM:\n\n"
        "- **[Service Map](/app/apm/service-map)** — Dependency graph\n"
        "- **[Services](/app/apm/services)** — All services\n"
        "- **[Traces](/app/apm/traces)** — Waterfall views\n\n"
        "### What Traces Unlock\n"
        "When MuleSoft calls core banking and a slow DB query happens, "
        "the trace waterfall reveals exactly where time was spent.\n\n"
        "**Try:** Filter by `span.duration.us > 3000000` to find slow spans.")

    lens_xy(c, f"{p}vol", "Trace Volume by Service",
            "span.duration.us", agg="count", chart="bar_stacked",
            split="service.name", dv="traces-otel", ylabel="Span Count")
    lens_pie(c, f"{p}svc", "Traces by Service",
             "span.duration.us", "service.name", agg="count", dv="traces-otel")
    _formula_xy(c, f"{p}dur", "Avg Span Duration by Service (ms)",
                "average(span.duration.us) / 1000", chart="bar",
                split="service.name", dv="traces-otel", ylabel="Avg Duration (ms)")
    lens_table(c, f"{p}top", "Slowest Spans",
               [("service.name", "Service", "terms"),
                ("span.name", "Span Name", "terms"),
                ("span.duration.us", "Max Duration (ms)", "formula:max(span.duration.us) / 1000")],
               dv="traces-otel")
    _formula_xy(c, f"{p}trend", "Span Duration Trend by Service (ms)",
                "average(span.duration.us) / 1000", chart="line",
                split="service.name", dv="traces-otel", ylabel="Avg Duration (ms)")

    print("  Creating dashboard...")
    dashboard(c, "fnb-dashboard-tracing",
        "[FNB] Phase 2: Distributed Tracing — Service Flow",
        "End-to-end request paths, service dependencies, and latency breakdown.",
        [(f"{p}md",    "visualization", 0,  0,  16, 16),
         (f"{p}vol",   "lens",          16, 0,  16, 16),
         (f"{p}svc",   "lens",          32, 0,  16, 16),
         (f"{p}dur",   "lens",          0,  16, 48, 14),
         (f"{p}top",   "lens",          0,  30, 48, 14),
         (f"{p}trend", "lens",          0,  44, 48, 14)])


# ─── Dashboard 5: Operations Command Center ─────────────────────────────────

def build_d5(c):
    p = "fnb-o-"
    print("  Creating visualizations...")

    markdown(c, f"{p}hdr", "Operations Command Center",
        "# FNB Operations Command Center\n\n"
        "**Combined view:** Metrics + Traces + Logs\n\n"
        "1. **Spot anomalies** in metrics (top)\n"
        "2. **Trace root cause** via APM (middle)\n"
        "3. **Read logs** for details (bottom)\n\n"
        "---\n*Phase 1 = MuleSoft metrics (EDOT). Phase 2 = OTel + MuleSoft. Phase 3 = OTel + all apps.*")

    lens_metric(c, f"{p}fl", "Total Flows", "mule.flow.executions", subtitle="MuleSoft")
    lens_metric(c, f"{p}er", "Portal Errors", "portal.errors.total", subtitle="Portal")
    lens_metric(c, f"{p}pr", "Portal Requests", "portal.requests.total", subtitle="Portal")
    lens_metric(c, f"{p}nt", "Notifications Sent", "notification.sent.total", subtitle="Notifications")

    lens_xy(c, f"{p}ft", "MuleSoft Flow Executions Over Time",
            "mule.flow.executions", chart="line", split="mule.flow.name", ylabel="Executions")
    lens_xy(c, f"{p}et", "Portal Errors Over Time",
            "portal.errors.total", chart="area", split="portal.operation")
    lens_xy(c, f"{p}bh", "MuleSoft HTTP Requests by Backend",
            "mule.http.requests", chart="bar", split="mule.backend", ylabel="Requests")
    lens_xy(c, f"{p}fc", "Fraud Checks by Risk Level",
            "fraud.checks.total", chart="bar", split="fraud.risk_level")
    lens_xy(c, f"{p}sq", "Slow Queries Over Time",
            "db.slow_queries.total", chart="bar", ylabel="Slow Queries")
    lens_pie(c, f"{p}np", "Notifications by Type",
             "notification.sent.total", "notification.type")
    lens_xy(c, f"{p}tl", "Active Tellers Over Time",
            "portal.tellers.active", agg="max", chart="line", ylabel="Active Tellers")
    lens_table(c, f"{p}lg", "Recent Error & Warning Logs",
               [("service.name", "Service", "terms"),
                ("log.level", "Level", "terms"),
                ("@timestamp", "Count", "count")],
               dv="logs-otel", query="log.level: (ERROR OR WARN*)")

    print("  Creating dashboard...")
    dashboard(c, "fnb-dashboard-ops-center",
        "[FNB] Phase 3: Operations Command Center",
        "Full picture: metrics + traces + logs correlated for incident response.",
        [(f"{p}hdr", "visualization", 0,  0,  16, 12),
         (f"{p}fl",  "lens",          16, 0,  8,  8),
         (f"{p}er",  "lens",          24, 0,  8,  8),
         (f"{p}pr",  "lens",          32, 0,  8,  8),
         (f"{p}nt",  "lens",          40, 0,  8,  8),
         (f"{p}ft",  "lens",          16, 8,  32, 12),
         (f"{p}et",  "lens",          0,  20, 24, 14),
         (f"{p}bh",  "lens",          24, 20, 24, 14),
         (f"{p}fc",  "lens",          0,  34, 24, 12),
         (f"{p}sq",  "lens",          24, 34, 24, 12),
         (f"{p}np",  "lens",          0,  46, 16, 12),
         (f"{p}tl",  "lens",          16, 46, 32, 12),
         (f"{p}lg",  "lens",          0,  58, 48, 14)])


# ─── Dashboard 6: Key Transactions & Span Breakdown ─────────────────────────

def build_d6(c):
    p = "fnb-tx-"
    dv = "traces-otel"
    print("  Creating visualizations...")

    # ── Section 1: Top-level Trace Performance ──
    markdown(c, f"{p}hdr1", "Trace Performance Overview",
        "## Overall Trace Performance\n\n"
        "High-level view of all distributed traces across the payment platform. "
        "How many transactions are flowing, how fast, and are they succeeding?")

    lens_metric(c, f"{p}traces", "Total Traces", "trace_id", agg="unique_count",
                dv=dv, subtitle="Unique Traces")
    lens_metric(c, f"{p}spans", "Total Spans", "span_id", agg="unique_count",
                dv=dv, subtitle="Span Count")
    _formula_metric(c, f"{p}avg", "Avg Span Duration",
                    "average(span.duration.us) / 1000", dv=dv, subtitle="ms")

    lens_xy(c, f"{p}tpt", "Trace Throughput Over Time",
            "span.duration.us", agg="count", chart="bar", split="service.name", dv=dv,
            ylabel="Span Count")
    _formula_xy(c, f"{p}lat", "End-to-End Latency Over Time (ms)",
                "average(span.duration.us) / 1000", chart="line", split="service.name",
                dv=dv, ylabel="Avg Duration (ms)")
    lens_pie(c, f"{p}svc", "Time Spent by Service", "span.duration.us", "service.name", dv=dv)
    lens_pie(c, f"{p}outcome", "Span Outcomes", "span.duration.us", "event.outcome", dv=dv)

    # ── Section 2: Span Breakdown ──
    markdown(c, f"{p}hdr2", "Span Breakdown",
        "## Span Breakdown\n\n"
        "Drill down into individual spans — which transactions are slowest, "
        "which services consume the most time, and where are the bottlenecks?")

    _formula_xy(c, f"{p}dur", "Avg Duration by Span Name (ms)",
                "average(span.duration.us) / 1000", chart="bar", split="span.name",
                dv=dv, ylabel="Avg Duration (ms)")
    _formula_xy(c, f"{p}vol", "Span Volume by Transaction",
                "sum(span.duration.us) / 1000", chart="bar_stacked", split="span.name",
                dv=dv, ylabel="Total Duration (ms)")
    lens_table(c, f"{p}top", "Slowest Spans (Top 20)",
               [("span.name", "Transaction", "terms"),
                ("service.name", "Service", "terms"),
                ("span.duration.us", "Max Duration (ms)", "formula:max(span.duration.us) / 1000")],
               dv=dv)

    lens_pie(c, f"{p}dbop", "DB Operation Breakdown", "span.duration.us", "db.operation", dv=dv)
    _formula_xy(c, f"{p}mule", "MuleSoft Flow Spans Over Time",
                "sum(span.duration.us) / 1000", chart="bar", split="mule.flow.name",
                dv=dv, ylabel="Total Duration (ms)")
    _formula_xy(c, f"{p}http", "HTTP Routes by Status Code",
                "sum(span.duration.us) / 1000", chart="bar_stacked", split="http.status_code",
                dv=dv, ylabel="Total Duration (ms)")

    print("  Creating dashboard...")
    dashboard(c, "fnb-dashboard-trace-transactions",
        "[FNB] Phase 2: Key Transactions & Span Breakdown",
        "Top-level trace performance, then drill into individual span breakdowns.",
        [# Section 1: Trace Performance Overview
         (f"{p}hdr1",    "visualization", 0,  0,  12, 8),
         (f"{p}traces",  "lens",          12, 0,  12, 8),
         (f"{p}spans",   "lens",          24, 0,  12, 8),
         (f"{p}avg",     "lens",          36, 0,  12, 8),
         (f"{p}tpt",     "lens",          0,  8,  48, 14),
         (f"{p}lat",     "lens",          0,  22, 48, 14),
         (f"{p}svc",     "lens",          0,  36, 24, 14),
         (f"{p}outcome", "lens",          24, 36, 24, 14),
         # Section 2: Span Breakdown
         (f"{p}hdr2",    "visualization", 0,  50, 48, 6),
         (f"{p}dur",     "lens",          0,  56, 48, 14),
         (f"{p}vol",     "lens",          0,  70, 48, 14),
         (f"{p}top",     "lens",          0,  84, 48, 14),
         (f"{p}dbop",    "lens",          0,  98, 16, 14),
         (f"{p}mule",    "lens",          16, 98, 16, 14),
         (f"{p}http",    "lens",          32, 98, 16, 14)])


# ─── Dashboard 7: FNB Portal Service ─────────────────────────────────────────

def build_d7(c):
    p = "fnb-portal-"
    svc = 'service.name: "fnb-portal"'
    dv_t = "traces-otel"
    print("  Creating visualizations...")

    lens_metric(c, f"{p}reqs", "Total Requests", "portal.requests.total", subtitle="Portal")
    lens_metric(c, f"{p}errs", "Total Errors", "portal.errors.total", subtitle="Portal")
    lens_metric(c, f"{p}tellers", "Active Tellers", "portal.tellers.active", agg="max", subtitle="Current")
    lens_metric(c, f"{p}mscalls", "MuleSoft Calls", "portal.mulesoft.calls", subtitle="Total")

    lens_xy(c, f"{p}reqs-t", "Requests Over Time",
            "portal.requests.total", chart="bar_stacked", split="portal.operation", ylabel="Requests")
    lens_xy(c, f"{p}errs-t", "Errors Over Time",
            "portal.errors.total", chart="bar", split="portal.operation", ylabel="Errors")
    lens_xy(c, f"{p}ms-t", "MuleSoft Calls Over Time",
            "portal.mulesoft.calls", chart="line", split="mulesoft.operation", ylabel="Calls")
    lens_xy(c, f"{p}mserr-t", "MuleSoft Errors Over Time",
            "portal.mulesoft.errors", chart="bar", split="mulesoft.operation", ylabel="Errors")
    lens_pie(c, f"{p}ops-pie", "Requests by Operation",
             "portal.requests.total", "portal.operation")
    _formula_xy(c, f"{p}lat", "Avg Latency by Operation (ms)",
                "average(span.duration.us) / 1000", chart="bar", split="span.name",
                dv=dv_t, ylabel="Avg Latency (ms)", query=svc)

    print("  Creating dashboard...")
    dashboard(c, "fnb-dashboard-portal",
        "[FNB] Phase 3: FNB Portal Service",
        "Portal service health: request volumes, errors, MuleSoft integration, and latency.",
        [(f"{p}reqs",    "lens", 0,  0,  12, 8),
         (f"{p}errs",    "lens", 12, 0,  12, 8),
         (f"{p}tellers", "lens", 24, 0,  12, 8),
         (f"{p}mscalls", "lens", 36, 0,  12, 8),
         (f"{p}reqs-t",  "lens", 0,  8,  48, 14),
         (f"{p}errs-t",  "lens", 0,  22, 24, 14),
         (f"{p}ms-t",    "lens", 24, 22, 24, 14),
         (f"{p}mserr-t", "lens", 0,  36, 24, 14),
         (f"{p}ops-pie", "lens", 24, 36, 24, 14),
         (f"{p}lat",     "lens", 0,  50, 48, 14)])


# ─── Dashboard 8: MuleSoft Anypoint Runtime ─────────────────────────────────

def build_d8(c):
    p = "fnb-mule-"
    svc = 'service.name: "mulesoft-anypoint-runtime"'
    dv_t = "traces-otel"
    print("  Creating visualizations...")

    lens_metric(c, f"{p}exec", "Flow Executions", "mule.flow.executions", subtitle="Total")
    lens_metric(c, f"{p}http", "HTTP Requests", "mule.http.requests", subtitle="Total")
    lens_metric(c, f"{p}msgs", "Messages Processed", "mule.messages.processed", subtitle="Total")
    lens_metric(c, f"{p}act", "Active Flows", "mule.flows.active", agg="max", subtitle="Current")

    lens_xy(c, f"{p}exec-t", "Flow Executions by Flow",
            "mule.flow.executions", chart="bar_stacked", split="mule.flow.name", ylabel="Executions")
    lens_xy(c, f"{p}http-t", "HTTP Requests by Backend",
            "mule.http.requests", chart="line", split="mule.backend", ylabel="Requests")
    lens_xy(c, f"{p}msgs-t", "Messages by Flow",
            "mule.messages.processed", chart="area", split="mule.flow.name", ylabel="Messages")
    lens_xy(c, f"{p}pay-t", "Executions by Payment Type",
            "mule.flow.executions", chart="bar_stacked", split="payment.type", ylabel="Executions")
    lens_pie(c, f"{p}bk-pie", "HTTP Requests by Backend",
             "mule.http.requests", "mule.backend")
    _formula_xy(c, f"{p}lat", "Avg Flow Latency by Span (ms)",
                "average(span.duration.us) / 1000", chart="bar", split="span.name",
                dv=dv_t, ylabel="Avg Latency (ms)", query=svc)

    print("  Creating dashboard...")
    dashboard(c, "fnb-dashboard-mulesoft-svc",
        "[FNB] Phase 2: MuleSoft Anypoint Runtime",
        "MuleSoft runtime: flow executions, HTTP connector activity, message processing, and latency.",
        [(f"{p}exec",   "lens", 0,  0,  12, 8),
         (f"{p}http",   "lens", 12, 0,  12, 8),
         (f"{p}msgs",   "lens", 24, 0,  12, 8),
         (f"{p}act",    "lens", 36, 0,  12, 8),
         (f"{p}exec-t", "lens", 0,  8,  48, 14),
         (f"{p}http-t", "lens", 0,  22, 24, 14),
         (f"{p}bk-pie", "lens", 24, 22, 24, 14),
         (f"{p}msgs-t", "lens", 0,  36, 24, 14),
         (f"{p}pay-t",  "lens", 24, 36, 24, 14),
         (f"{p}lat",    "lens", 0,  50, 48, 14)])


# ─── Dashboard 9: Core Banking Service ──────────────────────────────────────

def build_d9(c):
    p = "fnb-cbs-"
    svc = 'service.name: "core-banking-svc"'
    dv_t = "traces-otel"
    print("  Creating visualizations...")

    lens_metric(c, f"{p}qry", "Total Queries", "db.queries.total", subtitle="DB")
    lens_metric(c, f"{p}slw", "Slow Queries", "db.slow_queries.total", subtitle="DB")
    lens_metric(c, f"{p}ses", "Active Sessions", "db.sessions.active", agg="max", subtitle="Current")
    lens_metric(c, f"{p}deb", "Accounts Debited", "banking.accounts.debited", subtitle="Total")

    lens_xy(c, f"{p}qt", "Queries by Operation",
            "db.queries.total", chart="bar_stacked", split="db.operation", ylabel="Queries")
    lens_xy(c, f"{p}st", "Slow Queries by Table",
            "db.slow_queries.total", chart="bar", split="db.sql.table", ylabel="Slow Queries")
    lens_pie(c, f"{p}tbl", "Queries by Table",
             "db.queries.total", "db.sql.table")
    lens_xy(c, f"{p}bal", "Balance Checks by Account Type",
            "banking.balance.checks", chart="bar_stacked", split="account.type", ylabel="Checks")
    lens_xy(c, f"{p}sess-t", "Active Sessions Over Time",
            "db.sessions.active", agg="max", chart="line", ylabel="Sessions")
    _formula_xy(c, f"{p}lat", "Avg Latency by Operation (ms)",
                "average(span.duration.us) / 1000", chart="bar", split="span.name",
                dv=dv_t, ylabel="Avg Latency (ms)", query=svc)
    lens_table(c, f"{p}log", "Slow Query Log",
               [("service.name", "Service", "terms"), ("@timestamp", "Count", "count")],
               dv="logs-otel", query="SLOW QUERY")

    print("  Creating dashboard...")
    dashboard(c, "fnb-dashboard-core-banking-svc",
        "[FNB] Phase 3: Core Banking Service",
        "Core banking: DB queries, slow query detection, sessions, and transaction activity.",
        [(f"{p}qry",    "lens", 0,  0,  12, 8),
         (f"{p}slw",    "lens", 12, 0,  12, 8),
         (f"{p}ses",    "lens", 24, 0,  12, 8),
         (f"{p}deb",    "lens", 36, 0,  12, 8),
         (f"{p}qt",     "lens", 0,  8,  48, 14),
         (f"{p}st",     "lens", 0,  22, 24, 14),
         (f"{p}tbl",    "lens", 24, 22, 24, 14),
         (f"{p}bal",    "lens", 0,  36, 24, 14),
         (f"{p}sess-t", "lens", 24, 36, 24, 14),
         (f"{p}lat",    "lens", 0,  50, 48, 14),
         (f"{p}log",    "lens", 0,  64, 48, 14)])


# ─── Dashboard 10: Fraud Detection Service ──────────────────────────────────

def build_d10(c):
    p = "fnb-fraud-"
    svc = 'service.name: "fraud-detection-svc"'
    dv_t = "traces-otel"
    print("  Creating visualizations...")

    lens_metric(c, f"{p}chk", "Total Fraud Checks", "fraud.checks.total", subtitle="Total")
    lens_metric(c, f"{p}flg", "Flagged Transactions", "fraud.flags.total", subtitle="Suspicious")
    lens_metric(c, f"{p}spans", "Total Spans", "span_id", agg="unique_count",
                dv=dv_t, subtitle="Traces", query=svc)
    _formula_metric(c, f"{p}avg", "Avg Check Duration",
                    "average(span.duration.us) / 1000", dv=dv_t, subtitle="ms", query=svc)

    lens_xy(c, f"{p}chk-t", "Fraud Checks Over Time",
            "fraud.checks.total", chart="bar_stacked", split="fraud.risk_level", ylabel="Checks")
    lens_xy(c, f"{p}flg-t", "Flagged Transactions Over Time",
            "fraud.flags.total", chart="bar", ylabel="Flagged")
    lens_pie(c, f"{p}risk", "Checks by Risk Level",
             "fraud.checks.total", "fraud.risk_level")
    _formula_xy(c, f"{p}lat", "Avg Latency by Span (ms)",
                "average(span.duration.us) / 1000", chart="bar", split="span.name",
                dv=dv_t, ylabel="Avg Latency (ms)", query=svc)
    lens_xy(c, f"{p}vol", "Span Volume Over Time",
            "span.duration.us", agg="count", chart="bar", split="span.name",
            dv=dv_t, ylabel="Span Count", query=svc)

    print("  Creating dashboard...")
    dashboard(c, "fnb-dashboard-fraud",
        "[FNB] Phase 3: Fraud Detection Service",
        "Fraud detection: check volumes, flagged transactions, risk levels, and processing latency.",
        [(f"{p}chk",   "lens", 0,  0,  12, 8),
         (f"{p}flg",   "lens", 12, 0,  12, 8),
         (f"{p}spans", "lens", 24, 0,  12, 8),
         (f"{p}avg",   "lens", 36, 0,  12, 8),
         (f"{p}chk-t", "lens", 0,  8,  48, 14),
         (f"{p}flg-t", "lens", 0,  22, 24, 14),
         (f"{p}risk",  "lens", 24, 22, 24, 14),
         (f"{p}lat",   "lens", 0,  36, 48, 14),
         (f"{p}vol",   "lens", 0,  50, 48, 14)])


# ─── Dashboard 11: AML Screening Service ────────────────────────────────────

def build_d11(c):
    p = "fnb-aml-"
    svc = 'service.name: "aml-screening-svc"'
    dv_t = "traces-otel"
    print("  Creating visualizations...")

    lens_metric(c, f"{p}chk", "Total AML Screenings", "aml.checks.total", subtitle="Total")
    lens_metric(c, f"{p}hits", "Watchlist Hits", "aml.hits.total", subtitle="Matches")
    lens_metric(c, f"{p}spans", "Total Spans", "span_id", agg="unique_count",
                dv=dv_t, subtitle="Traces", query=svc)
    _formula_metric(c, f"{p}avg", "Avg Screening Duration",
                    "average(span.duration.us) / 1000", dv=dv_t, subtitle="ms", query=svc)

    lens_xy(c, f"{p}chk-t", "AML Screenings Over Time",
            "aml.checks.total", chart="bar", ylabel="Screenings")
    lens_xy(c, f"{p}hits-t", "Watchlist Hits Over Time",
            "aml.hits.total", chart="bar", ylabel="Hits")
    _formula_xy(c, f"{p}lat", "Avg Latency by Span (ms)",
                "average(span.duration.us) / 1000", chart="bar", split="span.name",
                dv=dv_t, ylabel="Avg Latency (ms)", query=svc)
    lens_xy(c, f"{p}vol", "Span Volume Over Time",
            "span.duration.us", agg="count", chart="bar_stacked", split="span.name",
            dv=dv_t, ylabel="Span Count", query=svc)
    lens_pie(c, f"{p}spans-pie", "Time Spent by Span",
             "span.duration.us", "span.name", dv=dv_t, query=svc)

    print("  Creating dashboard...")
    dashboard(c, "fnb-dashboard-aml",
        "[FNB] Phase 3: AML Screening Service",
        "AML compliance: screening volumes, watchlist hits, and processing latency.",
        [(f"{p}chk",       "lens", 0,  0,  12, 8),
         (f"{p}hits",      "lens", 12, 0,  12, 8),
         (f"{p}spans",     "lens", 24, 0,  12, 8),
         (f"{p}avg",       "lens", 36, 0,  12, 8),
         (f"{p}chk-t",     "lens", 0,  8,  24, 14),
         (f"{p}hits-t",    "lens", 24, 8,  24, 14),
         (f"{p}lat",       "lens", 0,  22, 48, 14),
         (f"{p}vol",       "lens", 0,  36, 24, 14),
         (f"{p}spans-pie", "lens", 24, 36, 24, 14)])


# ─── Dashboard 12: Customer Profile (CRM) Service ───────────────────────────

def build_d12(c):
    p = "fnb-crm-"
    svc = 'service.name: "customer-profile-svc"'
    dv_t = "traces-otel"
    print("  Creating visualizations...")

    lens_metric(c, f"{p}lookups", "Profile Lookups", "crm.profile.lookups", subtitle="Total")
    lens_metric(c, f"{p}creates", "Profiles Created", "crm.profile.creates", subtitle="Total")
    lens_metric(c, f"{p}spans", "Total Spans", "span_id", agg="unique_count",
                dv=dv_t, subtitle="Traces", query=svc)
    _formula_metric(c, f"{p}avg", "Avg Query Duration",
                    "average(span.duration.us) / 1000", dv=dv_t, subtitle="ms", query=svc)

    lens_xy(c, f"{p}look-t", "Profile Lookups Over Time",
            "crm.profile.lookups", chart="line", ylabel="Lookups")
    lens_xy(c, f"{p}create-t", "Profile Creates Over Time",
            "crm.profile.creates", chart="bar", ylabel="Creates")
    _formula_xy(c, f"{p}lat", "Avg Latency by Span (ms)",
                "average(span.duration.us) / 1000", chart="bar", split="span.name",
                dv=dv_t, ylabel="Avg Latency (ms)", query=svc)
    lens_xy(c, f"{p}vol", "Span Volume Over Time",
            "span.duration.us", agg="count", chart="bar_stacked", split="span.name",
            dv=dv_t, ylabel="Span Count", query=svc)
    lens_pie(c, f"{p}spans-pie", "Time Spent by Span",
             "span.duration.us", "span.name", dv=dv_t, query=svc)

    print("  Creating dashboard...")
    dashboard(c, "fnb-dashboard-crm",
        "[FNB] Phase 3: Customer Profile (CRM) Service",
        "CRM service: profile lookups, creates, SOQL query performance, and latency.",
        [(f"{p}lookups",   "lens", 0,  0,  12, 8),
         (f"{p}creates",   "lens", 12, 0,  12, 8),
         (f"{p}spans",     "lens", 24, 0,  12, 8),
         (f"{p}avg",       "lens", 36, 0,  12, 8),
         (f"{p}look-t",    "lens", 0,  8,  24, 14),
         (f"{p}create-t",  "lens", 24, 8,  24, 14),
         (f"{p}lat",       "lens", 0,  22, 48, 14),
         (f"{p}vol",       "lens", 0,  36, 24, 14),
         (f"{p}spans-pie", "lens", 24, 36, 24, 14)])


# ─── Dashboard 13: Notification Service ─────────────────────────────────────

def build_d13(c):
    p = "fnb-notif-"
    svc = 'service.name: "notification-svc"'
    dv_t = "traces-otel"
    print("  Creating visualizations...")

    lens_metric(c, f"{p}sent", "Notifications Sent", "notification.sent.total", subtitle="Total")
    lens_metric(c, f"{p}fail", "Notifications Failed", "notification.failed.total", subtitle="Failures")
    lens_metric(c, f"{p}spans", "Total Spans", "span_id", agg="unique_count",
                dv=dv_t, subtitle="Traces", query=svc)
    _formula_metric(c, f"{p}avg", "Avg Send Duration",
                    "average(span.duration.us) / 1000", dv=dv_t, subtitle="ms", query=svc)

    lens_xy(c, f"{p}sent-t", "Notifications Sent Over Time",
            "notification.sent.total", chart="bar_stacked", split="notification.type", ylabel="Sent")
    lens_xy(c, f"{p}fail-t", "Notification Failures Over Time",
            "notification.failed.total", chart="bar", ylabel="Failures")
    lens_pie(c, f"{p}type-pie", "Notifications by Type",
             "notification.sent.total", "notification.type")
    _formula_xy(c, f"{p}lat", "Avg Latency by Span (ms)",
                "average(span.duration.us) / 1000", chart="bar", split="span.name",
                dv=dv_t, ylabel="Avg Latency (ms)", query=svc)
    lens_xy(c, f"{p}vol", "Span Volume Over Time",
            "span.duration.us", agg="count", chart="bar_stacked", split="span.name",
            dv=dv_t, ylabel="Span Count", query=svc)

    print("  Creating dashboard...")
    dashboard(c, "fnb-dashboard-notification",
        "[FNB] Phase 3: Notification Service",
        "Notification service: delivery volumes, failures, channel distribution, and latency.",
        [(f"{p}sent",     "lens", 0,  0,  12, 8),
         (f"{p}fail",     "lens", 12, 0,  12, 8),
         (f"{p}spans",    "lens", 24, 0,  12, 8),
         (f"{p}avg",      "lens", 36, 0,  12, 8),
         (f"{p}sent-t",   "lens", 0,  8,  48, 14),
         (f"{p}fail-t",   "lens", 0,  22, 24, 14),
         (f"{p}type-pie", "lens", 24, 22, 24, 14),
         (f"{p}lat",      "lens", 0,  36, 48, 14),
         (f"{p}vol",      "lens", 0,  50, 48, 14)])


# ─── Main ────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(description="Create FNB demo dashboards in Kibana")
    parser.add_argument("--kibana-url", default=os.getenv("KIBANA_URL"))
    parser.add_argument("--api-key", default=os.getenv("ELASTIC_API_KEY"))
    args = parser.parse_args()

    if not args.kibana_url or not args.api_key:
        print("ERROR: --kibana-url and --api-key required (or set KIBANA_URL / ELASTIC_API_KEY)")
        sys.exit(1)

    c = KibanaClient(args.kibana_url, args.api_key)
    print(f"Connecting to Kibana: {args.kibana_url}")

    print("\n1. Creating data views...")
    c.create_data_view("metrics-otel", METRICS_INDEX)
    c.create_data_view("traces-otel", TRACES_INDEX)
    c.create_data_view("logs-otel", LOGS_INDEX)

    for i, (name, fn) in enumerate([
        ("MuleSoft Integration — Runtime Metrics", build_d1),
        ("Payment Operations — Business Metrics", build_d2),
        ("Core Banking — Database Performance", build_d3),
        ("Distributed Tracing — Service Flow", build_d4),
        ("Operations Command Center", build_d5),
        ("Key Transactions & Span Breakdown", build_d6),
        ("FNB Portal Service", build_d7),
        ("MuleSoft Anypoint Runtime", build_d8),
        ("Core Banking Service", build_d9),
        ("Fraud Detection Service", build_d10),
        ("AML Screening Service", build_d11),
        ("Customer Profile (CRM) Service", build_d12),
        ("Notification Service", build_d13),
    ], 1):
        print(f"\n{i+1}. Building: {name}")
        fn(c)

    print(f"\n{'='*60}")
    print(f"Created {c.created} objects, {c.errors} errors")
    print(f"{'='*60}")

    base = args.kibana_url.rstrip("/")
    print("\nDashboard URLs:")
    for did, name in [
        ("fnb-dashboard-mulesoft-metrics",  "[Phase 1] MuleSoft Runtime Metrics (EDOT)"),
        ("fnb-dashboard-tracing",           "[Phase 2] Distributed Tracing — Service Flow"),
        ("fnb-dashboard-trace-transactions","[Phase 2] Key Transactions & Span Breakdown"),
        ("fnb-dashboard-mulesoft-svc",      "[Phase 2] MuleSoft Anypoint Runtime"),
        ("fnb-dashboard-payment-ops",       "[Phase 3] Payment Operations — Business Metrics"),
        ("fnb-dashboard-core-banking-db",   "[Phase 3] Core Banking — Database Performance"),
        ("fnb-dashboard-ops-center",        "[Phase 3] Operations Command Center"),
        ("fnb-dashboard-portal",            "[Phase 3] FNB Portal"),
        ("fnb-dashboard-core-banking-svc",  "[Phase 3] Core Banking Service"),
        ("fnb-dashboard-fraud",             "[Phase 3] Fraud Detection"),
        ("fnb-dashboard-aml",              "[Phase 3] AML Screening"),
        ("fnb-dashboard-crm",              "[Phase 3] Customer Profile (CRM)"),
        ("fnb-dashboard-notification",      "[Phase 3] Notification"),
    ]:
        print(f"  {name}")
        print(f"    {base}/app/dashboards#/view/{did}")

    print()
    if c.errors > 0:
        print(f"WARNING: {c.errors} objects failed. Check errors above.")
        sys.exit(1)
    else:
        print("All dashboards created successfully!")


if __name__ == "__main__":
    main()
