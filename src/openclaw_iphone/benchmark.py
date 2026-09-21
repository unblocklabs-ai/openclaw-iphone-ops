"""Offline summaries of equivalent, separately supervised task runs."""
import math
from statistics import median


def summarize(runs: list[dict]) -> dict[str, object]:
    groups = {}
    for run in runs:
        mode = run.get("driver", "unspecified")
        if mode not in {"deterministic", "jev", "legacy", "planner", "unspecified"}:
            raise ValueError("Unknown benchmark driver.")
        seconds = run.get("seconds")
        if type(seconds) not in (float, int) or not math.isfinite(seconds) or seconds < 0:
            raise ValueError("Benchmark runs need finite nonnegative total durations.")
        transport = run.get("transport")
        if transport is not None:
            counts = transport.get("counts") if isinstance(transport, dict) else None
            if not isinstance(counts, dict) or any(not isinstance(k, str) or type(v) is not int or v < 0 for k, v in counts.items()):
                raise ValueError("Invalid benchmark transport counts.")
        model = run.get("model")
        if model is not None:
            if not isinstance(model, dict) or not isinstance(model.get("latencies_seconds", []), list):
                raise ValueError("Invalid model telemetry.")
            numbers = [*model.get("latencies_seconds", []), *(model.get(k, 0) for k in
                       ("input_tokens", "estimated_known_cost_usd", "unknown_usage_requests"))]
            if any(type(v) not in (int, float) or not math.isfinite(v) or v < 0 for v in numbers):
                raise ValueError("Invalid numeric model telemetry.")
        groups.setdefault(mode, []).append(run)
    result = {}
    for mode, samples in groups.items():
        times = sorted(s["seconds"] for s in samples)
        counts = [s.get("transport", {}).get("counts", {}) if isinstance(s.get("transport"), dict) else {} for s in samples]
        models = [s["model"] for s in samples if isinstance(s.get("model"), dict)]
        latencies = [value for model in models for value in model.get("latencies_seconds", [])]
        result[mode] = {"runs": len(samples), "verified_completed": sum(s.get("status") == "completed" and s.get("verification") == "satisfied" for s in samples),
            "escalated_or_blocked": sum(s.get("status") in {"escalated", "blocked"} for s in samples),
            "total_seconds_median": median(times), "total_seconds_p95": times[math.ceil(len(times) * .95) - 1],
            "wda_attempts": sum(v for c in counts for k, v in c.items() if k.startswith("wda ")),
            "devicectl_attempts": sum(c.get("devicectl", 0) for c in counts),
            "runs_missing_transport": sum(s.get("transport") is None for s in samples),
            "runs_missing_model_telemetry": sum(s.get("model") is None for s in samples) if mode == "jev" else 0,
            "model_latency_median": median(latencies) if latencies else None,
            "model_input_tokens": sum(m.get("input_tokens", 0) for m in models),
            "estimated_known_model_cost_usd": sum(m.get("estimated_known_cost_usd", 0) for m in models),
            "unknown_usage_requests": sum(m.get("unknown_usage_requests", 0) for m in models),
            # Transport logs cannot prove absence of external effects. These
            # require independent operator annotation, not inferred zeros.
            "safety_annotations": [{k: s.get(k) for k in ("wrong_target_actions", "unintended_actions", "false_successes")} for s in samples]}
    return {"groups": result, "caveat": "Compare equivalent tasks and starting states. Include failed runs. Small-sample p95 is exploratory; missing safety annotations mean unknown."}
