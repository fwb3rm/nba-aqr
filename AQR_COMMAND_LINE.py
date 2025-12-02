import requests
from collections import defaultdict
import json
import time

# ============================================================
# 0. ---- GLOBAL SETTINGS + CACHES
# ============================================================

SHOOTER_CACHE = {}       # shooter_id → all shots
SKILL_CACHE = {}         # shooter_id → skill dict


# ============================================================
# 1. ------- ZONE MAPPING
# ============================================================

def get_zone(shot):
    st = shot.get("shot_type")
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

    # fallback rules
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
# 2. ------- DATA FETCHING (LIVE API)
# ============================================================

def fetch_shots(entity_type, entity_id, season):
    """Fetch shots for Player/Team."""
    url = "https://api.pbpstats.com/get-shots/nba"
    params = {
        "Season": season,
        "SeasonType": "Regular Season",
        "EntityType": entity_type,
        "EntityId": entity_id
    }
    resp = requests.get(url, params=params).json()
    return resp["results"]


def fetch_assister_shots(assister_id, team_id, season):
    """Fetch all real assisted shots for a specific assister."""
    url = "https://api.pbpstats.com/get-shots/nba"
    params = {
        "Season": season,
        "SeasonType": "Regular Season",
        "EntityType": "Team",
        "EntityId": team_id,
        "AssistPlayerId": assister_id
    }
    resp = requests.get(url, params=params).json()
    return resp["results"]


# ============================================================
# 3. ------- SHOOTER SKILL MODEL
# ============================================================

ZONE_PRIORS = {
    "AtRim": 0.60,
    "ShortMidRange": 0.41,
    "LongMidRange": 0.38,
    "Arc3": 0.354,
    "Corner3": 0.378,
}

def compute_shooter_skill(shots, m=40):
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
            floor = 0.15
            t = share / 0.05
            skills[zone] = floor + t * (base_skill - floor)

    return skills


def get_or_compute_skill(shooter_id, season):
    """Caching wrapper."""
    if shooter_id in SKILL_CACHE:
        return SKILL_CACHE[shooter_id]

    if shooter_id not in SHOOTER_CACHE:
        SHOOTER_CACHE[shooter_id] = fetch_shots("Player", shooter_id, season)

    skills = compute_shooter_skill(SHOOTER_CACHE[shooter_id])
    SKILL_CACHE[shooter_id] = skills
    return skills


# ============================================================
# 4. ------- AQR COMPONENTS
# ============================================================

LEAGUE_AVG_SQ = {
    "AtRim": 0.6,
    "ShortMidRange": 0.41,
    "LongMidRange": 0.38,
    "Arc3": 0.354,
    "Corner3": 0.378,
}

def get_creation_boost(shot):
    zone = get_zone(shot)
    sq = shot["shot_quality"]
    baseline = LEAGUE_AVG_SQ[zone]
    return 0.5 + 0.5 * (sq / baseline)


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
    t = shot["shot_time"]
    m = shot["score_margin"]

    if p >= 4 and t <= 120:
        return 1.25
    if p >= 4 and t <= 300 and abs(m) <= 5:
        return 1.15
    return 1.0


def get_distance_factor(shot):
    dist = shot["shot_distance"]
    bonus = 1 + 0.10 * (1 / (dist + 3))
    return min(bonus, 1.10)


def compute_AQR_for_shot(shot, skills, season):
    zone = get_zone(shot)
    creation = get_creation_boost(shot)
    skill = skills.get(zone, 1.0)
    defense = get_defense_factor(season, shot["opponent"])
    clutch = get_clutch_factor(shot)
    distance = get_distance_factor(shot)
    return creation * skill * defense * clutch * distance


# ============================================================
# 5. ------- MAIN AQR FUNCTIONS
# ============================================================

def list_assists_for_game(assister_id, team_id, game_id, season):
    all_assists = fetch_assister_shots(assister_id, team_id, season)
    return [a for a in all_assists if a["gid"] == game_id]


def compute_single_assist_AQR(shot, season):
    shooter_id = shot["player_id"]
    skills = get_or_compute_skill(shooter_id, season)
    return compute_AQR_for_shot(shot, skills, season)


def avg_assister_game(assister_id, team_id, game_id, season):
    assists = list_assists_for_game(assister_id, team_id, game_id, season)
    if not assists:
        return None

    values = []
    for s in assists:
        values.append(compute_single_assist_AQR(s, season))

    return sum(values) / len(values)


def avg_assister_season(assister_id, team_id, season):
    assists = fetch_assister_shots(assister_id, team_id, season)
    assists = [a for a in assists if a["assisted"]]

    if not assists:
        return None

    values = []
    for s in assists:
        values.append(compute_single_assist_AQR(s, season))

    return sum(values) / len(values)


# ============================================================
# 6. ------- CLI MENU
# ============================================================

def menu():
    print("\n============================")
    print("   Assist AQR Calculator")
    print("============================")
    print("1. AQR for a SINGLE assist")
    print("2. Average AQR for assister in a GAME")
    print("3. Average AQR for assister in a SEASON")
    print("4. Exit")
    return input("Choose an option: ").strip()


# ============================================================
# 7. ------- CLI HANDLERS
# ============================================================

def cli_single_assist():
    assister = input("Assister Player ID: ").strip()
    team = input("Team ID (assister's team): ").strip()
    game = input("Game ID: ").strip()
    season = input("Season (default 2024-25): ").strip() or "2024-25"

    assists = list_assists_for_game(assister, team, game, season)
    if not assists:
        print("No assists found.")
        return

    print("\nAvailable assists by this player in this game:")
    for i, s in enumerate(assists, 1):
        print(f"{i}. Shooter {s['player']} | P{ s['period'] } @ {s['time']} | {s['shot_type']} {s['shot_distance']} ft")

    idx = int(input("Pick assist #: ")) - 1
    shot = assists[idx]

    aqr = compute_single_assist_AQR(shot, season)
    print(f"\nAQR for selected assist: {aqr:.3f}")


def cli_game_avg():
    assister = input("Assister Player ID: ").strip()
    team = input("Team ID: ").strip()
    game = input("Game ID: ").strip()
    season = input("Season (default 2024-25): ").strip() or "2024-25"

    result = avg_assister_game(assister, team, game, season)
    if result is None:
        print("No assists found.")
    else:
        print(f"\nAverage AQR for assister in this game: {result:.3f}")


def cli_season_avg():
    assister = input("Assister Player ID: ").strip()
    team = input("Team ID: ").strip()
    season = input("Season (default 2024-25): ").strip() or "2024-25"

    result = avg_assister_season(assister, team, season)
    if result is None:
        print("No assists found.")
    else:
        print(f"\nSeason Average AQR for assister: {result:.3f}")


# ============================================================
# 8. ------- MAIN LOOP
# ============================================================

if __name__ == "__main__":
    while True:
        choice = menu()

        if choice == "1":
            cli_single_assist()
        elif choice == "2":
            cli_game_avg()
        elif choice == "3":
            cli_season_avg()
        elif choice == "4":
            print("Goodbye!")
            break
        else:
            print("Invalid choice.")
