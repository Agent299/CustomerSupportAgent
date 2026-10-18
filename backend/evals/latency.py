"""Time to first token through the running chat API, as the widget sees it (docs/PLAN.md §10).

Sends answerable golden questions one at a time over HTTP and reads the SSE stream, timing
POST → first ``token`` event and POST → terminal event. The first request warms connections
and is not counted (the Phase 2 target is the *warm* P90).
"""

import json
import time
from dataclasses import dataclass

import httpx

from evals.golden import GoldenSet


@dataclass
class Sample:
    id: str
    ttft_ms: float | None  # None: the turn ended without tokens (escalated)
    total_ms: float
    outcome: str


def _percentile(values: list[float], pct: int) -> float:
    ordered = sorted(values)
    rank = max(0, min(len(ordered) - 1, round(pct / 100 * len(ordered) + 0.5) - 1))
    return ordered[rank]


def measure_turn(
    client: httpx.Client, base: str, query: str, version: str | None
) -> tuple[float | None, float, str]:
    session = client.post(f"{base}/sessions", json={"version_hint": version}).json()
    headers = {"X-Session-Token": session["session_token"]}
    sid = session["session_id"]
    started = time.perf_counter()
    accepted = client.post(f"{base}/sessions/{sid}/messages", json={"text": query}, headers=headers)
    accepted.raise_for_status()
    after = accepted.json()["stream_after"]
    ttft = None
    event = ""
    with client.stream(
        "GET", f"{base}/sessions/{sid}/stream", params={"after": after}, headers=headers
    ) as response:
        for line in response.iter_lines():
            if line.startswith("event: "):
                event = line.removeprefix("event: ")
                if event == "token" and ttft is None:
                    ttft = (time.perf_counter() - started) * 1000
                if event in {"final", "escalated", "error"}:
                    break
    return ttft, (time.perf_counter() - started) * 1000, event


def measure(golden: GoldenSet, api: str, n: int) -> list[Sample]:
    base = f"{api.rstrip('/')}/api/v1/chat"
    examples = [e for e in golden.examples if e.expected_route == "ANSWERED" and not e.history]
    samples: list[Sample] = []
    with httpx.Client(timeout=60) as client:
        measure_turn(client, base, examples[0].query, examples[0].version)  # warm-up
        for example in examples[:n]:
            ttft, total, outcome = measure_turn(client, base, example.query, example.version)
            samples.append(Sample(example.id, ttft, total, outcome))
    return samples


def summarize(samples: list[Sample]) -> str:
    ttft = [s.ttft_ms for s in samples if s.ttft_ms is not None]
    total = [s.total_ms for s in samples]
    lines = [
        f"{len(samples)} warm turns, sequential; {len(ttft)} streamed an answer",
        "",
        "| | p50 | p90 | max |",
        "|---|---|---|---|",
        f"| time to first token | {_percentile(ttft, 50) / 1000:.2f}s | "
        f"{_percentile(ttft, 90) / 1000:.2f}s | {max(ttft) / 1000:.2f}s |",
        f"| full answer | {_percentile(total, 50) / 1000:.2f}s | "
        f"{_percentile(total, 90) / 1000:.2f}s | {max(total) / 1000:.2f}s |",
    ]
    return "\n".join(lines)


def to_json(samples: list[Sample]) -> str:
    return json.dumps([s.__dict__ for s in samples], indent=2)
