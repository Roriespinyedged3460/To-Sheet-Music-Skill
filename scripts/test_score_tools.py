import json
import subprocess
import tempfile
from unittest.mock import patch
import unittest
import xml.etree.ElementTree as ET
import zipfile
from pathlib import Path

from score_tools import audit_score, load_score, export_score


def fixture(pitch=64, string=0, fret=0):
    root = ET.fromstring("<museScore version='4.0'><Score/></museScore>")
    score = root.find("Score")
    groups = [("Lead", "tablature", 6), ("Rhythm", "tablature", 6),
              ("Bass", "tablature", 4), ("Keys", "pitched", 5),
              ("Drums", "percussion", 5)]
    staff_id = 1
    for name, group, lines in groups:
        part = ET.SubElement(score, "Part")
        ET.SubElement(part, "trackName").text = name
        instrument = ET.SubElement(part, "Instrument")
        if group == "tablature":
            data = ET.SubElement(instrument, "StringData")
            tuning = [40, 45, 50, 55, 59, 64] if lines == 6 else [28, 33, 38, 43]
            for p in tuning:
                ET.SubElement(data, "string").text = str(p)
        for _ in range(2 if name == "Keys" else 1):
            definition = ET.SubElement(part, "Staff", id=str(staff_id))
            kind = ET.SubElement(definition, "StaffType", group=group)
            ET.SubElement(kind, "lines").text = str(lines)
            staff = ET.SubElement(score, "Staff", id=str(staff_id))
            voice = ET.SubElement(ET.SubElement(staff, "Measure"), "voice")
            if staff_id == 1:
                chord = ET.SubElement(voice, "Chord")
                note = ET.SubElement(chord, "Note")
                for tag, value in (("pitch", pitch), ("string", string), ("fret", fret)):
                    ET.SubElement(note, tag).text = str(value)
            else:
                ET.SubElement(voice, "Rest")
            staff_id += 1
    return root


class AuditTests(unittest.TestCase):
    def test_export_refuses_existing_output_directory(self):
        with tempfile.TemporaryDirectory() as folder:
            with self.assertRaises(FileExistsError):
                export_score("unused.mscx", folder, "band", "unused.exe")

    def test_export_rejects_path_traversal_in_name(self):
        with self.assertRaises(ValueError):
            export_score("unused.mscx", "unused-output", "../source", "unused.exe")

    def test_valid_high_string_is_zero_in_mscx(self):
        report = audit_score(fixture())
        self.assertEqual(report["errors"], [])
        self.assertFalse(report["musical_accuracy_verified"])
        self.assertEqual(len(report["score_native_sha256"]), 64)

    def test_wrong_fret_cannot_pass(self):
        report = audit_score(fixture(pitch=64, fret=2))
        self.assertIn("tab_pitch_mismatch", {v["code"] for v in report["errors"]})

    def test_open_string_zero_fingering_is_valid(self):
        root = fixture()
        ET.SubElement(root.find("Score/Staff/Measure/voice/Chord/Note"), "fingering").text = "0"
        self.assertNotIn("open_string_fingering", {v["code"] for v in audit_score(root)["errors"]})

    def test_native_musescore_nested_fingering_is_valid(self):
        for number in (1, 2, 3, 4):
            with self.subTest(number=number):
                root = fixture(pitch=65, fret=1)
                note = root.find("Score/Staff/Measure/voice/Chord/Note")
                finger = ET.SubElement(note, "Fingering")
                ET.SubElement(finger, "eid").text = "native-id"
                ET.SubElement(finger, "text").text = str(number)
                self.assertEqual(audit_score(root)["errors"], [])

    def test_native_musescore_nested_invalid_fingering_cannot_pass(self):
        for value in ("5", "x", ""):
            with self.subTest(value=value):
                root = fixture(pitch=65, fret=1)
                note = root.find("Score/Staff/Measure/voice/Chord/Note")
                ET.SubElement(ET.SubElement(note, "Fingering"), "text").text = value
                self.assertIn("invalid_fingering",
                              {v["code"] for v in audit_score(root)["errors"]})

    def test_native_musescore_nested_fingering_checks_open_string(self):
        root = fixture()
        note = root.find("Score/Staff/Measure/voice/Chord/Note")
        ET.SubElement(ET.SubElement(note, "Fingering"), "text").text = "1"
        self.assertIn("open_string_fingering",
                      {v["code"] for v in audit_score(root)["errors"]})

    def test_out_of_range_string_cannot_pass(self):
        report = audit_score(fixture(string=6))
        self.assertIn("invalid_tab", {v["code"] for v in report["errors"]})

    def test_misaligned_measure_counts_cannot_pass(self):
        root = fixture()
        ET.SubElement(root.find("Score/Staff"), "Measure")
        self.assertIn("measure_count_mismatch", {v["code"] for v in audit_score(root)["errors"]})

    def test_two_notes_on_same_string_cannot_pass(self):
        root = fixture()
        chord = root.find("Score/Staff/Measure/voice/Chord")
        chord.append(ET.fromstring("<Note><pitch>65</pitch><string>0</string><fret>1</fret></Note>"))
        self.assertIn("duplicate_string", {v["code"] for v in audit_score(root)["errors"]})

    def test_compressed_score_uses_declared_rootfile(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "中文 score.mscz"
            with zipfile.ZipFile(path, "w") as archive:
                archive.writestr("excerpt.mscx", "<invalid/>")
                archive.writestr("META-INF/container.xml", '<container><rootfiles><rootfile full-path="main.mscx"/></rootfiles></container>')
                archive.writestr("main.mscx", ET.tostring(fixture()))
            self.assertEqual(len(load_score(path).findall("Score/Part")), 5)

    def test_playability_report_blocks_unresolved_score(self):
        report = {"version": 1, "status": "needs_repair", "events_sha256": "stale"}
        audited = audit_score(fixture(), playability_report=report)
        self.assertIn("playability_not_passed", {v["code"] for v in audited["errors"]})
        self.assertFalse(audited["playability_checks_passed"])

    def test_playability_report_is_exposed_when_clean(self):
        report = {"version": 1, "status": "passed", "events_sha256": "events"}
        audited = audit_score(fixture(), playability_report=report)
        self.assertTrue(audited["playability_checks_passed"])

    def test_stale_events_are_rejected_when_binding_is_requested(self):
        report = {"version": 1, "status": "passed", "events_sha256": "events"}
        with self.assertRaisesRegex(ValueError, "events source"):
            audit_score(fixture(), playability_report=report, events={"version": 1})

    def test_inconsistent_report_config_hash_is_rejected(self):
        report = {"version": 1, "status": "passed", "events_sha256": "events",
                  "config_sha256": "bad", "parameters": {"version": 1}}
        with self.assertRaisesRegex(ValueError, "inconsistent playability config"):
            audit_score(fixture(), playability_report=report)

    def test_export_blocks_a_nonpassed_playability_report_before_running_musescore(self):
        with tempfile.TemporaryDirectory() as folder:
            source = Path(folder) / "score.mscx"
            source.write_text("<museScore><Score/></museScore>", encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "formal export is blocked"):
                export_score(source, Path(folder) / "out", "band", "unused.exe",
                             playability_report={"version": 1, "status": "needs_repair",
                                                 "events_sha256": "events"})


class RhythmExportTests(unittest.TestCase):
    def test_review_export_cannot_disguise_an_unresolved_action_report(self):
        with tempfile.TemporaryDirectory() as folder:
            source=Path(folder)/'score.musicxml';source.write_text('<score-partwise/>')
            with self.assertRaisesRegex(ValueError,'formal export is blocked'):
                export_score(source,Path(folder)/'out','band','unused',
                    playability_report={'version':1,'status':'needs_repair','events_sha256':'x'},review_draft='review requested')

    def test_circular_fidelity_evidence_blocks_before_conversion(self):
        from test_fidelity_audit import score,evidence
        proof=evidence();proof['provenance']['kind']='generated_score'
        with tempfile.TemporaryDirectory() as folder:
            source=Path(folder)/'score.musicxml';source.write_text('<score-partwise/>')
            with patch('score_tools.audit_rhythm',return_value={'status':'passed'}),patch('score_tools.subprocess.run') as run:
                with self.assertRaisesRegex(ValueError,'Fidelity'):
                    export_score(source,Path(folder)/'out','band','unused',events=score(),
                                 rhythm_evidence={},fidelity_evidence=proof)
                run.assert_not_called()

    def test_formal_export_requires_independent_rhythm_evidence(self):
        with tempfile.TemporaryDirectory() as folder:
            source = Path(folder) / "score.mscx"
            source.write_text("<museScore><Score/></museScore>", encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "rhythm evidence"):
                export_score(source, Path(folder) / "out", "band", "unused.exe",
                             events={"version": 1})

    def test_failed_rhythm_blocks_export_before_musescore(self):
        from test_rhythm_audit import score, evidence
        data = score()
        data["tracks"][0]["events"][0][0] = .125
        with tempfile.TemporaryDirectory() as folder:
            source = Path(folder) / "score.mscx"
            source.write_text("<museScore><Score/></museScore>", encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "Rhythm evidence status"):
                export_score(source, Path(folder) / "out", "band", "unused.exe",
                             events=data, rhythm_evidence=evidence())

    def test_audit_missing_evidence_is_explicitly_unverified(self):
        result = audit_score(fixture())
        self.assertIsNone(result["rhythm_checks_passed"])

    def test_audit_exposes_uncovered_rhythm(self):
        from test_rhythm_audit import score
        result = audit_score(fixture(), events=score(), rhythm_evidence={"version": 1})
        self.assertFalse(result["rhythm_checks_passed"])
        self.assertIn("rhythm_not_passed", {v["code"] for v in result["errors"]})


class ExportEfficiencyTests(unittest.TestCase):
    @staticmethod
    def write_native(path):
        with zipfile.ZipFile(path, "w") as archive:
            archive.writestr("main.mscx", ET.tostring(fixture()))
            archive.writestr("Pictures/keep.bin", b"embedded resource")

    def fake_musescore(self, command, **kwargs):
        output = Path(command[2])
        if output.suffix == ".mscz":
            self.write_native(output)
        else:
            # Both renderings must consume the canonical delivered score.
            self.assertEqual(Path(command[3]).suffix, ".mscz")
            load_score(command[3])
            magic = b"%PDF-" if output.suffix == ".pdf" else b"MThd"
            output.write_bytes(magic + b"fixture payload" * 2)
        return subprocess.CompletedProcess(command, 0, b"", b"")

    def test_native_source_preserves_resources_without_resaving(self):
        with tempfile.TemporaryDirectory() as folder:
            source = Path(folder) / "input.mscz"
            self.write_native(source)
            original = source.read_bytes()
            out = Path(folder) / "out"
            with patch("score_tools.subprocess.run", side_effect=self.fake_musescore) as run:
                export_score(source, out, "band", "MuseScore")
            self.assertEqual(run.call_count, 2)
            self.assertEqual((out / "band.mscz").read_bytes(), original)
            self.assertEqual(source.read_bytes(), original)
            manifest = json.loads((out / "export-manifest.json").read_text(encoding="utf-8"))
            self.assertEqual(manifest["canonical_source_action"], "copied")
            self.assertEqual(len(manifest["files_sha256"]), 3)

    def test_xml_imports_once_and_checks_rhythm_once(self):
        from rhythm_audit import audit_rhythm
        from test_rhythm_audit import score, evidence
        with tempfile.TemporaryDirectory() as folder:
            source = Path(folder) / "input.musicxml"
            source.write_text("<score-partwise/>", encoding="utf-8")
            out = Path(folder) / "out"
            with patch("score_tools.subprocess.run", side_effect=self.fake_musescore) as run, \
                    patch("score_tools.audit_rhythm", wraps=audit_rhythm) as rhythm:
                export_score(source, out, "band", "MuseScore",
                             events=score(), rhythm_evidence=evidence())
            self.assertEqual(run.call_count, 3)
            self.assertEqual(rhythm.call_count, 1)
            manifest = json.loads((out / "export-manifest.json").read_text(encoding="utf-8"))
            self.assertTrue(manifest["rhythm_checks_passed"])
            self.assertEqual(manifest["canonical_source_action"], "converted")

    def test_failed_render_does_not_publish_or_modify_native_source(self):
        with tempfile.TemporaryDirectory() as folder:
            source = Path(folder) / "input.mscz"
            self.write_native(source)
            original = source.read_bytes()
            out = Path(folder) / "out"
            failed = subprocess.CompletedProcess([], 1, b"", b"render failed")
            with patch("score_tools.subprocess.run", return_value=failed):
                with self.assertRaisesRegex(RuntimeError, "export failed"):
                    export_score(source, out, "band", "MuseScore")
            self.assertFalse(out.exists())
            self.assertEqual(source.read_bytes(), original)
            self.assertEqual(sorted(p.name for p in Path(folder).iterdir()), ["input.mscz"])

    def test_native_structure_is_still_audited(self):
        from test_rhythm_audit import score, evidence
        with tempfile.TemporaryDirectory() as folder:
            source = Path(folder) / "input.mscz"
            with zipfile.ZipFile(source, "w") as archive:
                archive.writestr("main.mscx", ET.tostring(fixture(fret=2)))
            out = Path(folder) / "out"
            with patch("score_tools.subprocess.run", side_effect=self.fake_musescore):
                with self.assertRaisesRegex(ValueError, "tab_pitch_mismatch"):
                    export_score(source, out, "band", "MuseScore",
                                 events=score(), rhythm_evidence=evidence())
            self.assertFalse(out.exists())


if __name__ == "__main__":
    unittest.main()
