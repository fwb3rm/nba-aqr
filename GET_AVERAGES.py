import json
import os
import time
import requests
from collections import defaultdict

# ============================================================
# CONFIG
# ============================================================

SEASON = "2024-25"
PROGRESS_FILE = f"league_progress_{SEASON}.json"
OUTPUT_FILE = f"league_shots_CACHE_{SEASON}.json"

TIME_WINDOWS = [
    (0, 90), (90, 180), (180, 270), (270, 360),
    (360, 450), (450, 540), (540, 630), (630, 720)
]

PERIODS = [1, 2, 3, 4]

# ============================================================
# SAFE JSON WRAPPER
# ============================================================

def safe_get_json(url, params=None, retries=4):
    """Return {} on failure, instead of crashing."""
    for attempt in range(1, retries+1):
        try:
            resp = requests.get(url, params=params, timeout=10)
            try:
                data = resp.json()
                return data
            except:
                print(f"   ⚠ Non-JSON response (attempt {attempt}), retrying...")
        except Exception as e:
            print(f"   ⚠ Request error (attempt {attempt}): {e}")
        time.sleep(0.35)
    print("   ❌ Failed permanently. Returning empty results.")
    return {"results": []}

# ============================================================
# TEAM FETCH
# ============================================================

def get_team_ids():
    url = "https://api.pbpstats.com/get-teams/nba"
    data = safe_get_json(url)
    teams = [t["id"] for t in data.get("teams", [])]
    print(f"✔ Found {len(teams)} NBA teams.")
    return teams

# ============================================================
# PROGRESS SYSTEM
# ============================================================

def load_progress():
    if os.path.exists(PROGRESS_FILE):
        print("📦 Loading existing resume state...")
        with open(PROGRESS_FILE, "r") as f:
            return json.load(f)
    return {"done": {}, "shots": []}

def save_progress(progress):
    with open(PROGRESS_FILE, "w") as f:
        json.dump(progress, f)
    print("💾 Progress saved.")

# ============================================================
# SHOT SCRAPER W/ RESUME
# ============================================================

def scrape_team(team_id, season, progress):
    """Scrape all partitions for one team with resume."""
    print(f"\n🏀 Scraping team {team_id}...")

    # Initialize if first time
    if team_id not in progress["done"]:
        progress["done"][team_id] = {
            str(period): {f"{gte}-{lte}": False for gte, lte in TIME_WINDOWS}
            for period in PERIODS
        }

    url = "https://api.pbpstats.com/get-shots/nba"

    for period in PERIODS:
        print(f"  → Period {period}")

        for gte, lte in TIME_WINDOWS:
            window_key = f"{gte}-{lte}"

            # Skip if already done
            if progress["done"][team_id][str(period)][window_key]:
                print(f"     ✔ Already completed window {window_key}, skipping.")
                continue

            print(f"     ⏳ Scraping window {window_key}...")

            params = {
                "Season": season,
                "SeasonType": "Regular Season",
                "EntityType": "Team",
                "EntityId": team_id,
                "PeriodEquals": period,
                "ShotTimeGte": gte,
                "ShotTimeLte": lte,
            }

            resp = safe_get_json(url, params)
            shots = resp.get("results", [])

            print(f"       → Retrieved {len(shots)} shots")

            # Save shots into global progress
            progress["shots"].extend(shots)

            # Mark window done
            progress["done"][team_id][str(period)][window_key] = True

            # Save after *every* window
            save_progress(progress)

            # small sleep prevents rate limits
            time.sleep(0.08)

    print(f"🏁 Finished team {team_id}.")

# ============================================================
# MAIN SCRAPER
# ============================================================

def get_all_league_shots(season=SEASON):
    progress = load_progress()
    team_ids = get_team_ids()

    for tid in team_ids:
        scrape_team(tid, season, progress)

    # Once fully finished, save final output
    with open(OUTPUT_FILE, "w") as f:
        json.dump(progress["shots"], f)
    print(f"\n🎉 ALL DONE! Saved full league dataset: {len(progress['shots'])} shots")
    return progress["shots"]

# ============================================================
# RUN
# ============================================================

if __name__ == "__main__":
    print("🚀 Starting League Scraper (Resume Mode Enabled)...")
    shots = get_all_league_shots()
    print(f"\n✔ Final shot count: {len(shots)}")
