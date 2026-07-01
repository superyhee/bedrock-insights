"""
Outbound threshold alerts.

Dependency-free (stdlib urllib) POST of a JSON payload to a webhook URL when
spend crosses a threshold. The payload includes a Slack-compatible ``text``
field plus structured fields any generic webhook consumer can use.

Only aggregate spend figures are sent — never prompt/response content.
"""

from __future__ import annotations

import json
import threading
import urllib.request

from rich.console import Console

_console = Console()


def send_webhook(url: str, payload: dict, timeout: int = 10) -> tuple[bool, str]:
    """POST ``payload`` as JSON to ``url``. Returns (ok, info_or_error)."""
    data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(
        url, data=data,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            code = getattr(resp, "status", None) or resp.getcode()
            return (200 <= int(code) < 300), str(code)
    except Exception as exc:  # noqa: BLE001 — network errors must never crash the caller
        return False, str(exc)


class ThresholdAlerter:
    """Fires a one-shot alert (terminal + optional webhook) when cost ≥ threshold.

    De-duplicates: once fired it stays quiet for the rest of the run, so a live
    or web monitor that keeps polling won't spam the channel.
    """

    #: fraction of a budget at which a "warning" (vs "critical") alert fires.
    WARN_FRAC = 0.8

    def __init__(
        self,
        threshold: float | None,
        webhook_url: str | None = None,
        *,
        daily_budget: float | None = None,
        monthly_budget: float | None = None,
        region: str = "",
        label: str = "",
        console: Console | None = None,
    ) -> None:
        self.threshold = threshold
        self.webhook_url = webhook_url
        self.daily_budget = daily_budget
        self.monthly_budget = monthly_budget
        self.region = region
        self.label = label
        self._console = console or _console
        self._fired = False               # window-threshold one-shot
        self._budget_fired: dict[str, str] = {}  # "scope:period" -> "warning"|"critical"
        self._anomaly_key: str | None = None     # dedup key of the last anomaly alerted
        self._lock = threading.Lock()

    @property
    def fired(self) -> bool:
        return self._fired

    def configure(
        self,
        threshold: float | None,
        webhook_url: str | None,
        daily_budget: float | None = None,
        monthly_budget: float | None = None,
    ) -> None:
        """Update thresholds/budgets/webhook at runtime (from the dashboard). Re-arms on change."""
        with self._lock:
            if threshold != self.threshold or webhook_url != self.webhook_url:
                self._fired = False  # re-arm so the new threshold can fire again
            if (daily_budget != self.daily_budget or monthly_budget != self.monthly_budget
                    or webhook_url != self.webhook_url):
                self._budget_fired.clear()  # re-arm budget levels
                self._anomaly_key = None
            self.threshold = threshold
            self.webhook_url = webhook_url
            self.daily_budget = daily_budget
            self.monthly_budget = monthly_budget

    def settings(self) -> dict:
        with self._lock:
            return {
                "threshold": self.threshold,
                "webhook_url": self.webhook_url,
                "daily_budget": self.daily_budget,
                "monthly_budget": self.monthly_budget,
            }

    def _build_payload(self, cost: float, threshold: float) -> dict:
        text = (
            f":rotating_light: *Bedrock Insights alert* — spend "
            f"${cost:.4f} crossed threshold ${threshold:.2f}\n"
            f"Window: {self.label or 'n/a'} · Region: {self.region or 'n/a'}"
        )
        return {
            "text":      text,
            "event":     "threshold_exceeded",
            "source":    "bedrock-insights",
            "cost":      round(cost, 6),
            "threshold": threshold,
            "region":    self.region,
            "window":    self.label,
        }

    def send_test(self, url: str | None = None) -> tuple[bool, str]:
        """Send a one-off test message to verify a webhook (Slack or generic)."""
        target = url or self.webhook_url
        if not target:
            return False, "no webhook URL configured"
        payload = {
            "text":   ":white_check_mark: *Bedrock Insights* test alert — your webhook is configured correctly.",
            "event":  "test",
            "source": "bedrock-insights",
            "region": self.region,
            "window": self.label,
        }
        return send_webhook(target, payload)

    def check_budgets(self, daily_cost: float, monthly_cost: float,
                      day_key: str, month_key: str) -> list[tuple[str, str]]:
        """Fire daily/monthly budget alerts at warning (≥80%) and critical (≥100%).

        De-duplicates per (scope, period): warning fires at most once per period,
        critical fires at most once per period (and still fires even if warning
        already did). Returns the list of (scope, level) alerts fired this call.
        """
        fired: list[tuple[str, str]] = []
        for scope, cost, budget, pkey in (
            ("daily", daily_cost, self.daily_budget, day_key),
            ("monthly", monthly_cost, self.monthly_budget, month_key),
        ):
            if not budget or budget <= 0:
                continue
            frac = cost / budget
            level = "critical" if frac >= 1.0 else ("warning" if frac >= self.WARN_FRAC else None)
            if level is None:
                continue
            key = f"{scope}:{pkey}"
            with self._lock:
                prev = self._budget_fired.get(key)
                if level == "critical" and prev == "critical":
                    continue
                if level == "warning" and prev in ("warning", "critical"):
                    continue
                self._budget_fired[key] = level
                webhook = self.webhook_url
            icon = "🔴" if level == "critical" else "🟠"
            self._console.print(
                f"\n[bold]{icon} {scope.upper()} BUDGET {level.upper()}:[/bold] "
                f"${cost:.4f} of ${budget:.2f} ({frac * 100:.0f}%)\n"
            )
            if webhook:
                payload = {
                    "text": (
                        f"{icon} *Bedrock Insights — {scope} budget {level}*\n"
                        f"Spend ${cost:.4f} is {frac * 100:.0f}% of the ${budget:.2f} {scope} budget."
                    ),
                    "event": f"budget_{level}",
                    "source": "bedrock-insights",
                    "scope": scope, "cost": round(cost, 6),
                    "budget": budget, "fraction": round(frac, 4),
                }
                send_webhook(webhook, payload)
            fired.append((scope, level))
        return fired

    def notify_anomaly(self, info: dict) -> bool:
        """Fire a one-off alert for a detected cost spike (deduped per bucket)."""
        key = str(info.get("bucket_t"))
        with self._lock:
            if self._anomaly_key == key:
                return False
            self._anomaly_key = key
            webhook = self.webhook_url
        cost = info.get("cost", 0.0)
        baseline = info.get("baseline", 0.0)
        self._console.print(
            f"\n[bold yellow]📈 COST ANOMALY:[/bold yellow] a bucket cost "
            f"${cost:.4f} vs ~${baseline:.4f} baseline\n"
        )
        if webhook:
            payload = {
                "text": (
                    "📈 *Bedrock Insights — cost anomaly*\n"
                    f"A time bucket cost ${cost:.4f}, well above the ~${baseline:.4f} baseline."
                ),
                "event": "cost_anomaly",
                "source": "bedrock-insights",
                "cost": round(cost, 6), "baseline": round(baseline, 6),
            }
            send_webhook(webhook, payload)
        return True

    def check(self, cost: float) -> bool:
        """Alert if cost crosses the threshold for the first time. Returns True if fired."""
        with self._lock:
            if self.threshold is None or self._fired or cost < self.threshold:
                return False
            self._fired = True
            threshold = self.threshold
            webhook_url = self.webhook_url

        self._console.print(
            f"\n[bold red]⚠  THRESHOLD EXCEEDED:[/bold red]  "
            f"${cost:.4f} ≥ ${threshold:.2f}\n"
        )

        if webhook_url:
            ok, info = send_webhook(webhook_url, self._build_payload(cost, threshold))
            if ok:
                self._console.print(f"[dim]Alert delivered to webhook (HTTP {info}).[/dim]")
            else:
                self._console.print(f"[yellow]Webhook delivery failed:[/yellow] {info}")
        return True
