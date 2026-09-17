"""Regression contract for independent, note-level musical evidence.

All pitches are sounding MIDI pitches (including octave); offsets/durations are
quarter-note units. Required phrases are declared separately from observations.
The synthetic reference represents external evidence, never arranged events.
"""
import copy
import hashlib
import importlib
import json
import unittest

from rhythm_audit import audit_rhythm


def snapshot_hash(value):
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True,
                                    separators=(",", ":")).encode("utf-8")).hexdigest()


def score():
    return {"version": 1, "title": "Independent riff fixture", "time": [4, 4],
            "tempo": 160, "bars": [{}, {}], "tracks": [
                {"bar": 1, "part": "lead", "events": [
                    [0, 64, .25], [.25, 64, .25], [.5, 67, .5], [1, 69, 1]]}]}


def evidence():
    return {"version": 1,
            "provenance": {"kind": "reference_score", "artifact_sha256": "a" * 64},
            "required_phrases": [
                {"id": "opening-riff", "part": "lead", "start": [1, 0], "end": [1, 2]}],
            "phrases": [{"id": "opening-riff", "part": "lead", "start": [1, 0],
                         "end": [1, 2], "notes": [
                             [0, 64, .25], [.25, 64, .25], [.5, 67, .5], [1, 69, 1]],
                         "source_kind": "reference_score",
                         "source_ref": "external reference PDF, page 1, bar 1"}]}


def legacy_timing_proof(notes):
    """The old loophole: a candidate can write its own passing timing answer."""
    return {"version": 1, "anchors": [
        {"at": [1, 0], "seconds": 0, "source": "measured mix transient"}],
        "phrases": [{"id": "opening-riff", "parts": ["lead"], "start": [1, 0],
                     "end": [1, 2], "onsets": [n[0] for n in notes],
                     "durations": [n[2] for n in notes],
                     "source": "independent onset evidence"}]}


class FidelityAuditTests(unittest.TestCase):
    def test_invalid_evidence_shapes_are_rejected_before_comparison(self):
        data = score()
        for invalid in (None, [], "bad"):
            with self.subTest(invalid=invalid), self.assertRaisesRegex(ValueError, "evidence"):
                self.audit(data, invalid)
        with self.assertRaisesRegex(ValueError, "Authorized changes"):
            self.audit(data, evidence(), authorized_changes=["not", "a", "dict"])

    def test_declared_unrepresented_technique_is_not_complete_reference_match(self):
        proof=evidence();proof['known_differences']=[{'part':'lead','reason':'continuous bend rendered as target notes'}]
        result=self.audit(score(),proof)
        self.assertEqual(result['status'],'passed')
        self.assertFalse(result['reference_match'])
        self.assertTrue(result['known_differences'])

    def test_deleted_dead_stroke_cannot_pass_note_level_gate(self):
        data=score();proof=evidence()
        proof['phrases'][0]['dead_strokes']=[[.75,[4],.25]]
        self.assertIn('dead_stroke_mismatch',self.issue_codes(self.audit(data,proof)))
        data['tracks'][0]['events'].append([.75,None,.25,[[4,0]],{'dead':True}])
        self.assertEqual(self.audit(data,proof)['status'],'passed')

    def test_phrase_cannot_hide_arranged_lineage(self):
        proof=evidence();proof['phrases'][0]['derived_from_events_sha256']='b'*64
        self.assertNotEqual(self.audit(score(),proof)['status'],'passed')

    def test_unknown_part_or_evidence_kind_is_not_verified_silence(self):
        for change in ('part','kind','hash'):
            proof=evidence();data=score();data['tracks']=[]
            proof['phrases'][0].update(notes=[],silence_basis='reference_score')
            if change=='part':
                proof['phrases'][0]['part']='typo';proof['required_phrases'][0]['part']='typo'
            elif change=='kind':proof['provenance']['kind']='unreviewed_guess'
            else:proof['provenance']['artifact_sha256']='not-a-hash'
            with self.subTest(change=change):self.assertNotEqual(self.audit(data,proof)['status'],'passed')

    def test_tempo_or_empty_bar_meter_change_requires_visible_adaptation(self):
        for change in ('tempo','meter'):
            original=score();arranged=copy.deepcopy(original)
            if change=='tempo':arranged['tempo']=80
            else:arranged['bars'][1]['time']=[3,4]
            result=self.audit(arranged,evidence(),original_events=original)
            self.assertIn('unauthorized_arrangement_change',self.issue_codes(result))

    def test_plain_detector_candidates_do_not_become_reviewed_truth(self):
        proof=evidence();proof['provenance']['kind']='stem_candidate'
        proof['phrases'][0]['source_kind']='stem_candidate'
        self.assertEqual(self.audit(score(),proof)['status'],'incomplete')

    def test_exact_rational_tuplet_notes_can_be_compared(self):
        data=score();data['tracks'][0]['events']=[[0,64,'1/3'],['1/3',67,'1/3']]
        proof=evidence();proof['phrases'][0]['notes']=[[0,64,'1/3'],['1/3',67,'1/3']]
        self.assertEqual(self.audit(data,proof)['status'],'passed')

    def audit(self, data, proof, **kwargs):
        try:
            module = importlib.import_module("fidelity_audit")
        except ModuleNotFoundError as exc:
            if exc.name != "fidelity_audit":
                raise
            self.fail("Missing independent note-evidence gate: fidelity_audit.audit_fidelity")
        return module.audit_fidelity(data, proof, **kwargs)

    def issue_codes(self, result):
        return {issue["code"] for issue in result["issues"]}

    def test_matching_external_phrase_passes_only_its_declared_scope(self):
        result = self.audit(score(), evidence())
        self.assertEqual(result["status"], "passed")
        self.assertTrue(result["reference_match"])
        self.assertFalse(result["musical_accuracy_verified"])

    def test_deleted_notes_cannot_write_their_own_passing_reference(self):
        data = score()
        data["tracks"][0]["events"].pop(1)
        notes = data["tracks"][0]["events"]
        self.assertEqual(audit_rhythm(data, legacy_timing_proof(notes))["status"], "passed")
        proof = evidence()
        proof["phrases"][0]["notes"] = copy.deepcopy(notes)
        proof["provenance"]["derived_from_events_sha256"] = snapshot_hash(data)
        result = self.audit(data, proof)
        self.assertNotEqual(result["status"], "passed")
        self.assertIn("circular_evidence", self.issue_codes(result))

    def test_arranged_candidate_is_not_independent_even_without_matching_hash(self):
        proof = evidence()
        proof["provenance"]["kind"] = "arranged_candidate"
        result = self.audit(score(), proof)
        self.assertNotEqual(result["status"], "passed")
        self.assertIn("circular_evidence", self.issue_codes(result))

    def test_reference_lineage_from_an_earlier_arranged_snapshot_is_rejected(self):
        proof = evidence()
        proof["provenance"]["derived_from_events_sha256"] = "b" * 64
        result = self.audit(score(), proof)
        self.assertNotEqual(result["status"], "passed")
        self.assertIn("circular_evidence", self.issue_codes(result))

    def test_octave_error_fails_even_when_all_timing_matches(self):
        data = score()
        data["tracks"][0]["events"][2][1] += 12
        self.assertEqual(audit_rhythm(data, legacy_timing_proof(
            evidence()["phrases"][0]["notes"]))["status"], "passed")
        result = self.audit(data, evidence())
        self.assertEqual(result["status"], "needs_repair")
        self.assertFalse(result["reference_match"])
        self.assertIn("pitch_mismatch", self.issue_codes(result))

    def test_equal_pitch_reattack_cannot_be_merged_into_sustain(self):
        data = score()
        data["tracks"][0]["events"][:2] = [[0, 64, .5]]
        result = self.audit(data, evidence())
        self.assertEqual(result["status"], "needs_repair")
        self.assertIn("missing_note", self.issue_codes(result))

    def test_deleted_short_riff_note_is_not_a_playability_success(self):
        data = score()
        data["tracks"][0]["events"].pop(2)
        result = self.audit(data, evidence())
        self.assertEqual(result["status"], "needs_repair")
        self.assertIn("missing_note", self.issue_codes(result))

    def test_unobserved_required_phrase_cannot_disappear_from_denominator(self):
        proof = evidence()
        proof["required_phrases"].append(
            {"id": "bass-entry", "part": "bass", "start": [2, 0], "end": [2, 2]})
        result = self.audit(score(), proof)
        self.assertEqual(result["status"], "incomplete")
        self.assertIn("uncovered_phrase", self.issue_codes(result))

    def test_same_phrase_id_cannot_cover_an_unobserved_part_or_shorter_span(self):
        for field, value in (("part", "bass"), ("end", [1, 1])):
            proof = evidence()
            proof["phrases"][0][field] = value
            if field == "end":
                proof["phrases"][0]["notes"] = proof["phrases"][0]["notes"][:3]
            with self.subTest(field=field):
                result = self.audit(score(), proof)
                self.assertNotEqual(result["status"], "passed")
                self.assertIn("uncovered_phrase", self.issue_codes(result))

    def test_empty_stem_candidate_does_not_prove_original_part_is_silent(self):
        data = score()
        data["tracks"] = []
        proof = evidence()
        proof["provenance"]["kind"] = "stem_candidate"
        proof["phrases"][0].update(notes=[], source_kind="stem_candidate",
                                    source_ref="six-source guitar candidate, below threshold",
                                    silence_basis="stem_candidate")
        result = self.audit(data, proof)
        self.assertEqual(result["status"], "incomplete")
        self.assertIn("insufficient_silence_evidence", self.issue_codes(result))

    def test_reference_confirmed_silence_can_cover_an_empty_phrase(self):
        data = score()
        data["tracks"] = []
        proof = evidence()
        proof["phrases"][0].update(notes=[], silence_basis="reference_score")
        result = self.audit(data, proof)
        self.assertEqual(result["status"], "passed")
        self.assertFalse(result["musical_accuracy_verified"])

    def test_candidate_rewrite_cannot_hide_unauthorized_deleted_note(self):
        original = score()
        arranged = copy.deepcopy(original)
        arranged["tracks"][0]["events"].pop(1)
        proof = evidence()
        proof["phrases"][0]["notes"] = copy.deepcopy(arranged["tracks"][0]["events"])
        result = self.audit(arranged, proof, original_events=original)
        self.assertEqual(result["status"], "needs_repair")
        self.assertIn("unauthorized_arrangement_change", self.issue_codes(result))

    def test_authorized_adaptation_remains_a_visible_reference_difference(self):
        original = score()
        arranged = copy.deepcopy(original)
        arranged["tracks"][0]["events"].pop(1)
        authorization = {"original_events_sha256": snapshot_hash(original),
                         "arranged_events_sha256": snapshot_hash(arranged),
                         "authorization": "user permits this specific repeated-note reduction",
                         "reason": "retain the riff pitches while reducing one repeated attack"}
        result = self.audit(arranged, evidence(), original_events=original,
                            authorized_changes=authorization)
        self.assertNotIn("unauthorized_arrangement_change", self.issue_codes(result))
        self.assertFalse(result["reference_match"])
        self.assertTrue(result["adapted"])
        self.assertFalse(result["musical_accuracy_verified"])

    def test_authorization_for_old_snapshot_does_not_allow_further_deletion(self):
        original = score()
        arranged = copy.deepcopy(original)
        arranged["tracks"][0]["events"].pop(1)
        authorization = {"original_events_sha256": snapshot_hash(original),
                         "arranged_events_sha256": snapshot_hash(arranged),
                         "authorization": "user permits one attack reduction",
                         "reason": "reduce one repeated attack"}
        arranged["tracks"][0]["events"].pop(1)
        result = self.audit(arranged, evidence(), original_events=original,
                            authorized_changes=authorization)
        self.assertIn("unauthorized_arrangement_change", self.issue_codes(result))

    def test_title_change_is_not_an_unauthorized_musical_adaptation(self):
        original = score()
        arranged = copy.deepcopy(original)
        arranged["title"] = "Rehearsal copy"
        result = self.audit(arranged, evidence(), original_events=original)
        self.assertEqual(result["status"], "passed")
        self.assertNotIn("unauthorized_arrangement_change", self.issue_codes(result))


if __name__ == "__main__":
    unittest.main()
