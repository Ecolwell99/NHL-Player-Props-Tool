import json
import os
import time
from datetime import datetime
from zoneinfo import ZoneInfo

import requests
import streamlit as st

SCOREBOARD_URL = "https://api-web.nhle.com/v1/scoreboard/now"
PBP_URL = "https://api-web.nhle.com/v1/gamecenter/{game_id}/play-by-play"
REFRESH_SECS = 3

ALERT_LOG_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "nhl_alert_logs")

st.set_page_config(page_title="NHL Player Props Dev Tool", layout="wide")

STATE_VERSION = 1


def init_state():
    defaults = {
        "games": [],
        "selected_game_label": None,
        "selected_game_id": None,
        "tracking": False,
        "prev_shot_attr": {},
        "prev_goal_attr": {},
        "warning_message": "STATUS: OK",
        "warning_type": "ok",
        "alert_shown_until": 0.0,
        "alert_log": [],
        "color_mode": True,
        "active_only": True,
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


# --- Alert log ---

def alert_log_path(game_id: int) -> str:
    return os.path.join(ALERT_LOG_DIR, f"props_game_{game_id}.json")


def load_alert_log(game_id: int) -> list:
    try:
        path = alert_log_path(game_id)
        if os.path.exists(path):
            with open(path) as f:
                return json.load(f)
    except Exception:
        pass
    return []


def save_alert_log(game_id: int, log: list):
    try:
        os.makedirs(ALERT_LOG_DIR, exist_ok=True)
        with open(alert_log_path(game_id), "w") as f:
            json.dump(log, f)
    except Exception:
        pass


def clear_alert_log(game_id: int):
    try:
        path = alert_log_path(game_id)
        if os.path.exists(path):
            os.remove(path)
    except Exception:
        pass


# --- API ---

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


# --- Lookups ---

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


def get_home_away_abbrevs(game_data: dict):
    home = game_data.get("homeTeam") or {}
    away = game_data.get("awayTeam") or {}
    return (
        extract_abbrev(home.get("abbrev"), "HOME"),
        extract_abbrev(away.get("abbrev"), "AWAY"),
    )


# --- Stat parsing ---

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


def parse_player_stats(game_data: dict) -> tuple[dict, dict, dict]:
    """
    Returns:
        player_stats: player_id -> {name, team, goals, assists, points, sog}
        shot_attr:    event_id  -> {pid, period, time_remaining}
        goal_attr:    event_id  -> {scorer, a1, a2, period, time_remaining}
    """
    plays = game_data.get("plays") or []
    player_lookup = build_player_lookup(game_data)
    player_team = build_player_team_lookup(game_data)

    player_stats: dict = {}
    shot_attr: dict = {}
    goal_attr: dict = {}

    def ensure(pid):
        if pid not in player_stats:
            player_stats[pid] = {
                "name": player_lookup.get(pid, f"ID {pid}"),
                "team": player_team.get(pid, "UNK"),
                "goals": 0,
                "assists": 0,
                "points": 0,
                "sog": 0,
            }

    for play in plays:
        play_type = str(play.get("typeDescKey", "")).lower()
        details = play.get("details") or {}
        event_id = play.get("eventId")
        period = (play.get("periodDescriptor") or {}).get("number")
        time_rem = convert_to_time_remaining(play.get("timeInPeriod", ""), period, game_data)

        if play_type == "shot-on-goal":
            pid = details.get("shootingPlayerId")
            if pid:
                ensure(pid)
                player_stats[pid]["sog"] += 1
                shot_attr[event_id] = {"pid": pid, "period": period, "time_remaining": time_rem}

        elif play_type == "goal":
            scorer = details.get("scoringPlayerId")
            a1 = details.get("assist1PlayerId")
            a2 = details.get("assist2PlayerId")

            if scorer:
                ensure(scorer)
                player_stats[scorer]["goals"] += 1
                player_stats[scorer]["sog"] += 1  # goals count as SOG
            if a1:
                ensure(a1)
                player_stats[a1]["assists"] += 1
            if a2:
                ensure(a2)
                player_stats[a2]["assists"] += 1

            goal_attr[event_id] = {
                "scorer": scorer, "a1": a1, "a2": a2,
                "period": period, "time_remaining": time_rem,
            }

    for s in player_stats.values():
        s["points"] = s["goals"] + s["assists"]

    return player_stats, shot_attr, goal_attr


# --- Correction detection ---

def detect_corrections(
    cur_shot: dict,
    cur_goal: dict,
    prev_shot: dict,
    prev_goal: dict,
    player_lookup: dict,
) -> list[tuple]:
    alerts = []

    def pname(pid):
        return player_lookup.get(pid, f"ID {pid}") if pid else "None"

    for eid, attr in prev_shot.items():
        if eid not in cur_shot:
            alerts.append((attr["period"], f"SOG REMOVED: {pname(attr['pid'])} — P{attr['period']} {attr['time_remaining']}"))

    for eid, attr in cur_shot.items():
        if eid in prev_shot and prev_shot[eid]["pid"] != attr["pid"]:
            alerts.append((attr["period"], (
                f"SOG RE-ATTRIBUTED: P{attr['period']} {attr['time_remaining']} — "
                f"{pname(prev_shot[eid]['pid'])} → {pname(attr['pid'])}"
            )))

    for eid, attr in prev_goal.items():
        if eid not in cur_goal:
            alerts.append((attr["period"], f"GOAL REMOVED: {pname(attr['scorer'])} — P{attr['period']} {attr['time_remaining']}"))

    for eid, attr in cur_goal.items():
        if eid not in prev_goal:
            continue
        prev = prev_goal[eid]
        p, t = attr["period"], attr["time_remaining"]
        if prev["scorer"] != attr["scorer"]:
            alerts.append((p, f"GOAL RE-ATTRIBUTED: P{p} {t} — {pname(prev['scorer'])} → {pname(attr['scorer'])}"))
        if prev["a1"] != attr["a1"]:
            alerts.append((p, f"PRIMARY ASSIST CHANGED: P{p} {t} — {pname(prev['a1'])} → {pname(attr['a1'])}"))
        if prev["a2"] != attr["a2"]:
            alerts.append((p, f"SECONDARY ASSIST CHANGED: P{p} {t} — {pname(prev['a2'])} → {pname(attr['a2'])}"))

    return alerts


# --- Display ---

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


def html_player_table(rows: list[dict], color_mode: bool = False) -> str:
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
            if color_mode and h == "Team":
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


# --- App ---

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
            st.session_state.prev_shot_attr = {}
            st.session_state.prev_goal_attr = {}
            st.session_state.is_first_tick = True
            st.session_state.warning_message = "STATUS: OK"
            st.session_state.warning_type = "ok"
            st.session_state.alert_shown_until = 0.0
            st.session_state.alert_log = load_alert_log(st.session_state.selected_game_id)

    active_label = "Active Players Only: ON" if st.session_state.active_only else "Active Players Only: OFF"
    if st.button(active_label, use_container_width=True):
        st.session_state.active_only = not st.session_state.active_only

    color_label = "Color Mode: ON" if st.session_state.color_mode else "Color Mode: OFF"
    if st.button(color_label, use_container_width=True):
        st.session_state.color_mode = not st.session_state.color_mode


# --- Main ---

if st.session_state.tracking:

    tab_main, tab_log = st.tabs(["Live", "Correction Log"])

    with tab_log:
        log = st.session_state.alert_log
        if log:
            if st.button("Clear Log", key="clear_log"):
                st.session_state.alert_log = []
                clear_alert_log(st.session_state.selected_game_id)
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
            st.info("No corrections recorded yet.")

    with tab_main:
        try:
            game_data = fetch_json(PBP_URL.format(game_id=st.session_state.selected_game_id))
            player_stats, cur_shot, cur_goal = parse_player_stats(game_data)
            player_lookup = build_player_lookup(game_data)
            home_abbrev, away_abbrev = get_home_away_abbrevs(game_data)

            # Correction detection — skip first tick (no baseline yet)
            alerts = []
            if not st.session_state.is_first_tick:
                alerts = detect_corrections(
                    cur_shot, cur_goal,
                    st.session_state.prev_shot_attr,
                    st.session_state.prev_goal_attr,
                    player_lookup,
                )

            if alerts:
                msg = " | ".join(f"⚠ {a}" for _, a in alerts)
                st.session_state.warning_message = msg
                st.session_state.warning_type = "alert"
                st.session_state.alert_shown_until = time.time() + 7
                for period, a in alerts:
                    st.session_state.alert_log.append({
                        "Time": datetime.now(ZoneInfo("America/New_York")).strftime("%I:%M:%S %p ET"),
                        "Period": period,
                        "Alert": a,
                        "Type": "alert",
                    })
                save_alert_log(st.session_state.selected_game_id, st.session_state.alert_log)
            elif time.time() >= st.session_state.alert_shown_until:
                st.session_state.warning_message = "STATUS: OK"
                st.session_state.warning_type = "ok"

            st.session_state.prev_shot_attr = cur_shot
            st.session_state.prev_goal_attr = cur_goal
            st.session_state.is_first_tick = False

            warning_box(st.session_state.warning_message, st.session_state.warning_type)

            # Team filter
            col_away, col_home, col_all = st.columns(3)
            with col_away:
                if st.button(f"{away_abbrev} (Away)", use_container_width=True):
                    st.session_state["team_filter"] = away_abbrev
            with col_home:
                if st.button(f"{home_abbrev} (Home)", use_container_width=True):
                    st.session_state["team_filter"] = home_abbrev
            with col_all:
                if st.button("All Players", use_container_width=True):
                    st.session_state["team_filter"] = "All"

            if "team_filter" not in st.session_state:
                st.session_state["team_filter"] = "All"
            team_filter = st.session_state["team_filter"]

            # Build display rows
            rows = []
            for pid, s in player_stats.items():
                if team_filter != "All" and s["team"] != team_filter:
                    continue
                if st.session_state.active_only and s["goals"] == 0 and s["assists"] == 0 and s["sog"] == 0:
                    continue
                rows.append({
                    "Player": s["name"],
                    "Team": s["team"],
                    "G": s["goals"],
                    "A": s["assists"],
                    "PTS": s["points"],
                    "SOG": s["sog"],
                })

            rows.sort(key=lambda r: (-r["PTS"], -r["SOG"], r["Player"]))

            filter_desc = team_filter if team_filter != "All" else f"{away_abbrev} + {home_abbrev}"
            active_desc = " (active only)" if st.session_state.active_only else ""
            st.markdown(
                f"<div style='font-size:20px; font-weight:400; margin-top:8px; margin-bottom:8px;'>"
                f"Player Stats — {filter_desc}{active_desc}</div>",
                unsafe_allow_html=True,
            )

            if rows:
                st.markdown(html_player_table(rows, st.session_state.color_mode), unsafe_allow_html=True)
            else:
                st.info("No player stats yet." if st.session_state.active_only else "No players found.")

        except RateLimitedError:
            st.session_state.warning_message = "⚠ RATE LIMITED — retrying next tick"
            st.session_state.warning_type = "alert"
            st.session_state.alert_shown_until = time.time() + 15
            warning_box(st.session_state.warning_message, st.session_state.warning_type)
        except Exception as e:
            st.error(f"Refresh error: {e}")

    time.sleep(REFRESH_SECS)
    st.rerun()

else:
    warning_box("STATUS: OK", "ok")
    st.info("Load live games, select one, and click Track Selected Game.")
