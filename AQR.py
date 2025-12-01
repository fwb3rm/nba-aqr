import requests
from collections import defaultdict

# ============================================================
# 1. ------- ZONE MAPPING (Most Important Part)
# ============================================================

def get_zone(shot):
    """Map shot_type + distance into consistent shot zones."""
    st = shot["shot_type"]
    dist = shot["shot_distance"]

    # Handle 3s first (only reliable 3pt indicator)
    if st == "Corner3":
        return "Corner3"
    if st in ("AboveBreak3", "Arc3"):
        return "Arc3"

    # Handle 2s by distance
    if dist <= 5:
        return "AtRim"
    elif dist <= 14:
        return "ShortMidRange"
    else:
        return "LongMidRange"


# ============================================================
# 2. ------- DATA FETCHING FROM PBPSTATS
# ============================================================

def get_player_shots(player_id, season="2024-25"):
    """Fetch all shots for a player."""
    url = "https://api.pbpstats.com/get-shots/nba"
    params = {
        "Season": season,
        "SeasonType": "Regular Season",
        "EntityType": "Player",
        "EntityId": player_id,
        "StartType": "All"
    }
    resp = requests.get(url, params=params).json()
    return resp["results"]


def get_assisted(shots):
    return [s for s in shots if s.get("assisted")]


# ============================================================
# 3. ------- LEAGUE PRIORS (YOU CAN EDIT THESE)
# ============================================================

ZONE_PRIORS = {
    "AtRim": 0.60,
    "ShortMidRange": 0.41,
    "LongMidRange": 0.38,
    "Arc3": 0.354,
    "Corner3": 0.378,
}


# ============================================================
# 4. ------- SHOOTER SKILL MODEL (BEST SIMPLIFIED VERSION)
# ============================================================

def compute_shooter_skill(shots, m=50):
    """
    Bayesian smoothing + zone frequency floor.
    Returns dict: zone → skill multiplier.
    """

    # Count makes/attempts for each zone
    makes = defaultdict(int)
    attempts = defaultdict(int)

    for s in shots:
        zone = get_zone(s)
        attempts[zone] += 1
        if s["made"]:
            makes[zone] += 1

    total_attempts = sum(attempts.values())
    skills = {}

    for zone in ZONE_PRIORS:
        prior = ZONE_PRIORS[zone]

        zone_makes = makes[zone]
        zone_att = attempts[zone]

        # Bayesian smoothed FG
        smoothed_fg = (zone_makes + m * prior) / (zone_att + m)

        # Skill vs league prior
        base_skill = smoothed_fg / prior

        # Frequency scaling
        zone_share = zone_att / total_attempts if total_attempts > 0 else 0

        if zone_share >= 0.05:
            skills[zone] = base_skill
        else:
            # floor at 20% of value
            floor = 0.20
            t = zone_share / 0.05  # 0–1
            skills[zone] = floor + t * (base_skill - floor)

    return skills


# ============================================================
# 5. ------- CREATION BOOST (BEST SIMPLE VERSION)
# ============================================================

LEAGUE_AVG_SQ = {
    "AtRim": 0.6,
    "ShortMidRange": 0.41,
    "LongMidRange": 0.38,
    "Arc3": 0.354,
    "Corner3": 0.378,
}

def get_creation_boost(shot):
    """
    Compare shot_quality to league avg shot quality for that zone.
    More stable than dividing by shooter exp_fg.
    """
    zone = get_zone(shot)
    sq = shot["shot_quality"]
    baseline = LEAGUE_AVG_SQ[zone]

    ratio = sq / baseline

    # Compress extremes to reduce noise effect
    return 0.5 + 0.5 * ratio  # keeps output ~0.8–1.25


# ============================================================
# 6. ------- DEFENSE FACTOR
# ============================================================

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


# ============================================================
# 7. ------- CLUTCH FACTOR
# ============================================================

def get_clutch_factor(shot):
    period = shot["period"]
    margin = shot["score_margin"]
    time_remaining = shot["shot_time"]

    if period >= 4 and time_remaining <= 120:
        return 1.25
    if period >= 4 and time_remaining <= 300 and abs(margin) <= 5:
        return 1.15
    return 1.0

def get_distance_factor(shot):
    dist = shot["shot_distance"]

    # Smooth positive-only boost, never penalizes long shots
    bonus = 1 + 0.10 * (1 / (dist + 3))

    # Ensures rim shots get meaningful boost but never blow up the metric
    return min(bonus, 1.10)

# ============================================================
# 8. ------- COMPUTE AQR FOR A SINGLE SHOT
# ============================================================

def compute_AQR_for_shot(shot, skills, season="2024-25"):
    zone = get_zone(shot)

    creation = get_creation_boost(shot)
    skill = skills.get(zone, 1.0)
    defense = get_defense_factor(season, shot["opponent"])
    clutch = get_clutch_factor(shot)
    distance = get_distance_factor(shot)

    return creation * skill * defense * clutch * distance


# ============================================================
# 9. ------- FULL PLAYER AQR PIPELINE
# ============================================================

def compute_player_AQR(player_id, season="2024-25"):
    shots = get_player_shots(player_id, season)
    assisted = get_assisted(shots)

    print("Total assisted shots:", len(assisted))

    # Shooter skill model (based on ALL shots, not only assisted)
    skills = compute_shooter_skill(shots)

    # Compute AQR per shot
    AQRs = [compute_AQR_for_shot(s, skills, season) for s in assisted]

    return AQRs, skills


# ============================================================
# 10. ------- EXAMPLE RUN (Norman Powell)
# ============================================================

if __name__ == "__main__":
    AQRs, skills = compute_player_AQR("1626181")  # Example player
    print("Sample Shot:", get_player_shots(1626181, "2024-25")[0])
    print("Shooter Skill:", skills)
    print("Sample AQRs:", AQRs[:10])