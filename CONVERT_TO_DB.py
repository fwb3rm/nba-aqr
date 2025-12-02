import json
import sqlite3
import os

JSON_FILE = "league_shots_CACHE_2024-25.json"
DB_FILE = "shots.db"

# ============================================================
# 1. Create SQLite Table
# ============================================================

def create_table(conn):
    conn.execute("""
        CREATE TABLE IF NOT EXISTS shots (
            id INTEGER PRIMARY KEY AUTOINCREMENT,

            -- identifying fields
            gid TEXT,
            game_date TEXT,
            period INTEGER,
            time TEXT,
            poss_num INTEGER,

            -- players
            player TEXT,
            player_id INTEGER,
            team TEXT,
            opponent TEXT,

            -- assist info
            assisted BOOLEAN,
            assist_player TEXT,
            assist_player_id INTEGER,

            -- shot details
            shot_type TEXT,
            shot_value INTEGER,
            shot_distance REAL,
            shot_quality REAL,
            shot_time REAL,
            made BOOLEAN,

            -- location
            x REAL,
            y REAL,

            -- rebounding context
            oreb_rebound_player TEXT,
            oreb_rebound_player_id INTEGER,
            oreb_shot_player TEXT,
            oreb_shot_player_id INTEGER,
            oreb_shot_type TEXT,
            putback BOOLEAN,
            seconds_since_oreb REAL,

            -- lineup context
            lineup_id TEXT,
            opponent_lineup_id TEXT,

            -- block info
            blocked BOOLEAN,
            block_player TEXT,
            block_player_id INTEGER,

            -- misc
            score_margin INTEGER,
            url TEXT,
            start_time REAL,
            end_time REAL,
            start_type TEXT
        );
    """)

    conn.execute("PRAGMA journal_mode = WAL;")
    conn.execute("PRAGMA synchronous = OFF;")


# ============================================================
# 2. Normalize JSON shot → SQLite row dict
# ============================================================

def normalize_shot(s):
    return {
        "gid": s.get("gid"),
        "game_date": s.get("game_date"),
        "period": s.get("period"),
        "time": s.get("time"),
        "poss_num": s.get("poss_num"),

        "player": s.get("player"),
        "player_id": s.get("player_id"),
        "team": s.get("team"),
        "opponent": s.get("opponent"),

        "assisted": s.get("assisted"),
        "assist_player": s.get("assist_player"),
        "assist_player_id": s.get("assist_player_id"),

        "shot_type": s.get("shot_type"),
        "shot_value": s.get("shot_value"),
        "shot_distance": s.get("shot_distance"),
        "shot_quality": s.get("shot_quality"),
        "shot_time": s.get("shot_time"),
        "made": s.get("made"),

        "x": s.get("x"),
        "y": s.get("y"),

        "oreb_rebound_player": s.get("oreb_rebound_player"),
        "oreb_rebound_player_id": s.get("oreb_rebound_player_id"),
        "oreb_shot_player": s.get("oreb_shot_player"),
        "oreb_shot_player_id": s.get("oreb_shot_player_id"),
        "oreb_shot_type": s.get("oreb_shot_type"),
        "putback": s.get("putback"),
        "seconds_since_oreb": s.get("seconds_since_oreb"),

        "lineup_id": s.get("lineup_id"),
        "opponent_lineup_id": s.get("opponent_lineup_id"),

        "blocked": s.get("blocked"),
        "block_player": s.get("block_player"),
        "block_player_id": s.get("block_player_id"),

        "score_margin": s.get("score_margin"),
        "url": s.get("url"),

        "start_time": s.get("start_time"),
        "end_time": s.get("end_time"),
        "start_type": s.get("start_type"),
    }


# ============================================================
# 3. Insert into SQLite in batches (FAST)
# ============================================================

INSERT_SQL = """
INSERT INTO shots (
    gid, game_date, period, time, poss_num,
    player, player_id, team, opponent,
    assisted, assist_player, assist_player_id,
    shot_type, shot_value, shot_distance, shot_quality, shot_time, made,
    x, y,
    oreb_rebound_player, oreb_rebound_player_id,
    oreb_shot_player, oreb_shot_player_id,
    oreb_shot_type, putback, seconds_since_oreb,
    lineup_id, opponent_lineup_id,
    blocked, block_player, block_player_id,
    score_margin, url, start_time, end_time, start_type
)
VALUES (
    ?, ?, ?, ?, ?,
    ?, ?, ?, ?, ?,
    ?, ?, ?, ?, ?,
    ?, ?, ?, ?, ?,
    ?, ?, ?, ?, ?,
    ?, ?, ?, ?, ?,
    ?, ?, ?, ?, ?,
    ?, ?
)
"""

def insert_shots(conn, shots):
    cur = conn.cursor()
    batch = []

    for s in shots:
        row = normalize_shot(s)
        batch.append(tuple(row.values()))

        if len(batch) >= 5000:
            cur.executemany(INSERT_SQL, batch)
            conn.commit()
            batch = []

    if batch:
        cur.executemany(INSERT_SQL, batch)
        conn.commit()


# ============================================================
# 4. Load JSON file
# ============================================================

def load_json():
    with open(JSON_FILE, "r") as f:
        data = json.load(f)
        if isinstance(data, dict):
            return data.get("shots", [])
        return data


# ============================================================
# 5. Main
# ============================================================

def main():
    if not os.path.exists(JSON_FILE):
        print("❌ JSON file not found:", JSON_FILE)
        return

    print("📥 Loading JSON...")
    shots = load_json()
    print(f"Found {len(shots):,} shots")

    print("🗄️ Creating database...")
    conn = sqlite3.connect(DB_FILE)
    create_table(conn)

    print("⬆️ Inserting into DB...")
    insert_shots(conn, shots)

    conn.close()
    print("✅ Done! Saved → shots.db")


if __name__ == "__main__":
    main()
