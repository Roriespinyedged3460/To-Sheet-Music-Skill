"""Deterministic compact-event to MusicXML compiler (stdlib only).

Quarter-note beats; absent events are rests. See references/score-events.md.
No transcription or musical inference happens here; unsupported data fails.
"""
from __future__ import annotations

import argparse
from bisect import bisect_right
import copy
import hashlib
import itertools
import json
import math
import sys
import xml.etree.ElementTree as ET
from fractions import Fraction
from pathlib import Path

DIVISIONS = 8
MAX_FRET = 24
PARTS = (("lead", "主音吉他", 30), ("rhythm", "节奏吉他", 29),
         ("bass", "贝斯", 34), ("keys", "键盘", 1), ("drums", "鼓", 1))
LANES = ("lead", "rhythm", "bass", "keys_rh", "keys_lh", "drums")
TUNINGS = {"lead": [40, 45, 50, 55, 59, 64], "rhythm": [40, 45, 50, 55, 59, 64],
           "bass": [28, 33, 38, 43]}
DRUMS = {35: ("F", 4, "normal"), 36: ("F", 4, "normal"),
         37: ("C", 5, "x"), 38: ("C", 5, "normal"), 40: ("C", 5, "normal"),
         41: ("F", 4, "normal"), 43: ("A", 4, "normal"), 45: ("C", 5, "normal"),
         47: ("D", 5, "normal"), 48: ("E", 5, "normal"), 50: ("F", 5, "normal"),
         42: ("G", 5, "x"), 44: ("D", 4, "x"), 46: ("G", 5, "x"),
         49: ("A", 5, "x"), 51: ("F", 5, "x"), 52: ("A", 5, "x"),
         53: ("F", 5, "diamond"), 55: ("A", 5, "x"), 57: ("A", 5, "x"),
         59: ("F", 5, "x")}
DURATIONS = {32: ("whole", False), 24: ("half", True), 16: ("half", False),
             12: ("quarter", True), 8: ("quarter", False), 6: ("eighth", True),
             4: ("eighth", False), 3: ("16th", True), 2: ("16th", False), 1: ("32nd", False)}


def add(parent, tag, text=None, **attrs):
    el = ET.SubElement(parent, tag, {k: str(v) for k, v in attrs.items()})
    if text is not None:
        el.text = str(text)
    return el


def fields(value, allowed, required=()):
    if not isinstance(value, dict) or set(value) - set(allowed) or set(required) - set(value):
        raise ValueError(f"Expected fields {sorted(allowed)}, required {sorted(required)}")


def integer(value, low, high, name):
    if type(value) is not int or not low <= value <= high:
        raise ValueError(f"{name} must be an integer in {low}..{high}")
    return value


def ticks(value, allow_triplet=False):
    if isinstance(value, bool) or not isinstance(value, (int, float, str)):
        raise ValueError("Beats must be a number or rational string")
    try:
        result = Fraction(str(value)) * DIVISIONS
    except (ValueError, ZeroDivisionError) as exc:
        raise ValueError("Invalid beat value") from exc
    if result.denominator not in ((1, 3) if allow_triplet else (1,)) or result < 0:
        raise ValueError("Beats must be non-negative multiples of 1/8 quarter; tuplets unsupported in v1")
    return int(result) if result.denominator == 1 else result


class EventPitches(list):
    """Retain optional notation metadata without changing four-field lane tuples.

    Dead strokes contain None, never an inferred sounding pitch. MusicXML needs
    a positional pitch carrier for TAB; it is derived solely from supplied TAB.
    """
    def __init__(self, pitches, *, dead=False, tuplet=False, display_pitches=()):
        super().__init__(pitches)
        self.dead = dead
        self.tuplet = tuplet
        self.display_pitches = tuple(display_pitches)


def _dead(pitches):
    return getattr(pitches, "dead", False)


def _triplet(pitches):
    return getattr(pitches, "tuplet", False)


def _json_numbers(value):
    if isinstance(value, Fraction):
        return int(value) if value.denominator == 1 else float(value)
    if isinstance(value, dict):
        return {key: _json_numbers(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_json_numbers(item) for item in value]
    return value


def tempo(value):
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or value <= 0:
        raise ValueError("Tempo must be positive and finite")
    return float(value)


def meter(value):
    if not isinstance(value, list) or len(value) != 2:
        raise ValueError("Time must be [numerator, denominator]")
    n, d = value
    integer(n, 1, 32, "numerator")
    if type(d) is not int or d not in (1, 2, 4, 8, 16, 32):
        raise ValueError("Unsupported time denominator")
    return n, d


def positions(pitches, tuning, override=None, max_fret=None):
    max_fret = MAX_FRET if max_fret is None else max_fret
    if override is not None:
        if not isinstance(override, list) or len(override) != len(pitches):
            raise ValueError("TAB overrides must match chord pitches")
        candidates = []
        for p, pair in zip(pitches, override):
            if not isinstance(pair, list) or len(pair) != 2:
                raise ValueError("TAB override must be [string, fret]")
            s, f = pair
            integer(s, 1, len(tuning), "string")
            integer(f, 0, max_fret, "fret")
            if tuning[-s] + f != p:
                raise ValueError("TAB pitch mismatch")
            candidates.append([(s, f)])
    else:
        candidates = [[(s, p - opening) for s, opening in enumerate(reversed(tuning), 1)
                       if 0 <= p - opening <= max_fret] for p in pitches]
    if len(pitches) > len(tuning):
        raise ValueError("Impossible TAB chord")
    valid = []
    for choice in itertools.product(*candidates):
        if len({s for s, _ in choice}) != len(choice):
            continue
        frets = [f for _, f in choice if f]
        span = max(frets) - min(frets) if frets else 0
        valid.append(((span > 4, span, max(f for _, f in choice), sum(f for _, f in choice), choice), choice))
    if not valid:
        raise ValueError("Impossible TAB chord: no distinct playable strings; change events or tuning")
    return min(valid)[1]



# Relative heuristic costs, not measured human biomechanical limits.
PLAYABILITY_VERSION = 1
HAND_REACH = 3
EXTENDED_REACH = 4
FINGERING_COST = {"fret": 0.02, "extension": 1.5, "string": 0.35,
                  "shift": 2.0, "shift_start": 1.5, "repeat_shift": 3.0,
                  "reverse_shift": 2.0, "comfort": 0.0, "open": 0.0}
MIN_TRANSITION_SECONDS = 0.05
SHIFT_MEMORY_SECONDS = 1.0

DEFAULT_PLAYABILITY_CONFIG = {
    "version": PLAYABILITY_VERSION,
    "model": "heuristic-v1",
    "profiles": {
        "lead": {
            "max_fret": MAX_FRET,
            "hand_reach": HAND_REACH,
            "extended_reach": EXTENDED_REACH,
            "allowed_fingers": [1, 2, 3, 4],
            "disabled_fingers": [],
            "finger_offsets": {"1": 0, "2": 1, "3": 2, "4": 3},
            "comfortable_fret_range": [0, MAX_FRET],
            "open_string_preference": 0.0,
            "costs": dict(FINGERING_COST),
            "timing": {
                "shift_base_seconds": 0.05,
                "shift_per_fret_seconds": 0.02,
                "string_per_crossed_seconds": 0.02,
            },
            "allow_same_finger": True,
            "same_finger_requires_same_fret_or_string": False,
            "shift_memory_seconds": SHIFT_MEMORY_SECONDS,
            "min_transition_seconds": MIN_TRANSITION_SECONDS,
        },
        "bass": {
            "max_fret": MAX_FRET,
            "hand_reach": HAND_REACH,
            "extended_reach": EXTENDED_REACH,
            "allowed_fingers": [1, 2, 3, 4],
            "disabled_fingers": [],
            "finger_offsets": {"1": 0, "2": 1, "3": 2, "4": 3},
            "comfortable_fret_range": [0, MAX_FRET],
            "open_string_preference": 0.0,
            "costs": dict(FINGERING_COST),
            "timing": {
                "shift_base_seconds": 0.05,
                "shift_per_fret_seconds": 0.02,
                "string_per_crossed_seconds": 0.02,
            },
            "allow_same_finger": True,
            "same_finger_requires_same_fret_or_string": False,
            "shift_memory_seconds": SHIFT_MEMORY_SECONDS,
            "min_transition_seconds": MIN_TRANSITION_SECONDS,
        },
        # Rhythm guitar keeps the v1 single-note behavior when configured.
        "rhythm": {
            "max_fret": MAX_FRET,
            "hand_reach": HAND_REACH,
            "extended_reach": EXTENDED_REACH,
            "allowed_fingers": [1, 2, 3, 4],
            "disabled_fingers": [],
            "finger_offsets": {"1": 0, "2": 1, "3": 2, "4": 3},
            "comfortable_fret_range": [0, MAX_FRET],
            "open_string_preference": 0.0,
            "costs": dict(FINGERING_COST),
            "timing": {
                "shift_base_seconds": 0.05,
                "shift_per_fret_seconds": 0.02,
                "string_per_crossed_seconds": 0.02,
            },
            "allow_same_finger": True,
            "same_finger_requires_same_fret_or_string": False,
            "shift_memory_seconds": SHIFT_MEMORY_SECONDS,
            "min_transition_seconds": MIN_TRANSITION_SECONDS,
        },
    },
    "limits": {
        "max_states_per_layer": 50000,
        "max_transitions_per_layer": 500000,
    },
}


class PlayabilityError(ValueError):
    """A deterministic planner failure with a machine-readable diagnostic."""

    def __init__(self, message, report=None):
        super().__init__(message)
        self.report = report or {"status": "needs_repair", "issues": []}


class PlanPath(list):
    """List-compatible path carrying the data needed by the compiler and audit."""

    def __init__(self, pairs, *, states=(), transitions=(), cost=0.0,
                 max_states=0, max_transitions=0):
        super().__init__(pairs)
        self.states = list(states)
        self.transitions = list(transitions)
        self.cost = float(cost)
        self.max_states = int(max_states)
        self.max_transitions = int(max_transitions)


def _copy_default_playability_config():
    return copy.deepcopy(DEFAULT_PLAYABILITY_CONFIG)


def _finite_number(value, name, low=0.0):
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"{name} must be a finite number")
    value = float(value)
    if not math.isfinite(value) or value < low:
        raise ValueError(f"{name} must be a finite number >= {low}")
    return value


def _normalize_profile(raw, base, name):
    if not isinstance(raw, dict):
        raise ValueError(f"{name} profile must be an object")
    allowed = ("max_fret", "hand_reach", "extended_reach", "allowed_fingers", "disabled_fingers",
               "finger_offsets", "comfortable_fret_range", "open_string_preference",
               "costs", "timing",
               "allow_same_finger", "same_finger_requires_same_fret_or_string",
               "shift_memory_seconds", "min_transition_seconds")
    fields(raw, allowed)
    profile = copy.deepcopy(base)
    if "max_fret" in raw:
        profile["max_fret"] = integer(raw["max_fret"], 1, 36, f"{name}.max_fret")
    if "hand_reach" in raw:
        profile["hand_reach"] = integer(raw["hand_reach"], 0, profile["max_fret"], f"{name}.hand_reach")
    if "extended_reach" in raw:
        profile["extended_reach"] = integer(raw["extended_reach"], profile["hand_reach"], profile["max_fret"], f"{name}.extended_reach")
    if "allowed_fingers" in raw:
        fingers = raw["allowed_fingers"]
        if not isinstance(fingers, list) or any(type(f) is not int or not 1 <= f <= 4 for f in fingers):
            raise ValueError(f"{name}.allowed_fingers must contain integers 1..4")
        if len(set(fingers)) != len(fingers):
            raise ValueError(f"{name}.allowed_fingers must not contain duplicates")
        profile["allowed_fingers"] = sorted(fingers)
    if "disabled_fingers" in raw:
        disabled = raw["disabled_fingers"]
        if not isinstance(disabled, list) or any(type(f) is not int or not 1 <= f <= 4 for f in disabled):
            raise ValueError(f"{name}.disabled_fingers must contain integers 1..4")
        if len(set(disabled)) != len(disabled):
            raise ValueError(f"{name}.disabled_fingers must not contain duplicates")
        profile["disabled_fingers"] = sorted(disabled)
        profile["allowed_fingers"] = [
            finger for finger in profile["allowed_fingers"] if finger not in profile["disabled_fingers"]
        ]
    if "finger_offsets" in raw:
        offsets = raw["finger_offsets"]
        if not isinstance(offsets, dict):
            raise ValueError(f"{name}.finger_offsets must be an object")
        normalized = dict(profile["finger_offsets"])
        for key, value in offsets.items():
            if str(key) not in {"1", "2", "3", "4"}:
                raise ValueError(f"{name}.finger_offsets has an invalid finger")
            normalized[str(key)] = integer(value, 0, profile["extended_reach"], f"{name}.finger_offsets[{key}]")
        profile["finger_offsets"] = normalized
    if "comfortable_fret_range" in raw:
        zone = raw["comfortable_fret_range"]
        if not isinstance(zone, list) or len(zone) != 2:
            raise ValueError(f"{name}.comfortable_fret_range must be [low, high]")
        low = integer(zone[0], 0, profile["max_fret"], f"{name}.comfortable_fret_range.low")
        high = integer(zone[1], low, profile["max_fret"], f"{name}.comfortable_fret_range.high")
        profile["comfortable_fret_range"] = [low, high]
    if "open_string_preference" in raw:
        profile["open_string_preference"] = _finite_number(
            raw["open_string_preference"], f"{name}.open_string_preference",
        )
    if "costs" in raw:
        costs = raw["costs"]
        if not isinstance(costs, dict) or set(costs) - set(FINGERING_COST):
            raise ValueError(f"{name}.costs contains an unknown attribute")
        for key, value in costs.items():
            profile["costs"][key] = _finite_number(value, f"{name}.costs.{key}")
    if "timing" in raw:
        timing = raw["timing"]
        allowed_timing = ("shift_base_seconds", "shift_per_fret_seconds", "string_per_crossed_seconds")
        if not isinstance(timing, dict) or set(timing) - set(allowed_timing):
            raise ValueError(f"{name}.timing contains an unknown attribute")
        for key, value in timing.items():
            profile["timing"][key] = _finite_number(value, f"{name}.timing.{key}")
    for key in ("allow_same_finger", "same_finger_requires_same_fret_or_string"):
        if key in raw:
            if type(raw[key]) is not bool:
                raise ValueError(f"{name}.{key} must be boolean")
            profile[key] = raw[key]
    for key in ("shift_memory_seconds", "min_transition_seconds"):
        if key in raw:
            profile[key] = _finite_number(raw[key], f"{name}.{key}")
    if profile["hand_reach"] > profile["max_fret"]:
        raise ValueError(f"{name}.hand_reach cannot exceed max_fret")
    if profile["extended_reach"] > profile["max_fret"]:
        raise ValueError(f"{name}.extended_reach cannot exceed max_fret")
    if profile["comfortable_fret_range"][1] > profile["max_fret"]:
        profile["comfortable_fret_range"][1] = profile["max_fret"]
    if profile["extended_reach"] < max(profile["finger_offsets"].values()):
        raise ValueError(f"{name}.extended_reach is shorter than finger_offsets")
    return profile


def normalize_playability_config(config=None):
    """Return a validated, fully populated configuration without mutating input."""
    if config is None:
        return _copy_default_playability_config()
    if not isinstance(config, dict):
        raise ValueError("playability config must be an object")
    allowed = ("version", "model", "profiles", "lead", "guitar", "bass", "rhythm", "limits")
    fields(config, allowed, ("version",))
    if type(config["version"]) is not int or config["version"] != PLAYABILITY_VERSION:
        raise ValueError(f"Unsupported playability config version: {config['version']}")
    normalized = _copy_default_playability_config()
    if "model" in config:
        if not isinstance(config["model"], str) or not config["model"].strip():
            raise ValueError("playability config model must be text")
        normalized["model"] = config["model"]
    raw_profiles = {}
    if "profiles" in config:
        if not isinstance(config["profiles"], dict) or set(config["profiles"]) - {"lead", "guitar", "bass", "rhythm"}:
            raise ValueError("profiles must contain only lead, guitar, bass, and rhythm")
        raw_profiles.update(config["profiles"])
        if "guitar" in raw_profiles and "lead" not in raw_profiles:
            raw_profiles["lead"] = raw_profiles.pop("guitar")
        else:
            raw_profiles.pop("guitar", None)
    for lane in ("lead", "bass", "rhythm"):
        if lane in config:
            raw_profiles[lane] = config[lane]
    if "guitar" in config and "lead" not in config:
        raw_profiles["lead"] = config["guitar"]
    for lane, raw in raw_profiles.items():
        normalized["profiles"][lane] = _normalize_profile(raw, normalized["profiles"][lane], lane)
    if "limits" in config:
        limits = config["limits"]
        if not isinstance(limits, dict) or set(limits) - {"max_states_per_layer", "max_transitions_per_layer"}:
            raise ValueError("limits contains an unknown attribute")
        for key, value in limits.items():
            normalized["limits"][key] = integer(value, 0, 10_000_000, f"limits.{key}")
    return normalized


def load_playability_config(source):
    if source is None:
        return normalize_playability_config()
    path = Path(source)
    return normalize_playability_config(json.loads(path.read_text(encoding="utf-8")))


def _profile_for_lane(config, lane):
    config = normalize_playability_config(config)
    return config["profiles"].get(lane, config["profiles"]["lead"])


def _planner_profile(config, lane):
    """Return the profile used by the planner, keeping rhythm on its v1 path."""
    profile = _profile_for_lane(config, lane)
    if lane != "rhythm":
        return profile
    profile = copy.deepcopy(profile)
    # The first release adds hard action timing only to lead and bass.  Rhythm
    # guitar keeps its existing soft TAB behavior even when it shares a score.
    profile["timing"] = {
        "shift_base_seconds": 0.0,
        "shift_per_fret_seconds": 0.0,
        "string_per_crossed_seconds": 0.0,
    }
    profile["allow_same_finger"] = True
    profile["same_finger_requires_same_fret_or_string"] = False
    return profile


def _config_hash(config):
    payload = json.dumps(normalize_playability_config(config), ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _events_hash(data):
    payload = json.dumps(data, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _fingering_states(pitch, tuning, locked, config=None, lane="lead"):
    profile = _profile_for_lane(config, lane)
    max_fret = profile["max_fret"]
    pairs = locked or tuple((s, pitch - opening)
                            for s, opening in enumerate(reversed(tuning), 1)
                            if 0 <= pitch - opening <= max_fret)
    for string, fret in pairs:
        if fret == 0:
            # Open strings leave the hand free, rather than teleporting it to fret 0.
            for hand in range(1, max_fret + 1):
                cost = profile["costs"]["open"] + profile["open_string_preference"]
                yield (string, fret, hand, 0), cost
            continue
        for finger in profile["allowed_fingers"]:
            offset = profile["finger_offsets"][str(finger)]
            possible_offsets = [offset]
            if finger == 4 and offset < profile["extended_reach"]:
                possible_offsets.append(offset + 1)
            for actual_offset in possible_offsets:
                hand = fret - actual_offset
                if hand < 1 or hand > max_fret:
                    continue
                extension = max(0, actual_offset - profile["hand_reach"])
                if actual_offset > profile["extended_reach"]:
                    continue
                low, high = profile["comfortable_fret_range"]
                comfort_distance = max(low - fret, 0, fret - high)
                local = (profile["costs"]["fret"] * fret
                         + profile["costs"]["extension"] * extension ** 2
                         + profile["costs"]["comfort"] * comfort_distance ** 2)
                yield (string, fret, hand, finger), local


def _state_values(state):
    """Normalize old three/five-field test states and new six-field states."""
    if len(state) >= 6:
        return state[0], state[1], state[2], state[3], state[4], state[5]
    if len(state) == 5:
        return state[0], state[1], state[2], 0, state[3], state[4]
    if len(state) == 4:
        return state[0], state[1], state[2], state[3], 0, -1.0
    if len(state) == 3:
        return state[0], state[1], state[2], 0, 0, -1.0
    raise ValueError("Invalid fingering state")


def _profile_value(profile, key, fallback):
    return fallback if profile is None else profile.get(key, fallback)


def _hand_state(prior, current, now, profile=None):
    """Keep the most recent shift through intervening notes, until recovery."""
    _, _, prior_hand, _, prior_direction, prior_time = _state_values(prior)
    string, fret, hand, finger = (tuple(current) + (0,))[:4] if len(current) == 3 else tuple(current[:4])
    delta = hand - prior_hand
    shift_memory = _profile_value(profile, "shift_memory_seconds", SHIFT_MEMORY_SECONDS)
    if delta:
        return (string, fret, hand, finger, 1 if delta > 0 else -1, now)
    if prior_direction and prior_time >= 0 and now - prior_time < shift_memory:
        return (string, fret, hand, finger, prior_direction, prior_time)
    return (string, fret, hand, finger, 0, -1.0)


def _transition_cost(prior, current, elapsed, now=0.0, profile=None):
    return sum(_transition_cost_breakdown(prior, current, elapsed, now, profile).values())


def _transition_cost_breakdown(prior, current, elapsed, now=0.0, profile=None):
    _, _, prior_hand, _, prior_direction, prior_time = _state_values(prior)
    current_string, _, current_hand, _, _, _ = _state_values(current)
    delta = current_hand - prior_hand
    distance = abs(delta)
    shifted = distance > 0
    min_seconds = _profile_value(profile, "min_transition_seconds", MIN_TRANSITION_SECONDS)
    costs = _profile_value(profile, "costs", FINGERING_COST)
    urgency = 1 / max(min_seconds, elapsed, 1e-9)
    shift_memory = _profile_value(profile, "shift_memory_seconds", SHIFT_MEMORY_SECONDS)
    age = now - prior_time
    recent = (max(0.0, 1 - age / shift_memory)
              if prior_direction and prior_time >= 0 and age >= 0 and shift_memory > 0
              else 0.0)
    string_cost = costs["string"] * abs(current_string - _state_values(prior)[0])
    return {
        "string": string_cost,
        "shift": urgency * costs["shift"] * distance,
        "shift_start": urgency * costs["shift_start"] * shifted,
        "repeat_shift": urgency * recent * costs["repeat_shift"] * shifted,
        "reverse_shift": urgency * recent * costs["reverse_shift"] *
                         bool(delta * prior_direction < 0),
    }


def _movement_seconds(prior, previous_event, next_start, seconds_at):
    start, duration, _, _ = previous_event
    # A fretted note occupies the hand until release; an open string does not.
    available_from = start if _state_values(prior)[1] == 0 else start + duration
    return max(0.0, seconds_at(next_start) - seconds_at(available_from))


def _transition_requirements(prior, current, previous_event, next_start, seconds_at, profile):
    """Return hard action requirements for one adjacent pair of notes."""
    prior_string, prior_fret, prior_hand, prior_finger, _, _ = _state_values(prior)
    current_string, current_fret, current_hand, current_finger, _, _ = _state_values(current)
    previous_start, _, _, _ = previous_event
    ioi = max(0.0, seconds_at(next_start) - seconds_at(previous_start))
    available = _movement_seconds(prior, previous_event, next_start, seconds_at)
    shift_distance = abs(current_hand - prior_hand)
    timing = profile["timing"]
    required_shift = (0.0 if shift_distance == 0 else
                      timing["shift_base_seconds"] + timing["shift_per_fret_seconds"] * shift_distance)
    required_string = timing["string_per_crossed_seconds"] * abs(current_string - prior_string)
    issues = []
    if shift_distance and available + 1e-9 < required_shift:
        issues.append("insufficient_shift_time")
    if required_string and ioi + 1e-9 < required_string:
        issues.append("insufficient_string_change_time")
    if (prior_finger and current_finger and prior_finger == current_finger
            and not profile["allow_same_finger"]):
        same_fret_or_string = prior_fret == current_fret or prior_string == current_string
        if profile["same_finger_requires_same_fret_or_string"] and not same_fret_or_string:
            issues.append("same_finger_restricted")
        elif not profile["same_finger_requires_same_fret_or_string"]:
            issues.append("same_finger_restricted")
    return {
        "from_start": previous_start,
        "to_start": next_start,
        "ioi_seconds": ioi,
        "available_seconds": available,
        "required_shift_seconds": required_shift,
        "required_string_seconds": required_string,
        "shift_distance_frets": shift_distance,
        "string_distance": abs(current_string - prior_string),
        "issues": issues,
    }


def _plan_phrase(events, tuning, locked, seconds_at, config=None, lane="lead"):
    """Exact DP over string/fret/finger/hand and recent shift history."""
    profile = _planner_profile(config, lane)
    limits = normalize_playability_config(config)["limits"]
    locked = locked or {}
    layers = []
    previous = {}
    transitions_seen = 0
    for index, (start, _, pitches, _) in enumerate(events):
        layer = {}
        rejected_issues = []
        rejected_details = {}
        layer_transitions = 0
        now = seconds_at(start)
        movement = {prior: _movement_seconds(prior, events[index - 1], start, seconds_at)
                    for prior in previous} if index else {}
        for base_state, local in _fingering_states(pitches[0], tuning, locked.get(start), config, lane):
            if not index:
                state = (*base_state, 0, -1.0)
                layer[state] = (local, None, None, 0.0)
                continue
            for prior, record in previous.items():
                cost = record[0]
                transitions_seen += 1
                layer_transitions += 1
                if layer_transitions > limits["max_transitions_per_layer"]:
                    diagnostic = {"status": "computation_incomplete", "issues": [{
                        "code": "transition_limit_exceeded", "lane": lane, "note_index": index,
                        "limit": limits["max_transitions_per_layer"],
                    }]}
                    raise PlayabilityError("Playability computation exceeded transition limit", diagnostic)
                state = _hand_state(prior, base_state, now, profile)
                requirements = _transition_requirements(prior, state, events[index - 1], start, seconds_at, profile)
                if requirements["issues"]:
                    for issue in requirements["issues"]:
                        rejected_issues.append(issue)
                        detail = dict(requirements)
                        detail["from_state"] = list(prior)
                        detail["to_state"] = list(state)
                        rejected_details.setdefault(issue, detail)
                    continue
                transition_cost = _transition_cost(prior, state, movement[prior], now, profile)
                requirements["transition_cost"] = transition_cost
                requirements["cost_breakdown"] = _transition_cost_breakdown(
                    prior, state, movement[prior], now, profile,
                )
                total = cost + local + transition_cost
                candidate = (total, prior)
                current = layer.get(state)
                if current is None or candidate < (current[0], current[1]):
                    layer[state] = (total, prior, requirements, transition_cost)
        if not layer:
            code = "no_playable_position" if not index else "no_feasible_transition"
            diagnostic_issues = []
            for issue in sorted(set(rejected_issues)):
                detail = rejected_details.get(issue, {})
                diagnostic_issues.append({"code": issue, "lane": lane, "note_index": index,
                                          "start": start, **{key: detail[key] for key in (
                                              "from_start", "to_start", "ioi_seconds",
                                              "available_seconds", "required_shift_seconds",
                                              "required_string_seconds", "shift_distance_frets",
                                              "string_distance", "from_state", "to_state")
                                             if key in detail}})
            diagnostic_issues.append({"code": code, "lane": lane, "note_index": index, "start": start})
            diagnostic = {"status": "needs_repair", "issues": diagnostic_issues}
            raise PlayabilityError("Impossible TAB melody: no feasible string/fret path", diagnostic)
        if len(layer) > limits["max_states_per_layer"]:
            diagnostic = {"status": "computation_incomplete", "issues": [{
                "code": "state_limit_exceeded", "lane": lane, "note_index": index,
                "limit": limits["max_states_per_layer"],
            }]}
            raise PlayabilityError("Playability computation exceeded state limit", diagnostic)
        layers.append(layer)
        previous = layer
    if not layers:
        return PlanPath([], states=[], transitions=[], cost=0.0)
    state = min(previous, key=lambda state: (previous[state][0], state))
    result, states, transitions = [], [], []
    for layer in reversed(layers):
        record = layer[state]
        result.append((state[0], state[1]))
        states.append(state)
        if record[2] is not None:
            transitions.append(record[2])
        state = record[1]
    return PlanPath(list(reversed(result)), states=list(reversed(states)),
                    transitions=list(reversed(transitions)), cost=previous[min(previous, key=lambda s: (previous[s][0], s))][0],
                    max_states=max(len(layer) for layer in layers), max_transitions=transitions_seen)


def _plan_string_part(events, tuning, locked, bars, offsets, initial_tempo, config=None, lane="lead"):
    # Integrate the tempo map, including changes underneath sustained notes.
    seconds, bpms = [0.0], []
    bpm = initial_tempo
    for i, bar in enumerate(bars):
        bpm = tempo(bar.get("tempo", bpm))
        bpms.append(bpm)
        seconds.append(seconds[-1] + (offsets[i + 1] - offsets[i]) / DIVISIONS * 60 / bpm)

    def seconds_at(tick):
        bar = min(bisect_right(offsets, tick) - 1, len(bars) - 1)
        return seconds[bar] + (tick - offsets[bar]) / DIVISIONS * 60 / bpms[bar]

    phrase = []
    result = {"status": "passed", "fingerings": {}, "states": {}, "transitions": [],
              "unsupported": [], "max_states": 0, "max_transitions": 0,
              "notes": 0, "total_cost": 0.0}

    def finish():
        if not phrase:
            return
        if config is None:
            # Keep the original four-argument call shape for existing integrations.
            planned = _plan_phrase([events[i] for i in phrase], tuning, locked, seconds_at)
        else:
            planned = _plan_phrase([events[i] for i in phrase], tuning, locked, seconds_at, config, lane)
        for i, pair in zip(phrase, planned):
            start, duration, pitches, _ = events[i]
            events[i] = (start, duration, pitches, (pair,))
        result["notes"] += len(phrase)
        if isinstance(planned, PlanPath):
            result["transitions"].extend(planned.transitions)
            result["total_cost"] += planned.cost
            result["max_states"] = max(result["max_states"], planned.max_states)
            result["max_transitions"] = max(result["max_transitions"], planned.max_transitions)
            for i, state in zip(phrase, planned.states):
                result["fingerings"][events[i][0]] = [state[3]]
                result["states"][events[i][0]] = list(state)
        phrase.clear()

    for i, (start, _, pitches, _) in enumerate(events):
        if _dead(pitches):
            result["unsupported"].append({"code": "dead_note_special_technique", "start": start})
            finish()
            continue
        if not pitches:
            continue
        if len(pitches) != 1:
            result["unsupported"].append({"code": "chord_connection", "start": start})
            finish()  # Chord fingering is still handled by positions().
            continue
        # Rests and section labels allow movement, not a free hand-position reset.
        phrase.append(i)
    finish()
    return result


def validate_score(data, config=None, return_playability=False, notation_only=False):
    """Validate compact events and optionally return the planner details.

    The five-value return shape is retained for existing callers.  New callers
    can request a sixth playability map without changing the events contract.
    """
    normalized_config = normalize_playability_config(config)
    fields(data, ("version", "title", "tempo", "time", "key", "bars", "tracks", "tunings"),
           ("version", "title", "tempo", "time", "bars", "tracks"))
    integer(data["version"], 1, 1, "version")
    if not isinstance(data["title"], str) or not data["title"].strip():
        raise ValueError("Title required")
    tempo(data["tempo"])
    integer(data.get("key", 0), -7, 7, "key fifths")
    current = meter(data["time"])
    if not isinstance(data["bars"], list) or not data["bars"]:
        raise ValueError("At least one explicit bar is required")
    lengths, meters = [], []
    for bar in data["bars"]:
        fields(bar, ("time", "tempo", "length", "section", "chord", "new_system", "new_page"))
        for key in ("new_system", "new_page"):
            if key in bar and type(bar[key]) is not bool:
                raise ValueError(f"{key} must be a boolean")
        current = meter(bar.get("time", list(current)))
        full = current[0] * 4 * DIVISIONS // current[1]
        length = ticks(bar.get("length", Fraction(full, DIVISIONS).__str__()))
        if not 0 < length <= full:
            raise ValueError("Bar length must be positive and no greater than its meter")
        if "tempo" in bar:
            tempo(bar["tempo"])
        for key in ("section", "chord"):
            if key in bar and not isinstance(bar[key], str):
                raise ValueError(f"{key} must be text")
        lengths.append(length)
        meters.append(current)
    tunings = dict(TUNINGS)
    fields(data.get("tunings", {}), TUNINGS)
    for lane, tuning in data.get("tunings", {}).items():
        if not isinstance(tuning, list) or len(tuning) != len(TUNINGS[lane]):
            raise ValueError("Tuning must list open MIDI pitches low to high")
        for pitch in tuning:
            integer(pitch, 0, 127, "open pitch")
        tunings[lane] = tuning
    offsets = [0]
    for length in lengths:
        offsets.append(offsets[-1] + length)
    lanes = {name: [] for name in LANES}
    locked_tabs = {lane: {} for lane in tunings}
    if not isinstance(data["tracks"], list):
        raise ValueError("tracks must be a list")
    seen = set()
    for track in data["tracks"]:
        fields(track, ("bar", "part", "events"), ("bar", "part", "events"))
        bar = integer(track["bar"], 1, len(lengths), "bar") - 1
        lane = track["part"]
        if not isinstance(lane, str) or lane not in lanes or (bar, lane) in seen:
            raise ValueError("Unknown part or duplicate bar/part record")
        seen.add((bar, lane))
        if not isinstance(track["events"], list):
            raise ValueError("events must be a list")
        for event in track["events"]:
            if not isinstance(event, list) or len(event) not in (3, 4, 5):
                raise ValueError("Event must be [offset,pitch-or-chord-or-null,duration,optional-tab,optional-options]")
            options = event[4] if len(event) == 5 else {}
            fields(options, ("dead", "dead_tabs", "tuplet", "grace"))
            if "dead" in options and type(options["dead"]) is not bool:
                raise ValueError("dead must be a boolean")
            dead = options.get("dead", False)
            triplet = "tuplet" in options
            if triplet and options["tuplet"] != [3, 2]:
                raise ValueError("Only explicit tuplet [3,2] is supported")
            start, pitch, duration = event[:3]
            start, duration = ticks(start, triplet), ticks(duration, triplet)
            if start >= lengths[bar] or duration <= 0 or offsets[bar] + start + duration > offsets[-1]:
                raise ValueError("Event outside score or invalid duration")
            pitches = [] if pitch is None else pitch if isinstance(pitch, list) else [pitch]
            if pitch == []:
                raise ValueError("Empty chord; use null for rest")
            for p in pitches:
                integer(p, 0, 127, "MIDI pitch")
            if len(set(pitches)) != len(pitches):
                raise ValueError("Duplicate pitch in chord")
            tab = event[3] if len(event) >= 4 else None
            if dead:
                if lane not in tunings or pitch is not None or not isinstance(tab, list) or not tab:
                    raise ValueError("Dead notes require null pitch and explicit TAB on a string part")
                pairs = []
                for pair in tab:
                    if not isinstance(pair, list) or len(pair) != 2:
                        raise ValueError("Dead-note TAB must be [string,fret]")
                    string, fret = pair
                    integer(string, 1, len(tunings[lane]), "string")
                    integer(fret, 0, _profile_for_lane(normalized_config, lane)["max_fret"], "fret")
                    pairs.append((string, fret))
                if len({s for s, _ in pairs}) != len(pairs):
                    raise ValueError("Dead-note chord needs distinct strings")
                tab = tuple(pairs)
                pitches = EventPitches([None] * len(tab), dead=True, tuplet=triplet,
                                       display_pitches=[tunings[lane][-s] + f for s, f in tab])
            elif lane in tunings and pitches:
                specified = tab is not None
                if notation_only and not specified:raise ValueError('Notation-only rendering requires explicit TAB for every pitched string event')
                tab = positions(
                    pitches, tunings[lane], tab,
                    max_fret=_profile_for_lane(normalized_config, lane)["max_fret"],
                )
                if specified:
                    locked_tabs[lane][offsets[bar] + start] = tab
            elif tab is not None:
                raise ValueError("TAB overrides only supported on sounding string parts")
            if 'dead_tabs' in options:
                muted=options['dead_tabs']
                if dead or lane not in tunings or not pitches or not isinstance(muted,list) or not muted:
                    raise ValueError('dead_tabs requires a sounding string chord and explicit muted strings')
                extra=[]
                for pair in muted:
                    if not isinstance(pair,list) or len(pair)!=2:raise ValueError('Expected muted [string,fret]')
                    s,f=pair
                    integer(s,1,len(tunings[lane]),'muted string');integer(f,0,24,'muted fret')
                    extra.append((s,f))
                tab=tuple(tab)+tuple(extra)
                if len({s for s,f in tab})!=len(tab):raise ValueError('Sounding and muted notes need distinct strings')
                display=list(pitches)+[tunings[lane][-s]+f for s,f in extra]
                pitches=EventPitches(list(pitches)+[None]*len(extra),dead=True,tuplet=triplet,display_pitches=display)
                dead=True
            if lane == "drums" and any(p not in DRUMS for p in pitches):
                raise ValueError("Unsupported drum MIDI pitch")
            if triplet and not dead:
                pitches = EventPitches(pitches, tuplet=True)
            if 'grace' in options:
                if lane not in ('keys_rh','keys_lh','drums') or not pitches or dead or triplet:
                    raise ValueError('Grace groups currently require ordinary sounding keyboard/drum events')
                grace=options['grace']
                if not isinstance(grace,list) or not grace:raise ValueError('Expected nonempty grace group')
                for gp,gd in grace:
                    integer(gp,0,127,'grace pitch')
                    if lane=='drums' and gp not in DRUMS:raise ValueError('Unsupported grace drum')
                    if ticks(gd) not in DURATIONS:raise ValueError('Unsupported grace written duration')
                pitches=EventPitches(pitches);pitches.grace=copy.deepcopy(grace)
            lanes[lane].append((offsets[bar] + start, duration, pitches, tab))
    for lane, events in lanes.items():
        end = 0
        for start, duration, _, _ in sorted(events, key=lambda e: e[0]):
            if start < end:
                raise ValueError(f"{lane}: overlap; combine simultaneous equal-duration pitches in a chord")
            end = start + duration
        events.sort(key=lambda e: e[0])
        # First implementation supports complete contiguous groups of three
        # equal written notes/rests, wholly inside one bar. Missing tuplet rests
        # must not become an unexplained fractional gap.
        group = []
        for event in events:
            if _triplet(event[2]):
                if not group:
                    if event[1] * Fraction(3, 2) not in DURATIONS:
                        raise ValueError("Unsupported tuplet written duration")
                elif event[0] != group[-1][0] + group[-1][1] or event[1] != group[0][1]:
                    raise ValueError("Tuplet groups must contain three contiguous equal notes/rests")
                group.append(event)
                if len(group) == 3:
                    first, last = group[0][0], group[-1][0] + group[-1][1]
                    bar_index = bisect_right(offsets, first) - 1
                    if last > offsets[bar_index + 1] or Fraction(first).denominator != 1:
                        raise ValueError("Tuplet group must start on the regular grid and stay in one bar")
                    group[0][2].tuplet_start = True
                    group[-1][2].tuplet_stop = True
                    group.clear()
            elif group:
                raise ValueError("Incomplete tuplet group; explicit tuplet rests required")
        if group:
            raise ValueError("Incomplete tuplet group; expected three notes/rests")
    playability = {}
    for lane, tuning in tunings.items():
        if notation_only:continue
        result = _plan_string_part(
            lanes[lane], tuning, locked_tabs[lane], data["bars"], offsets, data["tempo"],
            None if config is None else normalized_config, lane,
        )
        playability[lane] = result
    base = (lengths, meters, offsets, lanes, tunings)
    return base + (playability,) if return_playability else base


def _seconds_at_factory(bars, offsets, initial_tempo):
    seconds, bpms = [0.0], []
    bpm = initial_tempo
    for i, bar in enumerate(bars):
        bpm = tempo(bar.get("tempo", bpm))
        bpms.append(bpm)
        seconds.append(seconds[-1] + (offsets[i + 1] - offsets[i]) / DIVISIONS * 60 / bpm)

    def seconds_at(tick):
        bar = min(bisect_right(offsets, tick) - 1, len(bars) - 1)
        return seconds[bar] + (tick - offsets[bar]) / DIVISIONS * 60 / bpms[bar]

    return seconds_at


def _report_issue(code, lane, start=None, **extra):
    issue = {"code": code, "lane": lane}
    if start is not None:
        issue["start"] = start
    issue.update(extra)
    return issue


def _verify_planned_lane(events, plan, lane, tuning, profile, seconds_at):
    """Independently verify a chosen path; never trust its accumulated cost."""
    issues, transitions = [], []
    sounding = [event for event in events if event[2]]
    previous_event = None
    previous_state = None
    for event in sounding:
        start, duration, pitches, tab = event
        if _dead(pitches) or len(pitches) != 1:
            previous_event, previous_state = None, None
            continue
        state_values = plan.get("states", {}).get(start)
        if not state_values:
            issues.append(_report_issue("missing_planned_state", lane, start=start))
            continue
        state = tuple(state_values)
        string, fret, hand, finger, _, _ = _state_values(state)
        valid_string = 1 <= string <= len(tuning)
        valid_fret = 0 <= fret <= profile["max_fret"]
        if not valid_string or not valid_fret:
            issues.append(_report_issue("invalid_planned_tab", lane, start=start))
        if not 1 <= hand <= profile["max_fret"]:
            issues.append(_report_issue("invalid_hand_position", lane, start=start))
        if tab and (tab[0][0], tab[0][1]) != (string, fret):
            issues.append(_report_issue("planned_tab_state_mismatch", lane, start=start))
        if valid_string and valid_fret and tuning[-string] + fret != pitches[0]:
            issues.append(_report_issue("planned_tab_pitch_mismatch", lane, start=start))
        if fret == 0 and finger != 0:
            issues.append(_report_issue("open_string_has_finger", lane, start=start))
        if fret and finger not in profile["allowed_fingers"]:
            issues.append(_report_issue("disallowed_finger", lane, start=start, finger=finger))
        if fret and finger in profile["allowed_fingers"]:
            offset = fret - hand
            expected = profile["finger_offsets"][str(finger)]
            valid_offset = offset == expected or (
                finger == 4 and offset == expected + 1 and offset <= profile["extended_reach"]
            )
            if not valid_offset:
                issues.append(_report_issue("finger_position_mismatch", lane, start=start,
                                            finger=finger, offset=offset))
        if previous_event is not None and previous_state is not None:
            requirement = _transition_requirements(
                previous_state, state, previous_event, start, seconds_at, profile,
            )
            requirement["transition_cost"] = _transition_cost(
                previous_state, state, requirement["available_seconds"],
                seconds_at(start), profile,
            )
            requirement["cost_breakdown"] = _transition_cost_breakdown(
                previous_state, state, requirement["available_seconds"],
                seconds_at(start), profile,
            )
            transitions.append(requirement)
            for code in requirement["issues"]:
                issues.append(_report_issue(code, lane, start=start,
                                            from_start=requirement["from_start"],
                                            available_seconds=requirement["available_seconds"],
                                            required_shift_seconds=requirement["required_shift_seconds"],
                                            required_string_seconds=requirement["required_string_seconds"]))
        previous_event, previous_state = event, state
    return issues, transitions


def _report_from_validation(data, normalized, offsets, lanes, tunings, details):
    report = {
        "version": PLAYABILITY_VERSION,
        "model": normalized["model"],
        "config_sha256": _config_hash(normalized),
        "events_sha256": _events_hash(data),
        "parameters": copy.deepcopy(normalized),
        "heuristic": True,
        "calibration": "not_musician_calibrated",
        "checked_lanes": ["lead", "bass"],
        "check_scope": {
            "supported": ["lead", "bass", "single_note_segments"],
            "legacy": ["rhythm"],
            "unsupported": ["chord_connections", "let_ring", "bends", "slides", "other_special_techniques"],
        },
        "lanes": {},
        "issues": [],
        "unverified": [],
        "status": "passed",
    }
    seconds_at = _seconds_at_factory(data["bars"], offsets, data["tempo"])
    for lane in ("lead", "bass", "rhythm"):
        if lane not in lanes:
            continue
        profile = _planner_profile(normalized, lane)
        lane_detail = details.get(lane, {})
        lane_report = {
            "status": "passed",
            "scope": "legacy_v1" if lane == "rhythm" else "global_fingering_v1",
            "notes": lane_detail.get("notes", 0),
            "fingerings": [],
            "transitions": [],
            "max_states": lane_detail.get("max_states", 0),
            "max_transitions": lane_detail.get("max_transitions", 0),
            "total_cost": lane_detail.get("total_cost", 0.0),
            "hardest_transition": None,
            "cost_breakdown": {
                "transition": sum(item.get("transition_cost", 0.0)
                                   for item in lane_detail.get("transitions", [])),
                "string": 0.0,
                "shift": 0.0,
                "shift_start": 0.0,
                "repeat_shift": 0.0,
                "reverse_shift": 0.0,
                "fret": 0.0,
                "extension": 0.0,
                "comfort": 0.0,
                "open": 0.0,
                "shift_frets": sum(item.get("shift_distance_frets", 0)
                                    for item in lane_detail.get("transitions", [])),
                "string_distance": sum(item.get("string_distance", 0)
                                        for item in lane_detail.get("transitions", [])),
            },
            "unsupported": list(lane_detail.get("unsupported", [])),
            "issues": [],
        }
        for component in ("string", "shift", "shift_start", "repeat_shift", "reverse_shift"):
            lane_report["cost_breakdown"][component] = sum(
                item.get("cost_breakdown", {}).get(component, 0.0)
                for item in lane_detail.get("transitions", [])
            )
        for state_values in lane_detail.get("states", {}).values():
            _, fret, hand, _, _, _ = _state_values(tuple(state_values))
            if fret == 0:
                lane_report["cost_breakdown"]["open"] += (
                    profile["costs"]["open"] + profile["open_string_preference"]
                )
                continue
            extension = max(0, fret - hand - profile["hand_reach"])
            low, high = profile["comfortable_fret_range"]
            comfort_distance = max(low - fret, 0, fret - high)
            lane_report["cost_breakdown"]["fret"] += profile["costs"]["fret"] * fret
            lane_report["cost_breakdown"]["extension"] += profile["costs"]["extension"] * extension ** 2
            lane_report["cost_breakdown"]["comfort"] += profile["costs"]["comfort"] * comfort_distance ** 2
        for start, duration, pitches, tab in lanes[lane]:
            if not pitches or _dead(pitches) or len(pitches) != 1:
                continue
            fingers = lane_detail.get("fingerings", {}).get(start, [0])
            state = lane_detail.get("states", {}).get(start)
            if tab and lane in ("lead", "bass"):
                lane_report["fingerings"].append({
                    "start": start, "tab": list(tab[0]), "finger": fingers[0],
                    "hand": state[2] if state else None,
                })
        independent_issues, transitions = _verify_planned_lane(
            lanes[lane], lane_detail, lane, tunings[lane], profile, seconds_at,
        )
        lane_report["issues"].extend(independent_issues)
        lane_report["transitions"] = transitions
        fingering_by_start = {
            item["start"]: item for item in lane_report["fingerings"]
        }
        for transition in lane_report["transitions"]:
            transition["from_fingering"] = copy.deepcopy(
                fingering_by_start.get(transition.get("from_start"))
            )
            transition["to_fingering"] = copy.deepcopy(
                fingering_by_start.get(transition.get("to_start"))
            )
        if lane_report["transitions"]:
            def difficulty(item):
                available = max(float(item.get("available_seconds", 0.0)), 1e-9)
                ioi = max(float(item.get("ioi_seconds", 0.0)), 1e-9)
                shift_ratio = float(item.get("required_shift_seconds", 0.0)) / available
                string_ratio = float(item.get("required_string_seconds", 0.0)) / ioi
                return (
                    bool(item.get("issues")),
                    max(shift_ratio, string_ratio),
                    float(item.get("transition_cost", 0.0)),
                )
            lane_report["hardest_transition"] = max(
                lane_report["transitions"], key=difficulty,
            )
        else:
            lane_report["hardest_transition"] = None
        if lane_report["unsupported"]:
            lane_report["status"] = "incomplete"
            report["unverified"].extend(item["code"] for item in lane_report["unsupported"])
        if lane_report["issues"]:
            lane_report["status"] = "needs_repair"
            report["issues"].extend(lane_report["issues"])
        report["lanes"][lane] = lane_report

    all_transitions = []
    for lane, lane_report in report["lanes"].items():
        for transition in lane_report.get("transitions", []):
            item = copy.deepcopy(transition)
            item["lane"] = lane
            all_transitions.append(item)
    if all_transitions:
        def global_difficulty(item):
            available = max(float(item.get("available_seconds", 0.0)), 1e-9)
            ioi = max(float(item.get("ioi_seconds", 0.0)), 1e-9)
            return (
                bool(item.get("issues")),
                max(float(item.get("required_shift_seconds", 0.0)) / available,
                    float(item.get("required_string_seconds", 0.0)) / ioi),
                float(item.get("transition_cost", 0.0)),
            )
        report["hardest_transition"] = max(all_transitions, key=global_difficulty)
    else:
        report["hardest_transition"] = None

    # Keep unsupported features explicit even when a lane contains no notes.
    if any(item.get("code") == "chord_connection" for lane in details.values() for item in lane.get("unsupported", [])):
        report["unverified"].append("chord_connection")
    report["unverified"] = sorted(set(report["unverified"]))
    if report["issues"]:
        report["status"] = "needs_repair"
    elif report["unverified"]:
        report["status"] = "incomplete"
    return _json_numbers(report)


def _failure_report(data, normalized, exc):
    report = {
        "version": PLAYABILITY_VERSION,
        "model": normalized["model"],
        "config_sha256": _config_hash(normalized),
        "events_sha256": _events_hash(data),
        "parameters": copy.deepcopy(normalized),
        "heuristic": True,
        "calibration": "not_musician_calibrated",
        "checked_lanes": ["lead", "bass"],
        "check_scope": {
            "supported": ["lead", "bass", "single_note_segments"],
            "legacy": ["rhythm"],
            "unsupported": ["chord_connections", "let_ring", "bends", "slides", "other_special_techniques"],
        },
        "lanes": {}, "issues": [], "unverified": [],
        "hardest_transition": None,
        "status": "needs_repair",
    }
    report.update(exc.report)
    failure_transitions = []
    for lane in sorted({issue.get("lane") for issue in report.get("issues", []) if issue.get("lane")}):
        lane_issues = [issue for issue in report["issues"] if issue.get("lane") == lane]
        hardest = next((issue for issue in lane_issues
                        if "from_state" in issue and "to_state" in issue), None)
        if hardest is not None:
            hardest = copy.deepcopy(hardest)
            hardest["lane"] = lane
            failure_transitions.append(hardest)
        report["lanes"][lane] = {
            "status": report["status"],
            "scope": "global_fingering_v1" if lane in ("lead", "bass") else "legacy_v1",
            "notes": 0,
            "fingerings": [],
            "transitions": [],
            "hardest_transition": hardest,
            "cost_breakdown": {},
            "unsupported": [],
            "issues": lane_issues,
        }
    if failure_transitions:
        report["hardest_transition"] = failure_transitions[0]
    return report


def check_playability(data, config=None):
    """Plan and independently audit supported single-note string parts."""
    normalized = normalize_playability_config(config)
    try:
        _, _, offsets, lanes, tunings, details = validate_score(
            data, normalized, return_playability=True,
        )
    except PlayabilityError as exc:
        return _failure_report(data, normalized, exc)
    return _report_from_validation(data, normalized, offsets, lanes, tunings, details)


def normalize_repair_constraints(constraints=None):
    if constraints is None:
        return {"version": 1, "adjustable": [], "protected": []}
    if not isinstance(constraints, dict):
        raise ValueError("repair constraints must be an object")
    fields(constraints, ("version", "adjustable", "protected"), ("version",))
    if type(constraints["version"]) is not int or constraints["version"] != 1:
        raise ValueError("Unsupported repair constraints version")
    result = {"version": 1, "adjustable": [], "protected": []}
    allowed_operations = {"refinger", "shorten", "octave", "remove"}
    for key in ("adjustable", "protected"):
        entries = constraints.get(key, [])
        if not isinstance(entries, list):
            raise ValueError(f"constraints.{key} must be a list")
        for entry in entries:
            if not isinstance(entry, dict):
                raise ValueError("constraint entry must be an object")
            allowed = {"lane", "bar", "offset", "pitch", "operations"}
            fields(entry, allowed, ("lane", "bar", "offset"))
            if entry["lane"] not in LANES:
                raise ValueError("constraint lane is unknown")
            integer(entry["bar"], 1, 1_000_000, "constraint bar")
            ticks(entry["offset"])
            if "pitch" in entry:
                integer(entry["pitch"], 0, 127, "constraint pitch")
            operations = entry.get("operations", ["refinger", "shorten", "octave", "remove"] if key == "adjustable" else [])
            if not isinstance(operations, list) or any(op not in allowed_operations for op in operations):
                raise ValueError("constraint operations are invalid")
            normalized = dict(entry)
            normalized["offset"] = float(entry["offset"])
            normalized["operations"] = list(operations)
            result[key].append(normalized)
    return result


def load_repair_constraints(source):
    if source is None:
        return normalize_repair_constraints()
    return normalize_repair_constraints(json.loads(Path(source).read_text(encoding="utf-8")))


def _constraint_matches(entry, lane, bar, offset, pitches):
    if entry["lane"] != lane or entry["bar"] != bar or ticks(entry["offset"]) != offset:
        return False
    return "pitch" not in entry or entry["pitch"] in pitches


def _editable_events(data, constraints, operation):
    for track in data["tracks"]:
        lane, bar = track["part"], track["bar"]
        if lane not in ("lead", "bass"):
            continue
        for index, event in enumerate(track["events"]):
            if len(event) == 5 and event[4]:
                # Automatic repair does not own special techniques or their
                # explicitly grouped rhythm. Preserve them unchanged.
                continue
            offset = ticks(event[0])
            pitch = event[1]
            pitches = [] if pitch is None else pitch if isinstance(pitch, list) else [pitch]
            # Chord connections are outside the first release's repair scope;
            # never remove or octave-shift an entire chord through a single-note
            # constraint.
            if len(pitches) != 1:
                continue
            # A protected declaration always wins if the same event is listed in
            # both sets.  Missing classification is already protected by the
            # empty-adjustable default, so repair paths can only touch explicit
            # and unprotected events.
            if any(_constraint_matches(item, lane, bar, offset, pitches)
                   for item in constraints["protected"]):
                continue
            matches = [item for item in constraints["adjustable"]
                       if _constraint_matches(item, lane, bar, offset, pitches)
                       and operation in item.get("operations", [])]
            if matches:
                yield track, index, event, matches[0]


def _apply_repair_operation(data, constraints, operation):
    candidate = copy.deepcopy(data)
    changes = []
    if operation == "refinger":
        # Removing a manual TAB is only legal when the constraint explicitly permits it.
        for track, index, event, _ in _editable_events(candidate, constraints, operation):
            if len(event) == 4:
                previous_tab = copy.deepcopy(event[3])
                event.pop()
                changes.append({"lane": track["part"], "bar": track["bar"], "offset": event[0],
                                "change": "refinger", "from_tab": previous_tab,
                                "to_tab": "replanned"})
        return candidate, changes
    if operation == "remove":
        removals = []
        for track, index, event, _ in _editable_events(candidate, constraints, operation):
            removals.append((track, index, event))
        for track, index, event in reversed(removals):
            removed_event = copy.deepcopy(event)
            track["events"].pop(index)
            changes.append({"lane": track["part"], "bar": track["bar"], "offset": event[0],
                            "change": "remove", "from_event": removed_event})
        return candidate, changes
    for track, index, event, _ in _editable_events(candidate, constraints, operation):
        if operation == "octave":
            pitch = event[1]
            if isinstance(pitch, int):
                choices = [pitch + 12, pitch - 12]
                new_pitch = next((value for value in choices if 0 <= value <= 127), None)
                if new_pitch is not None and new_pitch != pitch:
                    event[1] = new_pitch
                    if len(event) == 4:
                        event.pop()
                    changes.append({"lane": track["part"], "bar": track["bar"], "offset": event[0],
                                    "change": "octave", "from": pitch, "to": new_pitch})
        elif operation == "shorten":
            start = Fraction(str(event[0]))
            duration = Fraction(str(event[2]))
            following = [Fraction(str(other[0])) for other in track["events"]
                         if other is not event and other[0] is not None and Fraction(str(other[0])) > start]
            if following:
                grid_step = Fraction(1, DIVISIONS)
                available_duration = min(following) - start
                new_duration = min(duration - grid_step, available_duration)
                new_duration = max(grid_step, new_duration)
                if new_duration < duration:
                    event[2] = float(new_duration)
                    changes.append({"lane": track["part"], "bar": track["bar"], "offset": event[0],
                                    "change": "shorten", "from": float(duration), "to": float(new_duration)})
    return candidate, changes


def _status_rank(status):
    return {"passed": 0, "incomplete": 1, "needs_repair": 2, "computation_incomplete": 3}.get(status, 4)


def _issue_signature(issue):
    return tuple(
        (key, issue.get(key))
        for key in ("code", "lane", "start", "note_index", "bar", "offset")
        if key in issue
    )


def repair_playability(data, config=None, constraints=None, max_attempts=3):
    """Try bounded, explicitly authorized local repairs without mutating input."""
    if type(max_attempts) is not int or not 0 <= max_attempts <= 3:
        raise ValueError("max_attempts must be an integer in 0..3")
    normalized_constraints = normalize_repair_constraints(constraints)
    normalized_config = normalize_playability_config(config)
    working = copy.deepcopy(data)
    initial = check_playability(working, normalized_config)
    attempts = []
    if initial["status"] in ("passed", "incomplete"):
        return {"version": 1, "status": initial["status"], "events": working,
                "best_draft": working,
                "events_sha256": _events_hash(working),
                "config_sha256": _config_hash(normalized_config),
                "parameters": copy.deepcopy(normalized_config),
                "attempts": attempts,
                "initial_report": initial, "final_report": initial}
    current_report = initial
    for strategy, operation in (("refinger", "refinger"), ("timing_or_octave", "shorten"), ("simplify", "remove")):
        if len(attempts) >= max_attempts:
            break
        before_report = copy.deepcopy(current_report)
        candidate, changes = _apply_repair_operation(working, normalized_constraints, operation)
        if strategy == "timing_or_octave" and not changes:
            candidate, changes = _apply_repair_operation(working, normalized_constraints, "octave")
        candidate_report = check_playability(candidate, normalized_config)
        current_issue_set = {_issue_signature(item) for item in current_report.get("issues", [])}
        candidate_issue_set = {_issue_signature(item) for item in candidate_report.get("issues", [])}
        new_issues = sorted(candidate_issue_set - current_issue_set, key=repr)
        accepted = bool(changes) and (
            _status_rank(candidate_report["status"]) < _status_rank(current_report["status"]) or
            (_status_rank(candidate_report["status"]) == _status_rank(current_report["status"])
             and len(candidate_report.get("issues", [])) < len(current_report.get("issues", [])))
        ) and not new_issues
        reasons = {
            "refinger": "保持音高、起音和时值，重新开放明确可调整事件的指法路径",
            "timing_or_octave": "仅在明确可调整事件上释放局部换把时间或尝试八度替代",
            "simplify": "删除明确标记的非核心装饰音，保留核心节奏重心",
        }
        attempts.append({"strategy": strategy, "reason": reasons[strategy],
                         "accepted": accepted, "changes": changes,
                         "before_events_sha256": _events_hash(working),
                         "after_events_sha256": _events_hash(candidate),
                         "new_issues": [list(item) for item in new_issues],
                         "before_report": before_report,
                         "after_report": candidate_report,
                         "report": candidate_report})
        if accepted:
            working, current_report = candidate, candidate_report
    return {"version": 1, "status": current_report["status"], "events": working,
            "best_draft": working,
            "events_sha256": _events_hash(working),
            "config_sha256": _config_hash(normalized_config),
            "parameters": copy.deepcopy(normalized_config),
            "attempts": attempts,
            "initial_report": initial, "final_report": current_report}


def pitch_xml(parent, pitch, prefix="", flats=False):
    names = ([("C", 0), ("D", -1), ("D", 0), ("E", -1), ("E", 0), ("F", 0),
              ("G", -1), ("G", 0), ("A", -1), ("A", 0), ("B", -1), ("B", 0)] if flats else
             [("C", 0), ("C", 1), ("D", 0), ("D", 1), ("E", 0), ("F", 0),
              ("F", 1), ("G", 0), ("G", 1), ("A", 0), ("A", 1), ("B", 0)])
    step, alter = names[pitch % 12]
    add(parent, prefix + "step", step)
    if alter:
        add(parent, prefix + "alter", alter)
    add(parent, prefix + "octave", pitch // 12 - 1)


def emit_notes(measure, lane, start, end, pitches, tab, voice, staff, tie_in, tie_out, flats,
                xml_scale=1):
    triplet = _triplet(pitches)
    if not tie_in:
        for gp,gd in getattr(pitches,'grace',[]):
            note=add(measure,'note');add(note,'grace',slash='yes')
            if lane=='drums':
                step,octave,head=DRUMS[gp];u=add(note,'unpitched')
                add(u,'display-step',step);add(u,'display-octave',octave)
                add(note,'instrument',id=f'P5-I{gp}')
            else:pitch_xml(add(note,'pitch'),gp,flats=flats)
            add(note,'voice',voice)
            kind,dotted=DURATIONS[ticks(gd)];add(note,'type',kind)
            if dotted:add(note,'dot')
            if lane=='drums':add(note,'notehead',head)
            if staff:add(note,'staff',staff)
    pieces = []
    ratio = Fraction(3, 2) if triplet else 1
    remaining = (end - start) * ratio
    while remaining:
        size = next((size for size in DURATIONS if size <= remaining), None)
        if size is None:
            raise ValueError("Duration cannot be engraved without explicit tuplet metadata")
        pieces.append(size)
        remaining -= size
    for i, size in enumerate(pieces):
        ties = (["stop"] if tie_in or i > 0 else []) + (["start"] if tie_out or i < len(pieces) - 1 else [])
        for j, pitch in enumerate(pitches or [None]):
            dead = _dead(pitches) and pitch is None
            note = add(measure, "note")
            if j:
                add(note, "chord")
            if dead:
                # This pitch is only a TAB positioning carrier. Native export
                # must run apply_native_dead_notes before audio/MIDI export.
                pitch_xml(add(note, "pitch"), pitches.display_pitches[j], flats=flats)
            elif pitch is None:
                add(note, "rest")
            elif lane == "drums":
                step, octave, _ = DRUMS[pitch]
                u = add(note, "unpitched")
                add(u, "display-step", step)
                add(u, "display-octave", octave)
            else:
                pitch_xml(add(note, "pitch"), pitch, flats=flats)
            add(note, "duration", int(size / ratio * xml_scale))
            if pitch is not None or dead:
                for kind in ties:
                    add(note, "tie", type=kind)
            if lane == "drums" and pitch is not None:
                add(note, "instrument", id=f"P5-I{pitch}")
            add(note, "voice", voice)
            kind, dotted = DURATIONS[size]
            add(note, "type", kind)
            if dotted:
                add(note, "dot")
            if triplet:
                modification = add(note, "time-modification")
                add(modification, "actual-notes", 3)
                add(modification, "normal-notes", 2)
                add(modification, "normal-type", kind)
            if dead:
                add(note, "notehead", "x")
            if lane == "drums" and pitch is not None:
                add(note, "stem", "up")
                add(note, "notehead", DRUMS[pitch][2])
            if staff:
                add(note, "staff", staff)
            if ((pitch is not None or dead) and (tab or ties)) or triplet:
                notation = add(note, "notations")
                for kind in ties:
                    add(notation, "tied", type=kind)
                if tab:
                    technical = add(notation, "technical")
                    add(technical, "string", tab[j][0])
                    add(technical, "fret", tab[j][1])
                    if dead:
                        add(technical, "other-technical", "dead-note")
                if triplet and j == 0:
                    if getattr(pitches, "tuplet_start", False):
                        add(notation, "tuplet", type="start", number=1, bracket="yes")
                    if getattr(pitches, "tuplet_stop", False):
                        add(notation, "tuplet", type="stop", number=1)
            if dead:
                add(add(note, "play"), "mute", "on")


def build_score(data, config=None, return_report=False, notation_only=False):
    lengths, meters, offsets, lanes, tunings, plan_details = validate_score(
        data, config, return_playability=True,notation_only=notation_only,
    )
    normalized = normalize_playability_config(config)
    xml_scale = 3 if any(_triplet(event[2]) for events in lanes.values() for event in events) else 1
    if notation_only:
        playability_report={'version':1,'status':'incomplete','events_sha256':_events_hash(data),
            'config_sha256':_config_hash(normalized),'parameters':normalized,'issues':[],
            'unverified':['physical_performance_of_preserved_reference_tablature'],
            'check_scope':{'supported':['pitch_string_fret_consistency','rhythmic_representation'],
                           'not_checked':['movement_timing','chord_connections','special_techniques']},
            'mode':'notation_only','musical_accuracy_verified':False}
    else:
        playability_report = _report_from_validation(data, normalized, offsets, lanes, tunings, plan_details)
    root = ET.Element("score-partwise", version="4.0")
    add(add(root, "work"), "work-title", data["title"])
    defaults = add(root, "defaults")
    scale = add(defaults, "scaling")
    add(scale, "millimeters", 7)
    add(scale, "tenths", 40)
    layout = add(defaults, "page-layout")
    add(layout, "page-height", 297 * 40 / 7)
    add(layout, "page-width", 210 * 40 / 7)
    margins = add(layout, "page-margins", type="both")
    for edge in ("left", "right", "top", "bottom"):
        add(margins, f"{edge}-margin", 12 * 40 / 7)
    system = add(defaults, "system-layout")
    system_margins = add(system, "system-margins")
    add(system_margins, "left-margin", 0)
    add(system_margins, "right-margin", 0)
    listing = add(root, "part-list")
    drum_pitches = sorted({p for _, _, ps, _ in lanes["drums"] for p in ps}) or [36]
    for number, (part, name, program) in enumerate(PARTS, 1):
        pid = f"P{number}"
        definition = add(listing, "score-part", id=pid)
        add(definition, "part-name", name)
        for pitch in drum_pitches if part == "drums" else [None]:
            iid = f"{pid}-I{pitch if pitch is not None else 1}"
            add(add(definition, "score-instrument", id=iid), "instrument-name", name if pitch is None else f"GM {pitch}")
        for pitch in drum_pitches if part == "drums" else [None]:
            midi = add(definition, "midi-instrument", id=f"{pid}-I{pitch if pitch is not None else 1}")
            add(midi, "midi-channel", 10 if part == "drums" else number)
            add(midi, "midi-program", program)
            if pitch is not None:
                add(midi, "midi-unpitched", pitch + 1)
        part_el = add(root, "part", id=pid)
        for index, bar in enumerate(data["bars"]):
            n, d = meters[index]
            measure = add(part_el, "measure", number=index + 1)
            if bar.get("new_system") or bar.get("new_page"):
                layout_break = add(measure, "print")
                if bar.get("new_system"):
                    layout_break.set("new-system", "yes")
                if bar.get("new_page"):
                    layout_break.set("new-page", "yes")
            if lengths[index] != n * 4 * DIVISIONS // d:
                measure.set("implicit", "yes")
            if index == 0 or "time" in bar:
                attrs = add(measure, "attributes")
                add(attrs, "divisions", DIVISIONS * xml_scale)
                if index == 0:
                    add(add(attrs, "key"), "fifths", data.get("key", 0))
                t = add(attrs, "time")
                add(t, "beats", n)
                add(t, "beat-type", d)
                if index == 0:
                    if part == "keys":
                        add(attrs, "staves", 2)
                        for s, sign, line in [(1, "G", 2), (2, "F", 4)]:
                            clef = add(attrs, "clef", number=s)
                            add(clef, "sign", sign)
                            add(clef, "line", line)
                    else:
                        clef = add(attrs, "clef")
                        add(clef, "sign", "percussion" if part == "drums" else "TAB")
                        add(clef, "line", 2 if part == "drums" else 5)
                    if part in tunings:
                        details = add(attrs, "staff-details")
                        add(details, "staff-lines", len(tunings[part]))
                        for line, pitch in enumerate(tunings[part], 1):
                            pitch_xml(add(details, "staff-tuning", line=line), pitch, "tuning-")
            if number == 1:
                if index == 0 or "tempo" in bar:
                    direction = add(measure, "direction", placement="above")
                    met = add(add(direction, "direction-type"), "metronome")
                    add(met, "beat-unit", "quarter")
                    bpm = tempo(bar.get("tempo", data["tempo"]))
                    add(met, "per-minute", bpm)
                    add(direction, "sound", tempo=bpm)
                for label in ("section", "chord"):
                    if bar.get(label):
                        add(add(add(measure, "direction", placement="above"), "direction-type"),
                            "rehearsal" if label == "section" else "words", bar[label])
            for voice, lane in enumerate(("keys_rh", "keys_lh") if part == "keys" else (part,), 1):
                staff = voice if part == "keys" else None
                if voice > 1:
                    add(add(measure, "backup"), "duration", lengths[index] * xml_scale)
                cursor, limit = offsets[index], offsets[index + 1]
                for start, duration, pitches, tab in lanes[lane]:
                    end = start + duration
                    if end <= cursor or start >= limit:
                        continue
                    if start > cursor:
                        emit_notes(measure, lane, cursor, start, [], None, voice, staff, False, False, False, xml_scale)
                    emit_notes(measure, lane, max(cursor, start), min(end, limit), pitches, tab,
                               voice, staff, start < cursor, end > limit, data.get("key", 0) < 0, xml_scale)
                    cursor = min(end, limit)
                if cursor < limit:
                    emit_notes(measure, lane, cursor, limit, [], None, voice, staff, False, False, False, xml_scale)
    playability_report["score_xml_sha256"] = hashlib.sha256(
        ET.tostring(root, encoding="utf-8"),
    ).hexdigest()
    return (root, playability_report) if return_report else root


def write_score(data, output, config=None, report_path=None, notation_only=False):
    normalized = normalize_playability_config(config)
    try:
        root, playability_report = build_score(data, config, return_report=True,notation_only=notation_only)
    except PlayabilityError as exc:
        if report_path:
            report_file = Path(report_path)
            report_file.parent.mkdir(parents=True, exist_ok=True)
            report_file.write_text(
                json.dumps(_failure_report(data, normalized, exc), ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
        raise
    ET.indent(root, space="  ")
    path = Path(output)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("xb") as handle:
        ET.ElementTree(root).write(handle, encoding="utf-8", xml_declaration=True)
    if report_path:
        report_file = Path(report_path)
        report_file.parent.mkdir(parents=True, exist_ok=True)
        report_file.write_text(
            json.dumps(playability_report, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
    return {"output": str(path), "bars": len(data["bars"]), "parts": 5,
            "events": sum(len(t["events"]) for t in data["tracks"]),
            "playability_status": playability_report["status"]}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("build", "inspect", "check-playability", "repair-playability"))
    parser.add_argument("--events", type=Path, required=True)
    parser.add_argument("--output", type=Path)
    parser.add_argument("--bar", type=int)
    parser.add_argument("--config", type=Path)
    parser.add_argument("--constraints", type=Path)
    parser.add_argument("--report", type=Path)
    parser.add_argument("--max-attempts", type=int, default=3)
    parser.add_argument('--notation-only',action='store_true',help='Render supplied TAB for review; never claim playability passed')
    args = parser.parse_args()
    data = json.loads(args.events.read_text(encoding="utf-8"))
    config = load_playability_config(args.config)
    if args.command == "check-playability":
        result = check_playability(data, config)
        if args.report:
            args.report.parent.mkdir(parents=True, exist_ok=True)
            args.report.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0 if result["status"] == "passed" else 2
    if args.command == "repair-playability":
        if args.output is None:
            parser.error("repair-playability requires --output")
        result = repair_playability(
            data, config, load_repair_constraints(args.constraints), args.max_attempts,
        )
        output = Path(args.output)
        output.parent.mkdir(parents=True, exist_ok=True)
        with output.open("x", encoding="utf-8") as handle:
            json.dump(result["events"], handle, ensure_ascii=False, indent=2)
        if args.report:
            args.report.parent.mkdir(parents=True, exist_ok=True)
            args.report.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
        print(json.dumps({"output": str(output), "status": result["status"],
                          "attempts": len(result["attempts"])}, ensure_ascii=False))
        return 0 if result["status"] in ("passed", "incomplete") else 2
    if args.command == "build":
        if args.output is None:
            parser.error("build requires --output")
        result = write_score(data, args.output, config, args.report,notation_only=args.notation_only)
    else:
        validate_score(data, config)
        result = {"bars": len(data["bars"]), "events": sum(len(t["events"]) for t in data["tracks"])}
        if args.bar is not None:
            integer(args.bar, 1, len(data["bars"]), "bar")
            result["bar"] = data["bars"][args.bar - 1]
            result["tracks"] = [t for t in data["tracks"] if t["bar"] == args.bar]
    print(json.dumps(result, ensure_ascii=False))


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")
    try:
        raise SystemExit(main())
    except (ValueError, OSError, TypeError) as exc:
        print(f"score events: {exc}", file=sys.stderr)
        raise SystemExit(2)
