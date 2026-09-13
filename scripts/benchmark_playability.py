"""Small deterministic benchmark for the single-note playability planner."""
from __future__ import annotations

import argparse
import json
import time
import tracemalloc

from score_events import check_playability


def benchmark(lengths=(128, 512, 1024)):
    rows = []
    for length in lengths:
        events = [[index / 2, 69 + (index % 5), 0.5]
                  for index in range(length)]
        bars = [{"length": 4} for _ in range((length + 7) // 8)]
        data = {
            "version": 1,
            "title": f"benchmark-{length}",
            "tempo": 120,
            "time": [4, 4],
            "bars": bars,
            "tracks": [],
        }
        for bar in range(len(bars)):
            start = bar * 8
            stop = min(length, start + 8)
            data["tracks"].append({
                "bar": bar + 1,
                "part": "lead",
                "events": [[(index - start) / 2, 69 + (index % 5), 0.5]
                           for index in range(start, stop)],
            })
        tracemalloc.start()
        started = time.perf_counter()
        report = check_playability(data)
        elapsed = time.perf_counter() - started
        _, peak_memory = tracemalloc.get_traced_memory()
        tracemalloc.stop()
        lead = report.get("lanes", {}).get("lead", {})
        rows.append({
            "notes": length,
            "elapsed_seconds": round(elapsed, 6),
            "status": report["status"],
            "max_states": lead.get("max_states", 0),
            "max_transitions": lead.get("max_transitions", 0),
            "peak_memory_bytes": peak_memory,
            "issues": len(report.get("issues", [])),
        })
    return rows


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--lengths", nargs="*", type=int, default=[128, 512, 1024])
    args = parser.parse_args()
    if any(length <= 0 for length in args.lengths):
        parser.error("lengths must be positive")
    print(json.dumps(benchmark(tuple(args.lengths)), ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
