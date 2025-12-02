import sqlite3
from collections import defaultdict
import requests
DB_FILE = "shots.db"

# ============================================================
# 0. ---- DB CONNECTION
# ============================================================

def get_db():
    return sqlite3.connect(DB_FILE)


# ============================================================
# 1. ------- SEASON DATE HELPER
# ============================================================

def get_season_dates(season):
    """
    '2024-25' → matches Oct 2024 through June 2025
    """
    start_year = int(season[:4])
    end_year = start_year + 1
    return f"{start_year}-10-01", f"{end_year}-06-30"


# ============================================================
# 2. ------- ZONE MAPPING
# ============================================================

def get_zone(shot):

    st = shot.get("shot_type")
    return st
    dist = shot.get("shot_distance")
    x = shot.get("x")

    TYPE_MAP = {
        "AtRim": "AtRim",
        "ShortMidRange": "ShortMidRange",
        "LongMidRange": "LongMidRange",
        "Corner3": "Corner3",
        "Arc3": "Arc3",
        "AboveBreak3": "Arc3",
    }

    if st in TYPE_MAP:
        return TYPE_MAP[st]

    if dist is None:
        return "LongMidRange"

    if dist <= 4.5:
        return "AtRim"
    if dist <= 14:
        return "ShortMidRange"
    if dist >= 22:
        if x is not None and abs(x) > 22 and dist <= 22.5:
            return "Corner3"
        return "Arc3"

    return "LongMidRange"


# ============================================================
# 3. ------- FETCH SHOTS FROM DATABASE
# ============================================================

def fetch_shots_by_player(player_id, season):
    conn = get_db()
    cur = conn.cursor()

    start, end = get_season_dates(season)

    cur.execute("""
        SELECT * FROM shots
        WHERE player_id = ?
          AND game_date BETWEEN ? AND ?
    """, (player_id, start, end))

    rows = cur.fetchall()
    conn.close()
    return [row_to_dict(r) for r in rows]


def fetch_assists_by_assister(assister_id, team_abbrev, season):
    conn = get_db()
    cur = conn.cursor()

    start, end = get_season_dates(season)

    cur.execute("""
        SELECT * FROM shots
        WHERE assisted = 1
          AND assist_player_id = ?
          AND team = ?
          AND game_date BETWEEN ? AND ?
    """, (assister_id, team_abbrev, start, end))

    rows = cur.fetchall()
    conn.close()
    return [row_to_dict(r) for r in rows]


def row_to_dict(row):
    """
    Convert DB row → dict using fixed column ordering.
    This matches your shots table exactly.
    """
    columns = [
        "id", "gid", "game_date", "period", "time", "poss_num",
        "player", "player_id", "team", "opponent",
        "assisted", "assist_player", "assist_player_id",
        "shot_type", "shot_value", "shot_distance", "shot_quality", "shot_time", "made",
        "x", "y",
        "oreb_rebound_player", "oreb_rebound_player_id",
        "oreb_shot_player", "oreb_shot_player_id", "oreb_shot_type",
        "putback", "seconds_since_oreb",
        "lineup_id", "opponent_lineup_id",
        "blocked", "block_player", "block_player_id",
        "score_margin", "url", "start_time", "end_time", "start_type"
    ]

    return {col: row[i] for i, col in enumerate(columns)}


# ============================================================
# 4. ------- SHOOTER SKILL MODEL
# ============================================================

SHOOTER_CACHE = {}
SKILL_CACHE = {}

ZONE_PRIORS = {
    "AtRim": 0.665,
    "ShortMidRange": 0.442,
    "LongMidRange": 0.413,
    "Arc3": 0.351,
    "Corner3": 0.388,
}


def compute_shooter_skill(shots, m=20):
    makes = defaultdict(int)
    attempts = defaultdict(int)

    for s in shots:
        zone = get_zone(s)
        attempts[zone] += 1
        if s["made"]:
            makes[zone] += 1

    total_att = sum(attempts.values())
    skills = {}

    for zone, prior in ZONE_PRIORS.items():
        att = attempts[zone]
        mk = makes[zone]

        smoothed_fg = (mk + m * prior) / (att + m)
        base_skill = smoothed_fg / prior

        share = att / total_att if total_att else 0

        if share >= 0.05:
            skills[zone] = base_skill
        else:
            floor = 0.5
            t = share / 0.05
            skills[zone] = floor + t * (base_skill - floor)

    for z in skills:
        skills[z] = min(skills[z], 1.10)   # cap at +10% over league avg

    return skills



def get_or_compute_skill(shooter_id, season):
    key = (shooter_id, season)

    if key in SKILL_CACHE:
        return SKILL_CACHE[key]

    if shooter_id not in SHOOTER_CACHE:
        SHOOTER_CACHE[shooter_id] = fetch_shots_by_player(shooter_id, season)

    skills = compute_shooter_skill(SHOOTER_CACHE[shooter_id])
    SKILL_CACHE[key] = skills
    return skills


# ============================================================
# 5. ------- AQR COMPONENTS
# ============================================================

LEAGUE_AVG_SQ = {
    "AtRim": 0.665,
    "ShortMidRange": 0.442,
    "LongMidRange": 0.413,
    "Arc3": 0.351,
    "Corner3": 0.388,
}


def get_creation_boost(shot):
    zone = get_zone(shot)
    sq = shot["shot_quality"]
    baseline = LEAGUE_AVG_SQ[zone]

    creation = 0.5 + 0.5 * (sq / baseline)

    # ---- CAP CREATION BOOST ----
    return min(creation, 1.25)


def load_defense_adjustments():
    url = "https://api.pbpstats.com/get-relative-off-def-efficiency/nba"
    resp = requests.get(url).json()
    table = {}
    for row in resp["results"]:
        table[(row["season"], row["team"])] = row["rel_drtg"]
    return table

REL_DEF = load_defense_adjustments()

def get_defense_factor(season, opponent_team, league_avg=113):
    rel = REL_DEF.get((season, opponent_team), 0)
    opp_rating = league_avg + rel
    diff = league_avg - opp_rating
    return 1.0 + diff / 100.0


def get_clutch_factor(shot):
    p = shot["period"]
    t = shot["shot_time"] or 720  # Default to full quarter if None
    m = shot["score_margin"] or 0

    # Clutch: 4th quarter or OT, under 2 minutes (120 seconds), margin <= 8
    if p>=4 and t<=5 and abs(m)<=3:
        return 1.2
    if p>=4 and t<=10 and abs(m)<=3:
        return 1.15
    if p>=4 and t<=20 and abs(m)<=4:
        return 1.
    if p>=4 and t<=60 and abs(m)<=6:
        return 1.05
    if p >= 4 and t <= 120 and abs(m) <= 8:
        return 1.025

    return 1.0


def get_distance_factor(shot):
    zone = get_zone(shot)

    # Close shots (rim)
    if zone == "AtRim":
        return .99
    if zone in "ShortMidRange":
        return .99
    if zone in "LongMidRange":
        return .97
    # 3-pointers
    if zone in ("Arc3", "Corner3"):
        return 1

    # fallback (shouldn't happen)
    return 1.0

def compute_AQR_for_shot(shot, skills, season):
    zone = get_zone(shot)

    creation = get_creation_boost(shot)
    skill = skills.get(zone, 1.0)
    defense = get_defense_factor(season, shot["opponent"])
    clutch = get_clutch_factor(shot)
    distance = get_distance_factor(shot)

    return creation * skill * defense * clutch * distance


# ============================================================
# 6. ------- AQR MAIN FUNCTIONS
# ============================================================

def list_assists_for_game(assister_id, team_abbrev, game_id, season):
    assists = fetch_assists_by_assister(assister_id, team_abbrev, season)
    return [a for a in assists if a["gid"] == game_id]


def compute_single_assist_AQR(shot, season):
    shooter_id = shot["player_id"]
    skills = get_or_compute_skill(shooter_id, season)
    return compute_AQR_for_shot(shot, skills, season)


def avg_assister_game(assister_id, team_abbrev, game_id, season):
    assists = list_assists_for_game(assister_id, team_abbrev, game_id, season)
    if not assists:
        return None

    vals = [compute_single_assist_AQR(a, season) for a in assists]
    return sum(vals) / len(vals)


def avg_assister_season(assister_id, team_abbrev, season):
    assists = fetch_assists_by_assister(assister_id, team_abbrev, season)
    if not assists:
        return None

    vals = [compute_single_assist_AQR(a, season) for a in assists]
    return sum(vals) / len(vals)


# ============================================================
# 7. ------- ANALYSIS HELPERS
# ============================================================

def analyze_assister(assister_id, team_abbrev, season):
    """Full breakdown of an assister's AQR profile."""
    assists = fetch_assists_by_assister(assister_id, team_abbrev, season)

    if not assists:
        print("No assists found.")
        return

    aqrs = [compute_single_assist_AQR(a, season) for a in assists]

    print(f"\n{'='*50}")
    print(f"AQR Analysis: {assists[0]['assist_player']}")
    print(f"{'='*50}")
    print(f"Total Assists: {len(aqrs)}")
    print(f"Mean AQR: {sum(aqrs)/len(aqrs):.3f}")
    print(f"Min AQR: {min(aqrs):.3f}")
    print(f"Max AQR: {max(aqrs):.3f}")

    # By zone
    by_zone = defaultdict(list)
    for a, aqr in zip(assists, aqrs):
        by_zone[get_zone(a)].append(aqr)

    print(f"\nBy Zone:")
    for zone in ["AtRim", "ShortMidRange", "LongMidRange", "Arc3", "Corner3"]:
        if zone in by_zone:
            vals = by_zone[zone]
            print(f"  {zone:15} | {len(vals):3} assists | avg AQR: {sum(vals)/len(vals):.3f}")

    # Top 5 assists
    sorted_assists = sorted(zip(assists, aqrs), key=lambda x: x[1], reverse=True)
    print(f"\nTop 5 Assists:")
    for a, aqr in sorted_assists[:5]:
        print(f"  AQR {aqr:.3f} | {a['player']:20} | {a['shot_type']:15} | {a['game_date']}")

    # By shooter
    by_shooter = defaultdict(list)
    for a, aqr in zip(assists, aqrs):
        by_shooter[a['player']].append(aqr)

    print(f"\nTop 5 Shooter Connections (min 10 assists):")
    shooter_avgs = [
        (name, len(vals), sum(vals)/len(vals))
        for name, vals in by_shooter.items()
        if len(vals) >= 10
    ]
    shooter_avgs.sort(key=lambda x: x[2], reverse=True)
    for name, count, avg in shooter_avgs[:5]:
        print(f"  {name:20} | {count:3} assists | avg AQR: {avg:.3f}")


def compare_assisters(player_ids, team_abbrev, season):
    """Compare multiple assisters' AQR."""
    print(f"\n{'='*50}")
    print(f"AQR Comparison - {season}")
    print(f"{'='*50}")
    print(f"{'Player':<25} | {'Assists':>7} | {'Avg AQR':>8}")
    print("-" * 50)

    results = []
    for pid in player_ids:
        assists = fetch_assists_by_assister(pid, team_abbrev, season)
        if assists:
            aqrs = [compute_single_assist_AQR(a, season) for a in assists]
            name = assists[0]['assist_player']
            avg_aqr = sum(aqrs) / len(aqrs)
            results.append((name, len(aqrs), avg_aqr))

    results.sort(key=lambda x: x[2], reverse=True)
    for name, count, avg in results:
        print(f"{name:<25} | {count:>7} | {avg:>8.3f}")


# ============================================================
# 8. ------- CLI
# ============================================================

def menu():
    print("\n" + "="*40)
    print("   Assist Quality Rating (AQR)")
    print("="*40)
    print("1. Single assist AQR")
    print("2. Game average AQR")
    print("3. Season average AQR")
    print("4. Full player analysis")
    print("5. Compare multiple players")
    print("6. Exit")
    return input("\nChoose option: ").strip()


def cli_single_assist():
    assister = input("Assister Player ID: ").strip()
    team = input("Team abbreviation (e.g. ATL): ").strip().upper()
    game = input("Game ID: ").strip()
    season = input("Season (default 2024-25): ").strip() or "2024-25"

    assists = list_assists_for_game(int(assister), team, game, season)
    if not assists:
        print("No assists found.")
        return

    print(f"\nFound {len(assists)} assists:")
    for i, s in enumerate(assists, 1):
        print(f"  {i}. {s['player']:20} | P{s['period']} {s['time']} | {s['shot_type']} ({s['shot_distance']}ft)")

    idx = int(input("\nPick assist #: ")) - 1
    aqr = compute_single_assist_AQR(assists[idx], season)

    # Show breakdown
    shot = assists[idx]
    skills = get_or_compute_skill(shot["player_id"], season)
    zone = get_zone(shot)

    print(f"\n--- AQR Breakdown ---")
    print(f"Creation Boost:  {get_creation_boost(shot):.3f}")
    print(f"Shooter Skill:   {skills.get(zone, 1.0):.3f}")
    print(f"Defense Factor:  {get_defense_factor(season, shot['opponent']):.3f}")
    print(f"Clutch Factor:   {get_clutch_factor(shot):.3f}")
    print(f"Distance Factor: {get_distance_factor(shot):.3f}")
    print(f"--------------------")
    print(f"TOTAL AQR:       {aqr:.3f}")


def cli_game_avg():
    assister = input("Assister ID: ").strip()
    team = input("Team abbreviation (e.g. ATL): ").strip().upper()
    game = input("Game ID: ").strip()
    season = input("Season (default 2024-25): ").strip() or "2024-25"

    assists = list_assists_for_game(int(assister), team, game, season)
    if not assists:
        print("No assists found.")
        return

    aqrs = [compute_single_assist_AQR(a, season) for a in assists]

    print(f"\nGame: {game}")
    print(f"Assists: {len(aqrs)}")
    print(f"Average AQR: {sum(aqrs)/len(aqrs):.3f}")
    print(f"\nIndividual assists:")
    for a, aqr in zip(assists, aqrs):
        print(f"  {a['player']:20} | {a['shot_type']:15} | AQR: {aqr:.3f}")


def cli_season_avg():
    assister = input("Assister ID: ").strip()
    team = input("Team abbreviation (e.g. ATL): ").strip().upper()
    season = input("Season (default 2024-25): ").strip() or "2024-25"

    result = avg_assister_season(int(assister), team, season)
    if result:
        print(f"\nSeason Average AQR: {result:.3f}")
    else:
        print("No assists found.")


def cli_full_analysis():
    assister = input("Assister ID: ").strip()
    team = input("Team abbreviation (e.g. ATL): ").strip().upper()
    season = input("Season (default 2024-25): ").strip() or "2024-25"

    analyze_assister(int(assister), team, season)


def cli_compare():
    team = input("Team abbreviation (e.g. ATL): ").strip().upper()
    season = input("Season (default 2024-25): ").strip() or "2024-25"
    ids_input = input("Player IDs (comma separated): ").strip()

    player_ids = [int(x.strip()) for x in ids_input.split(",")]
    compare_assisters(player_ids, team, season)

import sqlite3
import statistics
from collections import defaultdict

# =============================================
# 1. Shrinkage function (Bayesian smoothing)
# =============================================

def shrink_aqr(mean_aqr, n, league_avg=1.0158, m=250):
    """
    Bayesian shrinkage:
        shrunk = (n / (n+m)) * mean + (m / (n+m)) * league_mean
    """
    return (n / (n + m)) * mean_aqr + (m / (n + m)) * league_avg


# =============================================
# 2. Load assists + compute AQR for each
# =============================================

from AQR_CLI_DB import (
    row_to_dict,
    compute_single_assist_AQR,
    get_season_dates,
)

def load_all_assists(season="2024-25"):
    conn = sqlite3.connect("shots.db")
    cur = conn.cursor()

    start, end = get_season_dates(season)

    cur.execute("""
        SELECT * FROM shots
        WHERE assisted = 1
          AND game_date BETWEEN ? AND ?
    """, (start, end))

    rows = cur.fetchall()
    conn.close()

    return [row_to_dict(r) for r in rows]


# =============================================
# 3. Compute passer stats
# =============================================

def compute_adjusted_rankings(season="2024-25"):
    print("Loading all assists...")
    assists = load_all_assists(season)

    print(f"Computing AQR for {len(assists):,} assists...")

    passer_to_aqrs = defaultdict(list)
    passer_names = {}

    for i, a in enumerate(assists):
        if i % 5000 == 0 and i > 0:
            print(f"  Processed {i:,}...")

        try:
            aqr = compute_single_assist_AQR(a, season)
        except:
            continue

        pid = a["assist_player_id"]
        if pid is None:
            continue

        passer_to_aqrs[pid].append(aqr)
        passer_names[pid] = a["assist_player"]

    print("Finished computing AQRs.\n")

    # =============================================
    # League average (for shrinkage)
    # =============================================
    all_aqrs = [aqr for vals in passer_to_aqrs.values() for aqr in vals]
    league_avg = statistics.mean(all_aqrs)
    print(f"League average AQR = {league_avg:.4f}\n")

    # =============================================
    # Build stats table
    # =============================================

    table = []
    MIN_ASSISTS = 50  # can change threshold

    for pid, vals in passer_to_aqrs.items():
        n = len(vals)
        if n < MIN_ASSISTS:
            continue

        mean_aqr = statistics.mean(vals)
        shrunk = shrink_aqr(mean_aqr, n, league_avg=league_avg, m=250)

        table.append({
            "pid": pid,
            "name": passer_names.get(pid, "Unknown"),
            "assists": n,
            "mean": mean_aqr,
            "shrunk": shrunk,
            "elite_pct": sum(1 for x in vals if x >= 1.2) / n * 100,
            "bad_pct": sum(1 for x in vals if x < 0.9) / n * 100
        })

    # sort by adjusted value
    table.sort(key=lambda x: x["shrunk"], reverse=True)

    return table


# =============================================
# 4. Pretty print rankings
# =============================================

def print_rankings(table, limit=50):
    print("="*80)
    print(f"{'ADJUSTED AQR PASSER RANKINGS (SHRUNK AQR)':^80}")
    print("="*80)

    print(f"\n{'Rank':<5} | {'Player':<22} | {'Ast':>5} | {'Mean':>6} | {'AdjAQR':>7} | {'Elite%':>7} | {'Bad%':>6}")
    print("-"*80)

    for i, row in enumerate(table[:limit], 1):
        print(f"{i:<5} | "
              f"{row['name']:<22} | "
              f"{row['assists']:>5} | "
              f"{row['mean']:>6.3f} | "
              f"{row['shrunk']:>7.3f} | "
              f"{row['elite_pct']:>6.1f}% | "
              f"{row['bad_pct']:>5.1f}%")


# =============================================
# 5. Run
# =============================================

if __name__ == "__main__":
    table = compute_adjusted_rankings("2024-25")
    print_rankings(table, limit=100)

