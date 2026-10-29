"""Independent SQSTAT worker; network I/O never holds the wallet/game lock."""

import argparse
import gzip
import json
import logging
import time
import threading
from collections import Counter
from datetime import datetime, timedelta, timezone
from pathlib import Path
from database import SessionLocal, User
from runtime import initialize_database
from medals import (
    FIRST_WEEK,
    MOSCOW,
    ProtocolMatch,
    ProtocolProfile,
    MedalWeek,
    MedalMonitor,
    dump,
    ranks,
    utcnow,
    week_bounds,
)
from medals_source import (
    BASE,
    SQSTAT,
    SourceUnavailable,
    BrokenMatchPage,
    home,
    match,
    profile,
    resolve,
)
from protocol_week_source import collect_due_previous as collect_previous

logger = logging.getLogger("strike_team9.medals.monitor")
PROJECT = Path(__file__).resolve().parent


def weekly(documents, week, complete=False):
    metrics = {
        key: Counter()
        for key in ("kills", "revive", "damage", "vehicle", "wins", "cmd")
    }
    selected = [m for m in documents if m["week"] == week]
    names = {}
    unresolved = []
    unknown = {key: Counter() for key in ("wins", "cmd")}
    for m in sorted(selected, key=lambda m: m["end"]):
        if m["winner"] is None:
            unresolved.append(m["id"])
            for team in m["teams"]:
                for p in team["roster"]:
                    unknown["wins"][p["steam_id"]] += 1
                    if p["cmd"]:
                        unknown["cmd"][p["steam_id"]] += 1
        for sid, p in m["players"].items():
            names[sid] = p["name"]
            for key in metrics:
                metrics[key][sid] += p[key]
    safe = {}
    for metric, limit in (("cmd", 3), ("wins", 5)):
        scores = metrics[metric]
        ranked = ranks(scores)
        safe[metric] = []
        for sid, value in ranked.items():
            if value["rank"] > limit or unknown[metric][sid]:
                continue
            worst = 1 + sum(
                1
                for other in set(scores) | set(unknown[metric])
                if other != sid and scores[other] + unknown[metric][other] > scores[sid]
            )
            if worst == value["rank"]:
                safe[metric].append(sid)
    from distinctions import weekly as distinction_weekly

    return {
        "week": week,
        "complete": bool(complete),
        "wins_complete": not unresolved,
        "cmd_complete": not unresolved,
        "cmd_safe": safe["cmd"],
        "wins_safe": safe["wins"],
        "match_count": len(selected),
        "source": BASE + "/",
        "ranks": {key: ranks(values) for key, values in metrics.items()},
        "names": names,
        "unresolved": unresolved,
        "distinctions": distinction_weekly(documents, week),
    }


def publish_status(data):
    with SessionLocal.begin() as db:
        row = db.get(MedalMonitor, 1)
        if row is None:
            db.add(MedalMonitor(id=1, payload=dump(data), updated_at=utcnow()))
        else:
            row.payload = dump(data)
            row.updated_at = utcnow()


class Monitor:
    def __init__(self, client=None):
        self.client = client or SQSTAT()

    def cycle(self):
        with SessionLocal() as db:
            from automation_control import source_enabled

            if not source_enabled(db, "sqstat"):
                return False
        self.client.profile_cache.clear()
        now = utcnow()
        local = now.replace(tzinfo=timezone.utc).astimezone(MOSCOW)
        current = (local - timedelta(days=local.weekday())).date().isoformat()
        with SessionLocal() as db:
            previous = db.get(MedalMonitor, 1)
            state = json.loads(previous.payload) if previous else {}
            seen = int(state.get("cursor", 0))
            users = [
                sid
                for (sid,) in db.query(User.steam_id)
                .filter(User.deleted_at.is_(None), User.steam_id.is_not(None))
                .all()
            ]
        # A bad archive page must not starve all-time medal/pilot profiles.
        errors = 0
        unique_users = list(dict.fromkeys(users))
        publish_status(
            {
                **state,
                "status": "collecting",
                "phase": "profiles",
                "profiles_total": len(unique_users),
                "profiles_done": 0,
            }
        )
        for index, sid in enumerate(unique_users, 1):
            try:
                p = profile(self.client.player(sid), sid)
                with SessionLocal.begin() as db:
                    row = db.get(ProtocolProfile, sid)
                    if row is None:
                        db.add(
                            ProtocolProfile(
                                steam_id=sid, payload=dump(p), checked_at=utcnow()
                            )
                        )
                    else:
                        old = json.loads(row.payload)
                        previous_games = (
                            old.get("games", [])
                            if old.get("steam_id") == sid and old.get("verified")
                            else []
                        )
                        games = {
                            g["match_id"]: g
                            for g in previous_games
                            if isinstance(g, dict) and type(g.get("match_id")) is int
                        }
                        games.update({g["match_id"]: g for g in p.get("games", [])})
                        p["games"] = sorted(
                            games.values(), key=lambda g: g["match_id"], reverse=True
                        )[:500]
                        row.payload = dump(p)
                        row.checked_at = utcnow()
            except SourceUnavailable:
                errors += 1
                logger.warning(
                    "SQSTAT profile unavailable; retaining previous verified data"
                )
            publish_status(
                {
                    **state,
                    "status": "collecting",
                    "phase": "profiles",
                    "profiles_total": len(unique_users),
                    "profiles_done": index,
                    "profiles_failed": errors,
                }
            )
        state = {
            **state,
            "phase": "archive",
            "profiles_total": len(unique_users),
            "profiles_done": len(unique_users),
            "profiles_failed": errors,
            "profiles_checked_at": utcnow().isoformat() + "Z",
        }
        # Publish profile health even if the index/archive fails below.
        publish_status(state)
        recent = home(self.client.get("/"))
        newest = max(recent)
        if seen > newest:
            raise SourceUnavailable(
                "SQSTAT index moved backwards; retaining existing results"
            )
        # On first run walk back to the beginning of the requested first week.
        bootstrap = seen == 0
        floor = 0 if bootstrap else max(0, seen - 40)
        oldest = None
        fetched = 0
        failed = []
        consecutive_errors = 0
        skipped = set(state.get("archive_skipped", []))
        for ident in range(newest, floor, -1):
            publish_status(
                {
                    **state,
                    "status": "collecting",
                    "phase": "archive",
                    "current_match": ident,
                    "archive_failed": failed,
                    "archive_skipped": sorted(skipped, reverse=True),
                }
            )
            if fetched >= 2000:
                raise SourceUnavailable("Archive catch-up incomplete; will resume")
            with SessionLocal() as db:
                stored = db.get(ProtocolMatch, ident)
            if stored:
                document = json.loads(stored.payload)
            else:
                try:
                    document = match(
                        self.client.get("/game/" + str(ident)), ident, recent.get(ident)
                    )
                except BrokenMatchPage:
                    skipped.add(ident)
                    consecutive_errors = 0
                    logger.warning(
                        "SQSTAT match %s excluded: confirmed upstream PHP failure",
                        ident,
                    )
                    continue
                except SourceUnavailable as exc:
                    failed.append(ident)
                    consecutive_errors += 1
                    logger.warning(
                        "SQSTAT match %s unavailable (%s); retrying next cycle",
                        ident,
                        str(exc),
                    )
                    if consecutive_errors >= 5:
                        break
                    continue
                skipped.discard(ident)
                consecutive_errors = 0
                if document:
                    document = resolve(self.client, document)
                    with SessionLocal.begin() as db:
                        db.add(
                            ProtocolMatch(
                                match_id=ident,
                                ended_at=datetime.fromisoformat(document["end"]),
                                payload=dump(document),
                            )
                        )
                    fetched += 1
            if document:
                skipped.discard(ident)
                oldest = ident
                # A broad buffer handles long-running training/seed matches.
                if bootstrap and document["week"] < FIRST_WEEK:
                    start, _ = week_bounds(FIRST_WEEK)
                    if datetime.fromisoformat(document["end"]) < start.astimezone(
                        timezone.utc
                    ).replace(tzinfo=None) - timedelta(days=1):
                        break
        # Backfill old snapshots in bounded batches without changing existing results.
        from distinctions_source import VERSION

        with SessionLocal() as db:
            legacy = [
                json.loads(r.payload)
                for r in db.query(ProtocolMatch)
                .filter(ProtocolMatch.ended_at >= now - timedelta(days=30))
                .order_by(ProtocolMatch.ended_at.desc())
                if json.loads(r.payload).get("distinctions", {}).get("schema")
                != VERSION
            ][:80]
        for document in legacy:
            try:
                body = self.client.get("/game/" + str(document["id"]))
                checked = match(body, document["id"], document["winner"])
                if not checked or checked["end"] != document["end"]:
                    raise SourceUnavailable("SQSTAT match identity changed")
                document["distinctions"] = checked["distinctions"]
                with SessionLocal.begin() as db:
                    db.get(ProtocolMatch, document["id"]).payload = dump(document)
            except SourceUnavailable:
                logger.warning(
                    "Distinction match data unavailable; retaining existing medal statistics"
                )
        # Recheck unresolved results while player match histories are still recent.
        with SessionLocal() as db:
            records = (
                db.query(ProtocolMatch)
                .filter(ProtocolMatch.ended_at >= now - timedelta(days=9))
                .all()
            )
            unresolved = [
                json.loads(r.payload)
                for r in records
                if json.loads(r.payload).get("winner") is None
            ]
        for document in unresolved:
            checked = resolve(self.client, document)
            if checked["winner"] is not None:
                with SessionLocal.begin() as db:
                    db.get(ProtocolMatch, checked["id"]).payload = dump(checked)
        with SessionLocal() as db:
            documents = [
                json.loads(row.payload) for row in db.query(ProtocolMatch).all()
            ]
        # Confirmed upstream PHP failures are excluded per administrator policy.
        # Network errors and all other unverified pages still block finalization.
        weeks = sorted(
            {m["week"] for m in documents if FIRST_WEEK <= m["week"] < current}
        )
        for week in [] if failed else weeks:
            _, end = week_bounds(week)
            if local < end + timedelta(hours=1):
                continue
            doc = weekly(documents, week, complete=True)
            doc["ignored_match_ids"] = sorted(skipped, reverse=True)
            doc["missing_match_policy"] = "exclude_confirmed_php_fetch_assoc_errors"
            with SessionLocal.begin() as db:
                row = db.get(MedalWeek, week)
                if row is None:
                    db.add(
                        MedalWeek(week=week, payload=dump(doc), finalized_at=utcnow())
                    )
                else:
                    row.payload = dump(doc)
                    row.finalized_at = utcnow()
        publish_status(
            {
                **state,
                "phase": "idle",
                "current_match": None,
                "status": "unavailable" if failed else "ready",
                "cursor": seen if failed else newest,
                "oldest": oldest or state.get("oldest"),
                "checked_at": state.get("checked_at")
                if failed
                else utcnow().isoformat() + "Z",
                "archive_failed": failed,
                "archive_skipped": sorted(skipped, reverse=True),
                "live": weekly(documents, current),
                "first_week": FIRST_WEEK,
                "last_error": "Не прочитаны матчи SQSTAT; недельная выдача ждёт повторной проверки"
                if failed
                else None,
            }
        )
        with SessionLocal.begin() as db:
            from automation_control import queue_alert, resolve_alerts

            if failed:
                queue_alert(
                    db,
                    "sqstat_matches",
                    "SQSTAT",
                    "Не прочитаны матчи SQSTAT; недельная выдача приостановлена и будет повторена.",
                )
            else:
                resolve_alerts(db, "sqstat_matches")
        # Keep full recent match evidence for retries; permanent weekly aggregates remain.
        # Remove entire old weeks, never a partial week which could change its ranking.
        with SessionLocal.begin() as db:
            for row in db.query(MedalWeek).all():
                _, end = week_bounds(row.week)
                end_utc = end.astimezone(timezone.utc).replace(tzinfo=None)
                if end_utc < now - timedelta(days=30) and json.loads(row.payload).get(
                    "complete"
                ):
                    start, _ = week_bounds(row.week)
                    db.query(ProtocolMatch).filter(
                        ProtocolMatch.ended_at
                        >= start.astimezone(timezone.utc).replace(tzinfo=None),
                        ProtocolMatch.ended_at < end_utc,
                    ).delete(synchronize_session=False)


def seed(path):
    source = Path(path)
    data = json.loads(
        gzip.decompress(source.read_bytes()).decode("utf-8")
        if source.suffix == ".gz"
        else source.read_text(encoding="utf-8")
    )
    if (
        data.get("week") != FIRST_WEEK
        or data.get("schema") != 1
        or not data.get("complete")
    ):
        raise ValueError("Unsupported medal seed")
    with SessionLocal.begin() as db:
        for document in data["matches"]:
            if not document.get("verified") or document["week"] != FIRST_WEEK:
                raise ValueError("Invalid seed match")
            row = db.get(ProtocolMatch, document["id"])
            if row is None:
                db.add(
                    ProtocolMatch(
                        match_id=document["id"],
                        ended_at=datetime.fromisoformat(document["end"]),
                        payload=dump(document),
                    )
                )
        if db.get(MedalWeek, FIRST_WEEK) is None:
            db.add(
                MedalWeek(
                    week=FIRST_WEEK,
                    payload=dump(weekly(data["matches"], FIRST_WEEK, True)),
                    finalized_at=utcnow(),
                )
            )
        if db.get(MedalMonitor, 1) is None:
            db.add(
                MedalMonitor(
                    id=1,
                    payload=dump(
                        {
                            "status": "warming",
                            "cursor": max(m["id"] for m in data["matches"]),
                            "oldest": min(m["id"] for m in data["matches"]),
                            "first_week": FIRST_WEEK,
                        }
                    ),
                    updated_at=utcnow(),
                )
            )
    print(
        "Verified first-week statistics imported; awards will resolve against linked Steam IDs"
    )


def seed_distinctions(path):
    """Merge new evidence only; never replace a known winner or existing medal proof."""
    from distinctions import weekly as distinction_weekly
    from distinctions_source import VERSION

    data = json.loads(gzip.decompress(Path(path).read_bytes()).decode("utf-8"))
    if data.get("schema") not in (1, VERSION) or data.get("week") != FIRST_WEEK:
        raise ValueError("Unsupported distinction seed")
    with SessionLocal.begin() as db:
        for item in data["matches"]:
            if item["distinctions"].get("schema") not in (1, VERSION):
                raise ValueError("Invalid distinction seed")
            row = db.get(ProtocolMatch, item["id"])
            if row is None:
                continue
            doc = json.loads(row.payload)
            if doc["end"] != item["end"] or doc["week"] != FIRST_WEEK:
                raise ValueError("Seed match identity mismatch")
            doc["distinctions"] = item["distinctions"]
            row.payload = dump(doc)
        db.flush()
        weeks = sorted({doc["week"] for doc in data["matches"]})
        documents = [json.loads(r.payload) for r in db.query(ProtocolMatch)]
        for week in weeks:
            row = db.get(MedalWeek, week)
            if row:
                doc = json.loads(row.payload)
                doc["distinctions"] = distinction_weekly(documents, week)
                row.payload = dump(doc)
    print(
        "Verified public distinction evidence merged; existing medal results preserved"
    )


def source_cycle(monitor, propagate=False):
    try:
        monitor.cycle()
        with SessionLocal.begin() as db:
            from automation_control import resolve_alerts

            resolve_alerts(db, "sqstat_cycle")
    except Exception:
        logger.exception(
            "Medal source collection interrupted; no incomplete week is finalized"
        )
        with SessionLocal() as db:
            row = db.get(MedalMonitor, 1)
            old = json.loads(row.payload) if row else {}
        publish_status(
            {
                **old,
                "status": "unavailable",
                "last_error": "SQSTAT недоступен или статистика ещё не проверена",
            }
        )
        with SessionLocal.begin() as db:
            from automation_control import queue_alert

            queue_alert(
                db,
                "sqstat_cycle",
                "SQSTAT",
                "SQSTAT недоступен или статистика ещё не проверена; автоматическая выдача ждёт повторной попытки.",
            )
        if propagate:
            raise


class BackgroundSQSTAT:
    """A single scanner; long rate-limit waits must not block Protocol jobs."""

    def __init__(self, monitor):
        self.monitor = monitor
        self.thread = None

    def start_if_idle(self):
        if self.thread is not None and self.thread.is_alive():
            return False
        self.thread = threading.Thread(
            target=source_cycle,
            args=(self.monitor,),
            name="sqstat-archive",
            daemon=True,
        )
        self.thread.start()
        return True


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("command", choices=["run", "once", "seed"])
    parser.add_argument("path", nargs="?")
    args = parser.parse_args()
    initialize_database()
    if args.command == "seed":
        seed(args.path)
        return
    monitor = Monitor()
    background = BackgroundSQSTAT(monitor)
    while True:
        with SessionLocal.begin() as db:
            from automation_control import resolve_alerts, source_enabled

            enabled = {
                name: source_enabled(db, name)
                for name in ("protocol", "battlemetrics", "sqstat")
            }
            if not enabled["protocol"]:
                resolve_alerts(db, "protocol_previous_week")
            if not enabled["battlemetrics"]:
                resolve_alerts(db, "battlemetrics_awards")
            if not enabled["sqstat"]:
                resolve_alerts(db, "sqstat_cycle")
                resolve_alerts(db, "sqstat_matches")
        # Explicit administrator checks bypass the periodic source cooldown.
        try:
            from protocol_week_check import process_requested

            process_requested()
        except Exception:
            logger.exception("Manual Protocol week check will retry")
        # Independent published standings are collected even when SQSTAT fails.
        try:
            if enabled["protocol"]:
                from protocol_award_exports import collect_exports

                collect_exports()
        except Exception:
            logger.warning(
                "Protocol award exports will retry; previous evidence retained"
            )
        try:
            if enabled["protocol"]:
                from protocol_clan_source import collect_clan

                collect_clan()
        except Exception:
            logger.warning(
                "Protocol clan stats will retry; cached player data retained"
            )
        try:
            if not enabled["protocol"]:
                pass
            elif not collect_previous():
                logger.warning(
                    "Published Protocol week unavailable; cached standings retained"
                )
        except Exception:
            logger.exception("Published Protocol week will retry")
        try:
            if enabled["battlemetrics"]:
                from battlemetrics_awards import collect_fallback

                collect_fallback()
        except Exception:
            logger.warning("BattleMetrics award fallback will retry")
        if enabled["sqstat"]:
            if args.command == "once":
                source_cycle(monitor, propagate=True)
            else:
                background.start_if_idle()
        if args.command == "once":
            return
        time.sleep(300)


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    main()
