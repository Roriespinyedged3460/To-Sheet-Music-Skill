"""Compare sounding notes against independently retained musical evidence.

Passing proves only the supplied scope, never audio transcription accuracy.
Provenance declarations expose circular inputs; they cannot authenticate how
an annotation was actually made, which remains a reviewer responsibility.
"""
import argparse
from collections import Counter
import hashlib
import json
import math
import re
from fractions import Fraction
from pathlib import Path

PARTS={'lead','rhythm','bass','keys_rh','keys_lh','drums'}
INDEPENDENT={'reference_score','reviewed_mix_and_stems'}

def number(value):
    return float(Fraction(str(value)))

def musical_structure(data):
    return {'tempo':data.get('tempo'),'time':data.get('time'),'key':data.get('key',0),
            'tunings':data.get('tunings',{}),'bars':[
                {k:v for k,v in b.items() if k in {'time','tempo','length'}} for b in data['bars']]}


def snapshot_hash(value):
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True,
                                    separators=(",", ":")).encode("utf-8")).hexdigest()


def _timeline(data):
    starts = []; lengths = []; total = 0.; meter = data.get("time", [4, 4])
    for bar in data["bars"]:
        meter = bar.get("time", meter)
        length = number(bar.get("length", meter[0] * 4 / meter[1]))
        if not math.isfinite(length) or length <= 0:
            raise ValueError("Invalid bar length")
        starts.append(total); lengths.append(length); total += length

    def position(at):
        bar, offset = at; offset = number(offset)
        if type(bar) is not int or not 1 <= bar <= len(starts):
            raise ValueError("Position outside score")
        if not math.isfinite(offset) or not 0 <= offset <= lengths[bar - 1]:
            raise ValueError("Offset outside bar")
        return starts[bar - 1] + offset

    notes = {}
    for track in data["tracks"]:
        if track['part'] not in PARTS:raise ValueError('Unknown score part')
        lane = notes.setdefault(track["part"], [])
        for event in track["events"]:
            start = position([track["bar"], event[0]])
            duration = number(event[2])
            if not math.isfinite(duration) or duration <= 0:
                raise ValueError("Invalid note duration")
            pitches = event[1] if isinstance(event[1], list) else [event[1]]
            for pitch in pitches:
                if pitch is not None:
                    if type(pitch) is not int or not 0 <= pitch <= 127:
                        raise ValueError("Invalid sounding MIDI pitch")
                    lane.append((start, pitch, duration))
    return position, {lane: sorted(rows) for lane, rows in notes.items()}


def audit_fidelity(events, evidence, *, original_events=None, authorized_changes=None):
    if not isinstance(events, dict):
        raise ValueError("Events must be an object")
    if not isinstance(evidence, dict):
        raise ValueError("Expected fidelity evidence object")
    if original_events is not None and not isinstance(original_events, dict):
        raise ValueError("Original events must be an object")
    if authorized_changes is not None and not isinstance(authorized_changes, dict):
        raise ValueError("Authorized changes must be an object")
    if evidence.get("version") != 1:
        raise ValueError("Unsupported fidelity evidence version")
    position, actual = _timeline(events)
    issues = []; compared = []; adapted = []
    def issue(code, **details):
        issues.append({"code": code, **details})
    provenance = evidence.get("provenance", {})
    if not isinstance(provenance, dict):
        raise ValueError("Provenance must be an object")
    if (provenance.get("kind") in {"arranged_candidate", "generated_score"}
            or provenance.get("derived_from_events_sha256")):
        issue("circular_evidence")
    if not re.fullmatch(r'[0-9a-fA-F]{64}',str(provenance.get('artifact_sha256',''))):
        issue("missing_provenance")
    if provenance.get('kind') not in INDEPENDENT:
        issue('unreviewed_evidence')
    tolerance = float(evidence.get("tolerance_quarters", .01))
    if not math.isfinite(tolerance) or not 0 <= tolerance <= .125:
        raise ValueError("Evidence tolerance must be between 0 and .125 quarters")
    phrases = evidence.get("phrases", [])
    if not isinstance(phrases, list):
        raise ValueError("Evidence phrases must be a list")
    by_id = {}
    for phrase in phrases:
        if not isinstance(phrase, dict):
            raise ValueError("Each evidence phrase must be an object")
        if phrase["id"] in by_id:
            raise ValueError("Duplicate evidence phrase id")
        by_id[phrase["id"]] = phrase
    required = evidence.get("required_phrases", [])
    if not isinstance(required, list):
        raise ValueError("Required phrases must be a list")
    if not required:
        issue("uncovered_phrase", reason="No required musical scope declared")
    for required_phrase in required:
        ident = required_phrase["id"]
        if required_phrase.get('part') not in PARTS:
            issue('uncovered_phrase',phrase=ident,reason='Unknown part')
            continue
        phrase = by_id.get(ident)
        if phrase is None or any(phrase.get(k) != required_phrase[k]
                                 for k in ("part", "start", "end")):
            issue("uncovered_phrase", phrase=ident)
            continue
        start, end = position(phrase["start"]), position(phrase["end"])
        if end <= start:
            raise ValueError("Empty or reversed phrase span")
        expected = phrase.get("notes")
        duration_policy=phrase.get('duration_policy','exact')
        if duration_policy not in ('exact','attacks_only') or (duration_policy=='attacks_only' and phrase['part']!='drums'):
            raise ValueError('Only percussion evidence may compare attacks without note-off duration')
        if expected is None:
            issue("uncovered_phrase", phrase=ident, reason="No observation")
            continue
        if not phrase.get("source_ref") or not phrase.get("source_kind"):
            issue("missing_provenance", phrase=ident)
        if (phrase.get("source_kind") in {"arranged_candidate", "generated_score"}
                or phrase.get('derived_from_events_sha256')):
            issue("circular_evidence", phrase=ident)
        if phrase.get('source_kind') not in INDEPENDENT:
            issue('unreviewed_evidence',phrase=ident)
        if not expected and not phrase.get('dead_strokes') and phrase.get("silence_basis") not in {"reference_score", "mix_and_stems"}:
            issue("insufficient_silence_evidence", phrase=ident)
            continue
        wanted = []
        for offset, pitch, duration in expected:
            offset, duration = number(offset), number(duration)
            if (not math.isfinite(offset) or not math.isfinite(duration)
                    or offset < 0 or duration <= 0 or offset >= end - start):
                raise ValueError("Invalid reference note")
            pp = pitch if isinstance(pitch, list) else [pitch]
            for p in pp:
                if type(p) is not int or not 0 <= p <= 127:
                    raise ValueError("Reference pitch must be sounding MIDI pitch")
                wanted.append((offset, p, min(duration, end - start - offset)))
        found = [(at - start, p, min(dur, end - at))
                 for at, p, dur in actual.get(phrase["part"], []) if start <= at < end]
        remaining = list(found); matched = 0
        for at, pitch, duration in sorted(wanted):
            near = [i for i, n in enumerate(remaining) if abs(n[0] - at) <= tolerance]
            exact = [i for i in near if remaining[i][1] == pitch]
            if exact:
                index = min(exact, key=lambda i: abs(remaining[i][2] - duration))
                note = remaining.pop(index); matched += 1
                if duration_policy=='exact' and abs(note[2] - duration) > tolerance:
                    issue("duration_mismatch", phrase=ident, at=at, pitch=pitch,
                          expected=duration, actual=note[2])
            elif near:
                index = min(near, key=lambda i: abs(remaining[i][1] - pitch))
                note = remaining.pop(index)
                issue("pitch_mismatch", phrase=ident, at=at, expected=pitch,
                      actual=note[1], octave_error=abs(note[1] - pitch) == 12)
            else:
                issue("missing_note", phrase=ident, at=at, pitch=pitch)
        for at, pitch, duration in remaining:
            issue("extra_note", phrase=ident, at=at, pitch=pitch)
        if 'dead_strokes' in phrase:
            observed=[]
            for track in events['tracks']:
                if track['part']!=phrase['part']:continue
                for event in track['events']:
                    opts=event[4] if len(event)>4 else {}
                    tab=event[3] if opts.get('dead') else opts.get('dead_tabs',[])
                    at=position([track['bar'],event[0]])
                    if tab and start<=at<end:
                        observed.append((round(at-start,7),tuple(sorted(s for s,f in tab)),round(number(event[2]),7)))
            wanted_strokes=[(round(number(at),7),tuple(sorted(strings)),round(number(dur),7))
                            for at,strings,dur in phrase['dead_strokes']]
            if Counter(observed)!=Counter(wanted_strokes):
                issue('dead_stroke_mismatch',phrase=ident,expected=len(wanted_strokes),actual=len(observed))
        if 'grace_groups' in phrase:
            observed=[]
            for track in events['tracks']:
                if track['part']!=phrase['part']:continue
                for event in track['events']:
                    at=position([track['bar'],event[0]])
                    grace=event[4].get('grace') if len(event)>4 else None
                    if grace and start<=at<end:observed.append([at-start,grace])
            if observed!=phrase['grace_groups']:issue('grace_group_mismatch',phrase=ident)
        compared.append({"id": ident, "part": phrase["part"], "expected_notes": len(wanted),
                         "actual_notes": len(found), "onset_pitch_matches": matched,'duration_policy':duration_policy})
    if original_events is not None:
        _, original = _timeline(original_events)
        structure_changed=musical_structure(original_events)!=musical_structure(events)
        if original != actual or structure_changed:
            permission = authorized_changes or {}
            authorized = (permission.get("original_events_sha256") == snapshot_hash(original_events)
                          and permission.get("arranged_events_sha256") == snapshot_hash(events)
                          and bool(permission.get("authorization")) and bool(permission.get("reason")))
            if structure_changed:
                adapted.append({'part':'structure','before':musical_structure(original_events),
                                'after':musical_structure(events),'authorized':bool(authorized)})
            for lane in sorted(set(original) | set(actual)):
                before, after = Counter(original.get(lane, [])), Counter(actual.get(lane, []))
                if before != after:
                    adapted.append({"part": lane, "removed": list((before - after).elements()),
                                    "added": list((after - before).elements()), "authorized": bool(authorized)})
            if not authorized:
                issue("unauthorized_arrangement_change")
    incomplete = {"uncovered_phrase", "insufficient_silence_evidence", "missing_provenance",'unreviewed_evidence'}
    errors = [v for v in issues if v["code"] not in incomplete]
    known_differences=evidence.get('known_differences',[])
    if not isinstance(known_differences,list):raise ValueError('known_differences must be a list')
    return {"version": 1, "status": "needs_repair" if errors else "incomplete" if issues else "passed",
            "reference_match": not issues and not known_differences, "musical_accuracy_verified": False,
            'known_differences':known_differences,
            "scope": "Only independently declared required phrases; provenance must also be reviewed",
            "events_sha256": snapshot_hash(events), "evidence_sha256": snapshot_hash(evidence),
            "issues": issues, "phrases": compared, "adapted": adapted}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--events", type=Path, required=True)
    parser.add_argument("--evidence", type=Path, required=True)
    parser.add_argument("--report", type=Path, required=True)
    parser.add_argument("--original-events", type=Path)
    parser.add_argument("--authorized-changes", type=Path)
    args = parser.parse_args()
    def read(path):
        return json.loads(path.read_text(encoding="utf-8")) if path else None
    result = audit_fidelity(read(args.events), read(args.evidence), original_events=read(args.original_events),
                            authorized_changes=read(args.authorized_changes))
    args.report.parent.mkdir(parents=True, exist_ok=True)
    args.report.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({"status": result["status"], "issues": len(result["issues"])}))
    return 0 if result["status"] == "passed" else 2


if __name__ == "__main__":
    raise SystemExit(main())
