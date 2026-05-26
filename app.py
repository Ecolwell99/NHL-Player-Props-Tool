import json
import os
import time
from collections import defaultdict
from datetime import datetime
from zoneinfo import ZoneInfo

import requests
import streamlit as st

SCOREBOARD_URL = "https://api-web.nhle.com/v1/scoreboard/now"
PBP_URL = "https://api-web.nhle.com/v1/gamecenter/{game_id}/play-by-play"
REFRESH_SECS = 3

ALERT_LOG_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "nhl_alert_logs")

st.set_page_config(page_title="NHL Player Props Dev Tool", layout="wide")

STATE_VERSION = 3


def init_state():
    defaults = {
        "games": [],
        "selected_game_label": None,
        "selected_game_id": None,
        "tracking": False,
        "prev_skater_shot_attr": {},
        "prev_goalie_shot_attr": {},
        "prev_goal_attr": {},
        "prev_fo_attr": {},
        "warning_message": "STATUS: OK",
        "warning_type": "ok",
        "alert_shown_until": 0.0,
        "alert_log": [],
        "correction_log": [],
        "correction_summary": {},
        "stat_flash": {},
        "prev_stat_totals": {},
        "color_mode": True,
        "team_filter": "All",
        "is_first_tick": True,
        "sort_skaters": "Player",
        "sort_goalies": "Goalie",
        "sort_fo": "Player",
    }
    if st.session_state.get("_props_state_version") != STATE_VERSION:
        for key, value in defaults.items():
            st.session_state[key] = value
        st.session_state["_props_state_version"] = STATE_VERSION
    else:
        for key, value in defaults.items():
            if key not in st.session_state:
                st.session_state[key] = value


# ---------------------------------------------------------------------------
# Alert / correction log persistence
# ---------------------------------------------------------------------------

def _log_path(game_id: int, kind: str) -> str:
    return os.path.join(ALERT_LOG_DIR, f"{kind}_game_{game_id}.json")


def _load_log(game_id: int, kind: str) -> list:
    try:
        path = _log_path(game_id, kind)
        if os.path.exists(path):
            with open(path) as f:
                return json.load(f)
    except Exception:
        pass
    return []


def _save_log(game_id: int, kind: str, log: list):
    try:
        os.makedirs(ALERT_LOG_DIR, exist_ok=True)
        with open(_log_path(game_id, kind), "w") as f:
            json.dump(log, f)
    except Exception:
        pass


def _clear_log(game_id: int, kind: str):
    try:
        path = _log_path(game_id, kind)
        if os.path.exists(path):
            os.remove(path)
    except Exception:
        pass


# ---------------------------------------------------------------------------
# API
# ---------------------------------------------------------------------------

class RateLimitedError(Exception):
    pass


def fetch_json(url: str) -> dict:
    r = requests.get(url, timeout=10)
    if r.status_code == 429:
        raise RateLimitedError("Rate limited by NHL API (429)")
    r.raise_for_status()
    return r.json()


def extract_abbrev(value, fallback="UNK"):
    if isinstance(value, str) and value:
        return value
    if isinstance(value, dict):
        if value.get("default"):
            return value["default"]
        for v in value.values():
            if isinstance(v, str) and v:
                return v
    return fallback


def load_live_games() -> list:
    data = fetch_json(SCOREBOARD_URL)
    games = []
    for day in data.get("gamesByDate", []):
        for game in day.get("games", []):
            if game.get("gameState") not in {"LIVE", "CRIT"}:
                continue
            away = extract_abbrev(game.get("awayTeam", {}).get("abbrev"), "AWAY")
            home = extract_abbrev(game.get("homeTeam", {}).get("abbrev"), "HOME")
            game_id = game.get("id")
            games.append({"label": f"{away} @ {home} ({game_id})", "id": game_id, "away": away, "home": home})
    return games


# ---------------------------------------------------------------------------
# Lookups
# ---------------------------------------------------------------------------

def build_player_lookup(game_data: dict) -> dict:
    lookup = {}
    for spot in game_data.get("rosterSpots") or []:
        pid = spot.get("playerId")
        if pid:
            first = spot.get("firstName") or {}
            last = spot.get("lastName") or {}
            first_str = first.get("default", "") if isinstance(first, dict) else str(first)
            last_str = last.get("default", "") if isinstance(last, dict) else str(last)
            lookup[pid] = f"{first_str} {last_str}".strip()
    return lookup


def build_player_team_lookup(game_data: dict) -> dict:
    team_id_to_abbrev = {}
    for key, fallback in (("homeTeam", "HOME"), ("awayTeam", "AWAY")):
        team = game_data.get(key) or {}
        tid = team.get("id")
        if tid:
            team_id_to_abbrev[tid] = extract_abbrev(team.get("abbrev"), fallback)
    lookup = {}
    for spot in game_data.get("rosterSpots") or []:
        pid = spot.get("playerId")
        tid = spot.get("teamId")
        if pid and tid:
            lookup[pid] = team_id_to_abbrev.get(tid, "UNK")
    return lookup


def build_goalie_set(game_data: dict) -> set:
    goalies = set()
    for spot in game_data.get("rosterSpots") or []:
        if str(spot.get("positionCode", "")).upper() == "G":
            pid = spot.get("playerId")
            if pid:
                goalies.add(pid)
    return goalies


def get_home_away_abbrevs(game_data: dict):
    home = game_data.get("homeTeam") or {}
    away = game_data.get("awayTeam") or {}
    return (
        extract_abbrev(home.get("abbrev"), "HOME"),
        extract_abbrev(away.get("abbrev"), "AWAY"),
    )


# ---------------------------------------------------------------------------
# Clock helpers
# ---------------------------------------------------------------------------

def parse_clock_to_seconds(clock_str: str):
    try:
        m, s = clock_str.split(":")
        return int(m) * 60 + int(s)
    except Exception:
        return None


def seconds_to_clock(total: int) -> str:
    return f"{total // 60}:{total % 60:02d}"


def convert_to_time_remaining(clock_str: str, period: int | None, game_data=None) -> str:
    secs = parse_clock_to_seconds(clock_str)
    if secs is None:
        return clock_str
    period_len = 1200
    if period is not None and period > 3:
        game_type = str(game_data.get("gameType", "")).strip() if game_data else ""
        if game_type in {"2", "02"}:
            period_len = 300
        else:
            period_len = 1200 if secs > 300 else 300
    return seconds_to_clock(max(0, period_len - secs))


# ---------------------------------------------------------------------------
# Stat parsing
# ---------------------------------------------------------------------------

def parse_all_stats(game_data: dict) -> dict:
    plays = game_data.get("plays") or []
    player_lookup = build_player_lookup(game_data)
    player_team = build_player_team_lookup(game_data)
    goalie_set = build_goalie_set(game_data)

    skater_stats: dict = {}
    goalie_stats: dict = {}
    fo_stats: dict = {}

    # Pre-populate every dressed player with zeros so the full roster always shows
    for spot in game_data.get("rosterSpots") or []:
        pid = spot.get("playerId")
        if not pid:
            continue
        name = player_lookup.get(pid, f"ID {pid}")
        team = player_team.get(pid, "UNK")
        if pid in goalie_set:
            goalie_stats[pid] = {"name": name, "team": team, "shots_against": 0, "goals_allowed": 0}
        else:
            skater_stats[pid] = {"name": name, "team": team, "goals": 0, "assists": 0, "points": 0, "sog": 0, "blocked": 0}
    skater_shot_attr: dict = {}
    goalie_shot_attr: dict = {}
    goal_attr: dict = {}
    fo_attr: dict = {}

    def ensure_skater(pid):
        if pid not in skater_stats:
            skater_stats[pid] = {
                "name": player_lookup.get(pid, f"ID {pid}"),
                "team": player_team.get(pid, "UNK"),
                "goals": 0, "assists": 0, "points": 0, "sog": 0, "blocked": 0,
            }

    def ensure_goalie(pid):
        if pid not in goalie_stats:
            goalie_stats[pid] = {
                "name": player_lookup.get(pid, f"ID {pid}"),
                "team": player_team.get(pid, "UNK"),
                "shots_against": 0,
                "goals_allowed": 0,
            }

    def ensure_fo(pid):
        if pid not in fo_stats:
            fo_stats[pid] = {
                "name": player_lookup.get(pid, f"ID {pid}"),
                "team": player_team.get(pid, "UNK"),
                "fo_taken": 0, "fo_won": 0,
            }

    for play in plays:
        play_type = str(play.get("typeDescKey", "")).lower()
        details = play.get("details") or {}
        event_id = play.get("eventId")
        period = (play.get("periodDescriptor") or {}).get("number")
        time_rem = convert_to_time_remaining(play.get("timeInPeriod", ""), period, game_data)

        if play_type == "shot-on-goal":
            shooter = details.get("shootingPlayerId")
            goalie = details.get("goalieInNetId")
            if shooter:
                ensure_skater(shooter)
                skater_stats[shooter]["sog"] += 1
                skater_shot_attr[event_id] = {"pid": shooter, "period": period, "time_remaining": time_rem}
            if goalie:
                ensure_goalie(goalie)
                goalie_stats[goalie]["shots_against"] += 1
                goalie_shot_attr[event_id] = {"pid": goalie, "period": period, "time_remaining": time_rem}

        elif play_type == "goal":
            scorer = details.get("scoringPlayerId")
            a1 = details.get("assist1PlayerId")
            a2 = details.get("assist2PlayerId")
            goalie = details.get("goalieInNetId")

            if scorer:
                ensure_skater(scorer)
                skater_stats[scorer]["goals"] += 1
                skater_stats[scorer]["sog"] += 1
            if a1:
                ensure_skater(a1)
                skater_stats[a1]["assists"] += 1
            if a2:
                ensure_skater(a2)
                skater_stats[a2]["assists"] += 1
            if goalie:
                ensure_goalie(goalie)
                goalie_stats[goalie]["shots_against"] += 1
                goalie_stats[goalie]["goals_allowed"] += 1
                goalie_shot_attr[event_id] = {"pid": goalie, "period": period, "time_remaining": time_rem}

            goal_attr[event_id] = {
                "scorer": scorer, "a1": a1, "a2": a2,
                "period": period, "time_remaining": time_rem,
            }

        elif play_type == "blocked-shot":
            blocker = details.get("blockingPlayerId")
            if blocker:
                ensure_skater(blocker)
                skater_stats[blocker]["blocked"] += 1

        elif play_type == "faceoff":
            winner_id = details.get("winningPlayerId")
            loser_id = details.get("losingPlayerId")

            if winner_id:
                ensure_fo(winner_id)
                fo_stats[winner_id]["fo_taken"] += 1
                fo_stats[winner_id]["fo_won"] += 1
            if loser_id:
                ensure_fo(loser_id)
                fo_stats[loser_id]["fo_taken"] += 1

            fo_attr[event_id] = {
                "winner_id": winner_id, "loser_id": loser_id,
                "period": period, "time_remaining": time_rem,
            }

    for s in skater_stats.values():
        s["points"] = s["goals"] + s["assists"]

    return {
        "skater_stats": skater_stats,
        "goalie_stats": goalie_stats,
        "fo_stats": fo_stats,
        "skater_shot_attr": skater_shot_attr,
        "goalie_shot_attr": goalie_shot_attr,
        "goal_attr": goal_attr,
        "fo_attr": fo_attr,
    }


# ---------------------------------------------------------------------------
# Correction detection
# ---------------------------------------------------------------------------

def _blank_delta(name: str, team: str) -> dict:
    return {
        "name": name, "team": team,
        "sog": 0, "goals": 0, "assists": 0,
        "fo_wins": 0, "fo_losses": 0,
        "goalie_saves": 0,
        "last_ts": "",
    }


def detect_corrections(parsed: dict, prev: dict, player_lookup: dict, player_team: dict, now_str: str) -> tuple[list, list]:
    """Returns (alerts, deltas).
    deltas: list of {name, team, sog, goals, assists, fo_wins, fo_losses, goalie_saves, last_ts}
    """
    alerts = []
    deltas: dict = {}

    def pname(pid):
        return player_lookup.get(pid, f"ID {pid}") if pid else "None"

    def pteam(pid):
        return player_team.get(pid, "UNK") if pid else "UNK"

    def ensure_delta(pid):
        if pid and pid not in deltas:
            deltas[pid] = _blank_delta(pname(pid), pteam(pid))

    def stamp(pid):
        if pid and pid in deltas:
            deltas[pid]["last_ts"] = now_str

    cur_skater_shot = parsed["skater_shot_attr"]
    cur_goalie_shot = parsed["goalie_shot_attr"]
    cur_goal = parsed["goal_attr"]
    cur_fo = parsed["fo_attr"]

    prev_skater_shot = prev["prev_skater_shot_attr"]
    prev_goalie_shot = prev["prev_goalie_shot_attr"]
    prev_goal = prev["prev_goal_attr"]
    prev_fo = prev["prev_fo_attr"]

    def imp(name, stat, val):
        sign = "+" if val > 0 else ""
        return f"{name} {stat} {sign}{val}"

    # Skater SOG
    for eid, attr in prev_skater_shot.items():
        if eid not in cur_skater_shot:
            pid = attr["pid"]
            alerts.append((attr["period"], f"SOG REMOVED: {pname(pid)} — P{attr['period']} {attr['time_remaining']}", [imp(pname(pid), "SOG", -1)]))
            ensure_delta(pid); deltas[pid]["sog"] -= 1; stamp(pid)
    for eid, attr in cur_skater_shot.items():
        if eid in prev_skater_shot and prev_skater_shot[eid]["pid"] != attr["pid"]:
            old, new = prev_skater_shot[eid]["pid"], attr["pid"]
            p, t = attr["period"], attr["time_remaining"]
            alerts.append((p, f"SOG RE-ATTRIBUTED: P{p} {t} — {pname(old)} → {pname(new)}", [imp(pname(old), "SOG", -1), imp(pname(new), "SOG", +1)]))
            ensure_delta(old); deltas[old]["sog"] -= 1; stamp(old)
            ensure_delta(new); deltas[new]["sog"] += 1; stamp(new)

    # Goalie SOG
    for eid, attr in prev_goalie_shot.items():
        if eid not in cur_goalie_shot:
            pid = attr["pid"]
            alerts.append((attr["period"], f"GOALIE SOG REMOVED: {pname(pid)} — P{attr['period']} {attr['time_remaining']}", [imp(pname(pid), "SV", -1)]))
            ensure_delta(pid); deltas[pid]["goalie_saves"] -= 1; stamp(pid)
    for eid, attr in cur_goalie_shot.items():
        if eid in prev_goalie_shot and prev_goalie_shot[eid]["pid"] != attr["pid"]:
            old, new = prev_goalie_shot[eid]["pid"], attr["pid"]
            p, t = attr["period"], attr["time_remaining"]
            alerts.append((p, f"GOALIE SOG RE-ATTRIBUTED: P{p} {t} — {pname(old)} → {pname(new)}", [imp(pname(old), "SV", -1), imp(pname(new), "SV", +1)]))
            ensure_delta(old); deltas[old]["goalie_saves"] -= 1; stamp(old)
            ensure_delta(new); deltas[new]["goalie_saves"] += 1; stamp(new)

    # Goals
    for eid, attr in prev_goal.items():
        if eid not in cur_goal:
            pid = attr["scorer"]
            alerts.append((attr["period"], f"GOAL REMOVED: {pname(pid)} — P{attr['period']} {attr['time_remaining']}", [imp(pname(pid), "G", -1), imp(pname(pid), "SOG", -1)]))
            ensure_delta(pid); deltas[pid]["goals"] -= 1; deltas[pid]["sog"] -= 1; stamp(pid)
    for eid, attr in cur_goal.items():
        if eid not in prev_goal:
            continue
        p_attr = prev_goal[eid]
        p, t = attr["period"], attr["time_remaining"]
        if p_attr["scorer"] != attr["scorer"]:
            old, new = p_attr["scorer"], attr["scorer"]
            alerts.append((p, f"GOAL RE-ATTRIBUTED: P{p} {t} — {pname(old)} → {pname(new)}", [imp(pname(old), "G", -1), imp(pname(old), "SOG", -1), imp(pname(new), "G", +1), imp(pname(new), "SOG", +1)]))
            ensure_delta(old); deltas[old]["goals"] -= 1; deltas[old]["sog"] -= 1; stamp(old)
            ensure_delta(new); deltas[new]["goals"] += 1; deltas[new]["sog"] += 1; stamp(new)
        if p_attr["a1"] != attr["a1"]:
            old, new = p_attr["a1"], attr["a1"]
            impacts = []
            if old: impacts.append(imp(pname(old), "A", -1))
            if new: impacts.append(imp(pname(new), "A", +1))
            alerts.append((p, f"PRIMARY ASSIST CHANGED: P{p} {t} — {pname(old)} → {pname(new)}", impacts))
            if old: ensure_delta(old); deltas[old]["assists"] -= 1; stamp(old)
            if new: ensure_delta(new); deltas[new]["assists"] += 1; stamp(new)
        if p_attr["a2"] != attr["a2"]:
            old, new = p_attr["a2"], attr["a2"]
            impacts = []
            if old: impacts.append(imp(pname(old), "A", -1))
            if new: impacts.append(imp(pname(new), "A", +1))
            alerts.append((p, f"SECONDARY ASSIST CHANGED: P{p} {t} — {pname(old)} → {pname(new)}", impacts))
            if old: ensure_delta(old); deltas[old]["assists"] -= 1; stamp(old)
            if new: ensure_delta(new); deltas[new]["assists"] += 1; stamp(new)

    # Faceoffs
    for eid, attr in prev_fo.items():
        if eid not in cur_fo:
            w, l = attr["winner_id"], attr["loser_id"]
            impacts = []
            if w: impacts.append(imp(pname(w), "FO Wins", -1))
            if l: impacts.append(imp(pname(l), "FO Losses", -1))
            alerts.append((attr["period"], f"FACEOFF REMOVED: P{attr['period']} {attr['time_remaining']} winner={pname(w)}", impacts))
            if w: ensure_delta(w); deltas[w]["fo_wins"] -= 1; stamp(w)
            if l: ensure_delta(l); deltas[l]["fo_losses"] -= 1; stamp(l)
    for eid, attr in cur_fo.items():
        if eid not in prev_fo:
            continue
        p_attr = prev_fo[eid]
        p, t = attr["period"], attr["time_remaining"]
        if p_attr["winner_id"] != attr["winner_id"]:
            old, new = p_attr["winner_id"], attr["winner_id"]
            impacts = []
            if old: impacts += [imp(pname(old), "FO Wins", -1), imp(pname(old), "FO Losses", +1)]
            if new: impacts += [imp(pname(new), "FO Wins", +1), imp(pname(new), "FO Losses", -1)]
            alerts.append((p, f"FACEOFF WINNER CHANGED: P{p} {t} — {pname(old)} → {pname(new)}", impacts))
            if old: ensure_delta(old); deltas[old]["fo_wins"] -= 1; deltas[old]["fo_losses"] += 1; stamp(old)
            if new: ensure_delta(new); deltas[new]["fo_wins"] += 1; deltas[new]["fo_losses"] -= 1; stamp(new)
        if p_attr["loser_id"] != attr["loser_id"]:
            old, new = p_attr["loser_id"], attr["loser_id"]
            impacts = []
            if old: impacts.append(imp(pname(old), "FO Losses", -1))
            if new: impacts.append(imp(pname(new), "FO Losses", +1))
            alerts.append((p, f"FACEOFF LOSER CHANGED: P{p} {t} — {pname(old)} → {pname(new)}", impacts))
            if old: ensure_delta(old); deltas[old]["fo_losses"] -= 1; stamp(old)
            if new: ensure_delta(new); deltas[new]["fo_losses"] += 1; stamp(new)

    return alerts, list(deltas.values())


def apply_deltas(deltas: list, summary: dict):
    for d in deltas:
        key = d["name"]
        if key not in summary:
            summary[key] = _blank_delta(d["name"], d["team"])
        s = summary[key]
        s["sog"] += d["sog"]
        s["goals"] += d["goals"]
        s["assists"] += d["assists"]
        s["fo_wins"] += d["fo_wins"]
        s["fo_losses"] += d["fo_losses"]
        s["goalie_saves"] += d["goalie_saves"]
        if d["last_ts"]:
            s["last_ts"] = d["last_ts"]


FLASH_SECS = 10


def diff_stat_totals(parsed: dict, prev_totals: dict, now: float) -> dict:
    flash = {}

    def check(name, key, current, prev_snap, col):
        old_val = prev_snap.get(col, 0)
        if current[col] != old_val:
            if name not in flash:
                flash[name] = {}
            flash[name][col] = {"dir": "up" if current[col] > old_val else "down", "ts": now}

    for pid, s in parsed["skater_stats"].items():
        name = s["name"]
        prev = prev_totals.get(pid, {})
        cur = {"goals": s["goals"], "assists": s["assists"], "points": s["points"], "sog": s["sog"], "blocked": s["blocked"]}
        for col in cur:
            check(name, pid, cur, prev, col)

    for pid, g in parsed["goalie_stats"].items():
        name = g["name"]
        saves = g["shots_against"] - g["goals_allowed"]
        prev = prev_totals.get(f"g_{pid}", {})
        if saves != prev.get("saves", 0):
            if name not in flash:
                flash[name] = {}
            flash[name]["saves"] = {"dir": "up" if saves > prev.get("saves", 0) else "down", "ts": now}

    for pid, f in parsed["fo_stats"].items():
        name = f["name"]
        prev = prev_totals.get(f"fo_{pid}", {})
        cur = {"fo_taken": f["fo_taken"], "fo_won": f["fo_won"]}
        for col in cur:
            check(name, pid, cur, prev, col)

    return flash


def snapshot_stat_totals(parsed: dict) -> dict:
    totals = {}
    for pid, s in parsed["skater_stats"].items():
        totals[pid] = {"goals": s["goals"], "assists": s["assists"], "points": s["points"], "sog": s["sog"], "blocked": s["blocked"]}
    for pid, g in parsed["goalie_stats"].items():
        totals[f"g_{pid}"] = {"saves": g["shots_against"] - g["goals_allowed"]}
    for pid, f in parsed["fo_stats"].items():
        totals[f"fo_{pid}"] = {"fo_taken": f["fo_taken"], "fo_won": f["fo_won"]}
    return totals


def build_summary_rows(summary: dict) -> list[dict]:
    rows = []
    for s in summary.values():
        if all(s[k] == 0 for k in ("sog", "goals", "assists", "fo_wins", "fo_losses", "goalie_saves")):
            continue
        rows.append({
            "Player": s["name"],
            "Team": s["team"],
            "SOG Δ": s["sog"],
            "Goals Δ": s["goals"],
            "Assists Δ": s["assists"],
            "FO Wins Δ": s["fo_wins"],
            "FO Loss Δ": s["fo_losses"],
            "SV Δ": s["goalie_saves"],
            "Last": s["last_ts"],
        })
    rows.sort(key=lambda r: r["Player"].split()[-1])
    return rows


def html_delta_table(rows: list[dict]) -> str:
    if not rows:
        return ""
    headers = list(rows[0].keys())
    th = "".join(
        f'<th style="padding:6px 12px; text-align:left; border-bottom:2px solid var(--secondary-background-color); '
        f'font-size:13px; color:var(--text-color); font-weight:700; white-space:nowrap;">{h}</th>'
        for h in headers
    )
    body = ""
    for i, row in enumerate(rows):
        bg = "rgba(128,128,128,0.04)" if i % 2 == 0 else "rgba(128,128,128,0.12)"
        tds = ""
        for h in headers:
            val = row[h]
            if isinstance(val, int) and val != 0 and h != "Last":
                if abs(val) >= 3:
                    cell_bg = "rgba(204,34,0,0.25)"
                else:
                    cell_bg = "rgba(255,153,0,0.20)"
                sign = "+" if val > 0 else ""
                display = (
                    f'<span style="background:{cell_bg}; padding:2px 8px; border-radius:6px; '
                    f'font-weight:700; font-size:12px;">{sign}{val}</span>'
                )
            else:
                display = val
            tds += (
                f'<td style="padding:6px 12px; font-size:13px; white-space:nowrap; '
                f'color:var(--text-color); font-weight:600;">{display}</td>'
            )
        body += f'<tr style="background-color:{bg};">{tds}</tr>'
    return (
        f'<div style="overflow-x:auto; width:100%;">'
        f'<table style="width:100%; border-collapse:collapse;">'
        f'<thead><tr>{th}</tr></thead>'
        f'<tbody>{body}</tbody>'
        f'</table></div>'
    )


def extract_player_from_alert(alert_text: str) -> str:
    for sep in (":", "—"):
        if sep in alert_text:
            after = alert_text.split(sep, 1)[1].strip()
            name_part = after.split("—")[0].split("→")[0].strip()
            if name_part:
                return name_part
    return "Unknown"


# ---------------------------------------------------------------------------
# Display helpers
# ---------------------------------------------------------------------------

TEAM_COLORS = {
    "ANA": "#F47A38", "ARI": "#8C2633", "BOS": "#FFB81C", "BUF": "#003087",
    "CAR": "#CC0000", "CBJ": "#002654", "CGY": "#C8102E", "CHI": "#CF0A2C",
    "COL": "#6F263D", "DAL": "#006847", "DET": "#CE1126", "EDM": "#FF4C00",
    "FLA": "#C8102E", "LAK": "#111111", "MIN": "#154734", "MTL": "#AF1E2D",
    "NJD": "#CE1126", "NSH": "#FFB81C", "NYI": "#00539B", "NYR": "#0038A8",
    "OTT": "#C8102E", "PHI": "#F74902", "PIT": "#FCB514", "SEA": "#99D9D9",
    "SJS": "#006D75", "STL": "#002F87", "TBL": "#002868", "TOR": "#003E7E",
    "UTA": "#6CACE4", "VAN": "#00843D", "VGK": "#B4975A", "WSH": "#C8102E",
    "WPG": "#041E42",
}


def pill_text_color(bg_hex: str) -> str:
    h = bg_hex.lstrip("#")
    r, g, b = int(h[0:2], 16), int(h[2:4], 16), int(h[4:6], 16)
    luminance = (0.299 * r + 0.587 * g + 0.114 * b) / 255
    return "#000" if luminance > 0.5 else "#fff"


def team_pill(abbrev: str) -> str:
    color = TEAM_COLORS.get(abbrev, "#555555")
    text = pill_text_color(color)
    return f'<span style="background-color:{color}; color:{text}; padding:2px 10px; border-radius:12px; font-weight:700; font-size:12px;">{abbrev}</span>'


_STAT_COL_MAP = {"G": "goals", "A": "assists", "PTS": "points", "SOG": "sog", "BS": "blocked", "SV": "saves", "FO Taken": "fo_taken", "FO Won": "fo_won"}


def sort_bar(table_id: str, columns: list[str], name_col: str = "Player"):
    """Renders a compact sort dropdown. Returns the current sort column."""
    all_cols = [name_col] + columns
    current = st.session_state.get(f"sort_{table_id}", name_col)
    idx = all_cols.index(current) if current in all_cols else 0
    chosen = st.selectbox(
        "Sort by",
        options=all_cols,
        index=idx,
        key=f"sort_{table_id}_select",
        label_visibility="collapsed",
    )
    st.session_state[f"sort_{table_id}"] = chosen
    return chosen


def apply_sort(rows: list[dict], sort_col: str, name_col: str = "Player") -> list[dict]:
    if sort_col == name_col:
        return sorted(rows, key=lambda r: r[name_col].split()[-1])
    return sorted(rows, key=lambda r: (-(r[sort_col] if isinstance(r[sort_col], int) else 0), r[name_col].split()[-1]))


def html_table(rows: list[dict], color_mode: bool = False, team_col: str = "Team", flash: dict | None = None) -> str:
    if not rows:
        return ""
    now = time.time()
    headers = list(rows[0].keys())
    player_col = "Player" if "Player" in headers else ("Goalie" if "Goalie" in headers else None)
    th = "".join(
        f'<th style="padding:6px 12px; text-align:left; border-bottom:2px solid var(--secondary-background-color); '
        f'font-size:13px; color:var(--text-color); font-weight:700; white-space:nowrap;'
        f'{"width:80px; min-width:80px; max-width:80px;" if h == team_col else ("width:160px; min-width:160px; max-width:160px;" if h == player_col and player_col else "width:60px; min-width:60px; max-width:60px;")}">{h}</th>'
        for h in headers
    )
    body = ""
    for i, row in enumerate(rows):
        bg = "rgba(128,128,128,0.04)" if i % 2 == 0 else "rgba(128,128,128,0.12)"
        player_name = row.get(player_col) if player_col else None
        player_flash = (flash or {}).get(player_name, {}) if player_name else {}
        tds = ""
        for h in headers:
            val = row[h]
            cell_style = f"padding:6px 12px; font-size:13px; white-space:nowrap; color:var(--text-color); font-weight:600;{'width:80px; min-width:80px; max-width:80px;' if h == team_col else ('width:160px; min-width:160px; max-width:160px;' if h == player_col and player_col else 'width:60px; min-width:60px; max-width:60px;')}"
            if color_mode and h == team_col:
                display = team_pill(str(val))
            else:
                stat_key = _STAT_COL_MAP.get(h)
                flash_entry = player_flash.get(stat_key) if stat_key else None
                if flash_entry and (now - flash_entry["ts"]) <= FLASH_SECS:
                    if flash_entry["dir"] == "up":
                        cell_style += " background-color:rgba(0,200,80,0.30); border-radius:4px;"
                    else:
                        cell_style += " background-color:rgba(220,30,30,0.30); border-radius:4px;"
                display = val
            tds += f'<td style="{cell_style}">{display}</td>'
        body += f'<tr style="background-color:{bg};">{tds}</tr>'
    return (
        f'<div style="overflow-x:auto; width:100%;">'
        f'<table style="width:100%; border-collapse:collapse;">'
        f'<thead><tr>{th}</tr></thead>'
        f'<tbody>{body}</tbody>'
        f'</table></div>'
    )


_WARNING_STYLES = {
    "alert": ("background-color:#3a1600", "color:#ffd966", "border:2px solid #ff9900"),
    "ok": ("background-color:#132117", "color:#66ff99", "border:2px solid #2e6b45"),
}


def warning_box(message: str, warning_type: str):
    style = "; ".join(_WARNING_STYLES.get(warning_type, _WARNING_STYLES["ok"]))
    st.markdown(
        f'<div style="margin-top:10px; margin-bottom:18px; padding:16px; border-radius:10px;'
        f' font-size:26px; font-weight:700; {style}">{message}</div>',
        unsafe_allow_html=True,
    )


def section_header(text: str):
    st.markdown(
        f"<div style='font-size:20px; font-weight:500; margin-top:16px; margin-bottom:8px;'>{text}</div>",
        unsafe_allow_html=True,
    )


def team_summary_card(team: str, stats: dict):
    color = TEAM_COLORS.get(team, "#555555")
    text = pill_text_color(color)
    pill = f'<span style="background-color:{color}; color:{text}; padding:3px 12px; border-radius:12px; font-weight:700; font-size:13px; margin-right:14px;">{team}</span>'
    stat_parts = "".join(
        f'<span style="margin-right:16px; font-size:13px; font-weight:600;">'
        f'<span style="opacity:0.55; font-weight:500;">{label}:</span> {val}</span>'
        for label, val in stats.items()
    )
    st.markdown(
        f'<div style="padding:10px 16px; margin-bottom:10px; border-radius:8px; '
        f'background-color:var(--secondary-background-color); display:flex; align-items:center; flex-wrap:wrap;">'
        f'{pill}{stat_parts}</div>',
        unsafe_allow_html=True,
    )


# ---------------------------------------------------------------------------
# App
# ---------------------------------------------------------------------------

init_state()

with st.sidebar:
    st.title("NHL Player Props")
    st.divider()

    if st.button("Load Live Games", use_container_width=True):
        try:
            games = load_live_games()
            st.session_state.games = games
            if not games:
                st.session_state.selected_game_label = None
                st.session_state.selected_game_id = None
                st.session_state.tracking = False
                st.info("No live games found.")
            else:
                labels = [g["label"] for g in games]
                if st.session_state.selected_game_label not in labels:
                    st.session_state.selected_game_label = labels[0]
                    st.session_state.selected_game_id = games[0]["id"]
                st.success(f"Loaded {len(games)} game(s).")
        except Exception as e:
            st.error(f"Error: {e}")

    game_labels = [g["label"] for g in st.session_state.games]
    selected_label = st.selectbox(
        "Game",
        options=game_labels,
        index=game_labels.index(st.session_state.selected_game_label)
        if st.session_state.selected_game_label in game_labels
        else None,
        placeholder="Load games first",
    )
    if selected_label:
        st.session_state.selected_game_label = selected_label
        for game in st.session_state.games:
            if game["label"] == selected_label:
                st.session_state.selected_game_id = game["id"]
                break

    st.divider()
    manual_id = st.text_input("Or enter a Game ID manually", placeholder="e.g. 2024030411")
    if st.button("Load Manual Game ID", use_container_width=True):
        if manual_id.strip().isdigit():
            st.session_state.selected_game_id = int(manual_id.strip())
            st.session_state.selected_game_label = f"Manual ({manual_id.strip()})"
            st.success(f"Game ID {manual_id.strip()} loaded.")
        else:
            st.error("Enter a numeric game ID.")

    st.divider()
    if st.button("Track Selected Game", use_container_width=True, type="primary"):
        if st.session_state.selected_game_id is None:
            st.warning("Load and select a game first.")
        else:
            st.session_state.tracking = True
            st.session_state.prev_skater_shot_attr = {}
            st.session_state.prev_goalie_shot_attr = {}
            st.session_state.prev_goal_attr = {}
            st.session_state.prev_fo_attr = {}
            st.session_state.is_first_tick = True
            st.session_state.warning_message = "STATUS: OK"
            st.session_state.warning_type = "ok"
            st.session_state.alert_shown_until = 0.0
            st.session_state.alert_log = _load_log(st.session_state.selected_game_id, "alert")
            st.session_state.correction_log = _load_log(st.session_state.selected_game_id, "corrections")
            st.session_state.correction_summary = {}
            st.session_state.stat_flash = {}
            st.session_state.prev_stat_totals = {}

    color_label = "Color Mode: ON" if st.session_state.color_mode else "Color Mode: OFF"
    if st.button(color_label, use_container_width=True):
        st.session_state.color_mode = not st.session_state.color_mode


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

if not st.session_state.tracking:
    warning_box("STATUS: OK", "ok")
    st.stop()

@st.fragment(run_every=REFRESH_SECS)
def render_live():
    tab_box, tab_fo, tab_corrections, tab_info = st.tabs(["Boxscore", "Faceoffs", "Stat Corrections", "Info"])
    try:
        game_data = fetch_json(PBP_URL.format(game_id=st.session_state.selected_game_id))
        parsed = parse_all_stats(game_data)
        player_lookup = build_player_lookup(game_data)
        home_abbrev, away_abbrev = get_home_away_abbrevs(game_data)

        now_str = datetime.now(ZoneInfo("America/New_York")).strftime("%I:%M:%S %p ET")
        player_team = build_player_team_lookup(game_data)
        game_state = str(game_data.get("gameState", "")).upper()
        is_live = game_state in {"LIVE", "CRIT"}

        alerts = []
        deltas = []
        if is_live and not st.session_state.is_first_tick:
            prev_snapshot = {
                "prev_skater_shot_attr": st.session_state.prev_skater_shot_attr,
                "prev_goalie_shot_attr": st.session_state.prev_goalie_shot_attr,
                "prev_goal_attr": st.session_state.prev_goal_attr,
                "prev_fo_attr": st.session_state.prev_fo_attr,
            }
            alerts, deltas = detect_corrections(parsed, prev_snapshot, player_lookup, player_team, now_str)

        if alerts:
            msg = " | ".join(f"⚠ {a}" for _, a, _ in alerts)
            st.session_state.warning_message = msg
            st.session_state.warning_type = "alert"
            st.session_state.alert_shown_until = time.time() + 7
            for period, a, impacts in alerts:
                entry = {
                    "Time": now_str,
                    "Period": period,
                    "Alert": a,
                    "Impacts": impacts,
                    "Type": "alert",
                    "Player": extract_player_from_alert(a),
                }
                st.session_state.alert_log.append(entry)
                st.session_state.correction_log.append(entry)
            apply_deltas(deltas, st.session_state.correction_summary)
            _save_log(st.session_state.selected_game_id, "alert", st.session_state.alert_log)
            _save_log(st.session_state.selected_game_id, "corrections", st.session_state.correction_log)
        elif time.time() >= st.session_state.alert_shown_until:
            st.session_state.warning_message = "STATUS: OK"
            st.session_state.warning_type = "ok"

        st.session_state.prev_skater_shot_attr = parsed["skater_shot_attr"]
        st.session_state.prev_goalie_shot_attr = parsed["goalie_shot_attr"]
        st.session_state.prev_goal_attr = parsed["goal_attr"]
        st.session_state.prev_fo_attr = parsed["fo_attr"]

        # Stat flash — only diff and flash for live games
        if is_live and not st.session_state.is_first_tick:
            new_flashes = diff_stat_totals(parsed, st.session_state.prev_stat_totals, time.time())
            for player, cols in new_flashes.items():
                if player not in st.session_state.stat_flash:
                    st.session_state.stat_flash[player] = {}
                st.session_state.stat_flash[player].update(cols)
        st.session_state.prev_stat_totals = snapshot_stat_totals(parsed)
        st.session_state.is_first_tick = False

        color_mode = st.session_state.color_mode
        team_filter = st.session_state.team_filter

        # -----------------------------------------------------------------------
        # Tab 1: Boxscore
        # -----------------------------------------------------------------------
        with tab_box:
            warning_box(st.session_state.warning_message, st.session_state.warning_type)
            col_all, col_away, col_home = st.columns(3)
            with col_all:
                if st.button("All Players", use_container_width=True, key="box_all"):
                    st.session_state.team_filter = "All"
            with col_away:
                if st.button(f"{away_abbrev} (Away)", use_container_width=True, key="box_away"):
                    st.session_state.team_filter = away_abbrev
            with col_home:
                if st.button(f"{home_abbrev} (Home)", use_container_width=True, key="box_home"):
                    st.session_state.team_filter = home_abbrev

            st.markdown("<div style='margin-top:14px;'></div>", unsafe_allow_html=True)
            hdr_col, sort_col_box = st.columns([5, 1])
            with hdr_col:
                section_header("Skaters — G / A / PTS / SOG / BS")
            with sort_col_box:
                skater_sort = sort_bar("skaters", ["G", "A", "PTS", "SOG", "BS"])
            skater_rows = []
            for pid, s in parsed["skater_stats"].items():
                if team_filter != "All" and s["team"] != team_filter:
                    continue
                skater_rows.append({
                    "Team": s["team"],
                    "Player": s["name"],
                    "G": s["goals"],
                    "A": s["assists"],
                    "PTS": s["points"],
                    "SOG": s["sog"],
                    "BS": s["blocked"],
                })
            skater_rows = apply_sort(skater_rows, skater_sort)
            if team_filter != "All" and skater_rows:
                team_summary_card(team_filter, {
                    "G": sum(r["G"] for r in skater_rows),
                    "A": sum(r["A"] for r in skater_rows),
                    "PTS": sum(r["PTS"] for r in skater_rows),
                    "SOG": sum(r["SOG"] for r in skater_rows),
                    "BS": sum(r["BS"] for r in skater_rows),
                })
            if skater_rows:
                st.markdown(html_table(skater_rows, color_mode, flash=st.session_state.stat_flash), unsafe_allow_html=True)
            else:
                st.info("No skater stats yet.")

            st.markdown("<div style='margin-top:14px;'></div>", unsafe_allow_html=True)
            hdr_col_g, sort_col_g = st.columns([5, 1])
            with hdr_col_g:
                section_header("Goalies — Saves")
            with sort_col_g:
                goalie_sort = sort_bar("goalies", ["SV"], name_col="Goalie")
            goalie_rows = []
            for pid, g in parsed["goalie_stats"].items():
                if team_filter != "All" and g["team"] != team_filter:
                    continue
                saves = g["shots_against"] - g["goals_allowed"]
                if saves == 0:
                    continue
                goalie_rows.append({
                    "Team": g["team"],
                    "Goalie": g["name"],
                    "SV": saves,
                })
            goalie_rows = apply_sort(goalie_rows, goalie_sort, name_col="Goalie")
            if team_filter != "All" and goalie_rows:
                team_summary_card(team_filter, {
                    "SV": sum(r["SV"] for r in goalie_rows),
                })
            if goalie_rows:
                st.markdown(html_table(goalie_rows, color_mode, team_col="Team", flash=st.session_state.stat_flash), unsafe_allow_html=True)
            else:
                st.info("No goalie stats yet.")

        # -----------------------------------------------------------------------
        # Tab 2: Faceoffs
        # -----------------------------------------------------------------------
        with tab_fo:
            warning_box(st.session_state.warning_message, st.session_state.warning_type)
            col_all, col_away, col_home = st.columns(3)
            with col_all:
                if st.button("All Players", use_container_width=True, key="fo_all"):
                    st.session_state.team_filter = "All"
            with col_away:
                if st.button(f"{away_abbrev} (Away)", use_container_width=True, key="fo_away"):
                    st.session_state.team_filter = away_abbrev
            with col_home:
                if st.button(f"{home_abbrev} (Home)", use_container_width=True, key="fo_home"):
                    st.session_state.team_filter = home_abbrev

            st.markdown("<div style='margin-top:14px;'></div>", unsafe_allow_html=True)
            hdr_col_fo, sort_col_fo = st.columns([5, 1])
            with hdr_col_fo:
                section_header("Faceoffs — Taken / Won / Win%")
            with sort_col_fo:
                fo_sort = sort_bar("fo", ["FO Taken", "FO Won"])
            fo_rows = []
            for pid, f in parsed["fo_stats"].items():
                if team_filter != "All" and f["team"] != team_filter:
                    continue
                win_pct = f"{round(100 * f['fo_won'] / f['fo_taken'])}%" if f["fo_taken"] > 0 else "—"
                fo_rows.append({
                    "Team": f["team"],
                    "Player": f["name"],
                    "FO Taken": f["fo_taken"],
                    "FO Won": f["fo_won"],
                    "Win %": win_pct,
                })
            fo_rows = apply_sort(fo_rows, fo_sort)
            if team_filter != "All" and fo_rows:
                total_taken = sum(r["FO Taken"] for r in fo_rows)
                total_won = sum(r["FO Won"] for r in fo_rows)
                win_pct_total = f"{round(100 * total_won / total_taken)}%" if total_taken > 0 else "—"
                team_summary_card(team_filter, {
                    "FO Taken": total_taken,
                    "FO Won": total_won,
                    "Win %": win_pct_total,
                })
            if fo_rows:
                st.markdown(html_table(fo_rows, color_mode, flash=st.session_state.stat_flash), unsafe_allow_html=True)
            else:
                st.info("No faceoff data yet. Note: player IDs on faceoffs may not be populated by the API mid-game.")

        # -----------------------------------------------------------------------
        # Tab 3: Stat Corrections
        # -----------------------------------------------------------------------
        with tab_corrections:
            corr_log = st.session_state.correction_log

            col_clear, col_download, _ = st.columns([1, 1, 3])
            with col_clear:
                if corr_log and st.button("Clear Corrections", key="clear_corrections"):
                    st.session_state.correction_log = []
                    st.session_state.correction_summary = {}
                    _clear_log(st.session_state.selected_game_id, "corrections")
                    st.rerun()
            with col_download:
                if corr_log:
                    import io, csv as _csv, re as _re
                    def _players_from_impacts(impacts):
                        # Each impact string is like "Player Name STAT +1" or "Player Name STAT -1".
                        # Strip the trailing " STAT ±N" portion to get the player name.
                        names = []
                        for s in (impacts or []):
                            m = _re.match(r"^(.+?)\s+\S+\s+[+\-]\d+$", s.strip())
                            if m:
                                names.append(m.group(1))
                        unique = list(dict.fromkeys(names))
                        return unique[0] if len(unique) == 1 else ""
                    buf = io.StringIO()
                    buf.write("﻿")  # UTF-8 BOM for Excel
                    writer = _csv.DictWriter(buf, fieldnames=["Time", "Period", "Player", "Alert", "Impacts", "Type"])
                    writer.writeheader()
                    for entry in corr_log:
                        impacts = entry.get("Impacts") or []
                        writer.writerow({
                            "Time": entry.get("Time", ""),
                            "Period": entry.get("Period", ""),
                            "Player": _players_from_impacts(impacts),
                            "Alert": entry.get("Alert", ""),
                            "Impacts": " | ".join(impacts),
                            "Type": entry.get("Type", ""),
                        })
                    st.download_button(
                        "Download CSV",
                        data=buf.getvalue(),
                        file_name=f"nhl_stat_corrections_{st.session_state.selected_game_id}.csv",
                        mime="text/csv",
                        key="download_corrections",
                    )

            if corr_log:
                for entry in reversed(corr_log):
                    impacts = entry.get("Impacts") or []
                    impact_parts = []
                    for imp_str in impacts:
                        # color positive values green, negative red
                        if any(c in imp_str for c in ["+1", "+2", "+3"]):
                            color = "#00cc44"
                        else:
                            color = "#cc2200"
                        impact_parts.append(f'<span style="color:{color}; font-weight:700;">{imp_str}</span>')
                    impact_html = (
                        f'<div style="font-size:12px; margin-top:4px; opacity:0.85;">'
                        f'{"&nbsp;&nbsp;|&nbsp;&nbsp;".join(impact_parts)}</div>'
                    ) if impact_parts else ""
                    st.markdown(
                        f'<div style="padding:10px 14px; margin-bottom:6px; border-radius:8px; '
                        f'background-color:var(--secondary-background-color); border-left:4px solid #ff9900; '
                        f'font-size:15px; color:var(--text-color);">'
                        f'<span style="font-weight:700; color:#ff9900;">P{entry["Period"]}</span>'
                        f'&nbsp;&nbsp;{entry["Alert"]}'
                        f'<span style="float:right; font-size:12px; opacity:0.55;">{entry.get("Time", "")}</span>'
                        f'{impact_html}</div>',
                        unsafe_allow_html=True,
                    )
            else:
                st.info("No corrections recorded yet.")

        # -----------------------------------------------------------------------
        # Tab 4: Info
        # -----------------------------------------------------------------------
        with tab_info:
            st.markdown("""
<div style='font-size:15px; line-height:1.7;'>

<div style='font-size:20px; font-weight:700; margin-bottom:4px;'>Boxscore</div>

Live player stats pulled from the NHL play-by-play API, rebuilt from scratch every 3 seconds.

- **Skaters** — Goals, Assists, Points, Shots on Goal, and Blocked Shots per player
- **Goalies** — Saves (shots against minus goals allowed). Goalies only appear once they have recorded at least one save — the PBP feed has no way to identify who is in net until a shot is registered against them.
- **Cell flash** — a stat cell turns <span style='background:rgba(0,200,80,0.30); padding:1px 7px; border-radius:4px; font-weight:700;'>green</span> when a value increases and <span style='background:rgba(220,30,30,0.30); padding:1px 7px; border-radius:4px; font-weight:700;'>red</span> when it decreases. Flash lasts 10 seconds then clears automatically. This fires on both legitimate new stats and corrections. Applies to G, A, PTS, SOG, and BS columns.

---

<div style='font-size:20px; font-weight:700; margin-bottom:4px;'>Faceoffs</div>

Faceoffs taken, won, and win % per player, sourced from faceoff events in the play-by-play feed.

- Player IDs on faceoff events are not always populated by the NHL API mid-game — rows will appear as data becomes available
- **Cell flash** — same <span style='background:rgba(0,200,80,0.30); padding:1px 7px; border-radius:4px; font-weight:700;'>green</span> / <span style='background:rgba(220,30,30,0.30); padding:1px 7px; border-radius:4px; font-weight:700;'>red</span> flash applies to FO Taken and FO Won columns

---

<div style='font-size:20px; font-weight:700; margin-bottom:4px;'>Stat Corrections</div>

Monitors the NHL play-by-play feed for retroactive changes to player stats. Every tick the tool diffs the current event log against the previous tick and flags any discrepancy.

**Correction Log** — every individual correction event in reverse chronological order, with real-world ET timestamp, period, and a full description of what changed.

</div>
""", unsafe_allow_html=True)

            st.markdown(
                '<div style="font-size:11px; font-weight:700; letter-spacing:0.08em; opacity:0.5; margin-bottom:4px; text-transform:uppercase;">Example</div>'
                '<div style="padding:10px 14px; margin-bottom:6px; border-radius:8px; '
                'background-color:var(--secondary-background-color); border-left:4px solid #ff9900; '
                'font-size:15px; color:var(--text-color);">'
                '<span style="font-weight:700; color:#ff9900;">P2</span>'
                '&nbsp;&nbsp;SOG RE-ATTRIBUTED: P2 14:22 — Auston Matthews → William Nylander'
                '<span style="float:right; font-size:12px; opacity:0.55;">08:41:07 PM ET</span>'
                '<div style="font-size:12px; margin-top:4px; opacity:0.85;">'
                '<span style="color:#cc2200; font-weight:700;">Matthews SOG -1</span>'
                '&nbsp;&nbsp;|&nbsp;&nbsp;'
                '<span style="color:#00cc44; font-weight:700;">Nylander SOG +1</span>'
                '</div></div>',
                unsafe_allow_html=True,
            )

            st.markdown("""
<div style='font-size:15px; line-height:1.7;'>

Correction types detected:
- SOG removed or re-attributed to a different shooter
- Goalie shot-against removed or re-attributed
- Goal removed entirely
- Goal re-attributed to a different scorer
- Primary or secondary assist changed
- Faceoff winner or loser changed
- Faceoff event removed

**Status bar** — turns yellow with correction details whenever any of the above fires. Returns to green after 7 seconds if no further corrections.

</div>
""", unsafe_allow_html=True)

            st.markdown(
                '<div style="font-size:11px; font-weight:700; letter-spacing:0.08em; opacity:0.5; margin-bottom:4px; text-transform:uppercase;">Example</div>'
                '<div style="margin-bottom:18px; padding:10px 16px; border-radius:10px; font-size:14px; font-weight:700; '
                'background-color:#3a1600; color:#ffd966; border:2px solid #ff9900;">'
                '⚠ SOG RE-ATTRIBUTED: P2 14:22 — Auston Matthews → William Nylander</div>',
                unsafe_allow_html=True,
            )

            st.markdown("""
<div style='font-size:15px; line-height:1.7;'>

---



</div>
""", unsafe_allow_html=True)

    except RateLimitedError:
        st.session_state.warning_message = "⚠ RATE LIMITED — retrying next tick"
        st.session_state.warning_type = "alert"
        st.session_state.alert_shown_until = time.time() + 15
        with tab_box:
            warning_box(st.session_state.warning_message, st.session_state.warning_type)
    except Exception as e:
        with tab_box:
            st.error(f"Refresh error: {e}")


render_live()
