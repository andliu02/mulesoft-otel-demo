#!/usr/bin/env python3
"""
Create Kibana dashboards for the FNB MuleSoft OTel Demo.

Builds 5 dashboards via the Kibana Saved Objects API:
  Phase 1 (Metrics):
    1. MuleSoft Integration — Runtime Metrics
    2. Payment Operations — Business Metrics
    3. Core Banking — Database Performance
  Phase 2 (Traces + Full Picture):
    4. Distributed Tracing — Service Flow
    5. Operations Command Center

Usage:
    python create_dashboards.py --kibana-url <URL> --api-key <KEY>
    # or via env vars:
    KIBANA_URL=https://... ELASTIC_API_KEY=... python create_dashboards.py

Field naming in Elastic OTel indices:
    Metric values:  metrics.<name>    e.g. metrics.mule.flow.executions
    Attributes:     attributes.<name> e.g. attributes.mule.flow.name
    Resource attrs: resource.attributes.<name> (service.name is top-level)
"""

import argparse
import json
import os
import sys
import requests as http_requests

# ─── Configuration ───────────────────────────────────────────────────────────

METRICS_INDEX = "metrics-*"
TRACES_INDEX = "traces-*"
LOGS_INDEX = "logs-*"

# OTel → Elastic field path helpers
def M(name):
    """Metric value field path."""
    return f"metrics.{name}"

def A(name):
    """Attribute field path."""
    return f"attributes.{name}"


# ─── Kibana API client ──────────────────────────────────────────────────────

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
        resp = http_requests.request(method, url, headers=self.headers,
                                      json=body, timeout=30)
        return resp

    def create_data_view(self, dv_id, title, time_field="@timestamp"):
        resp = self._request("POST", "/api/data_views/data_view", {
            "data_view": {"id": dv_id, "title": title, "timeFieldName": time_field},
            "override": True,
        })
        status = "OK" if resp.status_code in (200, 201) else f"WARN ({resp.status_code})"
        print(f"    {title} → {status}")
        return resp

    def upsert_saved_object(self, obj_type, obj_id, attributes, references=None):
        """Create or overwrite a saved object."""
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
            print(f"    ERROR creating {obj_type}/{obj_id}: {resp.status_code} {resp.text[:200]}")
        return resp


# ─── Visualization builders ─────────────────────────────────────────────────

def lens_metric(client, viz_id, title, metric_field, agg="sum",
                data_view_id="metrics-otel", subtitle=""):
    """Single metric number visualization."""
    attrs = {
        "title": title,
        "visualizationType": "lnsMetric",
        "state": {
            "visualization": {
                "layerId": "layer1", "layerType": "data",
                "metricAccessor": "metric_col",
                **({"subtitle": subtitle} if subtitle else {}),
            },
            "query": {"query": "", "language": "kuery"},
            "filters": [],
            "datasourceStates": {"formBased": {"layers": {"layer1": {
                "columnOrder": ["metric_col"],
                "columns": {"metric_col": {
                    "dataType": "number", "isBucketed": False, "label": title,
                    "operationType": agg,
                    **({"sourceField": metric_field} if agg != "count" else {}),
                    "params": {},
                }},
                "incompleteColumns": {},
            }}}},
        },
    }
    refs = [{"id": data_view_id, "name": "indexpattern-datasource-layer-layer1", "type": "index-pattern"}]
    return client.upsert_saved_object("lens", viz_id, attrs, refs)


def lens_xy(client, viz_id, title, metric_field, agg="sum", chart_type="bar_stacked",
            breakdown_field=None, data_view_id="metrics-otel", value_label=None):
    """XY chart (bar/line/area) visualization."""
    columns = {
        "x_col": {
            "dataType": "date", "isBucketed": True, "label": "@timestamp",
            "operationType": "date_histogram", "sourceField": "@timestamp",
            "params": {"interval": "auto"},
        },
        "y_col": {
            "dataType": "number", "isBucketed": False,
            "label": value_label or title, "operationType": agg,
            **({"sourceField": metric_field} if agg != "count" else {}),
            "params": {},
        },
    }
    col_order = ["x_col", "y_col"]

    if breakdown_field:
        columns["break_col"] = {
            "dataType": "string", "isBucketed": True, "label": breakdown_field,
            "operationType": "terms", "sourceField": breakdown_field,
            "params": {"size": 10, "orderBy": {"type": "column", "columnId": "y_col"}, "orderDirection": "desc"},
        }
        col_order = ["x_col", "break_col", "y_col"]

    layer_viz = {
        "layerId": "layer1", "layerType": "data", "seriesType": chart_type,
        "accessors": ["y_col"], "xAccessor": "x_col",
    }
    if breakdown_field:
        layer_viz["splitAccessor"] = "break_col"

    attrs = {
        "title": title,
        "visualizationType": "lnsXY",
        "state": {
            "visualization": {
                "layers": [layer_viz],
                "preferredSeriesType": chart_type,
                "title": title,
            },
            "query": {"query": "", "language": "kuery"},
            "filters": [],
            "datasourceStates": {"formBased": {"layers": {"layer1": {
                "columnOrder": col_order, "columns": columns, "incompleteColumns": {},
            }}}},
        },
    }
    refs = [{"id": data_view_id, "name": "indexpattern-datasource-layer-layer1", "type": "index-pattern"}]
    return client.upsert_saved_object("lens", viz_id, attrs, refs)


def lens_pie(client, viz_id, title, metric_field, slice_field, agg="sum",
             data_view_id="metrics-otel"):
    """Pie/donut chart visualization."""
    columns = {
        "slice_col": {
            "dataType": "string", "isBucketed": True, "label": slice_field,
            "operationType": "terms", "sourceField": slice_field,
            "params": {"size": 10, "orderBy": {"type": "column", "columnId": "metric_col"}, "orderDirection": "desc"},
        },
        "metric_col": {
            "dataType": "number", "isBucketed": False, "label": title,
            "operationType": agg,
            **({"sourceField": metric_field} if agg != "count" else {}),
            "params": {},
        },
    }
    attrs = {
        "title": title,
        "visualizationType": "lnsPie",
        "state": {
            "visualization": {
                "layers": [{"layerId": "layer1", "layerType": "data",
                            "primaryGroups": ["slice_col"], "metric": "metric_col"}],
                "shape": "donut",
            },
            "query": {"query": "", "language": "kuery"},
            "filters": [],
            "datasourceStates": {"formBased": {"layers": {"layer1": {
                "columnOrder": ["slice_col", "metric_col"], "columns": columns, "incompleteColumns": {},
            }}}},
        },
    }
    refs = [{"id": data_view_id, "name": "indexpattern-datasource-layer-layer1", "type": "index-pattern"}]
    return client.upsert_saved_object("lens", viz_id, attrs, refs)


def lens_table(client, viz_id, title, col_configs, data_view_id="logs-otel",
               query_string=""):
    """Table visualization. col_configs: list of (field, label, op)."""
    columns = {}
    col_order = []
    for i, (field, label, op) in enumerate(col_configs):
        col_id = f"col_{i}"
        col_order.append(col_id)
        if op == "terms":
            columns[col_id] = {
                "dataType": "string", "isBucketed": True, "label": label,
                "operationType": "terms", "sourceField": field,
                "params": {"size": 20, "orderDirection": "desc",
                           "orderBy": {"type": "alphabetical"}},
            }
        elif op == "count":
            columns[col_id] = {
                "dataType": "number", "isBucketed": False, "label": label,
                "operationType": "count", "params": {},
            }
        else:
            columns[col_id] = {
                "dataType": "number", "isBucketed": False, "label": label,
                "operationType": op,
                **({"sourceField": field} if op != "count" else {}),
                "params": {},
            }
    attrs = {
        "title": title,
        "visualizationType": "lnsDatatable",
        "state": {
            "visualization": {
                "layerId": "layer1", "layerType": "data",
                "columns": [{"columnId": c} for c in col_order],
            },
            "query": {"query": query_string, "language": "kuery"},
            "filters": [],
            "datasourceStates": {"formBased": {"layers": {"layer1": {
                "columnOrder": col_order, "columns": columns, "incompleteColumns": {},
            }}}},
        },
    }
    refs = [{"id": data_view_id, "name": "indexpattern-datasource-layer-layer1", "type": "index-pattern"}]
    return client.upsert_saved_object("lens", viz_id, attrs, refs)


def markdown_panel(client, viz_id, title, content):
    """Markdown text panel."""
    attrs = {
        "title": title,
        "visState": json.dumps({
            "title": title, "type": "markdown", "aggs": [],
            "params": {"markdown": content, "openLinksInNewTab": True, "fontSize": 12},
        }),
        "uiStateJSON": "{}",
        "description": "",
        "kibanaSavedObjectMeta": {
            "searchSourceJSON": json.dumps({"query": {"query": "", "language": "kuery"}, "filter": []}),
        },
    }
    return client.upsert_saved_object("visualization", viz_id, attrs)


def create_dashboard(client, dash_id, title, description, panel_configs):
    """Create a dashboard. panel_configs: list of (viz_id, viz_type, x, y, w, h)."""
    panels = []
    references = []
    for i, (viz_id, viz_type, gx, gy, gw, gh) in enumerate(panel_configs):
        pid = f"panel_{i}"
        panels.append({
            "version": "8.15.0",
            "type": viz_type,
            "gridData": {"x": gx, "y": gy, "w": gw, "h": gh, "i": pid},
            "panelIndex": pid,
            "embeddableConfig": {},
            "panelRefName": f"panel_{pid}",
        })
        references.append({
            "id": viz_id,
            "name": f"panel_{pid}",
            "type": viz_type,
        })

    attrs = {
        "title": title,
        "description": description,
        "panelsJSON": json.dumps(panels),
        "optionsJSON": json.dumps({
            "useMargins": True, "syncColors": True, "syncCursor": True, "hidePanelTitles": False,
        }),
        "timeRestore": True,
        "timeTo": "now",
        "timeFrom": "now-1h",
        "refreshInterval": {"pause": False, "value": 30000},
        "kibanaSavedObjectMeta": {
            "searchSourceJSON": json.dumps({"query": {"query": "", "language": "kuery"}, "filter": []}),
        },
    }
    return client.upsert_saved_object("dashboard", dash_id, attrs, references)


# ─── Dashboard 1: MuleSoft Integration — Runtime Metrics ────────────────────

def build_dashboard_1(client):
    p = "fnb-mule-"
    print("  Creating visualizations...")

    # Row 1: Metric tiles
    lens_metric(client, f"{p}flow-exec", "Flow Executions", M("mule.flow.executions"), subtitle="Total")
    lens_metric(client, f"{p}flow-errors", "Flow Errors", M("mule.flow.errors"), subtitle="Total")
    lens_metric(client, f"{p}msgs-processed", "Messages Processed", M("mule.messages.processed"), subtitle="Total")
    lens_metric(client, f"{p}active-flows", "Active Flows", M("mule.flows.active"), agg="max", subtitle="Current")

    # Row 2: Flow executions over time by flow name
    lens_xy(client, f"{p}flow-exec-time", "Flow Executions Over Time",
            M("mule.flow.executions"), breakdown_field=A("mule.flow.name"))

    # Row 3: HTTP requests over time + by backend
    lens_xy(client, f"{p}http-over-time", "HTTP Connector Requests Over Time",
            M("mule.http.requests"), chart_type="line",
            breakdown_field=A("mule.backend"), value_label="Requests")
    lens_pie(client, f"{p}http-by-backend", "HTTP Requests by Backend",
             M("mule.http.requests"), A("mule.backend"))

    # Row 4: Messages processed + flow errors over time
    lens_xy(client, f"{p}msgs-over-time", "Messages Processed Over Time",
            M("mule.messages.processed"), chart_type="area",
            breakdown_field=A("mule.flow.name"), value_label="Messages")
    lens_xy(client, f"{p}flow-errors-time", "Flow Errors Over Time",
            M("mule.flow.errors"), chart_type="bar_stacked",
            breakdown_field=A("mule.flow.name"))

    print("  Creating dashboard...")
    create_dashboard(client, "fnb-dashboard-mulesoft-metrics",
        "[FNB] Phase 1: MuleSoft Integration — Runtime Metrics",
        "MuleSoft Anypoint Runtime health: flow executions, errors, HTTP connector activity, and backend connectivity.",
        [
            (f"{p}flow-exec",       "lens", 0,  0, 12, 8),
            (f"{p}flow-errors",     "lens", 12, 0, 12, 8),
            (f"{p}msgs-processed",  "lens", 24, 0, 12, 8),
            (f"{p}active-flows",    "lens", 36, 0, 12, 8),
            (f"{p}flow-exec-time",  "lens", 0,  8, 48, 12),
            (f"{p}http-over-time",  "lens", 0,  20, 28, 14),
            (f"{p}http-by-backend", "lens", 28, 20, 20, 14),
            (f"{p}msgs-over-time",  "lens", 0,  34, 24, 14),
            (f"{p}flow-errors-time","lens", 24, 34, 24, 14),
        ])


# ─── Dashboard 2: Payment Operations — Business Metrics ─────────────────────

def build_dashboard_2(client):
    p = "fnb-pay-"
    print("  Creating visualizations...")

    # Row 1: Key counters
    lens_metric(client, f"{p}portal-reqs", "Portal Requests", M("portal.requests.total"), subtitle="Total")
    lens_metric(client, f"{p}portal-errs", "Portal Errors", M("portal.errors.total"), subtitle="Total")
    lens_metric(client, f"{p}fraud-checks", "Fraud Checks", M("fraud.checks.total"), subtitle="Total")
    lens_metric(client, f"{p}aml-checks", "AML Screenings", M("aml.checks.total"), subtitle="Total")

    # Row 2: Transaction volume over time by operation
    lens_xy(client, f"{p}txn-volume", "Transaction Volume Over Time",
            M("portal.requests.total"), breakdown_field=A("portal.operation"))

    # Row 3: MuleSoft calls + portal errors
    lens_xy(client, f"{p}mule-calls", "MuleSoft Calls by Operation",
            M("portal.mulesoft.calls"), chart_type="bar",
            breakdown_field=A("mulesoft.operation"), value_label="Calls")
    lens_xy(client, f"{p}mule-errors", "MuleSoft Call Errors",
            M("portal.mulesoft.errors"), chart_type="bar_stacked",
            breakdown_field=A("mulesoft.operation"), value_label="Errors")

    # Row 4: Fraud by risk level + AML results + portal ops breakdown
    lens_pie(client, f"{p}fraud-risk", "Fraud Checks by Risk Level",
             M("fraud.checks.total"), A("fraud.risk_level"))
    lens_pie(client, f"{p}portal-ops", "Portal Requests by Operation",
             M("portal.requests.total"), A("portal.operation"))

    # Row 5: Portal errors over time
    lens_xy(client, f"{p}portal-errors-time", "Portal Errors Over Time",
            M("portal.errors.total"), chart_type="bar_stacked",
            breakdown_field=A("portal.operation"))

    print("  Creating dashboard...")
    create_dashboard(client, "fnb-dashboard-payment-ops",
        "[FNB] Phase 1: Payment Operations — Business Metrics",
        "Business-level view: transaction volumes, fraud detection, AML screening, and portal health.",
        [
            (f"{p}portal-reqs",         "lens", 0,  0, 12, 8),
            (f"{p}portal-errs",         "lens", 12, 0, 12, 8),
            (f"{p}fraud-checks",        "lens", 24, 0, 12, 8),
            (f"{p}aml-checks",          "lens", 36, 0, 12, 8),
            (f"{p}txn-volume",          "lens", 0,  8, 48, 12),
            (f"{p}mule-calls",          "lens", 0,  20, 24, 14),
            (f"{p}mule-errors",         "lens", 24, 20, 24, 14),
            (f"{p}fraud-risk",          "lens", 0,  34, 24, 14),
            (f"{p}portal-ops",          "lens", 24, 34, 24, 14),
            (f"{p}portal-errors-time",  "lens", 0,  48, 48, 12),
        ])


# ─── Dashboard 3: Core Banking — Database Performance ───────────────────────

def build_dashboard_3(client):
    p = "fnb-db-"
    print("  Creating visualizations...")

    # Row 1: Key DB metrics
    lens_metric(client, f"{p}total-queries", "Total DB Queries", M("db.queries.total"), subtitle="Count")
    lens_metric(client, f"{p}slow-queries", "Slow Queries", M("db.slow_queries.total"), subtitle="Count")
    lens_metric(client, f"{p}active-sessions", "Active DB Sessions", M("db.sessions.active"), agg="max", subtitle="Current")
    lens_metric(client, f"{p}debits", "Accounts Debited", M("banking.accounts.debited"), subtitle="Count")

    # Row 2: DB queries over time (normal vs slow)
    lens_xy(client, f"{p}queries-over-time", "DB Queries Over Time",
            M("db.queries.total"), chart_type="bar",
            breakdown_field=A("db.operation"), value_label="Queries")

    # Row 3: Slow queries over time + queries by table
    lens_xy(client, f"{p}slow-over-time", "Slow Queries Over Time (Smoking Gun)",
            M("db.slow_queries.total"), chart_type="bar",
            breakdown_field=A("db.sql.table"), value_label="Slow Queries")
    lens_pie(client, f"{p}queries-by-table", "Queries by Table",
             M("db.queries.total"), A("db.sql.table"))

    # Row 4: Balance checks + active sessions over time
    lens_xy(client, f"{p}balance-checks", "Balance Checks by Account Type",
            M("banking.balance.checks"), chart_type="bar_stacked",
            breakdown_field=A("account.type"))
    lens_xy(client, f"{p}sessions-over-time", "Active DB Sessions Over Time",
            M("db.sessions.active"), agg="max", chart_type="line",
            value_label="Active Sessions")

    # Row 5: Slow query log
    lens_table(client, f"{p}slow-log", "Slow Query Log",
               [("service.name", "Service", "terms"),
                ("message", "Message Count", "count")],
               data_view_id="logs-otel", query_string="SLOW QUERY")

    print("  Creating dashboard...")
    create_dashboard(client, "fnb-dashboard-core-banking-db",
        "[FNB] Phase 1: Core Banking — Database Performance",
        "Core banking DB health: query volumes, slow query detection (the demo's smoking gun), and transaction activity.",
        [
            (f"{p}total-queries",     "lens", 0,  0, 12, 8),
            (f"{p}slow-queries",      "lens", 12, 0, 12, 8),
            (f"{p}active-sessions",   "lens", 24, 0, 12, 8),
            (f"{p}debits",            "lens", 36, 0, 12, 8),
            (f"{p}queries-over-time", "lens", 0,  8, 48, 14),
            (f"{p}slow-over-time",    "lens", 0,  22, 28, 14),
            (f"{p}queries-by-table",  "lens", 28, 22, 20, 14),
            (f"{p}balance-checks",    "lens", 0,  36, 24, 12),
            (f"{p}sessions-over-time","lens", 24, 36, 24, 12),
            (f"{p}slow-log",          "lens", 0,  48, 48, 14),
        ])


# ─── Dashboard 4: Distributed Tracing — Service Flow ────────────────────────

def build_dashboard_4(client):
    p = "fnb-trace-"
    print("  Creating visualizations...")

    # Markdown: APM links
    markdown_panel(client, f"{p}apm-link", "Service Map & APM",
        "## Distributed Tracing\n\n"
        "Click links below to explore full traces in Elastic APM:\n\n"
        "- **[Service Map](/app/apm/service-map)** — Visual dependency graph\n"
        "- **[Services](/app/apm/services)** — All instrumented services\n"
        "- **[Traces](/app/apm/traces)** — Transaction waterfall views\n\n"
        "### What Traces Unlock\n"
        "Traces show the *full journey* of a request across services. "
        "When MuleSoft calls the core banking service and a slow DB query happens, "
        "the trace waterfall reveals exactly where time was spent.\n\n"
        "**Try it:** Filter traces by `transaction.duration.us > 3000000` to find "
        "transactions impacted by slow queries."
    )

    # Trace volume by service over time
    lens_xy(client, f"{p}trace-count", "Trace Volume by Service",
            "span.duration.us", agg="count", chart_type="bar_stacked",
            breakdown_field="service.name", data_view_id="traces-otel",
            value_label="Span Count")
    lens_pie(client, f"{p}traces-by-svc", "Traces by Service",
             "span.duration.us", "service.name", agg="count",
             data_view_id="traces-otel")

    # Span duration by service
    lens_xy(client, f"{p}duration-by-svc", "Avg Span Duration by Service",
            "span.duration.us", agg="average", chart_type="bar",
            breakdown_field="service.name", data_view_id="traces-otel",
            value_label="Avg Duration (μs)")

    # Slowest spans table
    lens_table(client, f"{p}slowest-txns", "Slowest Spans",
               [("service.name", "Service", "terms"),
                ("span.name", "Span Name", "terms"),
                ("span.duration.us", "Max Duration (μs)", "max")],
               data_view_id="traces-otel")

    # Span duration trend
    lens_xy(client, f"{p}e2e-latency", "Span Duration Trend by Service",
            "span.duration.us", agg="average", chart_type="line",
            breakdown_field="service.name", data_view_id="traces-otel",
            value_label="Avg Duration (μs)")

    print("  Creating dashboard...")
    create_dashboard(client, "fnb-dashboard-tracing",
        "[FNB] Phase 2: Distributed Tracing — Service Flow",
        "What distributed tracing unlocks: end-to-end request paths, service dependencies, and latency breakdown.",
        [
            (f"{p}apm-link",        "visualization", 0,  0,  16, 16),
            (f"{p}trace-count",     "lens",          16, 0,  16, 16),
            (f"{p}traces-by-svc",   "lens",          32, 0,  16, 16),
            (f"{p}duration-by-svc", "lens",          0,  16, 48, 14),
            (f"{p}slowest-txns",    "lens",          0,  30, 48, 14),
            (f"{p}e2e-latency",     "lens",          0,  44, 48, 14),
        ])


# ─── Dashboard 5: Operations Command Center ─────────────────────────────────

def build_dashboard_5(client):
    p = "fnb-ops-"
    print("  Creating visualizations...")

    # Markdown header
    markdown_panel(client, f"{p}header", "Operations Command Center",
        "# FNB Operations Command Center\n\n"
        "**Combined view:** Metrics + Traces + Logs\n\n"
        "This dashboard correlates all three signal types to give the full picture. "
        "When an alert fires, use this dashboard to:\n"
        "1. **Spot the anomaly** in metrics (top row)\n"
        "2. **Trace the root cause** via APM (middle row)\n"
        "3. **Read the logs** for details (bottom row)\n\n"
        "---\n"
        "*Phase 1 gave you the metrics. Phase 2 shows why they matter.*"
    )

    # Row 1: Health indicators
    lens_metric(client, f"{p}total-flows", "Total Flows Executed", M("mule.flow.executions"), subtitle="MuleSoft")
    lens_metric(client, f"{p}total-errors", "Total Errors", M("portal.errors.total"), subtitle="Portal")
    lens_metric(client, f"{p}portal-total", "Portal Requests", M("portal.requests.total"), subtitle="Portal")
    lens_metric(client, f"{p}notif-sent", "Notifications Sent", M("notification.sent.total"), subtitle="Notification Svc")

    # Row 2: MuleSoft flow executions trend
    lens_xy(client, f"{p}flow-trend", "MuleSoft Flow Executions Over Time",
            M("mule.flow.executions"), agg="sum", chart_type="line",
            breakdown_field=A("mule.flow.name"), value_label="Executions")

    # Row 3: Error rate + backend HTTP requests
    lens_xy(client, f"{p}error-rate", "Portal Errors Over Time",
            M("portal.errors.total"), chart_type="area",
            breakdown_field=A("portal.operation"))
    lens_xy(client, f"{p}backend-http", "MuleSoft HTTP Requests by Backend",
            M("mule.http.requests"), chart_type="bar",
            breakdown_field=A("mule.backend"), value_label="Requests")

    # Row 4: Fraud checks + slow queries
    lens_xy(client, f"{p}fraud-checks", "Fraud Checks by Risk Level",
            M("fraud.checks.total"), chart_type="bar",
            breakdown_field=A("fraud.risk_level"))
    lens_xy(client, f"{p}slow-queries", "Slow Queries (Correlated Impact)",
            M("db.slow_queries.total"), chart_type="bar",
            value_label="Slow Query Count")

    # Row 5: Notifications + tellers active
    lens_pie(client, f"{p}notif-types", "Notifications by Type",
             M("notification.sent.total"), A("notification.type"))
    lens_xy(client, f"{p}tellers", "Active Tellers Over Time",
            M("portal.tellers.active"), agg="max", chart_type="line",
            value_label="Active Tellers")

    # Row 6: Error log table
    lens_table(client, f"{p}error-logs", "Recent Error & Warning Logs",
               [("service.name", "Service", "terms"),
                ("log.level", "Level", "terms"),
                ("message", "Message Count", "count")],
               data_view_id="logs-otel", query_string="log.level: (ERROR OR WARN*)")

    print("  Creating dashboard...")
    create_dashboard(client, "fnb-dashboard-ops-center",
        "[FNB] Phase 2: Operations Command Center",
        "Full picture: metrics + traces + logs correlated for incident response.",
        [
            (f"{p}header",         "visualization", 0,  0,  16, 12),
            (f"{p}total-flows",    "lens",          16, 0,  8,  8),
            (f"{p}total-errors",   "lens",          24, 0,  8,  8),
            (f"{p}portal-total",   "lens",          32, 0,  8,  8),
            (f"{p}notif-sent",     "lens",          40, 0,  8,  8),
            (f"{p}flow-trend",     "lens",          16, 8,  32, 12),
            (f"{p}error-rate",     "lens",          0,  20, 24, 14),
            (f"{p}backend-http",   "lens",          24, 20, 24, 14),
            (f"{p}fraud-checks",   "lens",          0,  34, 24, 12),
            (f"{p}slow-queries",   "lens",          24, 34, 24, 12),
            (f"{p}notif-types",    "lens",          0,  46, 16, 12),
            (f"{p}tellers",        "lens",          16, 46, 32, 12),
            (f"{p}error-logs",     "lens",          0,  58, 48, 14),
        ])


# ─── Main ────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(description="Create FNB demo dashboards in Kibana")
    parser.add_argument("--kibana-url", default=os.getenv("KIBANA_URL"),
                        help="Kibana base URL")
    parser.add_argument("--api-key", default=os.getenv("ELASTIC_API_KEY"),
                        help="Elastic API key (base64-encoded)")
    args = parser.parse_args()

    if not args.kibana_url or not args.api_key:
        print("ERROR: --kibana-url and --api-key are required (or set KIBANA_URL / ELASTIC_API_KEY env vars)")
        sys.exit(1)

    client = KibanaClient(args.kibana_url, args.api_key)
    print(f"Connecting to Kibana: {args.kibana_url}")

    # Step 1: Create data views
    print("\n1. Creating data views...")
    client.create_data_view("metrics-otel", METRICS_INDEX)
    client.create_data_view("traces-otel", TRACES_INDEX)
    client.create_data_view("logs-otel", LOGS_INDEX)

    # Step 2: Build dashboards
    dashboards = [
        ("MuleSoft Integration — Runtime Metrics", build_dashboard_1),
        ("Payment Operations — Business Metrics", build_dashboard_2),
        ("Core Banking — Database Performance", build_dashboard_3),
        ("Distributed Tracing — Service Flow", build_dashboard_4),
        ("Operations Command Center", build_dashboard_5),
    ]

    for i, (name, builder) in enumerate(dashboards, 1):
        print(f"\n{i+1}. Building: {name}")
        builder(client)

    # Summary
    print(f"\n{'='*60}")
    print(f"Created {client.created} objects, {client.errors} errors")
    print(f"{'='*60}")

    # Dashboard URLs
    base = args.kibana_url.rstrip("/")
    print("\nDashboard URLs:")
    for dash_id, dash_name in [
        ("fnb-dashboard-mulesoft-metrics", "[Phase 1] MuleSoft Integration — Runtime Metrics"),
        ("fnb-dashboard-payment-ops",      "[Phase 1] Payment Operations — Business Metrics"),
        ("fnb-dashboard-core-banking-db",  "[Phase 1] Core Banking — Database Performance"),
        ("fnb-dashboard-tracing",          "[Phase 2] Distributed Tracing — Service Flow"),
        ("fnb-dashboard-ops-center",       "[Phase 2] Operations Command Center"),
    ]:
        print(f"  {dash_name}")
        print(f"    {base}/app/dashboards#/view/{dash_id}")

    print()
    if client.errors > 0:
        print(f"WARNING: {client.errors} objects failed to create. Check errors above.")
        sys.exit(1)
    else:
        print("All dashboards created successfully!")


if __name__ == "__main__":
    main()
