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

    def __init__(
        self,
        threshold: float | None,
        webhook_url: str | None = None,
        *,
        region: str = "",
        label: str = "",
        console: Console | None = None,
    ) -> None:
        self.threshold = threshold
        self.webhook_url = webhook_url
        self.region = region
        self.label = label
        self._console = console or _console
        self._fired = False
        self._lock = threading.Lock()

    @property
    def fired(self) -> bool:
        return self._fired

    def configure(self, threshold: float | None, webhook_url: str | None) -> None:
        """Update threshold/webhook at runtime (from the dashboard). Re-arms on change."""
        with self._lock:
            if threshold != self.threshold or webhook_url != self.webhook_url:
                self._fired = False  # re-arm so the new threshold can fire again
            self.threshold = threshold
            self.webhook_url = webhook_url

    def settings(self) -> dict:
        with self._lock:
            return {"threshold": self.threshold, "webhook_url": self.webhook_url}

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
