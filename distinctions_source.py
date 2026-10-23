"""Parse only attributable public data; unknown values never become zero proofs."""

import re
from collections import Counter
from medals_source import identity, text

VERSION = 2
SL_KITS = frozenset(("SL", "SLCrewman", "SLPilot"))
# Short all-season weapon names are the source's first underscore component.
# Vehicle/emplacement weapons (SPG9, Kornet, TOW, tank guns) are excluded.
HANDHELD = re.compile(
    r"^(?:RPG(?:7|26|28|29)|AT4|M72A[567]|NLAW|M3MAAWS|FFV(?:551|751)|SMAW|PF[-]?98|DZJ[-]?08|C90|HAR[-]?66)(?:$|[_ -])",
    re.I,
)
MINES = re.compile(
    r"^(?:Deployable_(?:TM62Mine|M15Mine|Type72Mine)|TM62Mine|M15Mine|Type72Mine)$",
    re.I,
)
VEHICLE = re.compile(
    r"^(?:LUVW|MSVS|MTLB|Kraz|Kamaz|BTR|BMP|BMD|BRDM|T72|T62|T90|ZTZ|ZBL|ZTD|ZBD|ZSL|M1A|M2A|M1151|M1117|M113|M939|M35|M60|M1126|FV|CAF_Util|Quadbike|Motorbike|Technical|Jeep|Toyota|Ural|UH60|UH1|Mi8|Mi17|SA330|CH146|CH47|Gator|LAV|ASLAV|Coyote|Stryker|BFV|AAV|Leopard|Warrior|Challenger|Scimitar|Marder|Wiesel|Kozak|Tiger|Tigr|Fuchs|Truck|Pickup|RHIB|AJS|YAK|Z8|Z9)(?:[_0-9A-Za-z-]|$)",
    re.I,
)


def valid_anti(weapon, target):
    return bool(HANDHELD.search(weapon) and VEHICLE.search(target))


def extend_profile(body):
    result = {
        "schema": VERSION,
        "kit_minutes": {},
        "kits_verified": False,
        "anti_verified": False,
        "anti_confirmed": 0,
    }
    # These cards can be a top list, so an absent kit is unknown, never a proven zero.
    cards = re.findall(
        r'<img\b[^>]*src="/assets/img/ico/kits/([A-Za-z]+)\.svg"[^>]*>\s*<b>[^<]+</b>\s*<span\b[^>]*>(\d+)ч\s*(\d+)м</span>',
        body,
    )
    if (
        cards
        and len({c[0] for c in cards}) == len(cards)
        and all(int(c[2]) < 60 for c in cards)
    ):
        result["kit_minutes"] = {
            kit: int(hours) * 60 + int(minutes) for kit, hours, minutes in cards
        }
        result["kits_verified"] = True
    heading = re.search(r"<h3[^>]*>(?:\s|<[^>]*>)*Уничтожение техники\s*</h3>", body)
    if not heading:
        return result
    table = re.search(r"<table\b.*?</table>", body[heading.end() :], re.S)
    if not table or not all(
        label in table[0] for label in ("Оружие", "Техника", "Количество")
    ):
        return result
    rows = []
    for row in re.findall(r"<tr\b.*?</tr>", table[0], re.S):
        cells = [text(c) for c in re.findall(r"<td\b[^>]*>(.*?)</td>", row, re.S)]
        if not cells:
            continue
        if len(cells) != 4 or not cells[0].isdigit() or not cells[3].isdigit():
            return result
        rows.append({"weapon": cells[1], "target": cells[2], "count": int(cells[3])})
    result.update(
        anti_verified=True,
        anti_confirmed=sum(
            r["count"] for r in rows if valid_anti(r["weapon"], r["target"])
        ),
        anti_rows=[r for r in rows if valid_anti(r["weapon"], r["target"])],
        anti_lower_bound=True,
    )
    return result


def extend_match(body, document):
    result = {
        "schema": VERSION,
        "knife_verified": False,
        "sl_verified": False,
        "knife": {},
        "sl_teams": [],
        "sl_unknown": [],
        "anti_confirmed": {},
        "anti_verified": False,
    }
    sl_teams = []
    sl_unknown = []
    knife_roster = {}
    valid_roster = True
    knife_roster_verified = True
    for team in (1, 2):
        head = re.search(
            r'<p[^>]*>(-?\d+)</p>\s*<p id="team_t'
            + str(team)
            + r'-name"[^>]*>(.*?)</p>',
            body,
            re.S,
        )
        table = (
            re.search(r"<table\b.*?</table>", body[head.end() :], re.S)
            if head
            else None
        )
        if not table:
            valid_roster = False
            break
        leaders = []
        unknown = []
        for row in re.findall(r"<tr\b.*?</tr>", table[0], re.S):
            sid = re.search(r'data-type="steam_id"[^>]*>(\d+)</span>', row)
            if not sid:
                continue
            sid = identity(sid[1])
            kit = re.search(r"/ico/kits/([A-Za-z]+)\.svg", row)
            knife = re.search(r'data-type="knife"[^>]*>(\d+)</td>', row)
            if knife:
                knife_roster[sid] = int(knife[1])
            else:
                knife_roster_verified = False
            if not kit:
                unknown.append(sid)
            elif kit[1] in SL_KITS:
                leaders.append(sid)
        sl_teams.append(leaders)
        sl_unknown.append(unknown)
    if valid_roster and len(sl_teams) == 2:
        result.update(sl_verified=True, sl_teams=sl_teams, sl_unknown=sl_unknown)
    # Dedicated knife log includes players who left before the final scoreboard.
    start = body.find('id="game_knife-block"')
    stop = body.find('id="playerDetail"', start)
    if start != -1 and stop != -1 and valid_roster and knife_roster_verified:
        counter = Counter()
        for sid, event in re.findall(
            r'<div class="panel[^>]*data-steamid="(\d+)"[^>]*>(.*?)</div>',
            body[start:stop],
            re.S,
        ):
            if "fa-skull-crossbones" not in event:
                continue
            if not re.search(r'href="/player/' + sid + r'"', event):
                continue
            counter[identity(sid)] += 1
        if all(counter[sid] == count for sid, count in knife_roster.items()):
            result.update(knife_verified=True, knife=dict(counter))
    start = body.find('id="game_vehicle-block"')
    stop = body.find('id="game_knife-block"', start)
    if start != -1 and stop != -1:
        counter = Counter()
        events = 0
        verified = True
        for sid, event in re.findall(
            r'<div class="panel[^>]*data-steamid="(\d+)"[^>]*>(.*?)</div>',
            body[start:stop],
            re.S,
        ):
            if "fa-explosion text-danger" not in event:
                continue
            events += 1
            weapon = re.search(r'fa-gun"></i>\s*<span>(.*?)</span>', event, re.S)
            target = re.search(
                r'fa-explosion text-danger"></i>\s*\d+\s*<b[^>]*>(.*?)</b>', event, re.S
            )
            if not weapon or not target:
                verified = False
                continue
            w, t = text(weapon[1]), text(target[1])
            if valid_anti(w, t):
                counter[identity(sid)] += 1
            elif not (
                MINES.fullmatch(w)
                or re.match(
                    r"^(?:SPG9|TOW|Kornet|BMP|BTR|BMD|T72|T62|T90|M1A|M2A|ZTZ|ZBD|LAV|ASLAV|FV|Leopard|Warrior|Challenger|Deployable_M112|M67|F1|RGD5|RGO|RKG3)(?:[_0-9A-Za-z-]|$)",
                    w,
                    re.I,
                )
            ):
                verified = False
        result["anti_confirmed"] = dict(counter)
        stated = (
            sum(p["vehicle"] for p in document.get("players", {}).values())
            if document.get("verified")
            else None
        )
        result["anti_verified"] = bool(
            verified and stated is not None and events == stated
        )
    return result
