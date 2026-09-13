import copy
import unittest
from rhythm_audit import audit_rhythm, summarize_coverage


def score():
    return {"version": 1, "title": "test", "time": [4, 4], "tempo": 80,
            "bars": [{}, {}], "tracks": [
                {"bar": 1, "part": "lead", "events": [[i / 4, 60, .25] for i in range(16)]}]}


def evidence():
    return {"version": 1, "anchors": [
        {"at": [1, 0], "seconds": 0, "source": "measured drum transient"},
        {"at": [2, 0], "seconds": 3, "source": "measured drum transient"}],
        "phrases": [{"id": "run", "parts": ["lead"], "start": [1, 0], "end": [2, 0],
                     "onsets": [i / 4 for i in range(16)], "durations": [.25] * 16,
                     "source": "independent reference transcription"}]}


class RhythmAuditTests(unittest.TestCase):
    def test_continuous_run_passes_without_claiming_audio_accuracy(self):
        result = audit_rhythm(score(), evidence())
        self.assertEqual(result["status"], "passed")
        self.assertFalse(result["musical_accuracy_verified"])

    def test_missing_onset_and_micro_rests_fail(self):
        data = score()
        data["tracks"][0]["events"].pop(3)
        data["tracks"][0]["events"][0] = [.125, 60, .125]
        result = audit_rhythm(data, evidence())
        self.assertEqual(result["status"], "needs_repair")
        self.assertTrue(result["phrases"][0]["missing_onsets"])

    def test_shortening_without_onset_changes_is_caught(self):
        data = score()
        data["tracks"][0]["events"][4][2] = .125
        result = audit_rhythm(data, evidence())
        self.assertEqual(result["status"], "needs_repair")
        self.assertEqual(len(result["phrases"][0]["duration_mismatches"]), 1)

    def test_held_note_is_not_repeated_attack(self):
        data = score()
        data["tracks"][0]["events"] = [[0, 60, 4]]
        self.assertEqual(len(audit_rhythm(data, evidence())["phrases"][0]["missing_onsets"]), 15)

    def test_cross_part_handoff_is_not_missing(self):
        data = score()
        data["tracks"].append({"bar": 1, "part": "rhythm",
                               "events": data["tracks"][0]["events"][1::2]})
        data["tracks"][0]["events"] = data["tracks"][0]["events"][::2]
        proof = evidence()
        proof["phrases"][0]["parts"].append("rhythm")
        self.assertEqual(audit_rhythm(data, proof)["status"], "passed")

    def test_real_thirty_second_rests_are_allowed_when_evidenced(self):
        data = score()
        data["tracks"][0]["events"] = [[.125, 60, .125], [.5, 60, .25]]
        proof = evidence()
        proof["phrases"][0].update(onsets=[.125, .5], durations=[.125, .25])
        self.assertEqual(audit_rhythm(data, proof)["status"], "passed")

    def test_no_evidence_never_passes(self):
        self.assertEqual(audit_rhythm(score(), {"version": 1})["status"], "incomplete")

    def test_empty_detection_is_not_confirmed_silence(self):
        data = score()
        data["tracks"] = []
        proof = evidence()
        proof["phrases"][0].update(onsets=[], durations=[])
        self.assertEqual(audit_rhythm(data, proof)["status"], "incomplete")
        proof["phrases"][0]["silence_confirmed"] = True
        self.assertEqual(audit_rhythm(data, proof)["status"], "passed")

    def test_uncovered_phrase_never_passes(self):
        proof = evidence()
        proof["phrases"][0]["onsets"] = None
        self.assertEqual(audit_rhythm(score(), proof)["status"], "incomplete")

    def test_tempo_mismatch_is_independent_of_same_note_count(self):
        data = score()
        data["tempo"] = 77
        self.assertEqual(audit_rhythm(data, evidence())["status"], "needs_repair")

    def test_pickup_and_tempo_change_are_integrated(self):
        data = score()
        data["bars"] = [{"length": 1}, {"tempo": 60}]
        data["tracks"] = [{"bar": 2, "part": "lead", "events": [[0, 60, 1]]}]
        proof = {"version": 1, "anchors": [
            {"at": [2, 0], "seconds": .75, "source": "audio"},
            {"at": [2, 1], "seconds": 1.75, "source": "audio"}],
            "phrases": [{"id": "entry", "parts": ["lead"], "start": [2, 0], "end": [2, 1],
                         "onsets": [0], "durations": [1], "source": "audio"}]}
        self.assertEqual(audit_rhythm(data, proof)["status"], "passed")

    def test_nonfinite_or_negative_evidence_is_rejected(self):
        for value in [float("nan"), float("inf"), -1]:
            proof = evidence()
            proof["anchors"][0]["seconds"] = value
            with self.subTest(value=value), self.assertRaises(ValueError):
                audit_rhythm(score(), proof)

    def test_duplicate_reference_onsets_are_rejected(self):
        proof = evidence()
        proof["phrases"][0]["onsets"][1] = 0
        with self.assertRaises(ValueError):
            audit_rhythm(score(), proof)

    def test_zero_signal_coverage_is_incomplete_not_zero_errors(self):
        result = summarize_coverage([{"part": "lead", "bar": 9, "status": "insufficient_evidence"}])
        self.assertEqual(result["status"], "incomplete")
        self.assertEqual(result["checked"], 0)
        self.assertEqual(result["required"], 1)

    def test_signal_mismatch_is_not_hidden_by_other_missing_evidence(self):
        result = summarize_coverage([{"part": "lead", "bar": 8, "status": "mismatch"},
                                     {"part": "lead", "bar": 9, "status": "insufficient_evidence"}])
        self.assertEqual(result["status"], "needs_repair")


if __name__ == "__main__":
    unittest.main()
