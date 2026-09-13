"""Compare compact score timing with independently recorded, explicitly scoped evidence."""
import argparse
import hashlib
import json
import math
from fractions import Fraction
from pathlib import Path

LANES = {"lead", "rhythm", "bass", "keys_rh", "keys_lh", "drums"}


def _number(value, name, positive=False):
    if isinstance(value, bool):
        raise ValueError(f"{name} must be a finite number")
    try:
        result = float(Fraction(str(value)))
    except (ValueError, ZeroDivisionError, OverflowError) as exc:
        raise ValueError(f"{name} must be a finite number") from exc
    if not math.isfinite(result) or result < 0 or (positive and result == 0):
        raise ValueError(f"{name} must be finite and {'positive' if positive else 'nonnegative'}")
    return result


def _hash(data):
    return hashlib.sha256(json.dumps(data, sort_keys=True, ensure_ascii=False,
                                    separators=(",", ":")).encode("utf-8")).hexdigest()


def summarize_coverage(records):
    """Every required observation must be represented, including rejected/empty windows."""
    allowed = {"checked", "mismatch", "insufficient_evidence"}
    if any(r.get("status") not in allowed for r in records):
        raise ValueError("Invalid coverage observation status")
    missing = [r for r in records if r["status"] == "insufficient_evidence"]
    mismatches = [r for r in records if r["status"] == "mismatch"]
    return {"status": "needs_repair" if mismatches else
            "incomplete" if missing or not records else "passed",
            "required": len(records), "checked": len(records) - len(missing),
            "uncovered": missing, "mismatches": mismatches}


def audit_rhythm(events, evidence):
    """Passing means conformance in the listed scopes, never original-audio accuracy."""
    if not isinstance(evidence, dict) or evidence.get("version") != 1:
        raise ValueError("Expected rhythm evidence version 1")
    unknown = set(evidence) - {"version", "anchors", "phrases", "tolerance_seconds",
                               "tolerance_quarters"}
    if unknown:
        raise ValueError(f"Unknown rhythm evidence fields: {sorted(unknown)}")
    seconds_tolerance = _number(evidence.get("tolerance_seconds", .08), "tolerance_seconds")
    quarters_tolerance = _number(evidence.get("tolerance_quarters", .01), "tolerance_quarters")
    starts, times, lengths, rates = [0.0], [0.0], [], []
    meter, bpm = events["time"], events["tempo"]
    for bar in events["bars"]:
        meter = bar.get("time", meter)
        bpm = _number(bar.get("tempo", bpm), "tempo", positive=True)
        length = _number(bar.get("length", 4 * _number(meter[0], "numerator", True) /
                                 _number(meter[1], "denominator", True)), "bar length", True)
        lengths.append(length)
        rates.append(60 / bpm)
        starts.append(starts[-1] + length)
        times.append(times[-1] + length * 60 / bpm)

    def position(at):
        if not isinstance(at, list) or len(at) != 2 or type(at[0]) is not int:
            raise ValueError("Position must be [bar, quarter offset]")
        index = at[0] - 1
        offset = _number(at[1], "offset")
        if not 0 <= index < len(lengths) or offset > lengths[index]:
            raise ValueError("Position outside score")
        return starts[index] + offset, times[index] + offset * rates[index]

    def source(item):
        if not isinstance(item.get("source"), str) or not item["source"].strip():
            raise ValueError("Each evidence item needs an independent source description")

    attacks = []
    for track in events["tracks"]:
        if track["part"] not in LANES:
            raise ValueError("Unknown score lane")
        for event in track["events"]:
            onset, _ = position([track["bar"], event[0]])
            duration = _number(event[2], "duration", True)
            if onset + duration > starts[-1] + 1e-9:
                raise ValueError("Event outside score")
            if event[1] is not None:
                attacks.append((track["part"], onset, duration))
    report = {"version": 1, "status": "incomplete", "events_sha256": _hash(events),
              "evidence_sha256": _hash(evidence), "musical_accuracy_verified": False,
              "scope": "Only the supplied tempo anchors and phrase onset/duration expectations",
              "anchors": [], "phrases": [], "uncovered": []}
    failed = False
    for anchor in evidence.get("anchors", []):
        if set(anchor) - {"at", "seconds", "source"}:
            raise ValueError("Unknown anchor fields")
        source(anchor)
        _, predicted = position(anchor["at"])
        observed = _number(anchor["seconds"], "anchor seconds")
        residual = predicted - observed
        ok = abs(residual) <= seconds_tolerance + 1e-9
        failed |= not ok
        report["anchors"].append({"at": anchor["at"], "source": anchor["source"],
                                   "residual_seconds": residual, "passed": ok})
    if not report["anchors"]:
        report["uncovered"].append("tempo anchors")

    ids = set()
    for phrase in evidence.get("phrases", []):
        if set(phrase) - {"id", "parts", "start", "end", "onsets", "durations", "source", "silence_confirmed"}:
            raise ValueError("Unknown phrase fields")
        source(phrase)
        ident = phrase["id"]
        if not isinstance(ident, str) or not ident or ident in ids:
            raise ValueError("Phrase ids must be nonempty and unique")
        ids.add(ident)
        parts = phrase["parts"]
        if not isinstance(parts, list) or not parts or any(p not in LANES for p in parts):
            raise ValueError("Phrase needs known score lanes")
        start, _ = position(phrase["start"])
        end, _ = position(phrase["end"])
        if end <= start:
            raise ValueError("Phrase end must follow its start")
        onsets = phrase.get("onsets")
        if onsets is None or (onsets == [] and phrase.get("silence_confirmed") is not True):
            report["uncovered"].append(ident)
            continue
        if not isinstance(onsets, list):
            raise ValueError("Phrase onsets must be a list or null")
        expected = [_number(x, "onset") for x in onsets]
        if any(x >= end - start for x in expected) or any(
                b <= a for a, b in zip(expected, expected[1:])):
            raise ValueError("Phrase onsets must be increasing and inside its span")
        durations = phrase.get("durations")
        if durations is not None:
            if not isinstance(durations, list) or len(durations) != len(expected):
                raise ValueError("Durations must correspond to phrase onsets")
            durations = [_number(d, "expected duration", True) for d in durations]
            if any(o + d > end - start + 1e-9 for o, d in zip(expected, durations)):
                raise ValueError("Expected durations must be clipped to the phrase span")
        # A long event entering the phrase is not a new attack. Union simultaneous
        # attacks across explicitly selected players; ties stay one source event.
        actual = {}
        for part, onset, duration in attacks:
            if part in parts and start <= onset < end:
                relative = onset - start
                actual[relative] = max(actual.get(relative, 0), min(duration, end - onset))
        remaining = set(actual)
        missing, duration_errors = [], []
        for index, onset in enumerate(expected):
            candidates = [a for a in remaining if abs(a - onset) <= quarters_tolerance + 1e-9]
            if not candidates:
                missing.append(onset)
                continue
            chosen = min(candidates, key=lambda a: (abs(a - onset), a))
            remaining.remove(chosen)
            if durations is not None and abs(actual[chosen] - durations[index]) > quarters_tolerance + 1e-9:
                duration_errors.append({"onset": onset, "expected": durations[index],
                                        "actual": actual[chosen]})
        failed |= bool(missing or remaining or duration_errors)
        report["phrases"].append({"id": ident, "source": phrase["source"], "parts": parts,
                                  "start": phrase["start"], "end": phrase["end"],
                                  "expected_attacks": len(expected), "actual_attacks": len(actual),
                                  "missing_onsets": missing, "unexpected_onsets": sorted(remaining),
                                  "duration_mismatches": duration_errors})
    if not evidence.get("phrases"):
        report["uncovered"].append("phrase onsets")
    report["status"] = "needs_repair" if failed else "incomplete" if report["uncovered"] else "passed"
    return report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--events", type=Path, required=True)
    parser.add_argument("--evidence", type=Path, required=True)
    parser.add_argument("--report", type=Path, required=True)
    args = parser.parse_args()
    result = audit_rhythm(json.loads(args.events.read_text(encoding="utf-8")),
                          json.loads(args.evidence.read_text(encoding="utf-8")))
    args.report.parent.mkdir(parents=True, exist_ok=True)
    args.report.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({"status": result["status"], "report": str(args.report)}, ensure_ascii=False))
    return 0 if result["status"] == "passed" else 2


if __name__ == "__main__":
    raise SystemExit(main())
