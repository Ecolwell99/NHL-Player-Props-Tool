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
        "color_mode": True,
        "active_only": True,
        "team_filter": "All",
        "is_first_tick": True,
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

    skater_stats: dict = {}
    goalie_stats: dict = {}
    fo_stats: dict = {}
    skater_shot_attr: dict = {}
    goalie_shot_attr: dict = {}
    goal_attr: dict = {}
    fo_attr: dict = {}

    def ensure_skater(pid):
        if pid not in skater_stats:
            skater_stats[pid] = {
                "name": player_lookup.get(pid, f"ID {pid}"),
                "team": player_team.get(pid, "UNK"),
                "goals": 0, "assists": 0, "points": 0, "sog": 0,
            }

    def ensure_goalie(pid):
        if pid not in goalie_stats:
            goalie_stats[pid] = {
                "name": player_lookup.get(pid, f"ID {pid}"),
                "team": player_team.get(pid, "UNK"),
                "shots_against": 0,
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
                goalie_shot_attr[event_id] = {"pid": goalie, "period": period, "time_remaining": time_rem}

            goal_attr[event_id] = {
                "scorer": scorer, "a1": a1, "a2": a2,
                "period": period, "time_remaining": time_rem,
            }

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

def detect_corrections(parsed: dict, prev: dict, player_lookup: dict) -> list[tuple]:
    alerts = []

    def pname(pid):
        return player_lookup.get(pid, f"ID {pid}") if pid else "None"

    cur_skater_shot = parsed["skater_shot_attr"]
    cur_goalie_shot = parsed["goalie_shot_attr"]
    cur_goal = parsed["goal_attr"]
    cur_fo = parsed["fo_attr"]

    prev_skater_shot = prev["prev_skater_shot_attr"]
    prev_goalie_shot = prev["prev_goalie_shot_attr"]
    prev_goal = prev["prev_goal_attr"]
    prev_fo = prev["prev_fo_attr"]

    for eid, attr in prev_skater_shot.items():
        if eid not in cur_skater_shot:
            alerts.append((attr["period"], f"SOG REMOVED: {pname(attr['pid'])} — P{attr['period']} {attr['time_remaining']}"))
    for eid, attr in cur_skater_shot.items():
        if eid in prev_skater_shot and prev_skater_shot[eid]["pid"] != attr["pid"]:
            alerts.append((attr["period"], (
                f"SOG RE-ATTRIBUTED: P{attr['period']} {attr['time_remaining']} — "
                f"{pname(prev_skater_shot[eid]['pid'])} → {pname(attr['pid'])}"
            )))

    for eid, attr in prev_goalie_shot.items():
        if eid not in cur_goalie_shot:
            alerts.append((attr["period"], f"GOALIE SOG REMOVED: {pname(attr['pid'])} — P{attr['period']} {attr['time_remaining']}"))
    for eid, attr in cur_goalie_shot.items():
        if eid in prev_goalie_shot and prev_goalie_shot[eid]["pid"] != attr["pid"]:
            alerts.append((attr["period"], (
                f"GOALIE SOG RE-ATTRIBUTED: P{attr['period']} {attr['time_remaining']} — "
                f"{pname(prev_goalie_shot[eid]['pid'])} → {pname(attr['pid'])}"
            )))

    for eid, attr in prev_goal.items():
        if eid not in cur_goal:
            alerts.append((attr["period"], f"GOAL REMOVED: {pname(attr['scorer'])} — P{attr['period']} {attr['time_remaining']}"))
    for eid, attr in cur_goal.items():
        if eid not in prev_goal:
            continue
        p_attr = prev_goal[eid]
        p, t = attr["period"], attr["time_remaining"]
        if p_attr["scorer"] != attr["scorer"]:
            alerts.append((p, f"GOAL RE-ATTRIBUTED: P{p} {t} — {pname(p_attr['scorer'])} → {pname(attr['scorer'])}"))
        if p_attr["a1"] != attr["a1"]:
            alerts.append((p, f"PRIMARY ASSIST CHANGED: P{p} {t} — {pname(p_attr['a1'])} → {pname(attr['a1'])}"))
        if p_attr["a2"] != attr["a2"]:
            alerts.append((p, f"SECONDARY ASSIST CHANGED: P{p} {t} — {pname(p_attr['a2'])} → {pname(attr['a2'])}"))

    for eid, attr in prev_fo.items():
        if eid not in cur_fo:
            alerts.append((attr["period"], f"FACEOFF REMOVED: P{attr['period']} {attr['time_remaining']} winner={pname(attr['winner_id'])}"))
    for eid, attr in cur_fo.items():
        if eid not in prev_fo:
            continue
        p_attr = prev_fo[eid]
        p, t = attr["period"], attr["time_remaining"]
        if p_attr["winner_id"] != attr["winner_id"]:
            alerts.append((p, f"FACEOFF WINNER CHANGED: P{p} {t} — {pname(p_attr['winner_id'])} → {pname(attr['winner_id'])}"))
        if p_attr["loser_id"] != attr["loser_id"]:
            alerts.append((p, f"FACEOFF LOSER CHANGED: P{p} {t} — {pname(p_attr['loser_id'])} → {pname(attr['loser_id'])}"))

    return alerts


def tally_corrections_by_player(correction_log: list) -> list[dict]:
    counts: dict = defaultdict(lambda: defaultdict(int))
    for entry in correction_log:
        alert_text = entry.get("Alert", "")
        player = entry.get("Player", "Unknown")
        if "GOALIE SOG" in alert_text:
            counts[player]["Goalie SOG"] += 1
        elif "SOG REMOVED" in alert_text or "SOG RE-ATTRIBUTED" in alert_text:
            counts[player]["SOG"] += 1
        elif "GOAL REMOVED" in alert_text or "GOAL RE-ATTRIBUTED" in alert_text:
            counts[player]["Goal"] += 1
        elif "ASSIST CHANGED" in alert_text:
            counts[player]["Assist"] += 1
        elif "FACEOFF" in alert_text:
            counts[player]["FO"] += 1
        else:
            counts[player]["Other"] += 1

    rows = []
    for player, c in counts.items():
        total = sum(c.values())
        rows.append({
            "Player": player,
            "SOG": c.get("SOG", 0),
            "Goalie SOG": c.get("Goalie SOG", 0),
            "Goal": c.get("Goal", 0),
            "Assist": c.get("Assist", 0),
            "FO": c.get("FO", 0),
            "Total": total,
        })
    rows.sort(key=lambda r: -r["Total"])
    return rows


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


def html_table(rows: list[dict], color_mode: bool = False, team_col: str = "Team") -> str:
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
            if color_mode and h == team_col:
                display = team_pill(str(val))
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


# ---------------------------------------------------------------------------
# App
# ---------------------------------------------------------------------------

init_state()

with st.sidebar:
    st.title("NHL Player Props")
    st.caption("Dev / review tool only. Do not result off this tool.")
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

    active_label = "Active Players Only: ON" if st.session_state.active_only else "Active Players Only: OFF"
    if st.button(active_label, use_container_width=True):
        st.session_state.active_only = not st.session_state.active_only

    color_label = "Color Mode: ON" if st.session_state.color_mode else "Color Mode: OFF"
    if st.button(color_label, use_container_width=True):
        st.session_state.color_mode = not st.session_state.color_mode


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

if not st.session_state.tracking:
    warning_box("STATUS: OK", "ok")
    st.stop()

tab_box, tab_fo, tab_corrections, tab_alerts = st.tabs(["Boxscore", "Faceoffs", "Stat Corrections", "Alert Log"])

try:
    game_data = fetch_json(PBP_URL.format(game_id=st.session_state.selected_game_id))
    parsed = parse_all_stats(game_data)
    player_lookup = build_player_lookup(game_data)
    home_abbrev, away_abbrev = get_home_away_abbrevs(game_data)

    alerts = []
    if not st.session_state.is_first_tick:
        prev_snapshot = {
            "prev_skater_shot_attr": st.session_state.prev_skater_shot_attr,
            "prev_goalie_shot_attr": st.session_state.prev_goalie_shot_attr,
            "prev_goal_attr": st.session_state.prev_goal_attr,
            "prev_fo_attr": st.session_state.prev_fo_attr,
        }
        alerts = detect_corrections(parsed, prev_snapshot, player_lookup)

    now_str = datetime.now(ZoneInfo("America/New_York")).strftime("%I:%M:%S %p ET")

    if alerts:
        msg = " | ".join(f"⚠ {a}" for _, a in alerts)
        st.session_state.warning_message = msg
        st.session_state.warning_type = "alert"
        st.session_state.alert_shown_until = time.time() + 7
        for period, a in alerts:
            entry = {
                "Time": now_str,
                "Period": period,
                "Alert": a,
                "Type": "alert",
                "Player": extract_player_from_alert(a),
            }
            st.session_state.alert_log.append(entry)
            st.session_state.correction_log.append(entry)
        _save_log(st.session_state.selected_game_id, "alert", st.session_state.alert_log)
        _save_log(st.session_state.selected_game_id, "corrections", st.session_state.correction_log)
    elif time.time() >= st.session_state.alert_shown_until:
        st.session_state.warning_message = "STATUS: OK"
        st.session_state.warning_type = "ok"

    st.session_state.prev_skater_shot_attr = parsed["skater_shot_attr"]
    st.session_state.prev_goalie_shot_attr = parsed["goalie_shot_attr"]
    st.session_state.prev_goal_attr = parsed["goal_attr"]
    st.session_state.prev_fo_attr = parsed["fo_attr"]
    st.session_state.is_first_tick = False

    color_mode = st.session_state.color_mode
    active_only = st.session_state.active_only
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

        section_header("Skaters — G / A / PTS / SOG")
        skater_rows = []
        for pid, s in parsed["skater_stats"].items():
            if team_filter != "All" and s["team"] != team_filter:
                continue
            if active_only and s["goals"] == 0 and s["assists"] == 0 and s["sog"] == 0:
                continue
            skater_rows.append({
                "Player": s["name"],
                "Team": s["team"],
                "G": s["goals"],
                "A": s["assists"],
                "PTS": s["points"],
                "SOG": s["sog"],
            })
        skater_rows.sort(key=lambda r: r["Player"].split()[-1])
        if skater_rows:
            st.markdown(html_table(skater_rows, color_mode), unsafe_allow_html=True)
        else:
            st.info("No skater stats yet.")

        section_header("Goalies — Shots Against")
        goalie_rows = []
        for pid, g in parsed["goalie_stats"].items():
            if team_filter != "All" and g["team"] != team_filter:
                continue
            if active_only and g["shots_against"] == 0:
                continue
            goalie_rows.append({
                "Goalie": g["name"],
                "Team": g["team"],
                "SA": g["shots_against"],
            })
        goalie_rows.sort(key=lambda r: r["Goalie"].split()[-1])
        if goalie_rows:
            st.markdown(html_table(goalie_rows, color_mode, team_col="Team"), unsafe_allow_html=True)
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

        section_header("Faceoffs — Taken / Won / Win%")
        fo_rows = []
        for pid, f in parsed["fo_stats"].items():
            if team_filter != "All" and f["team"] != team_filter:
                continue
            if active_only and f["fo_taken"] == 0:
                continue
            win_pct = f"{round(100 * f['fo_won'] / f['fo_taken'])}%" if f["fo_taken"] > 0 else "—"
            fo_rows.append({
                "Player": f["name"],
                "Team": f["team"],
                "FO Taken": f["fo_taken"],
                "FO Won": f["fo_won"],
                "Win %": win_pct,
            })
        fo_rows.sort(key=lambda r: r["Player"].split()[-1])
        if fo_rows:
            st.markdown(html_table(fo_rows, color_mode), unsafe_allow_html=True)
        else:
            st.info("No faceoff data yet. Note: player IDs on faceoffs may not be populated by the API mid-game.")

    # -----------------------------------------------------------------------
    # Tab 3: Stat Corrections
    # -----------------------------------------------------------------------
    with tab_corrections:
        corr_log = st.session_state.correction_log

        col_clear, _ = st.columns([1, 4])
        with col_clear:
            if corr_log and st.button("Clear Corrections", key="clear_corrections"):
                st.session_state.correction_log = []
                _clear_log(st.session_state.selected_game_id, "corrections")
                st.rerun()

        if corr_log:
            section_header("Correction Totals by Player")
            summary_rows = tally_corrections_by_player(corr_log)
            st.markdown(html_table(summary_rows, color_mode=False), unsafe_allow_html=True)

            section_header("Full Correction Log")
            log_rows = [
                {
                    "Time": e.get("Time", ""),
                    "Period": f"P{e['Period']}",
                    "Alert": e["Alert"],
                }
                for e in reversed(corr_log)
            ]
            st.markdown(html_table(log_rows, color_mode=False), unsafe_allow_html=True)
        else:
            st.info("No stat corrections recorded yet.")

    # -----------------------------------------------------------------------
    # Tab 4: Alert Log
    # -----------------------------------------------------------------------
    with tab_alerts:
        log = st.session_state.alert_log
        if log:
            if st.button("Clear Alert Log", key="clear_alerts"):
                st.session_state.alert_log = []
                _clear_log(st.session_state.selected_game_id, "alert")
                st.rerun()
            for entry in reversed(log):
                color = "#ff9900" if entry["Type"] == "alert" else "#66ff99"
                st.markdown(
                    f'<div style="padding:10px 14px; margin-bottom:6px; border-radius:8px; '
                    f'background-color:var(--secondary-background-color); border-left:4px solid {color}; '
                    f'font-size:15px; color:var(--text-color);">'
                    f'<span style="font-weight:700; color:{color};">P{entry["Period"]}</span>'
                    f'&nbsp;&nbsp;{entry["Alert"]}'
                    f'<span style="float:right; font-size:12px; opacity:0.55;">{entry.get("Time", "")}</span></div>',
                    unsafe_allow_html=True,
                )
        else:
            st.info("No alerts recorded yet.")

except RateLimitedError:
    st.session_state.warning_message = "⚠ RATE LIMITED — retrying next tick"
    st.session_state.warning_type = "alert"
    st.session_state.alert_shown_until = time.time() + 15
    with tab_box:
        warning_box(st.session_state.warning_message, st.session_state.warning_type)
except Exception as e:
    with tab_box:
        st.error(f"Refresh error: {e}")

time.sleep(REFRESH_SECS)
st.rerun()
