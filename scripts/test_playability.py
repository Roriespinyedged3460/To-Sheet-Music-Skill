import copy
import json
import tempfile
import unittest
import xml.etree.ElementTree as ET
from pathlib import Path

import score_events


def score(events, part="lead", bars=None):
    return {
        "version": 1,
        "title": "playability",
        "tempo": 120,
        "time": [4, 4],
        "bars": bars or [{}],
        "tracks": [{"bar": 1, "part": part, "events": events}],
    }


class PlayabilityConfigTests(unittest.TestCase):
    def test_config_can_disable_fingers_and_override_timing(self):
        config = score_events.normalize_playability_config({
            "version": 1,
            "profiles": {
                "lead": {
                    "allowed_fingers": [1, 2],
                    "timing": {"shift_base_seconds": 0.1},
                    "open_string_preference": 0.25,
                }
            },
        })
        self.assertEqual(config["profiles"]["lead"]["allowed_fingers"], [1, 2])
        self.assertEqual(config["profiles"]["lead"]["timing"]["shift_base_seconds"], 0.1)
        self.assertEqual(config["profiles"]["bass"]["allowed_fingers"], [1, 2, 3, 4])
        self.assertEqual(config["profiles"]["lead"]["open_string_preference"], 0.25)

    def test_unknown_config_field_is_rejected(self):
        with self.assertRaises(ValueError):
            score_events.normalize_playability_config({"version": 1, "wat": True})
        with self.assertRaises(ValueError):
            score_events.normalize_playability_config({"version": True})

    def test_reduced_fretboard_requires_consistent_reach_limits(self):
        with self.assertRaises(ValueError):
            score_events.normalize_playability_config({
                "version": 1, "profiles": {"lead": {"max_fret": 2}},
            })

    def test_disabled_fingers_are_removed_from_allowed_set(self):
        config = score_events.normalize_playability_config({
            "version": 1, "profiles": {"bass": {"disabled_fingers": [2, 4]}},
        })
        self.assertEqual(config["profiles"]["bass"]["allowed_fingers"], [1, 3])

    def test_guitar_profile_is_an_alias_for_lead(self):
        config = score_events.normalize_playability_config({
            "version": 1, "guitar": {"allowed_fingers": [1]},
        })
        self.assertEqual(config["profiles"]["lead"]["allowed_fingers"], [1])


class PlayabilityCheckTests(unittest.TestCase):
    def test_default_template_hides_tab_finger_annotations(self):
        template = Path(__file__).resolve().parents[1] / "assets" / "band-template.mscx"
        root = ET.parse(template).getroot()
        self.assertTrue(root.findall(".//StaffType[@group='tablature']/showTabFingering"))
        self.assertTrue(all(item.text == "0" for item in root.findall(
            ".//StaffType[@group='tablature']/showTabFingering"
        )))

    def test_dynamic_program_matches_complete_short_sequence_enumeration(self):
        config = score_events.normalize_playability_config()
        tuning = score_events.TUNINGS["lead"]
        events = [(0, 4, [69], None), (8, 4, [67], None), (16, 4, [72], None)]
        seconds_at = lambda tick: tick * 0.05
        plan = score_events._plan_phrase(
            events, tuning, {}, seconds_at, config, "lead",
        )
        profile = score_events._planner_profile(config, "lead")
        previous = []
        for base, local in score_events._fingering_states(69, tuning, None, config, "lead"):
            previous.append(((*base, 0, -1.0), local))
        for index, event in enumerate(events[1:], 1):
            current = []
            now = seconds_at(event[0])
            for prior, cost in previous:
                elapsed = score_events._movement_seconds(prior, events[index - 1], event[0], seconds_at)
                for base, local in score_events._fingering_states(
                        event[2][0], tuning, None, config, "lead"):
                    state = score_events._hand_state(prior, base, now, profile)
                    requirements = score_events._transition_requirements(
                        prior, state, events[index - 1], event[0], seconds_at, profile,
                    )
                    if not requirements["issues"]:
                        current.append((state, cost + local + score_events._transition_cost(
                            prior, state, elapsed, now, profile,
                        )))
            previous = current
        self.assertTrue(previous)
        self.assertAlmostEqual(plan.cost, min(cost for _, cost in previous), places=9)

    def test_report_contains_fingerings_and_transition_breakdown(self):
        report = score_events.check_playability(score([
            [0, 69, 0.5], [0.5, 67, 0.5], [1, 69, 0.5],
        ]))
        self.assertEqual(report["status"], "passed")
        self.assertEqual(report["version"], 1)
        self.assertTrue(report["events_sha256"])
        self.assertIn("single_note_segments", report["check_scope"]["supported"])
        self.assertIn("chord_connections", report["check_scope"]["unsupported"])
        lead = report["lanes"]["lead"]
        self.assertEqual(len(lead["fingerings"]), 3)
        self.assertEqual(len(lead["transitions"]), 2)
        self.assertIn("available_seconds", lead["transitions"][0])
        self.assertIn("required_shift_seconds", lead["transitions"][0])
        self.assertIn("from_fingering", lead["transitions"][0])
        self.assertIn("to_fingering", lead["transitions"][0])
        self.assertIsNotNone(lead["hardest_transition"])
        self.assertEqual(report["hardest_transition"]["lane"], "lead")

    def test_zero_gap_large_shift_is_needs_repair(self):
        data = score([
            [0, 65, 0.125, [[1, 1]]],
            [0.125, 88, 0.125, [[1, 24]]],
        ])
        report = score_events.check_playability(data)
        self.assertEqual(report["status"], "needs_repair")
        codes = {issue["code"] for issue in report["issues"]}
        self.assertIn("insufficient_shift_time", codes)
        self.assertIn("lead", report["lanes"])
        self.assertEqual(report["lanes"]["lead"]["status"], "needs_repair")

    def test_chord_boundary_is_marked_incomplete(self):
        report = score_events.check_playability(score([
            [0, 69, 0.5], [0.5, [60, 64, 67], 0.5], [1, 74, 0.5],
        ]))
        self.assertEqual(report["status"], "incomplete")
        self.assertIn("chord_connection", report["unverified"])

    def test_rhythm_keeps_legacy_soft_transition_check(self):
        report = score_events.check_playability(score([
            [0, 65, 0.125, [[1, 1]]],
            [0.125, 88, 0.125, [[1, 24]]],
        ], part="rhythm"))
        self.assertEqual(report["status"], "passed")
        self.assertEqual(report["lanes"]["rhythm"]["fingerings"], [])

    def test_manual_tab_is_rechecked_and_finger_is_not_written_to_input(self):
        data = score([[0, 69, 0.5, [[1, 5]]]])
        original = copy.deepcopy(data)
        report = score_events.check_playability(data)
        self.assertEqual(report["status"], "passed")
        self.assertEqual(data, original)
        self.assertEqual(report["lanes"]["lead"]["fingerings"][0]["tab"], [1, 5])

    def test_musicxml_omits_visible_finger_annotations(self):
        root = score_events.build_score(score([[0, 69, 0.5]]))
        self.assertIsNone(root.find(".//technical/fingering"))

    def test_build_report_is_bound_to_the_generated_musicxml(self):
        root, report = score_events.build_score(score([[0, 69, 0.5]]), return_report=True)
        self.assertEqual(report["status"], "passed")
        self.assertEqual(len(report["score_xml_sha256"]), 64)
        self.assertIsNone(root.find(".//technical/fingering"))
        self.assertTrue(report["lanes"]["lead"]["fingerings"])

    def test_build_failure_still_writes_structured_failure_report(self):
        data = score([
            [0, 65, 0.125, [[1, 1]]],
            [0.125, 88, 0.125, [[1, 24]]],
        ])
        with tempfile.TemporaryDirectory() as folder:
            with self.assertRaises(score_events.PlayabilityError):
                score_events.write_score(
                    data, Path(folder) / "score.musicxml", report_path=Path(folder) / "failure.json",
                )
            failure = json.loads((Path(folder) / "failure.json").read_text(encoding="utf-8"))
            self.assertEqual(failure["status"], "needs_repair")
            self.assertTrue(failure["issues"])

    def test_disallowed_finger_never_appears_in_report(self):
        config = {"version": 1, "profiles": {"lead": {"allowed_fingers": [1]}}}
        report = score_events.check_playability(score([[0, 69, 0.5]]), config)
        self.assertEqual(report["status"], "passed")
        self.assertEqual({item["finger"] for item in report["lanes"]["lead"]["fingerings"]}, {1})

    def test_resource_limit_is_reported_as_computation_incomplete(self):
        config = {"version": 1, "limits": {"max_transitions_per_layer": 1}}
        report = score_events.check_playability(score([
            [0, 69, 0.5], [0.5, 67, 0.5],
        ]), config)
        self.assertEqual(report["status"], "computation_incomplete")
        self.assertIn("transition_limit_exceeded", {item["code"] for item in report["issues"]})

    def test_zero_state_limit_is_a_computation_boundary(self):
        report = score_events.check_playability(
            score([[0, 69, 0.5]]),
            {"version": 1, "limits": {"max_states_per_layer": 0}},
        )
        self.assertEqual(report["status"], "computation_incomplete")
        self.assertIn("state_limit_exceeded", {item["code"] for item in report["issues"]})


class RepairTests(unittest.TestCase):
    def test_repair_requires_explicitly_adjustable_events(self):
        data = score([
            [0, 65, 0.125, [[1, 1]]],
            [0.125, 88, 0.125, [[1, 24]]],
        ])
        result = score_events.repair_playability(data)
        self.assertEqual(result["status"], "needs_repair")
        self.assertEqual(result["attempts"][-1]["strategy"], "simplify")
        self.assertEqual(data, score(data["tracks"][0]["events"]))

    def test_repair_can_remove_an_adjustable_decoration(self):
        data = score([
            [0, 65, 0.125, [[1, 1]]],
            [0.125, 88, 0.125, [[1, 24]]],
        ])
        constraints = {
            "version": 1,
            "adjustable": [{"lane": "lead", "bar": 1, "offset": 0.125,
                             "pitch": 88, "operations": ["remove"]}],
        }
        result = score_events.repair_playability(data, constraints=constraints)
        self.assertEqual(result["status"], "passed")
        self.assertEqual(len(result["events"]["tracks"][0]["events"]), 1)
        self.assertEqual(result["attempts"][-1]["strategy"], "simplify")

    def test_repair_refingers_an_explicit_path_before_simplifying(self):
        data = score([
            [0, 65, 0.125, [[1, 1]]],
            [0.125, 67, 0.125, [[2, 8]]],
        ])
        constraints = {
            "version": 1,
            "adjustable": [{"lane": "lead", "bar": 1, "offset": 0.125,
                             "pitch": 67, "operations": ["refinger"]}],
        }
        result = score_events.repair_playability(data, constraints=constraints)
        self.assertEqual(result["status"], "passed")
        self.assertEqual(result["attempts"][0]["strategy"], "refinger")
        self.assertTrue(result["attempts"][0]["accepted"])
        self.assertEqual(result["events"]["tracks"][0]["events"][1], [0.125, 67, 0.125])

    def test_repair_can_release_a_grid_step_for_a_local_shift(self):
        data = score([
            [0, 65, 0.5, [[1, 1]]],
            [0.5, 70, 0.125, [[1, 6]]],
        ])
        data["tempo"] = 100
        constraints = {
            "version": 1,
            "adjustable": [{"lane": "lead", "bar": 1, "offset": 0,
                             "pitch": 65, "operations": ["shorten"]}],
        }
        result = score_events.repair_playability(data, constraints=constraints)
        self.assertEqual(result["status"], "passed")
        timing_attempt = next(item for item in result["attempts"]
                              if item["strategy"] == "timing_or_octave")
        self.assertTrue(timing_attempt["accepted"])
        self.assertEqual(result["events"]["tracks"][0]["events"][0][2], 0.375)

    def test_protected_event_wins_over_adjustable_overlap(self):
        data = score([
            [0, 65, 0.125, [[1, 1]]],
            [0.125, 88, 0.125, [[1, 24]]],
        ])
        target = {"lane": "lead", "bar": 1, "offset": 0.125,
                  "pitch": 88, "operations": ["remove"]}
        constraints = {"version": 1, "adjustable": [target], "protected": [target]}
        result = score_events.repair_playability(data, constraints=constraints)
        self.assertEqual(result["status"], "needs_repair")
        self.assertEqual(len(result["events"]["tracks"][0]["events"]), 2)
        self.assertFalse(result["attempts"][-1]["changes"])


if __name__ == "__main__":
    unittest.main()
