import copy
import tempfile
import unittest
import xml.etree.ElementTree as ET
from pathlib import Path
from unittest.mock import patch

import score_events
from score_events import (TUNINGS, _plan_phrase, _transition_cost, build_score,
                          positions, validate_score, write_score)


def fixture():
    return {"version": 1, "title": "测试", "tempo": 80, "time": [4, 4], "key": -1,
            "bars": [{}, {}], "tracks": [
                {"bar": 1, "part": "bass", "events": [[1, 48, 1], [3, 50, 2]]},
                {"bar": 1, "part": "keys_rh", "events": [[0, [60, 64, 67], 4]]},
                {"bar": 1, "part": "drums", "events": [[0, [36, 42], 0.5]]}]}


class ScoreEventsTests(unittest.TestCase):
    def test_dead_note_has_explicit_unpitched_semantics_and_tab_cross(self):
        data = fixture()
        data["tracks"] = [{"bar": 1, "part": "lead", "events": [
            [0, None, .25, [[6, 0], [5, 0]], {"dead": True}], [.25, 45, .25]]}]
        original = copy.deepcopy(data)
        root, report = build_score(data, return_report=True)
        notes = root.findall("part[@id='P1']/measure/note[notehead]")
        self.assertEqual(len(notes), 2)
        self.assertEqual([n.findtext("notehead") for n in notes], ["x", "x"])
        self.assertEqual([n.findtext("notations/technical/string") for n in notes], ["6", "5"])
        self.assertEqual([n.findtext("notations/technical/other-technical") for n in notes],
                         ["dead-note", "dead-note"])
        self.assertEqual(report["status"], "incomplete")
        self.assertIn("dead_note_special_technique", report["unverified"])
        self.assertEqual(data, original)

    def test_dead_event_requires_no_melodic_pitch_and_explicit_distinct_strings(self):
        for event in [[0, 40, .25, [[6, 0]], {"dead": True}],
                      [0, None, .25, None, {"dead": True}],
                      [0, None, .25, [[6, 0], [6, 3]], {"dead": True}],
                      [0, None, .25, [[7, 0]], {"dead": True}],
                      [0, None, .25, [[6, 0]], {"dead": "true"}]]:
            data = fixture()
            data["tracks"] = [{"bar": 1, "part": "lead", "events": [event]}]
            with self.subTest(event=event), self.assertRaises(ValueError):
                build_score(data)

    def test_three_eighths_in_a_quarter_preserve_rests_and_actual_time(self):
        data = fixture()
        data["bars"] = [{}]
        data["tracks"] = [{"bar": 1, "part": "keys_rh", "events": [
            [str(__import__('fractions').Fraction(i, 3)), None if i % 3 == 1 else 60,
             "1/3", None, {"tuplet": [3, 2]}] for i in range(12)]}]
        root = build_score(data)
        part = root.find("part[@id='P4']")
        notes = part.findall("measure/note[voice='1']")
        divisions = int(part.findtext("measure/attributes/divisions"))
        self.assertEqual(len(notes), 12)
        self.assertEqual(sum(int(n.findtext("duration")) for n in notes), 4 * divisions)
        self.assertEqual([n.findtext("type") for n in notes], ["eighth"] * 12)
        self.assertEqual([n.findtext("time-modification/actual-notes") for n in notes], ["3"] * 12)
        self.assertEqual([n.findtext("time-modification/normal-notes") for n in notes], ["2"] * 12)
        self.assertEqual(len([n for n in notes if n.find("rest") is not None]), 4)

    def test_triplets_need_explicit_ratio_and_complete_groups(self):
        for events in [[[0, 60, "1/3"]],
                       [[0, 60, "1/3", None, {"tuplet": [5, 4]}]],
                       [[0, 60, "1/3", None, {"tuplet": [3, 2]}]]]:
            data = fixture()
            data["tracks"] = [{"bar": 1, "part": "keys_rh", "events": events}]
            with self.subTest(events=events), self.assertRaises(ValueError):
                build_score(data)

    def test_five_parts_six_staves_rest_gaps_and_cross_bar_tie(self):
        root = build_score(fixture())
        self.assertEqual(len(root.findall("part")), 5)
        self.assertEqual(len(root.findall("defaults/page-layout/page-margins/*")), 4)
        self.assertEqual(root.findtext("part[@id='P4']/measure/attributes/staves"), "2")
        bass = root.find("part[@id='P3']")
        self.assertEqual(len(bass.findall("measure")), 2)
        self.assertIsNotNone(bass.find("measure/note/rest"))
        self.assertEqual(len(bass.findall(".//tie[@type='start']")), 1)
        self.assertEqual(len(bass.findall(".//tie[@type='stop']")), 1)
        for n in bass.findall(".//note[pitch]"):
            string = int(n.findtext("notations/technical/string"))
            fret = int(n.findtext("notations/technical/fret"))
            self.assertIn([43, 38, 33, 28][string - 1] + fret, [48, 50])

    def test_drums_have_explicit_gm_and_simultaneous_hits(self):
        root = build_score(fixture())
        self.assertEqual(root.findtext("part-list/score-part[@id='P5']/midi-instrument[@id='P5-I36']/midi-unpitched"), "37")
        self.assertIsNotNone(root.find("part[@id='P5']/measure/note/chord"))

    def test_rejects_bad_input_instead_of_silent_correction(self):
        for event in [[0, 1, 1], [0, 128, 1], [-1, 48, 1], [0, 48, 0],
                      [0, 48, 20], [0, 48, 0.13], [0, True, 1]]:
            data = fixture()
            data["tracks"][0]["events"] = [event]
            with self.subTest(event=event), self.assertRaises(ValueError):
                build_score(data)

    def test_overlap_unknown_fields_and_impossible_tab_chord_fail(self):
        data = fixture()
        data["tracks"][0]["events"] = [[0, 48, 2], [1, 50, 1]]
        with self.assertRaisesRegex(ValueError, "overlap"):
            validate_score(data)
        data = fixture()
        data["tracks"][0]["typo"] = True
        with self.assertRaises(ValueError):
            validate_score(data)
        data = fixture()
        data["tracks"][0]["events"] = [[0, [28, 29], 1]]
        with self.assertRaisesRegex(ValueError, "TAB"):
            build_score(data)

    def test_pickup_meter_tempo_and_dotted_notes(self):
        data = fixture()
        data["bars"] = [{"length": 1}, {"time": [3, 4], "tempo": 90, "section": "B"}]
        data["tracks"] = [{"bar": 2, "part": "bass", "events": [[0, 48, 1.5]]}]
        root = build_score(data)
        bars = root.findall("part[@id='P3']/measure")
        self.assertEqual(bars[0].get("implicit"), "yes")
        self.assertEqual(bars[1].findtext("attributes/time/beats"), "3")
        self.assertIsNotNone(bars[1].find("note/dot"))
        self.assertEqual(root.find("part/measure[@number='2']/direction/sound").get("tempo"), "90.0")

    def test_layout_breaks_are_explicit_without_raw_xml(self):
        data = fixture()
        data["bars"][1] = {"new_system": True, "new_page": True}
        root = build_score(data)
        self.assertEqual(root.find("part/measure[@number='2']/print").get("new-page"), "yes")
        data["bars"][1]["new_page"] = "true"
        with self.assertRaises(ValueError):
            build_score(data)

    def test_deterministic_serialization_and_existing_file_protected(self):
        data = fixture()
        original = copy.deepcopy(data)
        self.assertEqual(ET.tostring(build_score(data)), ET.tostring(build_score(data)))
        self.assertEqual(data, original)
        with tempfile.TemporaryDirectory() as tmp:
            p = Path(tmp) / "score.musicxml"
            write_score(data, p)
            before = p.read_bytes()
            with self.assertRaises(FileExistsError):
                write_score(data, p)
            self.assertEqual(p.read_bytes(), before)


class PhraseFingeringTests(unittest.TestCase):
    def score(self, events, bars=None):
        data = fixture()
        data["bars"] = bars or [{}]
        data["tracks"] = [{"bar": 1, "part": "lead", "events": events}]
        return data

    def tabs(self, data):
        return [e[3][0] for e in validate_score(data)[3]["lead"] if e[2]]

    def test_opening_prepares_second_string_for_high_phrase(self):
        data = self.score([[i / 2, p, 0.5] for i, p in enumerate([69, 67, 69, 72, 74, 69])])
        tabs = self.tabs(data)
        self.assertEqual(tabs[:3], [(2, 10), (2, 8), (2, 10)])
        self.assertLessEqual(max(f for _, f in tabs) - min(f for _, f in tabs), 3)

    def test_looks_across_bar_line_and_keeps_cross_bar_tie(self):
        data = self.score([[3.5, 69, 1]], [{}, {}])
        data["tracks"].append({"bar": 2, "part": "lead",
                               "events": [[0.5, 67, 0.5], [1, 72, 0.5], [1.5, 74, 0.5]]})
        self.assertEqual(self.tabs(data)[0], (2, 10))
        notes = build_score(data).findall("part[@id='P1']/measure/note[pitch]")
        self.assertEqual(notes[0].findtext("notations/technical/string"), "2")
        self.assertEqual(notes[1].findtext("notations/technical/string"), "2")
        self.assertIsNotNone(notes[0].find("tie[@type='start']"))
        self.assertIsNotNone(notes[1].find("tie[@type='stop']"))

    def test_future_manual_fingering_guides_earlier_notes(self):
        data = self.score([[0, 69, 0.5], [0.5, 67, 0.5], [1, 74, 0.5, [[1, 10]]]])
        self.assertEqual(self.tabs(data), [(2, 10), (2, 8), (1, 10)])

    def test_overrides_tuning_pitch_and_input_are_preserved(self):
        data = self.score([[0, 67, 0.5, [[2, 10]]], [0.5, 65, 0.5], [1, 72, 1]])
        data["tunings"] = {"lead": [38, 43, 48, 53, 57, 62]}
        original = copy.deepcopy(data)
        tabs = self.tabs(data)
        self.assertEqual(tabs[0], (2, 10))
        for event, (string, fret) in zip(data["tracks"][0]["events"], tabs):
            self.assertEqual(data["tunings"]["lead"][-string] + fret, event[1])
        self.assertEqual(data, original)

    def test_explicit_and_implicit_long_rests_keep_same_connection(self):
        first = self.score([[0, 69, 0.5]])
        after = [[2, 84, 0.5], [2.5, 81, 0.5]]
        implicit = self.score(first["tracks"][0]["events"] + after)
        explicit = copy.deepcopy(implicit)
        explicit["tracks"][0]["events"].insert(1, [0.5, None, 1.5])
        self.assertEqual(self.tabs(implicit), self.tabs(explicit))

    def test_long_rest_connects_to_next_fixed_position(self):
        data = self.score([[0, 69, 0.5], [2, 74, 0.5, [[1, 10]]]])
        self.assertEqual(self.tabs(data), [(2, 10), (1, 10)])

    def test_movement_time_starts_at_fretted_note_release(self):
        seconds_at = lambda tick: tick / 8
        held = (0, 32, [69], ((1, 5),))
        self.assertEqual(score_events._movement_seconds((1, 5, 5, False), held, 36, seconds_at), 0.5)
        self.assertEqual(score_events._movement_seconds((1, 5, 5, False), held, 32, seconds_at), 0)
        open_note = (0, 32, [64], ((1, 0),))
        self.assertEqual(score_events._movement_seconds((1, 0, 5, False), open_note, 36, seconds_at), 4.5)


    def test_same_position_run_does_not_require_switching_strings(self):
        data = self.score([[i / 2, p, 0.5] for i, p in enumerate([69, 71, 72, 71, 69])])
        self.assertEqual(self.tabs(data), [(1, 5), (1, 7), (1, 8), (1, 7), (1, 5)])

    def test_locked_open_string_does_not_reset_hand_to_nut(self):
        data = self.score([[0, 69, 0.5], [0.5, 64, 0.5, [[1, 0]]],
                           [1, 74, 0.5, [[1, 10]]]])
        self.assertEqual(self.tabs(data), [(2, 10), (1, 0), (1, 10)])

    def test_short_rest_and_section_boundary_keep_hand_context(self):
        data = self.score([[0, 69, 0.5], [0.75, 74, 0.5, [[1, 10]]]])
        self.assertEqual(self.tabs(data)[0], (2, 10))
        data = self.score([[3.5, 69, 0.5]], [{}, {"section": "B"}])
        data["tracks"].append({"bar": 2, "part": "lead", "events": [[0, 74, 0.5, [[1, 10]]]]})
        self.assertEqual(self.tabs(data), [(2, 10), (1, 10)])

    def test_chords_and_isolated_notes_keep_existing_assignment(self):
        data = self.score([[0, 69, 0.5], [0.5, [60, 64, 67], 0.5], [1, 84, 0.5]])
        for part in ("rhythm", "bass"):
            data["tracks"].append({"bar": 1, "part": part, "events": [[0, 48, 1]]})
        lanes = validate_score(data)[3]
        self.assertEqual(lanes["lead"][0][3], ((1, 5),))
        self.assertEqual(lanes["lead"][1][3], positions([60, 64, 67], TUNINGS["lead"]))
        for part in ("rhythm", "bass"):
            self.assertEqual(lanes[part][0][3], positions([48], TUNINGS[part]))

    def test_rhythm_and_bass_single_notes_plan_independently(self):
        data = self.score([[0, 69, 1, [[1, 5]]]])
        data["tracks"].extend([
            {"bar": 1, "part": "rhythm", "events": [[0, 69, 0.5], [0.5, 74, 0.5, [[1, 10]]]]},
            {"bar": 1, "part": "bass", "events": [[0, 45, 0.5], [0.5, 50, 0.5, [[1, 7]]]]}])
        original = copy.deepcopy(data)
        lanes = validate_score(data)[3]
        self.assertEqual([e[3][0] for e in lanes["lead"]], [(1, 5)])
        self.assertEqual([e[3][0] for e in lanes["rhythm"]], [(2, 10), (1, 10)])
        self.assertEqual([e[3][0] for e in lanes["bass"]], [(2, 7), (1, 7)])
        self.assertEqual(data, original)

    def test_order_of_input_tracks_and_events_does_not_change_tab(self):
        data = self.score([[3, 69, 0.5], [3.5, 67, 0.5]], [{}, {}])
        data["tracks"].append({"bar": 2, "part": "lead",
                               "events": [[0, 72, 0.5], [0.5, 74, 0.5]]})
        expected = self.tabs(data)
        data["tracks"].reverse()
        for track in data["tracks"]:
            track["events"].reverse()
        self.assertEqual(self.tabs(data), expected)

    def test_shift_cost_uses_hand_position_speed_and_recent_shift(self):
        before = (1, 8, 8, 0, -1.0)
        # Three frets apart can be reached without moving the hand.
        self.assertEqual(_transition_cost(before, (1, 11, 8), 0.2), 0)
        moved = (1, 11, 10)
        self.assertGreater(_transition_cost(before, moved, 0.2),
                           _transition_cost(before, moved, 1.0))
        self.assertGreater(_transition_cost((1, 8, 8, 1, 0.0), moved, 0.2, now=0.2),
                           _transition_cost(before, moved, 0.2, now=0.2))

    def test_recent_shift_survives_stationary_notes_and_penalizes_reversal(self):
        shifted = (1, 11, 10, 1, 0.0)
        held = score_events._hand_state(shifted, (1, 12, 10), 0.2)
        reverse = (1, 8, 8)
        forward = (1, 12, 12)
        self.assertGreater(_transition_cost(held, reverse, 0.1, now=0.4),
                           _transition_cost(held, forward, 0.1, now=0.4))
        fresh = (1, 12, 10, 0, -1.0)
        self.assertGreater(_transition_cost(held, forward, 0.1, now=0.4),
                           _transition_cost(fresh, forward, 0.1, now=0.4))
        recovered = score_events._hand_state(held, (1, 12, 10), 5.0)
        self.assertEqual(_transition_cost(recovered, reverse, 0.1, now=5.1),
                         _transition_cost(fresh, reverse, 0.1, now=5.1))

    def test_tempo_change_under_sustained_note_is_integrated(self):
        data = self.score([[3.5, 69, 1]], [{}, {"tempo": 120}])
        data["tempo"] = 60
        data["tracks"].append({"bar": 2, "part": "lead",
                               "events": [[0.5, 74, 0.5], [1, 72, 0.5]]})
        times = []

        def observe(events, tuning, locked, seconds_at):
            times.extend(seconds_at(e[0]) for e in events)
            return _plan_phrase(events, tuning, locked, seconds_at)

        with patch("score_events._plan_phrase", side_effect=observe):
            self.tabs(data)
        self.assertEqual(times, [3.5, 4.25, 4.5])


if __name__ == "__main__":
    unittest.main()
