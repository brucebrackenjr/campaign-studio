"""A dated generation estimate, kept separate from reported usage and actual charges."""

import json
import math
from collections import Counter
from decimal import ROUND_HALF_UP, Decimal

from campaign_studio.generation import MODEL

PRICING_SOURCE = "https://developers.openai.com/api/docs/pricing#image-generation"
PRICING_DATE = "2026-10-29"

INCOMPLETE_REASONS = {
    "missing_usage": "Usage was not recorded.",
    "pricing": "Saved pricing is unavailable.",
    "request": "Saved request settings are unavailable.",
    "usage": "Saved usage could not be read.",
    "connection": "Usage was not recorded.",
    "timeout": "Usage was not recorded.",
}


def incomplete_reason(cost, *, detail=False):
    reasons = cost.get("incomplete_reasons", [])
    if len(reasons) != 1:
        return "Usage or pricing could not be verified."
    code = reasons[0]["code"]
    if detail and reasons[0]["count"] == 1:
        if code == "connection":
            return (
                "No usable usage record was saved after the connection was lost. "
                "This request’s cost could not be estimated."
            )
        if code == "timeout":
            return (
                "No usable usage record was saved before the request timed out. "
                "This request’s cost could not be estimated."
            )
    return INCOMPLETE_REASONS.get(code, "Usage or pricing could not be verified.")


def compact_usd(value):
    """Compact display only; calculations retain their unrounded recorded value."""
    if 0 < value < 0.01:
        return "< $0.01"
    rounded = Decimal(str(value)).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
    return f"${rounded:,.2f}"


def campaign_metric_display(summary):
    """Scoped report labels; the browser uses the same states and compact USD rules."""
    complete = summary.get("coverage") == "complete"
    count = summary.get("attempted_requests")
    known_count = type(count) is int and count >= 0
    cost = summary.get("usage_cost_estimate", {})
    estimate = cost.get("estimated_usd") if complete else None
    subtotal = cost.get("known_usage_subtotal_usd")
    amount = compact_usd(estimate) if estimate is not None else "Incomplete"
    zero = complete and known_count and count == 0 and estimate == 0
    return {
        "attempts": str(count)
        if complete and known_count
        else f"{count}+"
        if known_count and count > 0
        else "Not recorded",
        "attempts_helper": (
            "No generation attempts in this campaign."
            if zero
            else ""
            if complete
            else "Recorded attempts · history incomplete."
            if known_count and count > 0
            else "Earlier records are incomplete."
        ),
        "spend": amount,
        "known_spend": estimate is not None,
        "has_known_portion": estimate is None and subtotal is not None and subtotal > 0,
        "spend_helper": (
            "No generation attempts in this campaign."
            if zero
            else "USD · usage-based · not a bill"
            if estimate is not None
            else f"Known portion: {compact_usd(subtotal)} USD."
            if subtotal is not None and subtotal > 0
            else incomplete_reason(cost)
            if complete
            else "Earlier records are incomplete."
        ),
        "detail": (
            ""
            if estimate is not None
            else (
                f"{cost['unreported_attempts']} "
                + ("request" if cost["unreported_attempts"] == 1 else "requests")
                + " could not be estimated. "
                if cost.get("unreported_attempts")
                else ""
            )
            + (
                incomplete_reason(cost, detail=True)
                if complete
                else "Earlier request records are incomplete. The recorded count is a minimum."
                if known_count and count > 0
                else "Earlier records are incomplete."
            )
        ),
    }


def estimate_requests(requests):
    baseline = {
        "currency": "USD",
        "pricing_source": PRICING_SOURCE,
        "pricing_date": PRICING_DATE,
        "pricing_tier": "standard",
        "rates_per_million_tokens": {"text_input": 5, "image_input": 8, "image_output": 30},
        "actual_charge": None,
        "scope": "New generation requests in this plan, not exported images or prior attempts.",
    }
    if not requests:
        return {**baseline, "status": "no_new_requests", "request_count": 0, "estimated_usd": 0}
    estimates = {}
    for product, request in requests.items():
        if (request.model, request.size, request.quality, request.n) != (
            MODEL,
            "1920x1920",
            "high",
            1,
        ):
            return {
                **baseline,
                "status": "unknown",
                "request_count": len(requests),
                "estimated_usd": None,
                "reason": "No verified estimate for these settings.",
            }
        text_tokens = max(300, math.ceil(len(request.prompt.encode("utf-8")) / 3) + 64)
        estimates[product] = {
            "model": request.model,
            "size": request.size,
            "quality": request.quality,
            "image_output_tokens_assumed": 3276,
            "text_input_tokens_estimated": text_tokens,
            "image_input_tokens": 0,
            "estimated_usd": round((3276 * 30 + text_tokens * 5) / 1_000_000, 6),
        }
    return {
        **baseline,
        "status": "estimated",
        "request_count": len(requests),
        "estimated_usd": round(sum(item["estimated_usd"] for item in estimates.values()), 6),
        "requests": estimates,
        "assumptions": [
            (
                "3276 image-output tokens is an observed high-quality 1920-square baseline, "
                "not a guarantee."
            ),
            (
                "Text input is estimated from UTF-8 length with a small allowance; "
                "supplied images are not uploaded."
            ),
            "Actual usage and charges may differ; this is not an invoice or spending cap.",
        ],
    }


def estimate_recorded_usage(attempts, requests):
    """Estimate this run's observed tokens; an unreported attempt keeps its total unknown."""
    return _usage_estimate(
        [(attempt, requests.get(attempt["product_id"])) for attempt in attempts],
        "Reported token usage for attempts in this saved run only.",
    )


def estimate_campaign_usage(attempts, requests_by_run):
    """Use each attempt's own saved run request, never the latest campaign's model."""
    return _usage_estimate(
        [
            (attempt, requests_by_run.get(attempt["run_id"], {}).get(attempt["product_id"]))
            for attempt in attempts
        ],
        "Reported token usage across this campaign's saved production runs.",
    )


def _usage_estimate(attempt_requests, scope):
    values = []
    reasons = Counter()
    for attempt, request in attempt_requests:
        try:
            usage = json.loads(attempt["usage_json"]) if attempt.get("usage_json") else {}
        except ValueError, TypeError:
            usage = {}
        if not isinstance(usage, dict):
            usage = {}
        tokens = (usage.get("input_tokens"), usage.get("output_tokens"))
        known = (
            request is not None
            and request.model == MODEL
            and all(type(value) is int and value >= 0 for value in tokens)
        )
        values.append(round((tokens[0] * 5 + tokens[1] * 30) / 1_000_000, 6) if known else None)
        if not known:
            if request is None:
                reason = "request"
            elif request.model != MODEL:
                reason = "pricing"
            elif not attempt.get("usage_json"):
                kind = attempt.get("error_kind")
                reason = (
                    "connection"
                    if kind == "APIConnectionError"
                    else "timeout"
                    if kind in {"APITimeoutError", "overall_deadline"}
                    else "missing_usage"
                )
            else:
                reason = "usage"
            reasons[reason] += 1
    return {
        "currency": "USD",
        "pricing_source": PRICING_SOURCE,
        "pricing_date": PRICING_DATE,
        "scope": scope,
        "assumption": "The pinned Images request sends text only; output tokens are image tokens.",
        "attempts": len(values),
        "unreported_attempts": values.count(None),
        "incomplete_reasons": [
            {"code": code, "count": count} for code, count in sorted(reasons.items())
        ],
        "estimated_usd": round(sum(values), 6) if None not in values else None,
        "known_usage_subtotal_usd": round(sum(value for value in values if value is not None), 6),
        "actual_charge": None,
    }
