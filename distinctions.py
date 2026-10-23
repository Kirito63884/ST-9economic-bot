"""Permanent distinctions backed by verifiable public SQSTAT evidence."""

import json
from collections import Counter
from distinctions_catalog import BLOCKED, KIT_RULES
from distinctions_source import VERSION


def weekly(documents, week):
    from medals import ranks

    selected = [d for d in documents if d["week"] == week]
    knife = Counter()
    sl = Counter()
    unknown = Counter()
    anti = Counter()
    knife_complete = sl_verified = anti_complete = bool(selected)
    unverified = 0
    for d in selected:
        ext = d.get("distinctions", {})
        if ext.get("schema") not in (1, VERSION):
            knife_complete = sl_verified = anti_complete = False
            unverified += 1
            continue
        if not ext.get("knife_verified"):
            knife_complete = False
        else:
            knife.update(ext.get("knife", {}))
        if not ext.get("sl_verified"):
            sl_verified = False
        elif d["winner"] is None:
            for team in ext["sl_teams"]:
                unknown.update(team)
            for team in ext.get("sl_unknown", [[], []]):
                unknown.update(team)
        elif d["winner"] in (1, 2):
            sl.update(ext["sl_teams"][d["winner"] - 1])
            unknown.update(ext.get("sl_unknown", [[], []])[d["winner"] - 1])
        if ext.get("schema") == VERSION and ext.get("anti_verified"):
            anti.update(ext.get("anti_confirmed", {}))
        else:
            anti_complete = False
    safe = []
    ranked = ranks(sl)
    if sl_verified:
        for sid, value in ranked.items():
            if value["rank"] > 3 or unknown[sid]:
                continue
            worst = 1 + sum(
                1
                for other in set(sl) | set(unknown)
                if other != sid and sl[other] + unknown[other] > sl[sid]
            )
            if worst == value["rank"]:
                safe.append(sid)
    return {
        "schema": VERSION,
        "knife_complete": knife_complete,
        "sl_verified": sl_verified,
        "sl_complete": sl_verified and not unknown,
        "sl_safe": safe,
        "unverified_matches": unverified,
        "sl_unknown_results": sum(unknown.values()),
        "ranks": {
            "knife": ranks(knife),
            "sl": ranked,
            "anti": ranks(anti) if anti_complete else {},
        },
        "anti_complete": anti_complete,
        "anti_confirmed": dict(anti),
    }


def kit_value(ext, kits):
    if not ext.get("kits_verified"):
        return None
    values = ext.get("kit_minutes", {})
    selected = [values[k] for k in kits if k in values]
    if not selected or any(type(v) is not int or v < 0 for v in selected):
        return None
    return sum(selected)


def anti_total(profile, weeks, live, sid):
    # Historical top-table and recorded matches overlap: use max, never sum them.
    ext = (profile or {}).get("distinctions", {})
    historical = ext.get("anti_confirmed", 0) if ext.get("anti_verified") else 0
    recorded = sum(
        d.get("distinctions", {}).get("anti_confirmed", {}).get(sid, 0)
        for _, d in weeks
    )
    recorded += live.get("distinctions", {}).get("anti_confirmed", {}).get(sid, 0)
    return max(historical, recorded)


def evaluate(db, users, weeks, now, include_totals=True):
    from medals import MedalMonitor, grant, week_label
    from automation_control import setting

    status = db.get(MedalMonitor, 1)
    live = json.loads(status.payload).get("live", {}) if status else {}
    # Live data may still be the last closed week during an outage; avoid overlap.
    if any(w == live.get("week") for w, _ in weeks):
        live = {}
    for user in users:
        if not user.steam_id:
            continue
        sid = user.steam_id
        for week, document in weeks:
            ext = document.get("distinctions", {})
            if not document.get("complete"):
                continue
            damage = document.get("ranks", {}).get("damage", {}).get(sid)
            if damage and 0 < damage["rank"] <= setting(db, "weekly_best_crew_rank"):
                grant(
                    db,
                    user,
                    "best_crew",
                    f"{week_label(week)}: {damage['rank']}-е место среди всех игроков Protocol, {damage['value']} единиц урона.",
                    {"week": week, "metric": "damage", **damage, "source": "SQSTAT"},
                    now,
                )
            if ext.get("schema") not in (1, VERSION):
                continue
            for code, metric, limit, description in (
                (
                    "best_knife",
                    "knife",
                    setting(db, "weekly_best_knife_rank"),
                    "ножевых убийств",
                ),
                (
                    "squad_leader",
                    "sl",
                    setting(db, "weekly_squad_leader_rank"),
                    "побед сквадным",
                ),
            ):
                if metric == "knife" and (week, "knife") in db.info.get(
                    "protocol_export_primary", set()
                ):
                    continue
                if metric == "sl" and (
                    (week, "slwins") in db.info.get("protocol_export_primary", set())
                    or week in db.info.get("protocol_week_primary", set())
                ):
                    continue
                row = ext.get("ranks", {}).get(metric, {}).get(sid)
                verified = (
                    ext.get("knife_complete")
                    if metric == "knife"
                    else ext.get("sl_complete") or sid in ext.get("sl_safe", [])
                )
                if row and row["rank"] <= limit and verified:
                    grant(
                        db,
                        user,
                        code,
                        f"{week_label(week)}: {row['rank']}-е место среди всех игроков Protocol, {row['value']} {description}.",
                        {
                            "week": week,
                            "metric": metric,
                            **row,
                            "source": "SQSTAT",
                            "sl_basis": "Кит в итоговой таблице SQSTAT: SL, SLCrewman, SLPilot"
                            if metric == "sl"
                            else None,
                        },
                        now,
                    )
            anti = ext.get("ranks", {}).get("anti", {}).get(sid)
            if (
                (week, "handat") not in db.info.get("protocol_export_primary", set())
                and ext.get("schema") == VERSION
                and ext.get("anti_complete")
                and anti
                and 0 < anti["rank"] <= setting(db, "weekly_anti_tank_rank")
            ):
                grant(
                    db,
                    user,
                    "anti_tank",
                    f"{week_label(week)}: {anti['rank']}-е место среди всех игроков Protocol, {anti['value']} уничтожений техники трубами и тандемами. Мины и стрельба с техники исключены.",
                    {
                        "week": week,
                        "metric": "anti",
                        **anti,
                        "source": "SQSTAT",
                        "complete_weapons": True,
                    },
                    now,
                )


def progress_values(code, profile, live, weeks, sid):
    if code in ("best_driver", "best_pistol"):
        return [
            {
                "label": "Часы на ките мехвода за неделю"
                if code == "best_driver"
                else "Убийства с пистолета за неделю",
                "value": None,
                "rank": None,
                "target_rank": 1,
                "available": False,
            }
        ], BLOCKED[code]
    if code == "medical_aid":
        row = (
            live.get("ranks", {})
            .get("revive", {})
            .get(sid, {"rank": None, "value": None})
        )
        return [
            {
                "label": "Поднятия за неделю",
                "value": row["value"],
                "rank": row["rank"],
                "target_rank": 8,
            }
        ], None
    if code == "best_pilot":
        return [
            {
                "label": "Часы за неделю из выгрузки Protocol",
                "value": None,
                "rank": None,
                "target_rank": 1,
                "available": False,
            }
        ], "Ожидаем подтверждённое первое место недели Protocol."
    if code == "best_crew":
        row = (
            live.get("ranks", {}).get("damage", {}).get(sid, {"rank": None, "value": 0})
        )
        return [
            {
                "label": "Урон за неделю",
                "value": row["value"],
                "rank": row["rank"],
                "target_rank": 5,
            }
        ], None
    if code == "anti_tank":
        ext = live.get("distinctions", {})
        row = ext.get("ranks", {}).get("anti", {}).get(sid, {"rank": None, "value": 0})
        verified = ext.get("schema") == VERSION and ext.get("anti_complete")
        return [
            {
                "label": "Техника, уничтоженная трубами и тандемами за неделю",
                "value": row["value"] if verified else None,
                "rank": row["rank"] if verified else None,
                "target_rank": 3,
                "available": bool(verified),
            }
        ], None if verified else BLOCKED[code]
    if code == "best_demolition":
        return [
            {
                "label": "Уничтоженные вражеские FOB с 07.10.2026",
                "value": None,
                "target": 50,
            }
        ], "Ожидаем подтверждённые значения из выгрузок Protocol."
    if code in ("best_machine_gunner", "best_sniper", "best_engineer", "best_medic"):
        return (
            [
                {
                    "label": "Время на китах за неделю",
                    "value": None,
                    "rank": None,
                    "target_rank": 3,
                }
            ],
            "Ожидаем выгрузки Protocol. Точный учёт времени на китах начинается 12.10.2026.",
        )
    if code in BLOCKED:
        return [], BLOCKED[code]
    ext = (profile or {}).get("distinctions", {})
    if code in KIT_RULES:
        kits, hours = KIT_RULES[code]
        minutes = kit_value(ext, kits)
        return [
            {
                "label": "Часы на китах " + ", ".join(kits),
                "value": round(minutes / 60, 2) if minutes is not None else None,
                "target": hours,
                "lower_bound": True,
            }
        ], None
    metric = "knife" if code == "best_knife" else "sl"
    weekly_ext = live.get("distinctions", {})
    row = (
        weekly_ext.get("ranks", {}).get(metric, {}).get(sid, {"rank": None, "value": 0})
    )
    verified = (
        weekly_ext.get("knife_complete")
        if metric == "knife"
        else weekly_ext.get("sl_verified")
    )
    value = {
        "label": "Ножевые убийства за неделю"
        if metric == "knife"
        else "Победы сквадным за неделю",
        "value": row["value"] if verified else None,
        "rank": row["rank"] if verified else None,
        "target_rank": 3,
        "available": bool(verified),
    }
    return [value], None
