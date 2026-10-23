"""Normalize only published current-round fields; no inferred factions."""

import re


def mode_from_layer(layer):
    found = re.search(
        r"(?:^|[ _-])(RAAS|AAS|Invasion|Insurgency|Seed|Skirmish|Destruction|TC|Training)(?:[ _-]|$)",
        str(layer or ""),
        re.I,
    )
    names = {
        "raas": "RAAS",
        "aas": "AAS",
        "invasion": "Invasion",
        "insurgency": "Insurgency",
        "seed": "Seed",
        "skirmish": "Skirmish",
        "destruction": "Destruction",
        "tc": "TC",
        "training": "Training",
    }
    return names.get(found[1].lower(), "") if found else ""


def same_layer(left, right):
    norm = lambda s: re.sub(r"[\s_]+", " ", str(s or "")).strip().casefold()
    return bool(left and right and norm(left) == norm(right))


def queue_count(value):
    if type(value) is int and 0 <= value <= 1000:
        return value
    if isinstance(value, list) and len(value) <= 1000:
        return len(value)
    return None


def sqstat_metadata(data):
    layer = data.get("map") if isinstance(data.get("map"), str) else ""
    factions = []
    teams = data.get("teams")
    if isinstance(teams, list) and len(teams) == 2:
        by_id = {str(t.get("id")): t for t in teams if isinstance(t, dict)}
        if set(by_id) == {"1", "2"}:
            for ident in ("1", "2"):
                name = by_id[ident].get("name")
                if not isinstance(name, str) or not name.strip():
                    factions = []
                    break
                factions.append(name[:80])
    return {
        "map": layer[:200],
        "mode": mode_from_layer(layer),
        "queue": queue_count(data.get("queue_players")),
        "factions": factions,
    }
