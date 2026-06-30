"""
Web dashboard for bedrock-insights.

A dependency-free HTTP server (Python stdlib only) that exposes the same
CloudWatch + pricing data used by the terminal UI as a JSON API, and serves a
self-contained HTML dashboard that polls that API on an interval.

Routes
------
  GET /                  → the dashboard HTML page
  GET /api/usage         → latest cached usage snapshot as JSON
  GET /api/config        → static config (refresh interval, labels, threshold)

Scalability
-----------
A single background thread (``UsageMonitor``) polls CloudWatch incrementally —
mirroring terminal ``--live`` mode: it pins the window start at launch, then
re-queries only a short trailing overlap window every few seconds and
deduplicates events by ID. The aggregated result is cached in memory behind a
lock and re-rendered into a JSON payload once per poll.

HTTP requests only read that cached payload, so the load on CloudWatch is
constant regardless of how many browser tabs are open or how often they
refresh. ``FilterLogEvents`` is never called from the request path.
"""

from __future__ import annotations

import copy
import csv
import errno
import hmac
import io
import json
import os
import threading
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from botocore.exceptions import ClientError
from rich.console import Console

from .cloudwatch import get_time_range, iter_log_events, normalize_model_id, parse_since
from .display import period_label, since_label
from .notify import ThresholdAlerter
from .pricing import init_pricing_regions, lookup
from .storage import FactStore

console = Console()


def _env_int(name: str, default: int) -> int:
    """Read a positive int from the environment, falling back to default."""
    try:
        v = int(os.environ.get(name, ""))
        return v if v > 0 else default
    except (TypeError, ValueError):
        return default


# Background poll cadence + UI refresh default (seconds). Override via the
# BEDROCK_INSIGHTS_POLL_SECONDS env var (e.g. the CloudFormation deploy sets it).
REFRESH_SECONDS  = _env_int("BEDROCK_INSIGHTS_POLL_SECONDS", 5)
_LIVE_OVERLAP_MS = 90_000

# Persistence: per-event facts survive restarts and outlive CloudWatch retention.
_DB_RETENTION_DAYS = 90
_DB_PATH = Path.home() / ".config" / "bedrock-insights" / "facts.db"

# Hybrid storage: keep recent facts in memory (fast path); aggregate older
# windows on demand from SQLite. The memory window comfortably covers all
# built-in periods (today/yesterday/week); longer custom windows hit the DB.
_MEMORY_WINDOW_DAYS = 8


def _now_ms() -> int:
    return int(datetime.now(timezone.utc).timestamp() * 1000)


def _record_ms(r: dict) -> int:
    """Best-effort event timestamp (ms) for a log record, for time bucketing."""
    ts = r.get("timestamp")
    if isinstance(ts, str):
        try:
            return int(datetime.fromisoformat(ts.replace("Z", "+00:00")).timestamp() * 1000)
        except ValueError:
            pass
    it = r.get("_ingestionTime")
    if isinstance(it, (int, float)) and it:
        return int(it)
    return _now_ms()


def _record_cost_tokens(r: dict, region: str | None = None) -> tuple[float, int, int, int, int, bool, str]:
    """Return (cost, input, output, cache_write, cache_read, price_known, display_name).

    Priced with the given region's table when available (multi-region).
    """
    raw_id    = r.get("modelId", "unknown")
    inp_data  = r.get("input") or {}
    inp = inp_data.get("inputTokenCount")           or 0
    cw  = inp_data.get("cacheWriteInputTokenCount") or 0
    cr  = inp_data.get("cacheReadInputTokenCount")  or 0
    out = (r.get("output") or {}).get("outputTokenCount") or 0
    p = lookup(raw_id, prefer_global=raw_id.lower().startswith("global."), region=region)
    cost = (
        inp * p.input_per_1m + out * p.output_per_1m
        + cw * p.cache_write_per_1m + cr * p.cache_read_per_1m
    ) / 1_000_000
    return cost, inp, out, cw, cr, (not p.needs_pricing), p.display_name


def _identity_key(arn: str) -> tuple[str, str]:
    """Return (group_key, display_label) for an IAM principal ARN.

    Sessions of the same assumed role collapse to one group:
      arn:aws:sts::123:assumed-role/MyRole/session  → ("assumed-role/MyRole", "MyRole")
      arn:aws:iam::123:user/alice                    → ("user/alice", "alice")
    """
    if not arn:
        return "unknown", "unknown"
    resource = arn.split(":")[-1]
    parts = resource.split("/")
    if parts[0] == "assumed-role" and len(parts) >= 2:
        return "assumed-role/" + parts[1], parts[1]
    if len(parts) >= 2:
        return resource, parts[-1]
    return resource, resource


def build_payload(usage: dict[str, dict]) -> dict:
    """Convert an aggregated usage dict into a JSON-serialisable payload.

    Mirrors the cost math in display.build_table but emits structured data
    instead of a Rich table, so the frontend can render it however it likes.
    """
    models: list[dict] = []
    total_calls = total_input = total_output = 0
    total_cache_write = total_cache_read = 0
    total_cost = 0.0
    all_prices_known = True

    for model_id, stats in sorted(usage.items()):
        inp = stats["input_tokens"]
        out = stats["output_tokens"]
        cw  = stats.get("cache_write_tokens", 0)
        cr  = stats.get("cache_read_tokens", 0)
        calls = stats["calls"]

        if "cost" in stats:                      # region-priced upstream (multi-region)
            cost = stats["cost"]
            known = stats.get("price_known", True)
            name = stats.get("display_name") or model_id
        else:                                    # single-table fallback
            p = lookup(model_id, prefer_global=stats.get("is_global", False))
            known = not p.needs_pricing
            cost = (
                inp * p.input_per_1m + out * p.output_per_1m
                + cw * p.cache_write_per_1m + cr * p.cache_read_per_1m
            ) / 1_000_000
            name = p.display_name

        if not known:
            all_prices_known = False

        total_calls       += calls
        total_input       += inp
        total_output      += out
        total_cache_write += cw
        total_cache_read  += cr
        total_cost        += cost

        models.append({
            "model_id":          model_id,
            "display_name":      name,
            "calls":             calls,
            "input_tokens":      inp,
            "output_tokens":     out,
            "cache_write_tokens": cw,
            "cache_read_tokens":  cr,
            "total_tokens":      inp + out,
            "cost":              round(cost, 6),
            "price_known":       known,
            "is_global":         stats.get("is_global", False),
        })

    # Sort by cost desc (unknown-price models, cost 0, fall to the bottom).
    models.sort(key=lambda m: m["cost"], reverse=True)

    # Per-model cost share (fraction of total spend).
    for m in models:
        m["cost_share"] = round(m["cost"] / total_cost, 4) if total_cost > 0 else 0.0

    total_tokens = total_input + total_output
    cache_read_ratio = (
        total_cache_read / (total_input + total_cache_read)
        if (total_input + total_cache_read) > 0 else 0.0
    )

    return {
        "models": models,
        "totals": {
            "calls":              total_calls,
            "input_tokens":       total_input,
            "output_tokens":      total_output,
            "cache_write_tokens": total_cache_write,
            "cache_read_tokens":  total_cache_read,
            "total_tokens":       total_tokens,
            "cost":               round(total_cost, 6),
            "cost_known":         all_prices_known,
            "avg_cost_per_call":  round(total_cost / total_calls, 6) if total_calls else 0.0,
            "avg_tokens_per_call": round(total_tokens / total_calls, 1) if total_calls else 0.0,
            "cache_hit_rate":     round(cache_read_ratio, 4),
        },
        "has_cache": (total_cache_write + total_cache_read) > 0,
    }


def compute_projection(cost: float, start_ms: int, now_ms: int) -> dict:
    """Extrapolate a run-rate projection from spend so far.

    These are simple linear run-rate estimates (current burn rate × time), not
    forecasts — clearly labelled as estimates in the UI.
    """
    elapsed_hours = max((now_ms - start_ms) / 3.6e6, 1 / 60)
    burn_per_hour = cost / elapsed_hours
    return {
        "elapsed_hours":     round(elapsed_hours, 4),
        "burn_per_hour":     round(burn_per_hour, 6),
        "projected_daily":   round(burn_per_hour * 24, 4),
        "projected_monthly": round(burn_per_hour * 24 * 30, 2),
    }


def payload_to_csv(payload: dict) -> str:
    """Serialise a usage payload's per-model rows as CSV text."""
    out = io.StringIO()
    writer = csv.writer(out)
    writer.writerow([
        "model_id", "display_name", "calls", "input_tokens",
        "cache_write_tokens", "cache_read_tokens", "output_tokens",
        "total_tokens", "cost", "price_known", "cost_share",
    ])
    for m in payload.get("models", []):
        writer.writerow([
            m["model_id"], m["display_name"], m["calls"], m["input_tokens"],
            m["cache_write_tokens"], m["cache_read_tokens"], m["output_tokens"],
            m["total_tokens"], f'{m["cost"]:.6f}', m["price_known"],
            f'{m.get("cost_share", 0):.4f}',
        ])
    return out.getvalue()


def _prom_escape(v: str) -> str:
    return v.replace("\\", "\\\\").replace("\n", "\\n").replace('"', '\\"')


def render_prometheus(payload: dict) -> str:
    """Render a usage payload as Prometheus text exposition format (v0.0.4).

    Metrics are gauges reflecting the current window (they reset when the
    process restarts). Suitable for scraping into Prometheus/Grafana.
    """
    lines: list[str] = []

    def metric(name: str, mtype: str, help_text: str, samples: list[tuple[dict, float]]) -> None:
        lines.append(f"# HELP {name} {help_text}")
        lines.append(f"# TYPE {name} {mtype}")
        for labels, value in samples:
            if labels:
                ls = ",".join(f'{k}="{_prom_escape(str(v))}"' for k, v in labels.items())
                lines.append(f"{name}{{{ls}}} {value}")
            else:
                lines.append(f"{name} {value}")

    t       = payload["totals"]
    models  = payload.get("models", [])
    idents  = payload.get("identities", [])
    regions = payload.get("regions", [])
    errors  = payload.get("errors", {"rate": 0.0, "by_code": []})

    metric("bedrock_cost_usd", "gauge", "Estimated total cost (USD) for the window",
           [({}, t["cost"])])
    metric("bedrock_calls", "gauge", "Total model invocations for the window",
           [({}, t["calls"])])
    metric("bedrock_tokens", "gauge", "Total tokens (input + output) for the window",
           [({}, t["total_tokens"])])
    metric("bedrock_cache_hit_rate", "gauge", "Cache read tokens / (input + cache read)",
           [({}, t["cache_hit_rate"])])
    metric("bedrock_error_rate", "gauge", "Failed calls / total calls",
           [({}, errors["rate"])])

    metric("bedrock_model_cost_usd", "gauge", "Estimated cost (USD) per model",
           [({"model": m["model_id"]}, m["cost"]) for m in models])
    metric("bedrock_model_calls", "gauge", "Invocations per model",
           [({"model": m["model_id"]}, m["calls"]) for m in models])
    metric(
        "bedrock_model_tokens", "gauge", "Tokens per model by type",
        [({"model": m["model_id"], "type": "input"},  m["input_tokens"])  for m in models]
        + [({"model": m["model_id"], "type": "output"}, m["output_tokens"]) for m in models]
        + [({"model": m["model_id"], "type": "cache_read"},  m["cache_read_tokens"])  for m in models]
        + [({"model": m["model_id"], "type": "cache_write"}, m["cache_write_tokens"]) for m in models],
    )

    metric("bedrock_identity_cost_usd", "gauge", "Estimated cost (USD) per IAM identity",
           [({"identity": i["label"]}, i["cost"]) for i in idents])
    metric("bedrock_identity_calls", "gauge", "Invocations per IAM identity",
           [({"identity": i["label"]}, i["calls"]) for i in idents])

    metric("bedrock_region_cost_usd", "gauge", "Estimated cost (USD) per region",
           [({"region": r["region"]}, r["cost"]) for r in regions])
    metric("bedrock_region_calls", "gauge", "Invocations per region",
           [({"region": r["region"]}, r["calls"]) for r in regions])

    metric("bedrock_errors", "gauge", "Failed invocations by error code",
           [({"code": c["code"]}, c["count"]) for c in errors.get("by_code", [])])

    return "\n".join(lines) + "\n"


def _resolve_window(period: str, since: str | None) -> tuple[int, int, str]:
    """Return (start_ms, end_ms, label) for a period or a --since duration."""
    if since:
        start_ms, end_ms = parse_since(since)
        return start_ms, end_ms, since_label(since)
    start_ms, end_ms = get_time_range(period)
    return start_ms, end_ms, period_label(period)


def _bucket_seconds_for(span_ms: int) -> int:
    """Pick trend granularity from a window span: ≤3h→1min, ≤36h→5min, else 1h."""
    if span_ms <= 3 * 3_600_000:
        return 60
    if span_ms <= 36 * 3_600_000:
        return 300
    return 3_600


def _aggregate_facts(
    facts: list[dict], start_ms: int, end_ms: int, bucket_seconds: int, flt: dict | None,
) -> dict:
    """Aggregate slim per-event facts into a full dashboard payload.

    Applies a time window and an optional dimension filter (model / identity /
    region). Runs on demand per request so the dashboard can switch periods and
    drill into a single model/identity/region without re-querying CloudWatch.
    """
    bucket_ms = bucket_seconds * 1000
    f_model    = flt.get("model")    if flt else None
    f_identity = flt.get("identity") if flt else None
    f_region   = flt.get("region")   if flt else None

    usage:   dict[str, dict] = {}
    buckets: dict[int, dict] = {}
    by_identity: dict[str, dict] = {}
    by_region:   dict[str, dict] = {}
    errors_by_code: dict[str, int] = {}
    error_total = 0

    for f in facts:
        if f["t"] < start_ms or f["t"] > end_ms:
            continue
        if f_model is not None and f["model"] != f_model:
            continue
        if f_identity is not None and f["ident_key"] != f_identity:
            continue
        if f_region is not None and f["region"] != f_region:
            continue

        u = usage.setdefault(f["model"], {
            "calls": 0, "input_tokens": 0, "output_tokens": 0,
            "cache_write_tokens": 0, "cache_read_tokens": 0, "is_global": False,
            "cost": 0.0, "price_known": True, "display_name": f["model"],
        })
        u["calls"]              += 1
        u["input_tokens"]       += f["inp"]
        u["output_tokens"]      += f["out"]
        u["cache_write_tokens"] += f["cw"]
        u["cache_read_tokens"]  += f["cr"]
        u["is_global"]           = u["is_global"] or f["is_global"]
        u["cost"]               += f["cost"]
        u["price_known"]         = u["price_known"] and f["known"]
        u["display_name"]        = f["display"]

        bt = (f["t"] // bucket_ms) * bucket_ms
        b = buckets.setdefault(bt, {"cost": 0.0, "input": 0, "output": 0, "calls": 0})
        b["cost"]   += f["cost"]
        b["input"]  += f["inp"]
        b["output"] += f["out"]
        b["calls"]  += 1

        idn = by_identity.setdefault(f["ident_key"], {
            "label": f["ident_label"], "calls": 0, "input": 0, "output": 0,
            "cost": 0.0, "errors": 0,
        })
        idn["calls"]  += 1
        idn["input"]  += f["inp"]
        idn["output"] += f["out"]
        idn["cost"]   += f["cost"]
        idn["errors"] += 1 if f["err"] else 0

        reg = by_region.setdefault(f["region"], {
            "calls": 0, "input": 0, "output": 0, "cost": 0.0, "errors": 0,
        })
        reg["calls"]  += 1
        reg["input"]  += f["inp"]
        reg["output"] += f["out"]
        reg["cost"]   += f["cost"]
        reg["errors"] += 1 if f["err"] else 0

        if f["err"]:
            error_total += 1
            errors_by_code[f["err"]] = errors_by_code.get(f["err"], 0) + 1

    payload = build_payload(usage)
    total_cost  = payload["totals"]["cost"]
    total_calls = payload["totals"]["calls"]

    def share(c):
        return round(c / total_cost, 4) if total_cost > 0 else 0.0

    payload["trend"] = {
        "bucket_seconds": bucket_seconds,
        "points": [
            {
                "t": b, "cost": round(d["cost"], 6),
                "input_tokens": d["input"], "output_tokens": d["output"],
                "total_tokens": d["input"] + d["output"], "calls": d["calls"],
            }
            for b, d in sorted(buckets.items())
        ],
    }
    payload["identities"] = sorted(
        (
            {
                "key": key, "label": d["label"], "calls": d["calls"],
                "input_tokens": d["input"], "output_tokens": d["output"],
                "total_tokens": d["input"] + d["output"],
                "cost": round(d["cost"], 6), "errors": d["errors"],
                "cost_share": share(d["cost"]),
            }
            for key, d in by_identity.items()
        ),
        key=lambda x: x["cost"], reverse=True,
    )[:12]
    payload["regions"] = sorted(
        (
            {
                "region": name, "calls": d["calls"],
                "total_tokens": d["input"] + d["output"],
                "cost": round(d["cost"], 6), "errors": d["errors"],
                "cost_share": share(d["cost"]),
            }
            for name, d in by_region.items()
        ),
        key=lambda x: x["cost"], reverse=True,
    )
    payload["errors"] = {
        "total": error_total,
        "rate":  round(error_total / total_calls, 4) if total_calls else 0.0,
        "by_code": sorted(
            ({"code": c, "count": n} for c, n in errors_by_code.items()),
            key=lambda x: x["count"], reverse=True,
        ),
    }
    payload["projection"] = compute_projection(total_cost, start_ms, end_ms)
    return payload


def _filter_key(flt: dict | None) -> str:
    """Canonical, hashable string for a filter dict (cache key component)."""
    if not flt:
        return ""
    return ";".join(f"{k}={flt[k]}" for k in sorted(flt))


class UsageMonitor:
    """Background poller retaining slim per-event facts for on-demand aggregation.

    Like terminal --live mode, it polls CloudWatch incrementally (trailing
    overlap window + event-ID dedup) and never re-queries from the request path.
    Instead of caching one pre-aggregated payload, it keeps a compact fact per
    event so HTTP requests can aggregate any sub-window or filter on demand —
    powering UI period switching and drill-down filters without extra AWS calls.

    To serve period switches, it polls from the *earliest* start it may be asked
    about (the wider of the launch window and the past 7 days).
    """

    _SWITCHABLE = ("today", "yesterday", "week")

    def __init__(self, clients, period: str, since: str | None,
                 poll_callback=None, store=None, memory_window_days: int = _MEMORY_WINDOW_DAYS) -> None:
        # clients: list of (region, logs_client) tuples (one per region).
        self._clients = clients
        self._since = since
        self._default_period = "since" if since else period
        self._poll_callback = poll_callback
        self._store = store
        self._memory_window_ms = memory_window_days * 86_400_000

        starts = [get_time_range("week")[0]]
        starts.append(parse_since(since)[0] if since else get_time_range(period)[0])
        self._start_ms = min(starts)
        self._poll_from_ms = self._start_ms

        self._facts: list[dict] = []
        self._seen_ids: set[str] = set()
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._last_updated: datetime | None = None
        self._last_error: tuple[str, str] | None = None

        # Memory holds facts at or after this timestamp; older windows are served
        # from SQLite. Trimmed forward on each poll to keep memory bounded.
        self._memory_floor_ms = _now_ms() - self._memory_window_ms

        # Load recent persisted history into memory so trends are available
        # immediately and survive restarts. Seeding seen_ids prevents
        # CloudWatch's overlap window from re-counting events we already have.
        if store is not None:
            facts, keys = store.load(self._memory_floor_ms)
            self._facts.extend(facts)
            self._seen_ids.update(keys)

        # On-demand aggregation cache. Keyed by (period, filter); the whole cache
        # is invalidated whenever the fact count changes (i.e. a new poll added
        # events), so entries live at most one poll interval. This makes repeated
        # identical requests (many tabs, the Prometheus scraper, per-tab polling)
        # O(1) instead of O(events) when the data hasn't changed.
        self._cache: dict[tuple, dict] = {}
        self._cache_version = -1
        self._cache_lock = threading.Lock()

    # ── window resolution ────────────────────────────────────────────────────
    def _window_for(self, period: str | None, since: str | None = None) -> tuple[str, int, int, str, int]:
        """Return (period, start_ms, end_ms, label, bucket_seconds) for a request.

        A valid `since` duration (e.g. "2h") overrides the named period, giving a
        custom rolling window — bounded by the facts actually retained in memory.
        """
        if since:
            try:
                s, e = parse_since(since)
                return "custom", s, e, since_label(since), _bucket_seconds_for(e - s)
            except ValueError:
                pass  # invalid duration → fall back to the period
        period = period or self._default_period
        if period == "since" and self._since:
            s, e = parse_since(self._since)
            return "since", s, e, since_label(self._since), _bucket_seconds_for(e - s)
        if period in self._SWITCHABLE:
            s, e = get_time_range(period)
            bucket = 3_600 if period == "week" else 300
            return period, s, e, period_label(period), bucket
        # Unknown period → fall back to the launch default.
        if self._since:
            s, e = parse_since(self._since)
            return "since", s, e, since_label(self._since), _bucket_seconds_for(e - s)
        s, e = get_time_range(self._default_period)
        bucket = 3_600 if self._default_period == "week" else 300
        return self._default_period, s, e, period_label(self._default_period), bucket

    def periods(self) -> list[dict]:
        opts = [
            {"id": "today",     "label": "Today"},
            {"id": "yesterday", "label": "Yesterday"},
            {"id": "week",      "label": "Past 7 Days"},
        ]
        if self._since:
            opts.insert(0, {"id": "since", "label": since_label(self._since)})
        return opts

    # ── ingest ────────────────────────────────────────────────────────────────
    def _ingest(self, from_ms: int) -> None:
        """Pull new events from every region (concurrently) since from_ms, dedup, append facts."""
        to_ms = _now_ms()

        def fetch(region_client):
            region, client = region_client
            return region, list(iter_log_events(client, from_ms, to_ms))

        results: list[tuple[str, list]] = []
        error: tuple[str, str] | None = None

        if len(self._clients) == 1:
            region, client = self._clients[0]
            try:
                results.append((region, list(iter_log_events(client, from_ms, to_ms))))
            except ClientError as exc:
                error = (exc.response["Error"]["Code"], exc.response["Error"]["Message"])
        else:
            # Fetch all regions in parallel; a failure in one region becomes a
            # warning but doesn't drop the others.
            with ThreadPoolExecutor(max_workers=min(len(self._clients), 8)) as pool:
                futures = [pool.submit(fetch, rc) for rc in self._clients]
                for fut in futures:
                    try:
                        results.append(fut.result())
                    except ClientError as exc:
                        error = (exc.response["Error"]["Code"], exc.response["Error"]["Message"])

        # Dedup + fact-building stays single-threaded (shared seen_ids set).
        new_facts = []
        new_rows = []   # (key, fact) for persistence
        for client_region, records in results:
            for r in records:
                eid = r.get("_eventId", "")
                key = f"{client_region}:{eid}" if eid else ""
                if key and key in self._seen_ids:
                    continue
                if key:
                    self._seen_ids.add(key)
                raw_id = r.get("modelId", "unknown")
                rec_region = r.get("region") or client_region
                cost, inp, out, cw, cr, known, display = _record_cost_tokens(r, rec_region)
                ident_key, ident_label = _identity_key((r.get("identity") or {}).get("arn", ""))
                fact = {
                    "key":          key,
                    "t":            _record_ms(r),
                    "model":        normalize_model_id(raw_id),
                    "is_global":    raw_id.lower().startswith("global."),
                    "ident_key":    ident_key,
                    "ident_label":  ident_label,
                    "region":       rec_region,
                    "err":          r.get("errorCode") or "",
                    "inp": inp, "out": out, "cw": cw, "cr": cr, "cost": cost,
                    "known": known, "display": display,
                }
                new_facts.append(fact)
                if key:
                    new_rows.append((key, fact))

        floor = _now_ms() - self._memory_window_ms
        with self._lock:
            self._facts.extend(new_facts)
            # Trim memory to the window; older facts stay in SQLite (served via SQL).
            if self._facts and self._facts[0]["t"] < floor:
                self._facts = [f for f in self._facts if f["t"] >= floor]
                self._seen_ids = {f["key"] for f in self._facts if f.get("key")}
            self._memory_floor_ms = floor
            self._last_updated = datetime.now(timezone.utc)
            self._last_error = error

        # Persist outside the monitor lock (the store has its own lock).
        if self._store is not None and new_rows:
            self._store.add_many(new_rows)

    def _loop(self) -> None:
        while not self._stop.wait(REFRESH_SECONDS):
            self._ingest(max(self._start_ms, self._poll_from_ms))
            self._poll_from_ms = _now_ms() - _LIVE_OVERLAP_MS
            if self._poll_callback is not None:
                try:
                    self._poll_callback()
                except Exception:  # noqa: BLE001 — alert errors must not kill polling
                    pass

    # ── public API ───────────────────────────────────────────────────────────
    def prime(self) -> None:
        """Blocking initial load of the full tracked window before serving."""
        self._ingest(self._start_ms)
        self._poll_from_ms = _now_ms() - _LIVE_OVERLAP_MS

    def start(self) -> None:
        self._thread = threading.Thread(target=self._loop, name="usage-monitor", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=2)

    def snapshot(self, period: str | None = None, flt: dict | None = None,
                 since: str | None = None) -> dict:
        """Aggregate retained facts for the requested period/since and filter.

        Results are cached per (period, since, filter) and reused until the next
        poll adds events, so repeated identical requests avoid re-aggregating.
        """
        period, start_ms, end_ms, label, bucket_seconds = self._window_for(period, since)
        with self._lock:
            n = len(self._facts)
            view = self._facts[:n]  # cheap reference-copy slice
            err = self._last_error
            updated = self._last_updated

        cache_key = (period, since or "", _filter_key(flt))
        with self._cache_lock:
            if n != self._cache_version:        # new events → whole cache stale
                self._cache.clear()
                self._cache_version = n
            base = self._cache.get(cache_key)

        if base is None:
            if self._store is not None and start_ms < self._memory_floor_ms:
                # Window reaches older than memory holds → aggregate from SQLite.
                src, _ = self._store.load(start_ms)
            else:
                src = view
            base = _aggregate_facts(src, start_ms, end_ms, bucket_seconds, flt)
            base["window"] = {
                "label": label, "start_ms": start_ms, "end_ms": end_ms, "period": period,
            }
            with self._cache_lock:
                if n == self._cache_version:    # still fresh — store it
                    self._cache[cache_key] = base

        # Re-stamp volatile fields on a deep copy so the cached base stays
        # isolated — a consumer mutating the returned payload can't corrupt it.
        # The aggregated payload is small (bounded models/buckets/breakdowns),
        # so this copy is far cheaper than re-running O(events) aggregation.
        payload = copy.deepcopy(base)
        payload["generated_at"] = (updated or datetime.now(timezone.utc)).isoformat()
        payload["now_ms"] = _now_ms()
        if flt:
            payload["filter"] = flt
        if err is not None:
            payload["warning"] = {"code": err[0], "message": err[1]}
        return payload


def _token_matches(presented: str | None, expected: str | None) -> bool:
    """Constant-time token check. expected=None means auth is disabled (always ok)."""
    if expected is None:
        return True
    return presented is not None and hmac.compare_digest(presented, expected)


def run_web(
    clients,
    bedrock_client,
    host: str,
    port: int,
    token: str | None = None,
    persist: bool = True,
) -> None:
    """Start the dashboard HTTP server and block until interrupted.

    The initial view is today's usage with alerts disabled; the time window,
    threshold, webhook and refresh interval are all configured from the web UI.
    When `token` is set, every route requires it (cookie, Bearer header, or
    ?token= query) so the dashboard can be shared safely.
    When `persist` is True, per-event facts are stored in SQLite so history
    survives restarts and outlives CloudWatch log retention.
    """
    regions = [r for r, _ in clients]
    region_label = ", ".join(regions) or "unknown"
    period   = "today"   # initial window; changed at runtime from the dashboard
    threshold = None     # configured from the dashboard's Settings panel
    webhook   = None
    since     = None

    console.print("[dim]Loading pricing data…[/dim]")
    init_pricing_regions(regions, bedrock_client)

    store = None
    if persist:
        try:
            store = FactStore(_DB_PATH)
            store.prune(_now_ms() - _DB_RETENTION_DAYS * 86_400_000)
        except Exception as exc:  # noqa: BLE001 — fall back to in-memory on DB failure
            console.print(f"[yellow]Persistence disabled ({exc}); running in-memory only.[/yellow]")
            store = None

    _, _, label = _resolve_window(period, since)
    alerter = ThresholdAlerter(threshold, webhook, region=region_label, label=label, console=console)
    monitor = UsageMonitor(clients, period, since, store=store, memory_window_days=_MEMORY_WINDOW_DAYS)
    # Always wire the alert check; it's a no-op until a threshold is set (CLI or UI).
    monitor._poll_callback = lambda: alerter.check(monitor.snapshot()["totals"]["cost"])
    if store is not None:
        console.print(
            f"[dim]Persistence: {len(monitor._facts)} recent event(s) in memory, "
            f"{store.count()} total on disk at {_DB_PATH} "
            f"(memory window {_MEMORY_WINDOW_DAYS}d, kept {_DB_RETENTION_DAYS}d).[/dim]"
        )

    config = {
        "refresh_seconds": REFRESH_SECONDS,
        "region":          region_label,
        "regions":         regions,
        "threshold":       threshold,
        "periods":         monitor.periods(),
        "default_period":  monitor._default_period,
        "bind":            f"{host}:{port}",
        "poll_seconds":    REFRESH_SECONDS,
    }

    class Handler(BaseHTTPRequestHandler):
        # Silence the default stderr request logging; we print our own startup line.
        def log_message(self, *args) -> None:  # noqa: D401
            pass

        def _send_json(self, obj: dict, status: int = 200) -> None:
            body = json.dumps(obj).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)

        def _send_html(self, html: str, status: int = 200, cookie: str | None = None) -> None:
            body = html.encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            if cookie is not None:
                self.send_header(
                    "Set-Cookie",
                    f"bi_token={cookie}; HttpOnly; SameSite=Strict; Path=/",
                )
            self.end_headers()
            self.wfile.write(body)

        def _send_text(self, text: str, content_type: str) -> None:
            body = text.encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)

        def _send_attachment(self, body: bytes, content_type: str, filename: str) -> None:
            self.send_response(200)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Content-Disposition", f'attachment; filename="{filename}"')
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)

        # ── auth ────────────────────────────────────────────────────────────
        def _query_token(self):
            return parse_qs(urlparse(self.path).query).get("token", [None])[0]

        def _presented_token(self):
            auth = self.headers.get("Authorization", "")
            if auth.startswith("Bearer "):
                return auth[7:].strip()
            for part in self.headers.get("Cookie", "").split(";"):
                k, _, v = part.strip().partition("=")
                if k == "bi_token":
                    return v
            return self._query_token()

        def _authorized(self) -> bool:
            return _token_matches(self._presented_token(), token)

        def do_GET(self) -> None:  # noqa: N802
            parsed = urlparse(self.path)
            path = parsed.path

            if not self._authorized():
                if path == "/":
                    self._send_html(_AUTH_REQUIRED_HTML, status=401)
                else:
                    self._send_json({"error": "Unauthorized", "message": "Missing or invalid token"}, status=401)
                return

            if path == "/":
                # If the token arrived via ?token=, drop a cookie so later
                # requests (and reloads) authenticate without it in the URL.
                cookie = self._query_token() if token is not None else None
                self._send_html(DASHBOARD_HTML, cookie=cookie)
            elif path == "/api/config":
                self._send_json(config)
            elif path == "/api/settings":
                self._send_json(alerter.settings())
            elif path == "/api/usage":
                qs = parse_qs(parsed.query)
                period = qs.get("period", [None])[0]
                since  = qs.get("since", [None])[0]
                flt = {}
                for dim in ("model", "identity", "region"):
                    val = qs.get(dim, [None])[0]
                    if val:
                        flt[dim] = val
                self._send_json(monitor.snapshot(period, flt or None, since))
            elif path == "/api/export":
                qs = parse_qs(parsed.query)
                fmt = (qs.get("format", ["json"])[0]).lower()
                period = qs.get("period", [None])[0]
                since  = qs.get("since", [None])[0]
                flt = {}
                for dim in ("model", "identity", "region"):
                    val = qs.get(dim, [None])[0]
                    if val:
                        flt[dim] = val
                payload = monitor.snapshot(period, flt or None, since)
                stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
                if fmt == "csv":
                    self._send_attachment(
                        payload_to_csv(payload).encode("utf-8"),
                        "text/csv; charset=utf-8",
                        f"bedrock-usage-{stamp}.csv",
                    )
                else:
                    self._send_attachment(
                        json.dumps(payload, indent=2).encode("utf-8"),
                        "application/json; charset=utf-8",
                        f"bedrock-usage-{stamp}.json",
                    )
            elif path == "/metrics":
                self._send_text(
                    render_prometheus(monitor.snapshot()),
                    "text/plain; version=0.0.4; charset=utf-8",
                )
            else:
                self._send_json({"error": "NotFound", "message": path}, status=404)

        def _read_json(self) -> dict:
            try:
                length = int(self.headers.get("Content-Length", 0))
            except (TypeError, ValueError):
                length = 0
            raw = self.rfile.read(length) if length > 0 else b""
            try:
                return json.loads(raw or b"{}")
            except (json.JSONDecodeError, ValueError):
                return {}

        def do_POST(self) -> None:  # noqa: N802
            path = urlparse(self.path).path
            if not self._authorized():
                self._send_json({"error": "Unauthorized", "message": "Missing or invalid token"}, status=401)
                return
            body = self._read_json()

            if path == "/api/settings":
                raw_threshold = body.get("threshold")
                if raw_threshold in ("", None):
                    threshold = None
                else:
                    try:
                        threshold = float(raw_threshold)
                    except (TypeError, ValueError):
                        self._send_json({"error": "threshold must be a number"}, status=400)
                        return
                    if threshold < 0:
                        self._send_json({"error": "threshold must be ≥ 0"}, status=400)
                        return
                webhook_url = body.get("webhook_url") or None
                alerter.configure(threshold, webhook_url)
                config["threshold"] = threshold  # keep /api/config in sync
                self._send_json(alerter.settings())

            elif path == "/api/test-webhook":
                ok, info = alerter.send_test(body.get("webhook_url") or None)
                self._send_json({"ok": ok, "message": info})

            else:
                self._send_json({"error": "NotFound", "message": path}, status=404)

    try:
        httpd = ThreadingHTTPServer((host, port), Handler)
    except OSError as exc:
        if exc.errno == errno.EADDRINUSE:
            console.print(
                f"[red]Port {port} is already in use.[/red] "
                "Another bedrock-insights dashboard may still be running.\n"
                f"[dim]Try a different port with [bold]--port[/bold], "
                f"or find the process with [bold]lsof -i :{port}[/bold].[/dim]"
            )
        elif exc.errno in (errno.EACCES, errno.EPERM):
            console.print(
                f"[red]Permission denied binding to {host}:{port}.[/red] "
                "[dim]Ports below 1024 usually require elevated privileges — "
                "pick a higher port with [bold]--port[/bold].[/dim]"
            )
        else:
            console.print(f"[red]Could not start dashboard on {host}:{port}:[/red] {exc}")
        return

    # Prime the cache with the full window once, then let the background
    # poller keep it fresh with cheap incremental queries.
    console.print("[dim]Loading initial data from CloudWatch…[/dim]")
    monitor.prime()
    alerter.check(monitor.snapshot()["totals"]["cost"])  # initial load may already exceed
    monitor.start()

    url = f"http://{host}:{port}"
    dash_url = f"{url}/?token={token}" if token else url
    console.print(f"\n[bold green]Bedrock Insights dashboard[/bold green] → [bold]{dash_url}[/bold]")
    console.print(f"[dim]Metrics (Prometheus) → {url}/metrics[/dim]")
    console.print(f"[dim]Window: {label}  •  Region: {region_label}  •  Polling CloudWatch every {REFRESH_SECONDS}s[/dim]")
    if token:
        console.print(
            "[dim]Token auth is enabled — open the URL above (it includes the token). "
            "Scrapers can pass [bold]Authorization: Bearer <token>[/bold].[/dim]"
        )
        if host not in ("127.0.0.1", "localhost", "::1"):
            console.print(
                "[yellow]⚠  Exposed beyond localhost. Traffic is plain HTTP — put it behind a "
                "TLS-terminating reverse proxy for real remote use.[/yellow]"
            )
    elif host not in ("127.0.0.1", "localhost", "::1"):
        console.print(
            f"[yellow]⚠  Listening on {host} with no authentication — anyone who can reach this "
            "address can view your AWS usage/cost data. Pass [bold]--token[/bold] to require a token.[/yellow]"
        )
    console.print("[dim]Press Ctrl+C to stop.[/dim]\n")

    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        monitor.stop()
        if store is not None:
            store.close()
        httpd.shutdown()
        console.print("\n[dim]Dashboard stopped.[/dim]")


_AUTH_REQUIRED_HTML = """<!DOCTYPE html>
<html lang="en"><head><meta charset="utf-8"><title>Bedrock Insights — auth required</title>
<style>body{background:#0d1117;color:#e6edf3;font-family:-apple-system,Segoe UI,Roboto,sans-serif;
display:flex;align-items:center;justify-content:center;height:100vh;margin:0}
.box{max-width:420px;text-align:center;line-height:1.6}code{background:#161b22;border:1px solid #2a3340;
border-radius:6px;padding:2px 6px;color:#58a6ff}</style></head>
<body><div class="box"><h2>🔒 Access token required</h2>
<p>This dashboard is protected. Open it with your token appended to the URL:</p>
<p><code>?token=YOUR_TOKEN</code></p></div></body></html>"""


DASHBOARD_HTML = r"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8" />
<meta name="viewport" content="width=device-width, initial-scale=1" />
<title>Bedrock Insights</title>
<style>
  :root {
    --bg: #0d1117;
    --panel: #161b22;
    --panel-2: #1c2230;
    --border: #2a3340;
    --text: #e6edf3;
    --muted: #8b949e;
    --accent: #58a6ff;
    --green: #3fb950;
    --yellow: #d29922;
    --magenta: #bc8cff;
    --red: #f85149;
  }
  * { box-sizing: border-box; }
  body {
    margin: 0;
    background: var(--bg);
    color: var(--text);
    font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, Helvetica, Arial, sans-serif;
    font-size: 14px;
  }
  .wrap { max-width: 1100px; margin: 0 auto; padding: 28px 20px 60px; }
  header { display: flex; align-items: baseline; justify-content: space-between; flex-wrap: wrap; gap: 10px; }
  h1 { font-size: 20px; margin: 0; font-weight: 650; letter-spacing: .2px; }
  h1 .dot { color: var(--accent); }
  .meta { color: var(--muted); font-size: 12.5px; display: flex; gap: 16px; flex-wrap: wrap; }
  .meta b { color: var(--text); font-weight: 600; }
  .status { display: inline-flex; align-items: center; gap: 6px; }
  .pulse { width: 8px; height: 8px; border-radius: 50%; background: var(--green); box-shadow: 0 0 0 0 rgba(63,185,80,.6); animation: pulse 2s infinite; }
  .pulse.stale { background: var(--red); animation: none; }
  @keyframes pulse { 0% { box-shadow: 0 0 0 0 rgba(63,185,80,.5);} 70% { box-shadow: 0 0 0 6px rgba(63,185,80,0);} 100% { box-shadow: 0 0 0 0 rgba(63,185,80,0);} }

  .cards { display: grid; grid-template-columns: repeat(auto-fit, minmax(180px, 1fr)); gap: 14px; margin: 22px 0; }
  .card { background: var(--panel); border: 1px solid var(--border); border-radius: 12px; padding: 16px 18px; }
  .card .k { color: var(--muted); font-size: 12px; text-transform: uppercase; letter-spacing: .6px; }
  .card .v { font-size: 26px; font-weight: 680; margin-top: 6px; font-variant-numeric: tabular-nums; }
  .card.cost .v { color: var(--magenta); }
  .card.calls .v { color: var(--text); }
  .card.tokens .v { color: var(--green); }
  .card .v.small { font-size: 20px; }

  .panel { background: var(--panel); border: 1px solid var(--border); border-radius: 12px; padding: 14px 16px; margin-bottom: 22px; position: relative; }
  .panel-head { display: flex; justify-content: space-between; align-items: baseline; font-size: 12px; text-transform: uppercase; letter-spacing: .6px; color: var(--muted); margin-bottom: 8px; }
  .panel-head .sub { text-transform: none; letter-spacing: 0; }
  canvas { width: 100%; display: block; }
  .share { display: flex; align-items: center; justify-content: flex-end; gap: 8px; }
  .share .bar { height: 6px; border-radius: 3px; background: var(--magenta); opacity: .55; min-width: 0; }

  .card.errrate .v { color: var(--green); }
  .card.errrate.has-errors .v { color: var(--red); }

  .grid2 { display: grid; grid-template-columns: repeat(auto-fit, minmax(320px, 1fr)); gap: 14px; margin-top: 22px; }
  .mini h3 { margin: 0 0 10px; font-size: 12px; text-transform: uppercase; letter-spacing: .6px; color: var(--muted); font-weight: 600; }
  .mini table { border: none; border-radius: 0; }
  .mini thead th { padding: 6px 8px; }
  .mini tbody td { padding: 7px 8px; }
  .mini .empty { padding: 18px 0; }
  .err-code { color: var(--red); font-weight: 600; text-align: left; }
  .err-rate-line { color: var(--muted); font-size: 12.5px; margin-bottom: 10px; }
  .err-rate-line b { color: var(--red); }

  .banner { display: none; background: rgba(248,81,73,.12); border: 1px solid var(--red); color: #ffb4ad; border-radius: 10px; padding: 12px 16px; margin: 4px 0 18px; font-weight: 600; }
  .banner.show { display: block; }

  .warn { display: none; background: rgba(210,153,34,.12); border: 1px solid var(--yellow); color: #f0c674; border-radius: 10px; padding: 10px 14px; margin: 4px 0 16px; font-size: 13px; }
  .warn.show { display: block; }

  .toolbar { display: flex; align-items: center; gap: 12px; margin: 18px 0 4px; flex-wrap: wrap; }
  .periods { display: inline-flex; gap: 6px; }
  .periods button { background: var(--panel); color: var(--muted); border: 1px solid var(--border); border-radius: 8px; padding: 6px 12px; font-size: 13px; cursor: pointer; transition: all .12s; }
  .periods button:hover { color: var(--text); border-color: var(--accent); }
  .periods button.active { background: var(--accent); color: #06223f; border-color: var(--accent); font-weight: 650; }
  .filter-chip { background: rgba(88,166,255,.12); border: 1px solid var(--accent); color: var(--accent); border-radius: 8px; padding: 5px 10px; font-size: 12.5px; display: none; align-items: center; gap: 8px; }
  .filter-chip.show { display: inline-flex; }
  .filter-chip button { background: none; border: none; color: var(--accent); cursor: pointer; font-size: 15px; padding: 0; line-height: 1; }
  tr.clickable { cursor: pointer; }
  .gear { margin-left: auto; background: var(--panel); color: var(--muted); border: 1px solid var(--border); border-radius: 8px; padding: 6px 12px; font-size: 13px; cursor: pointer; }
  .gear:hover { color: var(--text); border-color: var(--accent); }
  .settings h3 { margin: 0 0 14px; font-size: 13px; text-transform: uppercase; letter-spacing: .6px; color: var(--muted); }
  .setrow { display: flex; flex-direction: column; gap: 5px; margin-bottom: 14px; max-width: 520px; }
  .setrow label { font-size: 12.5px; color: var(--text); }
  .setrow label .muted { color: var(--muted); }
  .setrow input { background: var(--bg); border: 1px solid var(--border); border-radius: 7px; padding: 8px 10px; color: var(--text); font-size: 13px; font-family: inherit; }
  .setrow input:focus { outline: none; border-color: var(--accent); }
  .setactions { display: flex; align-items: center; gap: 10px; }
  .setactions button { background: var(--accent); color: #06223f; border: none; border-radius: 7px; padding: 7px 16px; font-size: 13px; font-weight: 600; cursor: pointer; }
  .setactions button.secondary { background: var(--panel); color: var(--text); border: 1px solid var(--border); }
  .setactions button:hover { filter: brightness(1.08); }
  .setstatus { font-size: 12.5px; color: var(--muted); }
  .setstatus.ok { color: var(--green); }
  .setstatus.bad { color: var(--red); }
  .setnote { font-size: 11.5px; color: var(--muted); margin-top: 12px; max-width: 520px; }
  .tip { position: absolute; pointer-events: none; background: #0a0d12; border: 1px solid var(--border); border-radius: 6px; padding: 6px 9px; font-size: 11.5px; color: var(--text); white-space: nowrap; display: none; z-index: 10; box-shadow: 0 4px 14px rgba(0,0,0,.5); }
  .tip .tcost { color: var(--magenta); font-weight: 600; }

  table { width: 100%; border-collapse: collapse; background: var(--panel); border: 1px solid var(--border); border-radius: 12px; overflow: hidden; }
  thead th { text-align: right; font-size: 11.5px; text-transform: uppercase; letter-spacing: .5px; color: var(--muted); padding: 12px 14px; border-bottom: 1px solid var(--border); font-weight: 600; }
  thead th:first-child { text-align: left; }
  tbody td { padding: 11px 14px; text-align: right; font-variant-numeric: tabular-nums; border-bottom: 1px solid rgba(42,51,64,.5); }
  tbody td:first-child { text-align: left; color: var(--accent); font-weight: 600; }
  tbody tr:last-child td { border-bottom: none; }
  tbody tr:hover { background: var(--panel-2); }
  .col-in { color: var(--green); }
  .col-out { color: var(--yellow); }
  .col-cache { color: var(--accent); }
  .col-cost { color: var(--magenta); font-weight: 600; }
  .na { color: var(--muted); font-style: italic; }
  .badge { font-size: 10px; color: var(--muted); border: 1px solid var(--border); border-radius: 5px; padding: 1px 5px; margin-left: 7px; vertical-align: middle; }
  tfoot td { padding: 13px 14px; text-align: right; font-weight: 700; font-variant-numeric: tabular-nums; border-top: 2px solid var(--border); background: var(--panel-2); }
  tfoot td:first-child { text-align: left; }

  .empty { text-align: center; color: var(--muted); padding: 50px 0; font-style: italic; }
  .err { background: rgba(248,81,73,.1); border: 1px solid var(--red); color: #ffb4ad; border-radius: 10px; padding: 14px 16px; }
  footer { color: var(--muted); font-size: 12px; margin-top: 18px; text-align: right; }
</style>
</head>
<body>
<div class="wrap">
  <header>
    <h1>Bedrock<span class="dot">.</span>Insights</h1>
    <div class="meta">
      <span>Region <b id="region">—</b></span>
      <span>Window <b id="label">—</b></span>
      <span class="status"><span class="pulse" id="pulse"></span><b id="updated">connecting…</b></span>
    </div>
  </header>

  <div class="banner" id="banner"></div>
  <div class="warn" id="warn"></div>

  <div class="toolbar">
    <div class="periods" id="periods"></div>
    <div class="periods" id="region-sel"></div>
    <div class="filter-chip" id="filter-chip"></div>
    <button class="gear" id="export-json" title="Download current view as JSON">↓ JSON</button>
    <button class="gear" id="export-csv" title="Download current view as CSV">↓ CSV</button>
    <button class="gear" id="gear" title="Settings">⚙ Settings</button>
  </div>

  <div class="panel settings" id="settings" style="display:none">
    <h3>Time window</h3>
    <div class="setrow">
      <label>Custom rolling window <span class="muted">— e.g. 2h, 30m, 1d (overrides the period buttons)</span></label>
      <div class="setactions">
        <input id="set-since" type="text" placeholder="leave blank to use the period buttons" style="max-width:240px">
        <button id="set-window-apply">Apply</button>
        <button id="set-window-clear" class="secondary">Clear</button>
      </div>
    </div>

    <h3 style="margin-top:18px">Alerts</h3>
    <div class="setrow">
      <label>Threshold ($) <span class="muted">— leave blank to disable</span></label>
      <input id="set-threshold" type="number" step="0.01" min="0" placeholder="disabled">
    </div>
    <div class="setrow">
      <label>Webhook URL <span class="muted">(Slack incoming webhook or any JSON endpoint)</span></label>
      <input id="set-webhook" type="text" placeholder="https://hooks.slack.com/services/...">
    </div>
    <div class="setactions">
      <button id="set-save">Save</button>
      <button id="set-test" class="secondary">Send test</button>
      <span id="set-status" class="setstatus"></span>
    </div>
    <div class="setnote">Alerts fire once when spend crosses the threshold (re-armed when you change settings). The server POSTs to this URL, so only use a webhook you trust — and don't expose the dashboard publicly.</div>

    <h3 style="margin-top:18px">Display</h3>
    <div class="setrow">
      <label>Dashboard refresh interval (seconds)</label>
      <div class="setactions">
        <input id="set-refresh" type="number" min="2" step="1" style="max-width:120px">
        <button id="set-refresh-apply">Apply</button>
      </div>
    </div>

    <h3 style="margin-top:18px">Runtime <span class="muted">(set at launch — restart with CLI flags to change)</span></h3>
    <div class="setnote">
      Regions monitored: <b id="info-regions">—</b><br>
      Server bind address (<code>--host</code>/<code>--port</code>): <b id="info-bind">—</b><br>
      CloudWatch poll interval: <b id="info-poll">—</b>s
    </div>
  </div>

  <div class="cards">
    <div class="card cost"><div class="k">Estimated Cost</div><div class="v" id="c-cost">—</div></div>
    <div class="card calls"><div class="k">Total Calls</div><div class="v" id="c-calls">—</div></div>
    <div class="card tokens"><div class="k">Total Tokens</div><div class="v" id="c-tokens">—</div></div>
    <div class="card"><div class="k">Avg $ / Call</div><div class="v small" id="c-avgcost">—</div></div>
    <div class="card"><div class="k">Burn Rate</div><div class="v small" id="c-burn">—</div></div>
    <div class="card"><div class="k">Proj. $ / Day</div><div class="v small" id="c-proj" title="Run-rate estimate">—</div></div>
    <div class="card errrate" id="card-err"><div class="k">Error Rate</div><div class="v small" id="c-err">—</div></div>
    <div class="card" id="card-cache" style="display:none"><div class="k">Cache Hit Rate</div><div class="v small" id="c-cache">—</div></div>
  </div>

  <div class="panel" id="chart-panel">
    <div class="panel-head"><span>Cost over time</span><span class="sub" id="chart-meta"></span></div>
    <canvas id="chart" height="140"></canvas>
    <div class="tip" id="chart-tip"></div>
  </div>

  <div id="content"></div>

  <div class="grid2">
    <div class="panel mini"><h3>By IAM Identity</h3><div id="bd-identity"></div></div>
    <div class="panel mini"><h3>By Region</h3><div id="bd-region"></div></div>
    <div class="panel mini" id="panel-errors" style="display:none"><h3>Errors</h3><div id="bd-errors"></div></div>
  </div>

  <footer id="foot"></footer>
</div>

<script>
let CONFIG = { refresh_seconds: 5, threshold: null, periods: [], regions: [] };
let STATE = { period: null, region: null, since: null, filter: null };  // filter = {dim, value, label}
let REFRESH_TIMER = null;

function escAttr(s) {
  return String(s).replace(/&/g, "&amp;").replace(/"/g, "&quot;").replace(/</g, "&lt;");
}

function _windowParams(p) {
  // A custom `since` overrides the named period.
  if (STATE.since) p.set("since", STATE.since);
  else if (STATE.period) p.set("period", STATE.period);
  if (STATE.region) p.set("region", STATE.region);
  if (STATE.filter) p.set(STATE.filter.dim, STATE.filter.value);
}

function buildUsageUrl() {
  const p = new URLSearchParams();
  _windowParams(p);
  const q = p.toString();
  return "/api/usage" + (q ? "?" + q : "");
}

function downloadExport(fmt) {
  const p = new URLSearchParams();
  p.set("format", fmt);
  _windowParams(p);
  const a = document.createElement("a");
  a.href = "/api/export?" + p.toString();
  document.body.appendChild(a);
  a.click();
  a.remove();
}

function applyRefreshInterval(seconds) {
  const s = Math.max(2, Number(seconds) || CONFIG.refresh_seconds || 5);
  if (REFRESH_TIMER) clearInterval(REFRESH_TIMER);
  REFRESH_TIMER = setInterval(refresh, s * 1000);
  document.getElementById("foot").textContent =
    "Refreshing every " + s + "s · CloudWatch polled every " + (CONFIG.poll_seconds || 5) + "s";
}

function setCustomWindow(since) {
  STATE.since = since || null;
  if (STATE.since) {
    // Custom window overrides the period buttons — clear their active state.
    for (const b of document.querySelectorAll("#periods button")) b.classList.remove("active");
  }
  refresh();
}

function setRegion(region) {
  STATE.region = region || null;
  for (const b of document.querySelectorAll("#region-sel button"))
    b.classList.toggle("active", (b.dataset.region || null) === STATE.region);
  refresh();
}

function setFilter(dim, value, label) {
  STATE.filter = { dim, value, label };
  renderFilterChip();
  refresh();
}

function clearFilter() {
  STATE.filter = null;
  renderFilterChip();
  refresh();
}

function renderFilterChip() {
  const chip = document.getElementById("filter-chip");
  if (!STATE.filter) { chip.classList.remove("show"); chip.innerHTML = ""; return; }
  chip.classList.add("show");
  chip.innerHTML = "Filter: <b>" + STATE.filter.dim + "</b> = " +
    escAttr(STATE.filter.label) + ' <button title="Clear filter" onclick="clearFilter()">✕</button>';
}

function setPeriod(id) {
  STATE.period = id;
  STATE.since = null;                       // a named period overrides a custom window
  document.getElementById("set-since").value = "";
  for (const b of document.querySelectorAll("#periods button"))
    b.classList.toggle("active", b.dataset.id === id);
  refresh();
}

let SETTINGS = { threshold: null, webhook_url: null };

async function loadSettings() {
  try {
    const r = await fetch("/api/settings");
    SETTINGS = await r.json();
    document.getElementById("set-threshold").value =
      (SETTINGS.threshold === null || SETTINGS.threshold === undefined) ? "" : SETTINGS.threshold;
    document.getElementById("set-webhook").value = SETTINGS.webhook_url || "";
  } catch (e) { /* ignore */ }
}

function setStatus(msg, kind) {
  const el = document.getElementById("set-status");
  el.textContent = msg;
  el.className = "setstatus" + (kind ? " " + kind : "");
}

async function saveSettings() {
  const tv = document.getElementById("set-threshold").value.trim();
  const wv = document.getElementById("set-webhook").value.trim();
  setStatus("Saving…");
  try {
    const r = await fetch("/api/settings", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ threshold: tv === "" ? null : tv, webhook_url: wv || null }),
    });
    const data = await r.json();
    if (data.error) { setStatus(data.error, "bad"); return; }
    SETTINGS = data;
    setStatus("Saved.", "ok");
  } catch (e) {
    setStatus("Save failed.", "bad");
  }
}

async function testWebhook() {
  const wv = document.getElementById("set-webhook").value.trim();
  if (!wv) { setStatus("Enter a webhook URL first.", "bad"); return; }
  setStatus("Sending test…");
  try {
    const r = await fetch("/api/test-webhook", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ webhook_url: wv }),
    });
    const data = await r.json();
    setStatus(data.ok ? "Test sent (HTTP " + data.message + ")." : "Failed: " + data.message,
              data.ok ? "ok" : "bad");
  } catch (e) {
    setStatus("Test failed.", "bad");
  }
}

function fmtTokens(n) {
  if (n >= 1e6) return (n / 1e6).toFixed(2) + "M";
  if (n >= 1e4) return (n / 1e3).toFixed(1) + "K";
  return n.toLocaleString();
}
function fmtCost(c) { return "$" + c.toFixed(4); }
function fmtPct(x) { return (x * 100).toFixed(1) + "%"; }

let CHART = null;  // last-drawn geometry, for hover tooltips

function renderChart(trend) {
  const canvas = document.getElementById("chart");
  const pts = (trend && trend.points) || [];
  const bs = trend ? trend.bucket_seconds : 60;
  const bucketLabel = bs >= 3600 ? (bs / 3600) + "h" : (bs / 60) + "m";
  document.getElementById("chart-meta").textContent =
    pts.length ? (pts.length + " × " + bucketLabel + " buckets") : "";

  const dpr = window.devicePixelRatio || 1;
  const cssW = canvas.clientWidth || (canvas.parentElement.clientWidth - 32);
  const cssH = 140;
  canvas.width = cssW * dpr;
  canvas.height = cssH * dpr;
  const ctx = canvas.getContext("2d");
  ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
  ctx.clearRect(0, 0, cssW, cssH);
  CHART = null;

  if (!pts.length) {
    ctx.fillStyle = "#8b949e"; ctx.font = "12px sans-serif";
    ctx.fillText("No data in this window yet…", 8, 22);
    return;
  }

  const pad = { l: 8, r: 8, t: 14, b: 18 };
  const w = cssW - pad.l - pad.r, h = cssH - pad.t - pad.b;
  const maxCost = Math.max.apply(null, pts.map(p => p.cost).concat(1e-9));
  const n = pts.length;
  const slot = w / n;
  const bw = Math.max(1, slot - 2);

  ctx.strokeStyle = "#2a3340";
  ctx.beginPath(); ctx.moveTo(pad.l, pad.t + h); ctx.lineTo(pad.l + w, pad.t + h); ctx.stroke();

  for (let i = 0; i < n; i++) {
    const bh = (pts[i].cost / maxCost) * h;
    ctx.fillStyle = "#bc8cff";
    ctx.fillRect(pad.l + i * slot + 1, pad.t + h - bh, bw, bh);
  }

  const fmtT = ms => new Date(ms).toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" });
  ctx.fillStyle = "#8b949e"; ctx.font = "10px sans-serif";
  ctx.textAlign = "left";
  ctx.fillText("$" + maxCost.toFixed(maxCost < 0.01 ? 5 : 4), pad.l, 10);
  ctx.fillText(fmtT(pts[0].t), pad.l, cssH - 4);
  ctx.textAlign = "right";
  ctx.fillText(fmtT(pts[n - 1].t), pad.l + w, cssH - 4);

  // Stash geometry so the hover handler can map cursor x → bucket.
  CHART = { pad, slot, w, n, pts, bucketSeconds: bs };
}

function _chartHover(e) {
  const tip = document.getElementById("chart-tip");
  if (!CHART) { tip.style.display = "none"; return; }
  const canvas = document.getElementById("chart");
  const rect = canvas.getBoundingClientRect();
  const x = e.clientX - rect.left;
  const i = Math.floor((x - CHART.pad.l) / CHART.slot);
  if (i < 0 || i >= CHART.n) { tip.style.display = "none"; return; }

  const p = CHART.pts[i];
  const fmtT = ms => new Date(ms).toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" });
  const end = p.t + CHART.bucketSeconds * 1000;
  tip.innerHTML =
    "<div>" + fmtT(p.t) + "–" + fmtT(end) + "</div>" +
    '<div class="tcost">' + fmtCost(p.cost) + "</div>" +
    "<div>" + fmtTokens(p.total_tokens) + " tokens · " + p.calls + " calls</div>";

  // Position above the hovered bar, clamped inside the panel.
  const panel = document.getElementById("chart-panel");
  const prect = panel.getBoundingClientRect();
  tip.style.display = "block";
  const tw = tip.offsetWidth;
  let left = (rect.left - prect.left) + CHART.pad.l + i * CHART.slot + CHART.slot / 2 - tw / 2;
  left = Math.max(4, Math.min(left, panel.clientWidth - tw - 4));
  tip.style.left = left + "px";
  tip.style.top = ((rect.top - prect.top) + 6) + "px";
}

(function bindChartHover() {
  const canvas = document.getElementById("chart");
  canvas.addEventListener("mousemove", _chartHover);
  canvas.addEventListener("mouseleave", () => {
    document.getElementById("chart-tip").style.display = "none";
  });
})();

function setStale(stale) {
  document.getElementById("pulse").classList.toggle("stale", stale);
}

async function loadConfig() {
  try {
    const r = await fetch("/api/config");
    CONFIG = await r.json();
    document.getElementById("region").textContent = CONFIG.region || "—";
    STATE.period = CONFIG.default_period;
    const box = document.getElementById("periods");
    box.innerHTML = "";
    for (const p of (CONFIG.periods || [])) {
      const btn = document.createElement("button");
      btn.textContent = p.label;
      btn.dataset.id = p.id;
      btn.classList.toggle("active", p.id === STATE.period);
      btn.addEventListener("click", () => setPeriod(p.id));
      box.appendChild(btn);
    }

    // Region selector — only when monitoring more than one region.
    const rsel = document.getElementById("region-sel");
    rsel.innerHTML = "";
    const regions = CONFIG.regions || [];
    if (regions.length > 1) {
      const mk = (label, val) => {
        const b = document.createElement("button");
        b.textContent = label;
        b.dataset.region = val;
        b.classList.toggle("active", (val || null) === STATE.region);
        b.addEventListener("click", () => setRegion(val));
        return b;
      };
      rsel.appendChild(mk("All regions", ""));
      for (const r of regions) rsel.appendChild(mk(r, r));
    }

    // Read-only runtime info + refresh default
    document.getElementById("info-regions").textContent = (regions.length ? regions.join(", ") : CONFIG.region) || "—";
    document.getElementById("info-bind").textContent = CONFIG.bind || "—";
    document.getElementById("info-poll").textContent = CONFIG.poll_seconds || "—";
    document.getElementById("set-refresh").value = CONFIG.refresh_seconds || 5;
  } catch (e) { /* keep defaults */ }
}

// Click a model / identity row to drill in; click a region row to switch region.
document.addEventListener("click", (e) => {
  const tr = e.target.closest("tr.clickable");
  if (!tr || !tr.dataset.dim) return;
  if (tr.dataset.dim === "region") setRegion(tr.dataset.val);
  else setFilter(tr.dataset.dim, tr.dataset.val, tr.dataset.label);
});

function renderError(msg) {
  document.getElementById("content").innerHTML =
    '<div class="err"><b>Could not load usage.</b><br>' + msg + "</div>";
}

function renderBanner(totals) {
  const banner = document.getElementById("banner");
  const threshold = (SETTINGS.threshold != null) ? SETTINGS.threshold : CONFIG.threshold;
  if (threshold != null && totals.cost_known && totals.cost >= threshold) {
    banner.textContent = "⚠  Threshold exceeded — " + fmtCost(totals.cost) +
      " ≥ $" + Number(threshold).toFixed(2);
    banner.classList.add("show");
  } else {
    banner.classList.remove("show");
  }
}

function renderTable(data) {
  const hasCache = data.has_cache;
  const t = data.totals;

  if (!data.models.length) {
    document.getElementById("content").innerHTML =
      '<div class="empty">Waiting for Bedrock invocations in this window…</div>';
    return;
  }

  let head = "<tr><th>Model</th><th>Calls</th><th>Input</th>";
  if (hasCache) head += "<th>Cache Write</th><th>Cache Read</th>";
  head += "<th>Output</th>";
  if (!hasCache) head += "<th>Total</th>";
  head += "<th>Est. Cost</th><th>Share</th></tr>";

  let rows = "";
  for (const m of data.models) {
    const cost = m.price_known ? '<td class="col-cost">' + fmtCost(m.cost) + "</td>"
                               : '<td class="na">N/A</td>';
    const share = m.cost_share || 0;
    const shareCell = '<td><div class="share"><span>' + fmtPct(share) +
                      '</span><span class="bar" style="width:' + Math.round(share * 60) + 'px"></span></div></td>';
    const badge = m.is_global ? '<span class="badge">global</span>' : "";
    rows += '<tr class="clickable" data-dim="model" data-val="' + escAttr(m.model_id) +
            '" data-label="' + escAttr(m.display_name) + '"><td>' + m.display_name + badge + "</td>" +
            "<td>" + m.calls.toLocaleString() + "</td>" +
            '<td class="col-in">' + fmtTokens(m.input_tokens) + "</td>";
    if (hasCache) rows += '<td class="col-cache">' + fmtTokens(m.cache_write_tokens) + "</td>" +
                          '<td class="col-cache">' + fmtTokens(m.cache_read_tokens) + "</td>";
    rows += '<td class="col-out">' + fmtTokens(m.output_tokens) + "</td>";
    if (!hasCache) rows += "<td>" + fmtTokens(m.total_tokens) + "</td>";
    rows += cost + shareCell + "</tr>";
  }

  const totalCost = t.cost_known ? '<td class="col-cost">' + fmtCost(t.cost) + "</td>"
                                 : '<td class="na">N/A</td>';
  let foot = "<tr><td>TOTAL</td><td>" + t.calls.toLocaleString() + "</td>" +
             "<td>" + fmtTokens(t.input_tokens) + "</td>";
  if (hasCache) foot += "<td>" + fmtTokens(t.cache_write_tokens) + "</td>" +
                        "<td>" + fmtTokens(t.cache_read_tokens) + "</td>";
  foot += "<td>" + fmtTokens(t.output_tokens) + "</td>";
  if (!hasCache) foot += "<td>" + fmtTokens(t.total_tokens) + "</td>";
  foot += totalCost + "<td></td></tr>";

  document.getElementById("content").innerHTML =
    "<table><thead>" + head + "</thead><tbody>" + rows +
    "</tbody><tfoot>" + foot + "</tfoot></table>";
}

function renderBreakdownTable(rows, cols, rowMeta) {
  if (!rows.length) return '<div class="empty">No data yet…</div>';
  let head = "<tr>";
  for (const c of cols) head += "<th" + (c.left ? ' style="text-align:left"' : "") + ">" + c.label + "</th>";
  head += "</tr>";
  let body = "";
  for (const r of rows) {
    let attrs = "";
    if (rowMeta) {
      const meta = rowMeta(r);
      attrs = ' class="clickable" data-dim="' + meta.dim + '" data-val="' +
              escAttr(meta.val) + '" data-label="' + escAttr(meta.label) + '"';
    }
    body += "<tr" + attrs + ">";
    for (const c of cols) body += "<td>" + c.render(r) + "</td>";
    body += "</tr>";
  }
  return "<table><thead>" + head + "</thead><tbody>" + body + "</tbody></table>";
}

function renderIdentities(list) {
  const cols = [
    { label: "Identity", left: true, render: r => r.errors > 0
        ? r.label + ' <span class="badge" style="border-color:var(--red);color:var(--red)">' + r.errors + ' err</span>'
        : r.label },
    { label: "Calls", render: r => r.calls.toLocaleString() },
    { label: "Cost",  render: r => '<span class="col-cost">' + fmtCost(r.cost) + "</span>" },
    { label: "Share", render: r => fmtPct(r.cost_share || 0) },
  ];
  document.getElementById("bd-identity").innerHTML = renderBreakdownTable(
    list || [], cols, r => ({ dim: "identity", val: r.key, label: r.label }));
}

function renderRegions(list) {
  const cols = [
    { label: "Region", left: true, render: r => r.region },
    { label: "Calls",  render: r => r.calls.toLocaleString() },
    { label: "Cost",   render: r => '<span class="col-cost">' + fmtCost(r.cost) + "</span>" },
    { label: "Share",  render: r => fmtPct(r.cost_share || 0) },
  ];
  // Rows are clickable (→ switch region) only when there's a selector to switch with.
  const meta = (CONFIG.regions || []).length > 1
    ? r => ({ dim: "region", val: r.region, label: r.region })
    : null;
  document.getElementById("bd-region").innerHTML = renderBreakdownTable(list || [], cols, meta);
}

function renderErrors(errors) {
  const panel = document.getElementById("panel-errors");
  if (!errors || !errors.total) { panel.style.display = "none"; return; }
  panel.style.display = "";
  let html = '<div class="err-rate-line"><b>' + errors.total.toLocaleString() +
             "</b> failed call(s) · " + fmtPct(errors.rate) + " error rate</div>";
  const cols = [
    { label: "Error Code", left: true, render: r => '<span class="err-code">' + r.code + "</span>" },
    { label: "Count", render: r => r.count.toLocaleString() },
  ];
  html += renderBreakdownTable(errors.by_code || [], cols);
  document.getElementById("bd-errors").innerHTML = html;
}

async function refresh() {
  try {
    const r = await fetch(buildUsageUrl());
    const data = await r.json();
    if (data.error) { renderError(data.message || data.error); setStale(true); return; }

    // A warning means the background poller hit an error, but we still have
    // the last good cached data — show the notice and keep rendering it.
    const warn = document.getElementById("warn");
    if (data.warning) {
      warn.textContent = "⚠ " + data.warning.code + ": " + data.warning.message +
        " — showing last known data.";
      warn.classList.add("show");
    } else {
      warn.classList.remove("show");
    }

    document.getElementById("c-cost").textContent =
      data.totals.cost_known ? fmtCost(data.totals.cost) : "N/A";
    document.getElementById("c-calls").textContent = data.totals.calls.toLocaleString();
    document.getElementById("c-tokens").textContent = fmtTokens(data.totals.total_tokens);

    // Avg cost per call
    document.getElementById("c-avgcost").textContent =
      data.totals.cost_known && data.totals.calls ? "$" + data.totals.avg_cost_per_call.toFixed(5) : "—";

    // Burn rate = cost so far / hours elapsed since the window start.
    const nowMs = data.now_ms || Date.now();
    const startMs = (data.window && data.window.start_ms) || nowMs;
    const hours = Math.max((nowMs - startMs) / 3.6e6, 1 / 60);
    document.getElementById("c-burn").textContent =
      data.totals.cost_known && data.totals.cost > 0 ? "$" + (data.totals.cost / hours).toFixed(4) + "/hr" : "—";

    // Run-rate projection (server-computed).
    const proj = data.projection;
    if (proj && data.totals.cost_known && data.totals.cost > 0) {
      document.getElementById("c-proj").textContent = "$" + proj.projected_daily.toFixed(2);
      document.getElementById("c-proj").title =
        "Run-rate estimate · ~$" + proj.projected_monthly.toFixed(0) + "/month";
    } else {
      document.getElementById("c-proj").textContent = "—";
    }

    // Cache hit rate card (only when caching is in use).
    const cacheCard = document.getElementById("card-cache");
    if (data.has_cache) {
      cacheCard.style.display = "";
      document.getElementById("c-cache").textContent = fmtPct(data.totals.cache_hit_rate);
    } else {
      cacheCard.style.display = "none";
    }

    // Error rate card — turns red when any call has failed.
    const errors = data.errors || { total: 0, rate: 0, by_code: [] };
    document.getElementById("c-err").textContent = fmtPct(errors.rate);
    document.getElementById("card-err").classList.toggle("has-errors", errors.total > 0);

    renderBanner(data.totals);
    renderChart(data.trend);
    renderTable(data);
    renderIdentities(data.identities);
    renderRegions(data.regions);
    renderErrors(errors);

    const now = new Date();
    document.getElementById("updated").textContent = "updated " + now.toLocaleTimeString();
    if (data.window && data.window.label) document.getElementById("label").textContent = data.window.label;
    setStale(!!data.warning);
  } catch (e) {
    document.getElementById("updated").textContent = "connection lost — retrying";
    setStale(true);
  }
}

(async function () {
  // The cookie is already set by the page response; drop the token from the
  // address bar so it doesn't linger in history or get shared accidentally.
  if (location.search.includes("token=")) {
    history.replaceState({}, "", location.pathname);
  }
  await loadConfig();
  await loadSettings();

  document.getElementById("gear").addEventListener("click", () => {
    const el = document.getElementById("settings");
    const showing = el.style.display !== "none";
    el.style.display = showing ? "none" : "block";
    if (!showing) loadSettings();
  });
  document.getElementById("set-save").addEventListener("click", saveSettings);
  document.getElementById("set-test").addEventListener("click", testWebhook);
  document.getElementById("export-json").addEventListener("click", () => downloadExport("json"));
  document.getElementById("export-csv").addEventListener("click", () => downloadExport("csv"));
  document.getElementById("set-window-apply").addEventListener("click",
    () => setCustomWindow(document.getElementById("set-since").value.trim()));
  document.getElementById("set-window-clear").addEventListener("click", () => {
    document.getElementById("set-since").value = "";
    setPeriod(STATE.period || CONFIG.default_period);
  });
  document.getElementById("set-refresh-apply").addEventListener("click",
    () => applyRefreshInterval(document.getElementById("set-refresh").value));

  await refresh();
  applyRefreshInterval(CONFIG.refresh_seconds);
})();
</script>
</body>
</html>
"""
