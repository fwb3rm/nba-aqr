import sqlite3
from collections import defaultdict
import requests
import statistics
DB_FILE = "shots.db"

# Global cache for AQR statistics
AQR_STATS_CACHE = {}

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

def compute_AQR_for_shot_raw(shot, skills, season):
    """
    Compute raw AQR (internal use only).
    Returns the raw multiplicative AQR value.
    """
    zone = get_zone(shot)

    creation = get_creation_boost(shot)
    skill = skills.get(zone, 1.0)
    defense = get_defense_factor(season, shot["opponent"])
    clutch = get_clutch_factor(shot)
    distance = get_distance_factor(shot)

    return creation * skill * defense * clutch * distance


# ============================================================
# 5.5. ------- AQR NORMALIZATION (1-100 SCALE)
# ============================================================

def fetch_all_assists(season):
    """Fetch all assists from database for the season."""
    conn = get_db()
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


def compute_single_assist_AQR_raw(shot, season):
    """
    Compute raw AQR for a single assist (internal use only).
    Returns raw multiplicative value.
    """
    shooter_id = shot["player_id"]
    skills = get_or_compute_skill(shooter_id, season)
    return compute_AQR_for_shot_raw(shot, skills, season)


def shrink_aqr(mean_aqr, n, league_avg, m=250):
    """
    Apply Bayesian shrinkage to a mean AQR value.

    Args:
        mean_aqr: Raw mean AQR value
        n: Number of assists
        league_avg: League average raw AQR
        m: Shrinkage parameter (default 250)

    Returns:
        Shrunk AQR value
    """
    return (n / (n + m)) * mean_aqr + (m / (n + m)) * league_avg


def compute_aqr_statistics(season="2024-25", force_refresh=False):
    """
    Compute AQR statistics from all assists in the database.
    Returns dict with mean, std, and percentiles.
    Results are cached for performance.
    """
    global AQR_STATS_CACHE

    if season in AQR_STATS_CACHE and not force_refresh:
        return AQR_STATS_CACHE[season]

    print(f"Computing AQR statistics for {season}...")
    assists = fetch_all_assists(season)
    print(f"Loaded {len(assists):,} assists")

    all_aqrs = []
    for i, shot in enumerate(assists):
        if i % 5000 == 0 and i > 0:
            print(f"  Processed {i:,}/{len(assists):,}...")

        try:
            aqr = compute_single_assist_AQR_raw(shot, season)
            all_aqrs.append(aqr)
        except Exception:
            continue

    print(f"Successfully computed {len(all_aqrs):,} AQR values")

    # Calculate statistics
    stats = {
        "mean": statistics.mean(all_aqrs),
        "stdev": statistics.stdev(all_aqrs),
        "min": min(all_aqrs),
        "max": max(all_aqrs),
        "median": statistics.median(all_aqrs),
        "p5": statistics.quantiles(all_aqrs, n=20)[0],   # 5th percentile
        "p10": statistics.quantiles(all_aqrs, n=10)[0],  # 10th percentile
        "p25": statistics.quantiles(all_aqrs, n=4)[0],   # 25th percentile
        "p75": statistics.quantiles(all_aqrs, n=4)[2],   # 75th percentile
        "p90": statistics.quantiles(all_aqrs, n=10)[8],  # 90th percentile
        "p95": statistics.quantiles(all_aqrs, n=20)[18], # 95th percentile
        "all_values": sorted(all_aqrs)  # Store for percentile lookups
    }

    # Cache the results
    AQR_STATS_CACHE[season] = stats

    print(f"\nAQR Statistics for {season}:")
    print(f"  Mean:   {stats['mean']:.4f}")
    print(f"  Stdev:  {stats['stdev']:.4f}")
    print(f"  Min:    {stats['min']:.4f}")
    print(f"  Max:    {stats['max']:.4f}")
    print(f"  Median: {stats['median']:.4f}")
    print(f"  P5:     {stats['p5']:.4f}")
    print(f"  P25:    {stats['p25']:.4f}")
    print(f"  P75:    {stats['p75']:.4f}")
    print(f"  P95:    {stats['p95']:.4f}\n")

    return stats


def normalize_aqr(raw_aqr, season="2024-25"):
    """
    Convert raw AQR to 1-100 scale based on percentile rank.

    Args:
        raw_aqr: Raw AQR value
        season: Season to use for normalization

    Returns:
        Normalized AQR on 1-100 scale
    """
    stats = compute_aqr_statistics(season)
    all_values = stats["all_values"]

    # Find percentile rank
    rank = sum(1 for x in all_values if x < raw_aqr)
    percentile = (rank / len(all_values)) * 99 + 1  # Scale to 1-100

    return round(percentile, 1)


def compute_single_assist_AQR(shot, season="2024-25"):
    """
    Compute normalized AQR for a single assist.
    This is the main public function - always returns 1-100 normalized value.

    Args:
        shot: Shot dictionary
        season: Season string

    Returns:
        Normalized AQR on 1-100 scale
    """
    raw_aqr = compute_single_assist_AQR_raw(shot, season)
    return normalize_aqr(raw_aqr, season)


def get_aqr_with_breakdown(shot, season="2024-25"):
    """
    Compute AQR with component breakdown for display.

    Returns:
        dict with raw_aqr, normalized_aqr, and component values
    """
    shooter_id = shot["player_id"]
    skills = get_or_compute_skill(shooter_id, season)
    zone = get_zone(shot)

    # Component values
    creation = get_creation_boost(shot)
    skill = skills.get(zone, 1.0)
    defense = get_defense_factor(season, shot["opponent"])
    clutch = get_clutch_factor(shot)
    distance = get_distance_factor(shot)

    raw_aqr = creation * skill * defense * clutch * distance
    normalized_aqr = normalize_aqr(raw_aqr, season)

    return {
        "raw_aqr": raw_aqr,
        "normalized_aqr": normalized_aqr,
        "creation": creation,
        "skill": skill,
        "defense": defense,
        "clutch": clutch,
        "distance": distance
    }


# ============================================================
# 6. ------- AQR MAIN FUNCTIONS
# ============================================================

def list_assists_for_game(assister_id, team_abbrev, game_id, season):
    assists = fetch_assists_by_assister(assister_id, team_abbrev, season)
    return [a for a in assists if a["gid"] == game_id]


def avg_assister_game(assister_id, team_abbrev, game_id, season):
    """
    Calculate average AQR for a player in a single game.
    Applies shrinkage and returns normalized 1-100 value.
    """
    assists = list_assists_for_game(assister_id, team_abbrev, game_id, season)
    if not assists:
        return None

    # Get raw AQR values
    raw_vals = [compute_single_assist_AQR_raw(a, season) for a in assists]
    n = len(raw_vals)
    mean_raw = sum(raw_vals) / n

    # Apply shrinkage
    stats = compute_aqr_statistics(season)
    league_avg = stats["mean"]
    shrunk_raw = shrink_aqr(mean_raw, n, league_avg)

    # Normalize to 1-100
    return normalize_aqr(shrunk_raw, season)


def avg_assister_season(assister_id, team_abbrev, season):
    """
    Calculate average AQR for a player across the season.
    Applies shrinkage and returns normalized 1-100 value.
    """
    assists = fetch_assists_by_assister(assister_id, team_abbrev, season)
    if not assists:
        return None

    # Get raw AQR values
    raw_vals = [compute_single_assist_AQR_raw(a, season) for a in assists]
    n = len(raw_vals)
    mean_raw = sum(raw_vals) / n

    # Apply shrinkage
    stats = compute_aqr_statistics(season)
    league_avg = stats["mean"]
    shrunk_raw = shrink_aqr(mean_raw, n, league_avg)

    # Normalize to 1-100
    return normalize_aqr(shrunk_raw, season)


# ============================================================
# 7. ------- ANALYSIS HELPERS
# ============================================================

def analyze_assister(assister_id, team_abbrev, season):
    """Full breakdown of an assister's AQR profile."""
    assists = fetch_assists_by_assister(assister_id, team_abbrev, season)

    if not assists:
        print("No assists found.")
        return

    # Get raw values for statistics
    raw_aqrs = [compute_single_assist_AQR_raw(a, season) for a in assists]
    normalized_aqrs = [compute_single_assist_AQR(a, season) for a in assists]

    # Calculate average with shrinkage
    n = len(raw_aqrs)
    mean_raw = sum(raw_aqrs) / n
    stats = compute_aqr_statistics(season)
    league_avg = stats["mean"]
    shrunk_raw = shrink_aqr(mean_raw, n, league_avg)
    shrunk_normalized = normalize_aqr(shrunk_raw, season)

    print(f"\n{'='*50}")
    print(f"AQR Analysis: {assists[0]['assist_player']}")
    print(f"{'='*50}")
    print(f"Total Assists: {len(raw_aqrs)}")
    print(f"Average AQR (Shrunk): {shrunk_normalized:.1f} / 100")
    print(f"Min AQR: {min(normalized_aqrs):.1f} / 100")
    print(f"Max AQR: {max(normalized_aqrs):.1f} / 100")

    # By zone
    by_zone = defaultdict(list)
    for a, aqr in zip(assists, normalized_aqrs):
        by_zone[get_zone(a)].append(aqr)

    print(f"\nBy Zone:")
    for zone in ["AtRim", "ShortMidRange", "LongMidRange", "Arc3", "Corner3"]:
        if zone in by_zone:
            vals = by_zone[zone]
            print(f"  {zone:15} | {len(vals):3} assists | avg AQR: {sum(vals)/len(vals):.1f}")

    # Top 5 assists
    sorted_assists = sorted(zip(assists, normalized_aqrs), key=lambda x: x[1], reverse=True)
    print(f"\nTop 5 Assists:")
    for a, aqr in sorted_assists[:5]:
        print(f"  AQR {aqr:.1f} | {a['player']:20} | {a['shot_type']:15} | {a['game_date']}")

    # By shooter
    by_shooter = defaultdict(list)
    for a, aqr in zip(assists, normalized_aqrs):
        by_shooter[a['player']].append(aqr)

    print(f"\nTop 5 Shooter Connections (min 10 assists):")
    shooter_avgs = [
        (name, len(vals), sum(vals)/len(vals))
        for name, vals in by_shooter.items()
        if len(vals) >= 10
    ]
    shooter_avgs.sort(key=lambda x: x[2], reverse=True)
    for name, count, avg in shooter_avgs[:5]:
        print(f"  {name:20} | {count:3} assists | avg AQR: {avg:.1f}")


def compare_assisters(player_ids, team_abbrev, season):
    """Compare multiple assisters' AQR (with shrinkage applied)."""
    print(f"\n{'='*50}")
    print(f"AQR Comparison - {season}")
    print(f"{'='*50}")
    print(f"{'Player':<25} | {'Assists':>7} | {'AQR/100':>8}")
    print("-" * 50)

    results = []
    for pid in player_ids:
        # Use the avg_assister_season which applies shrinkage and normalization
        avg_normalized = avg_assister_season(pid, team_abbrev, season)
        if avg_normalized is not None:
            assists = fetch_assists_by_assister(pid, team_abbrev, season)
            name = assists[0]['assist_player']
            results.append((name, len(assists), avg_normalized))

    results.sort(key=lambda x: x[2], reverse=True)
    for name, count, aqr in results:
        print(f"{name:<25} | {count:>7} | {aqr:>8.1f}")


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
    shot = assists[idx]

    # Get breakdown
    breakdown = get_aqr_with_breakdown(shot, season)

    print(f"\n--- AQR Breakdown ---")
    print(f"Creation Boost:  {breakdown['creation']:.3f}")
    print(f"Shooter Skill:   {breakdown['skill']:.3f}")
    print(f"Defense Factor:  {breakdown['defense']:.3f}")
    print(f"Clutch Factor:   {breakdown['clutch']:.3f}")
    print(f"Distance Factor: {breakdown['distance']:.3f}")
    print(f"--------------------")
    print(f"Raw AQR:         {breakdown['raw_aqr']:.3f}")
    print(f"Normalized AQR:  {breakdown['normalized_aqr']:.1f} / 100")


def cli_game_avg():
    assister = input("Assister ID: ").strip()
    team = input("Team abbreviation (e.g. ATL): ").strip().upper()
    game = input("Game ID: ").strip()
    season = input("Season (default 2024-25): ").strip() or "2024-25"

    assists = list_assists_for_game(int(assister), team, game, season)
    if not assists:
        print("No assists found.")
        return

    # Get shrunk and normalized average
    avg_normalized = avg_assister_game(int(assister), team, game, season)

    # Get individual normalized AQRs
    individual_aqrs = [compute_single_assist_AQR(a, season) for a in assists]

    print(f"\nGame: {game}")
    print(f"Assists: {len(assists)}")
    print(f"Average AQR (Shrunk): {avg_normalized:.1f} / 100")
    print(f"\nIndividual assists:")
    for a, aqr in zip(assists, individual_aqrs):
        print(f"  {a['player']:20} | {a['shot_type']:15} | AQR: {aqr:.1f}")


def cli_season_avg():
    assister = input("Assister ID: ").strip()
    team = input("Team abbreviation (e.g. ATL): ").strip().upper()
    season = input("Season (default 2024-25): ").strip() or "2024-25"

    # Result is already shrunk and normalized
    result = avg_assister_season(int(assister), team, season)
    if result:
        print(f"\nSeason Average AQR (Shrunk): {result:.1f} / 100")
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

# ============================================================
# 9. ------- LEAGUE-WIDE RANKINGS
# ============================================================

def compute_adjusted_rankings(season="2024-25", min_assists=50):
    """
    Compute normalized AQR rankings for all passers in the league.
    Applies shrinkage to player averages, then normalizes to 1-100 scale.
    """
    # Compute statistics once (will use cache if already computed)
    stats = compute_aqr_statistics(season)
    league_avg = stats["mean"]

    print("Loading all assists...")
    assists = fetch_all_assists(season)

    print(f"Computing raw AQR for {len(assists):,} assists...")

    passer_to_raw_aqrs = defaultdict(list)
    passer_to_norm_aqrs = defaultdict(list)
    passer_names = {}
    passer_teams = {}

    for i, a in enumerate(assists):
        if i % 5000 == 0 and i > 0:
            print(f"  Processed {i:,}...")

        try:
            raw_aqr = compute_single_assist_AQR_raw(a, season)
            norm_aqr = normalize_aqr(raw_aqr, season)
        except Exception:
            continue

        pid = a["assist_player_id"]
        if pid is None:
            continue

        passer_to_raw_aqrs[pid].append(raw_aqr)
        passer_to_norm_aqrs[pid].append(norm_aqr)
        passer_names[pid] = a["assist_player"]
        passer_teams[pid] = a["team"]

    print("Finished computing AQRs.\n")

    # Build stats table with shrunk and normalized scores
    results_table = []

    for pid, raw_vals in passer_to_raw_aqrs.items():
        n = len(raw_vals)
        if n < min_assists:
            continue

        # Calculate mean of raw values
        mean_raw = statistics.mean(raw_vals)

        # Apply shrinkage
        shrunk_raw = shrink_aqr(mean_raw, n, league_avg)

        # Normalize shrunk value
        normalized = normalize_aqr(shrunk_raw, season)

        # Get normalized individual values for elite/bad percentages
        norm_vals = passer_to_norm_aqrs[pid]

        results_table.append({
            "pid": pid,
            "name": passer_names.get(pid, "Unknown"),
            "team": passer_teams.get(pid, ""),
            "assists": n,
            "raw_mean": mean_raw,
            "normalized": normalized,
            "elite_pct": sum(1 for x in norm_vals if x >= 80) / n * 100,  # Top 20%
            "bad_pct": sum(1 for x in norm_vals if x < 40) / n * 100      # Bottom 40%
        })

    # Sort by normalized AQR
    results_table.sort(key=lambda x: x["normalized"], reverse=True)

    return results_table


def print_rankings(results_table, limit=50):
    """Print normalized AQR rankings with shrinkage applied."""
    print("="*95)
    print(f"{'AQR PASSER RANKINGS (SHRUNK & NORMALIZED 1-100)':^95}")
    print("="*95)

    print(f"\n{'Rank':<5} | {'Player':<22} | {'Team':>4} | {'Ast':>5} | {'AQR/100':>7} | {'Elite%':>7} | {'Bad%':>6}")
    print("-"*95)

    for i, row in enumerate(results_table[:limit], 1):
        print(f"{i:<5} | "
              f"{row['name']:<22} | "
              f"{row['team']:>4} | "
              f"{row['assists']:>5} | "
              f"{row['normalized']:>7.1f} | "
              f"{row['elite_pct']:>6.1f}% | "
              f"{row['bad_pct']:>5.1f}%")


# =============================================
# 5. Run
# =============================================

if __name__ == "__main__":
    table = compute_adjusted_rankings("2024-25")
    print_rankings(table, limit=100)

