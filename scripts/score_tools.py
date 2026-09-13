"""Inspect native MuseScore scores; structural checks are not an audio accuracy test."""
import argparse
import copy
import hashlib
import json
import os
import shutil
import subprocess
import sys
import tempfile
import xml.etree.ElementTree as ET
import zipfile
from fractions import Fraction
from pathlib import Path

try:
    from rhythm_audit import audit_rhythm
    from fidelity_audit import audit_fidelity
except ModuleNotFoundError:
    from scripts.rhythm_audit import audit_rhythm
    from scripts.fidelity_audit import audit_fidelity


def apply_native_dead_notes(root, events):
    """Mute only explicitly declared TAB crosses, preserving all pitched notes.

MuseScore's MusicXML importer retains x heads but does not retain playback
mute reliably. Verify bar, chord order and string/fret before setting native
dead/play properties. The MIDI omits these noise strokes; PDF/MSCZ retain x.
    """
    expected={}
    for track in events.get('tracks',[]):
        rows=[]
        for event in sorted(track['events'],key=lambda e:Fraction(str(e[0]))):
            options=event[4] if len(event)>4 else {}
            pairs=event[3] if options.get('dead') else options.get('dead_tabs',[])
            if pairs:rows.append(sorted((s-1,f) for s,f in pairs))
        if rows:expected[track['part'],track['bar']]=rows
    if not expected:return {'muted_notes':0,'midi_noise_strokes_omitted':False}
    parts=root.findall('Score/Part');staff_list=root.findall('Score/Staff')
    staves={s.get('id'):s for s in staff_list}
    muted=0
    for (lane,bar),rows in expected.items():
        if lane not in ('lead','rhythm','bass'):raise ValueError('Dead events require string parts')
        part=parts[('lead','rhythm','bass').index(lane)]
        staff_id=part.find('Staff').get('id')
        staff=staves[staff_id] if staff_id is not None else staff_list[('lead','rhythm','bass').index(lane)]
        measures=staff.findall('Measure')
        if bar>len(measures):raise ValueError('Missing dead-note measure')
        crosses=[]
        for chord in measures[bar-1].findall('.//Chord'):
            notes=[n for n in chord.findall('Note') if n.findtext('head')=='cross' or n.findtext('dead')=='1']
            if notes:crosses.append(notes)
        if len(crosses)!=len(rows):raise ValueError(f'Dead-note chord count mismatch at {lane} bar {bar}')
        for notes,pairs in zip(crosses,rows):
            actual=sorted((int(n.findtext('string','-1')),int(n.findtext('fret','-1'))) for n in notes)
            if actual!=pairs:raise ValueError(f'Dead-note TAB mismatch at {lane} bar {bar}')
            for note in notes:
                for name,value in [('dead','1'),('play','0')]:
                    element=note.find(name)
                    if element is None:element=ET.SubElement(note,name)
                    element.text=value
                muted+=1
    return {'muted_notes':muted,'midi_noise_strokes_omitted':True,
            'scope':'Explicit TAB x strokes retained visually; not synthesized as pitched notes'}


def _replace_native_xml(path, root):
    with zipfile.ZipFile(path) as archive:
        entries=[(info,archive.read(info.filename)) for info in archive.infolist()]
    targets=[info.filename for info,_ in entries if info.filename.lower().endswith('.mscx')]
    if len(targets)!=1:raise ValueError('Cannot patch a multi-score archive')
    temporary=path.with_suffix('.patched.mscz')
    with zipfile.ZipFile(temporary,'w',zipfile.ZIP_DEFLATED) as archive:
        for info,body in entries:
            archive.writestr(info,ET.tostring(root,encoding='utf-8',xml_declaration=True) if info.filename==targets[0] else body)
    temporary.replace(path)


def load_score(path):
    path = Path(path)
    if path.suffix.lower() == ".mscz":
        with zipfile.ZipFile(path) as archive:
            names = archive.namelist()
            candidates = [n for n in names if n.lower().endswith(".mscx")]
            if "META-INF/container.xml" in names:
                container = ET.fromstring(archive.read("META-INF/container.xml"))
                declared = [el.get("full-path") for el in container.iter()
                            if el.tag.split("}")[-1] == "rootfile"]
                candidates = [n for n in declared if n in candidates] or candidates
            if len(candidates) != 1:
                raise ValueError("Cannot identify one main MSCX score in archive")
            info = archive.getinfo(candidates[0])
            if info.file_size > 64 * 1024 * 1024:
                raise ValueError("Score XML exceeds the 64 MiB inspection limit")
            root = ET.fromstring(archive.read(candidates[0]))
    else:
        root = ET.parse(path).getroot()
    if root.tag != "museScore" or root.find("Score") is None:
        raise ValueError("Expected native MSCX/MSCZ; import MusicXML with MuseScore first")
    return root


def _canonical_json_hash(value):
    payload = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _playability_config_hash(config):
    try:
        import score_events
    except ImportError:
        try:
            from scripts import score_events
        except ImportError:
            return _canonical_json_hash(config)
    return score_events._config_hash(config)


def _validate_playability_report(playability_report, events=None, config=None):
    if not isinstance(playability_report, dict):
        raise ValueError("playability report must be an object")
    if type(playability_report.get("version")) is not int or playability_report.get("version") != 1:
        raise ValueError("Unsupported playability report version")
    status = playability_report.get("status")
    if status not in {"passed", "incomplete", "needs_repair", "computation_incomplete"}:
        raise ValueError("Invalid playability report status")
    if not isinstance(playability_report.get("events_sha256"), str) or not playability_report["events_sha256"]:
        raise ValueError("Playability report has no events hash")
    if events is not None and playability_report["events_sha256"] != _canonical_json_hash(events):
        raise ValueError("Playability report does not match the events source")
    expected_config = playability_report.get("config_sha256")
    config_to_check = config
    if config_to_check is None and isinstance(playability_report.get("parameters"), dict):
        config_to_check = playability_report["parameters"]
    if expected_config and config_to_check is not None:
        if expected_config != _playability_config_hash(config_to_check):
            raise ValueError("Playability report has an inconsistent playability config hash")
    return status


def _canonical_xml_hash(path):
    path = Path(path)
    if path.suffix.lower() == ".mxl":
        with zipfile.ZipFile(path) as archive:
            names = [name for name in archive.namelist() if name.lower().endswith((".xml", ".musicxml"))]
            if "META-INF/container.xml" in archive.namelist():
                container = ET.fromstring(archive.read("META-INF/container.xml"))
                declared = [element.get("full-path") for element in container.iter()
                            if element.tag.split("}")[-1] == "rootfile"]
                names = [name for name in declared if name in archive.namelist()] or names
            if len(names) != 1:
                raise ValueError("Cannot identify one MusicXML root in MXL archive")
            info = archive.getinfo(names[0])
            if info.file_size > 64 * 1024 * 1024:
                raise ValueError("MusicXML exceeds the 64 MiB inspection limit")
            root = ET.fromstring(archive.read(names[0]))
    else:
        root = ET.parse(path).getroot()

    return _canonical_element_hash(root)


def _canonical_element_hash(root):
    root = copy.deepcopy(root)

    def strip_whitespace(element):
        if element.text and not element.text.strip():
            element.text = None
        if element.tail and not element.tail.strip():
            element.tail = None
        for child in element:
            strip_whitespace(child)

    strip_whitespace(root)
    return hashlib.sha256(ET.tostring(root, encoding="utf-8")).hexdigest()


def _canonical_native_hash(path):
    return _canonical_element_hash(load_score(path))


def audit_score(root, expected_parts=5, max_fret_span=4, playability_report=None,
                events=None, config=None, rhythm_evidence=None, fidelity_evidence=None):
    score = root.find("Score")
    parts, staves = score.findall("Part"), score.findall("Staff")
    report = {"errors": [], "warnings": [], "parts": [],
              "musical_accuracy_verified": False,
              "playability_checks_passed": None,
              "rhythm_checks_passed": None,
              "score_native_sha256": _canonical_element_hash(root)}

    if playability_report is not None:
        status = _validate_playability_report(playability_report, events, config)
        report["playability_report"] = {
            "status": status,
            "events_sha256": playability_report["events_sha256"],
            "config_sha256": playability_report.get("config_sha256"),
        }
        report["playability_checks_passed"] = status == "passed"
        expected_native_hash = playability_report.get("score_native_sha256")
        if expected_native_hash and _canonical_element_hash(root) != expected_native_hash:
            report["errors"].append({"code": "playability_native_score_mismatch",
                                     "part": "", "bar": None})
        if status != "passed":
            issue_code = "playability_not_passed"
            report["errors"].append({"code": issue_code, "part": "", "bar": None,
                                     "status": status})

    if rhythm_evidence is not None:
        if events is None:
            raise ValueError("Rhythm evidence requires the events source")
        rhythm = audit_rhythm(events, rhythm_evidence)
        report["rhythm_report"] = rhythm
        report["rhythm_checks_passed"] = rhythm["status"] == "passed"
        if rhythm["status"] != "passed":
            report["errors"].append({"code": "rhythm_not_passed", "part": "", "bar": None,
                                     "status": rhythm["status"]})

    if fidelity_evidence is not None:
        if events is None:raise ValueError('Fidelity evidence requires events')
        report['fidelity_report']=audit_fidelity(events,fidelity_evidence)
        if report['fidelity_report']['status']!='passed':
            report['errors'].append({'code':'fidelity_not_passed','status':report['fidelity_report']['status']})

    def issue(code, part="", bar=None, severity="errors"):
        report[severity].append({"code": code, "part": part, "bar": bar})

    if len(parts) != expected_parts:
        issue("part_count_mismatch")
    counts = [len(s.findall("Measure")) for s in staves]
    if not counts or not all(counts) or len(set(counts)) != 1:
        issue("measure_count_mismatch")
    definitions = [(p, s) for p in parts for s in p.findall("Staff")]
    if len(definitions) != len(staves):
        issue("staff_definition_mismatch")
    for (part, definition), staff in zip(definitions, staves):
        name = part.findtext("trackName", "Unnamed")
        kind = definition.find("StaffType")
        group = kind.get("group", "pitched") if kind is not None else "pitched"
        tuning = [int(v.text) for v in part.findall("Instrument/StringData/string")]
        # MSCX stores tuning low-to-high, while Note/string is zero-based high-to-low.
        high_to_low = list(reversed(tuning))
        notes = staff.findall(".//Note")
        report["parts"].append({"name": name, "staff_id": staff.get("id"),
                                "group": group, "measures": len(staff.findall("Measure")),
                                "notes": len(notes)})
        if not notes:
            issue("silent_staff_review", name, severity="warnings")
        if group != "tablature":
            continue
        for bar, measure in enumerate(staff.findall("Measure"), 1):
            for chord in measure.findall(".//Chord"):
                used, fretted = set(), []
                for note in chord.findall("Note"):
                    try:
                        string, fret, pitch = (int(note.findtext(k, "")) for k in ("string", "fret", "pitch"))
                    except ValueError:
                        issue("invalid_tab", name, bar)
                        continue
                    max_fret = int(part.findtext("Instrument/StringData/frets", "24"))
                    if not 0 <= string < len(high_to_low) or not 0 <= fret <= max_fret:
                        issue("invalid_tab", name, bar)
                        continue
                    if string in used:
                        issue("duplicate_string", name, bar)
                    used.add(string)
                    if high_to_low[string] + fret != pitch:
                        issue("tab_pitch_mismatch", name, bar)
                    if fret:
                        fretted.append(fret)
                    finger = note.findtext("fingering")
                    if finger is None:
                        native_finger = note.find("Fingering")
                        if native_finger is not None:
                            # MuseScore stores the digit inside a TextBase object.
                            finger = native_finger.findtext("text", native_finger.text or "")
                    if finger is not None:
                        try:
                            finger_number = int(finger)
                        except ValueError:
                            issue("invalid_fingering", name, bar)
                        else:
                            if finger_number not in (0, 1, 2, 3, 4):
                                issue("invalid_fingering", name, bar)
                            elif fret == 0 and finger_number != 0:
                                issue("open_string_fingering", name, bar)
                            elif fret and finger_number == 0:
                                issue("fretted_note_without_fingering", name, bar)
                if fretted and max(fretted) - min(fretted) > max_fret_span:
                    issue("wide_fret_span_review", name, bar, "warnings")
    report["structural_checks_passed"] = not report["errors"]
    return report


def export_score(source, out_dir, name, musescore=None, timeout=240, playability_report=None,
                 events=None, config=None, rhythm_evidence=None, fidelity_evidence=None,
                 original_events=None, authorized_changes=None, review_draft=None):
    if not name or any(c in name for c in '/\\:*?"<>|') or name in (".", ".."):
        raise ValueError("Name must be a filename stem without path separators")
    out = Path(out_dir).resolve()
    if out.exists():
        raise FileExistsError(f"Output directory already exists: {out}")
    source = Path(source).resolve(strict=True)
    if source.suffix.lower() not in (".mscz", ".mscx", ".musicxml", ".mxl", ".xml"):
        raise ValueError("Export a notation source, not an unedited transcription MIDI")
    if playability_report is not None:
        status = _validate_playability_report(playability_report, events, config)
        review_only=(bool(review_draft and review_draft.strip()) and status=='incomplete'
                     and playability_report.get('mode')=='notation_only' and not playability_report.get('issues'))
        if status != "passed" and not review_only:
            raise ValueError(f"Playability report status is {status}; formal export is blocked")
        expected_xml_hash = playability_report.get("score_xml_sha256")
        if expected_xml_hash and source.suffix.lower() in (".musicxml", ".mxl", ".xml"):
            actual_xml_hash = _canonical_xml_hash(source)
            if actual_xml_hash != expected_xml_hash:
                raise ValueError("Playability report does not match the MusicXML source")
        expected_native_hash = playability_report.get("score_native_sha256")
        if expected_native_hash and source.suffix.lower() in (".mscz", ".mscx"):
            if _canonical_native_hash(source) != expected_native_hash:
                raise ValueError("Playability report does not match the native score source")
    rhythm = None
    if events is not None and rhythm_evidence is None:
        raise ValueError("Formal export with events requires independent rhythm evidence (--rhythm-evidence)")
    if rhythm_evidence is not None:
        if events is None:
            raise ValueError("Rhythm evidence requires the events source")
        rhythm = audit_rhythm(events, rhythm_evidence)
        if rhythm["status"] != "passed":
            raise ValueError(f"Rhythm evidence status is {rhythm['status']}; formal export is blocked")
    fidelity=None
    if fidelity_evidence is not None:
        if events is None:raise ValueError('Fidelity evidence requires events')
        fidelity=audit_fidelity(events,fidelity_evidence,original_events=original_events,
                                authorized_changes=authorized_changes)
        if fidelity['status']!='passed':
            raise ValueError(f"Fidelity evidence status is {fidelity['status']}; formal export is blocked")
    if review_draft and (not playability_report or not fidelity or fidelity['status']!='passed'):
        raise ValueError('Review-draft export requires a bound playability report and passing independent note evidence')
    executable = musescore or os.environ.get("MUSESCORE_BIN")
    if not executable:
        executable = next((p for n in ("MuseScore4", "mscore", "musescore") if (p := shutil.which(n))), None)
    if not executable:
        raise ValueError("MuseScore not found; pass --musescore with its executable path")
    out.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="band-export-", dir=out.parent) as temporary:
        stage = Path(temporary)
        canonical = stage / f"{name}.mscz"
        logs = []
        canonical_source_action = "copied" if source.suffix.lower() == ".mscz" else "converted"
        if canonical_source_action == "copied":
            # Preserve the user's editable score and embedded resources byte for byte.
            shutil.copyfile(source, canonical)

        def convert(input_path, output_path):
            command = [str(executable), "-o", str(output_path), str(input_path)]
            completed = subprocess.run(command, capture_output=True, timeout=timeout)
            logs.append({"format": output_path.suffix, "returncode": completed.returncode,
                         "output": (completed.stdout + completed.stderr).decode("utf-8", errors="replace")[-12000:]})
            if completed.returncode or not output_path.is_file() or output_path.stat().st_size < 16:
                raise RuntimeError(f"MuseScore export failed: {logs[-1]}")
        if canonical_source_action == "converted":
            convert(source, canonical)
        root = load_score(canonical)
        dead_report=apply_native_dead_notes(root,events) if events is not None else None
        if dead_report and dead_report['muted_notes']:
            _replace_native_xml(canonical,root)
        if not root.findall("Score/Part"):
            raise ValueError("Exported MSCZ contains no instruments")
        if playability_report is not None or rhythm_evidence is not None:
            # Rhythm depends on events/evidence, already checked in this invocation.
            # Keep native structure and report bindings checked against the actual MSCZ.
            post_audit = audit_score(root, playability_report=None if review_draft else playability_report,
                                     events=events, config=config)
            if post_audit["errors"]:
                raise ValueError(f"Exported score failed audit: {post_audit['errors']}")
        for suffix in (".pdf", ".mid"):
            convert(canonical, stage / f"{name}{suffix}")
        for suffix, magic in ((".pdf", b"%PDF-"), (".mid", b"MThd")):
            if not (stage / f"{name}{suffix}").read_bytes().startswith(magic):
                raise ValueError(f"Invalid exported {suffix} file")
        # This proves common provenance, not PDF readability or musical accuracy.
        hashes = {p.name: hashlib.sha256(p.read_bytes()).hexdigest() for p in stage.iterdir() if p.is_file()}
        report = {"source": str(source), "files_sha256": hashes, "exports": logs,
                  "canonical_source_action": canonical_source_action,
                  "musical_accuracy_verified": False,
                  "fidelity_report":fidelity,"dead_note_playback":dead_report,
                  "release_status":"review_draft" if review_draft else "exported",
                  "review_limitations":review_draft,
                  "rhythm_report": rhythm, "rhythm_checks_passed": rhythm["status"] == "passed" if rhythm else None}
        if playability_report is not None:
            report["playability_report"] = {
                "status": playability_report["status"],
                "events_sha256": playability_report["events_sha256"],
                "config_sha256": playability_report.get("config_sha256"),
            }
        (stage / "export-manifest.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
        # The fresh directory is published only after all three exports are valid files.
        stage.rename(out)
    return {"out_dir": str(out), "files": list(hashes)}


def main():
    sys.stdout.reconfigure(encoding="utf-8")
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=["audit", "export"])
    parser.add_argument("--score", required=True, type=Path)
    parser.add_argument("--report", type=Path)
    parser.add_argument("--expected-parts", type=int, default=5)
    parser.add_argument("--max-fret-span", type=int, default=4)
    parser.add_argument("--out-dir", type=Path)
    parser.add_argument("--name", default="band-score")
    parser.add_argument("--musescore", type=Path)
    parser.add_argument("--timeout", type=int, default=240)
    parser.add_argument("--playability-report", type=Path)
    parser.add_argument("--events", type=Path,
                        help="Compact events JSON used to verify a playability report")
    parser.add_argument("--config", type=Path,
                        help="Playability config JSON used to verify a playability report")
    parser.add_argument("--rhythm-evidence", type=Path,
                        help="Independent tempo/onset/duration evidence; required for export with --events")
    parser.add_argument('--fidelity-evidence',type=Path,help='Independent note-level evidence, required by the high-fidelity workflow')
    parser.add_argument('--original-events',type=Path)
    parser.add_argument('--authorized-changes',type=Path)
    parser.add_argument('--review-draft',help='Explicit limitations for a reference-TAB review draft; does not mark playability passed')
    args = parser.parse_args()
    rhythm_evidence = json.loads(args.rhythm_evidence.read_text(encoding="utf-8")) if args.rhythm_evidence else None
    events = json.loads(args.events.read_text(encoding="utf-8")) if args.events else None
    config = json.loads(args.config.read_text(encoding="utf-8")) if args.config else None
    fidelity_evidence=json.loads(args.fidelity_evidence.read_text(encoding='utf-8')) if args.fidelity_evidence else None
    original_events=json.loads(args.original_events.read_text(encoding='utf-8')) if args.original_events else None
    authorized_changes=json.loads(args.authorized_changes.read_text(encoding='utf-8')) if args.authorized_changes else None
    if args.command == "export":
        if not args.out_dir:
            parser.error("export requires --out-dir")
        playability_report = None
        if args.playability_report:
            playability_report = json.loads(args.playability_report.read_text(encoding="utf-8"))
        print(json.dumps(export_score(args.score, args.out_dir, args.name, args.musescore, args.timeout,
                                      playability_report, events, config, rhythm_evidence,
                                      fidelity_evidence,original_events,authorized_changes,args.review_draft), ensure_ascii=False))
        return 0
    playability_report = None
    if args.playability_report:
        playability_report = json.loads(args.playability_report.read_text(encoding="utf-8"))
    result = audit_score(load_score(args.score), args.expected_parts, args.max_fret_span,
                         playability_report, events, config, rhythm_evidence,fidelity_evidence)
    content = json.dumps(result, ensure_ascii=False, indent=2)
    if args.report:
        args.report.parent.mkdir(parents=True, exist_ok=True)
        args.report.write_text(content, encoding="utf-8")
    print(content)
    return 0 if result["structural_checks_passed"] else 2


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (OSError, ValueError, RuntimeError, subprocess.TimeoutExpired, ET.ParseError, zipfile.BadZipFile) as exc:
        print(str(exc), file=sys.stderr)
        raise SystemExit(2)
