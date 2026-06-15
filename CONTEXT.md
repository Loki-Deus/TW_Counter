# TW Counter Bot — Project Context

## What this is

A Discord bot for Star Wars: Galaxy of Heroes (SWGOH) guild management. It collects and tracks Territory War (TW) counters — which attacking team leader beats which defending team leader — sourced from the guild's own match history rather than external resources.

This is a separate project from the existing **TB-Reminder bot** (which handles Territory Battle phase announcements), but follows the same stack and deployment pattern.

---

## Tech Stack

- **Language:** Python 3.12
- **Library:** discord.py 2.x
- **Database:** SQLite3 (stdlib) — single `counters.db` file in `/app/data`
- **Character data:** Fetched from SWGOH.gg API on startup, refreshed weekly, cached to `characters.json` in `/app/data`
- **Deployment:** Docker container, Portainer stack, same pattern as TB bot
- **Dev environment:** WSL2 (Ubuntu 24.04) + VS Code with Remote WSL extension

---

## Database Schema

```sql
CREATE TABLE counters (
    id                INTEGER PRIMARY KEY AUTOINCREMENT,
    defending_leader  TEXT NOT NULL,
    attacking_leader  TEXT NOT NULL,
    submitted_by_id   TEXT NOT NULL,
    submitted_by_name TEXT NOT NULL,
    submitted_at      INTEGER NOT NULL   -- unix timestamp
);

CREATE UNIQUE INDEX idx_counter_matchup
    ON counters(defending_leader, attacking_leader);

CREATE TABLE results (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    counter_id     INTEGER NOT NULL REFERENCES counters(id),
    result         INTEGER NOT NULL,    -- 1=win, 0=loss
    attacker_relic INTEGER NOT NULL,    -- relic level of the attacking team leader
    defender_relic INTEGER NOT NULL,    -- relic level of the defending team leader
    relic_delta    INTEGER NOT NULL,    -- attacker_relic - defender_relic, stored at insert
    reported_by_id TEXT NOT NULL,
    reported_at    INTEGER NOT NULL     -- unix timestamp
);
```

The unique index on `(defending_leader, attacking_leader)` prevents duplicate counter entries. Wins/losses are not denormalized onto the counters table — they are always derived from the results table, grouped by relic bucket. All three relic values are stored explicitly: the raw inputs are retained for future re-bucketing or absolute-relic queries, and the delta is stored redundantly for query performance.

Omicron, datacron, and required attacker fields were deliberately excluded from v1.

---

## Environment Variables

```
DISCORD_TOKEN
GUILD_ID             # for slash command registration
MEMBER_ROLE_ID       # who can submit counters and reports
MANAGER_IDS          # comma-separated user IDs with elevated permissions
BOT_TIMEZONE         # default: Europe/Vienna
DATA_DIR             # default: current directory
```

---

## Commands

| Command | Permission | Description |
|---|---|---|
| `/tw_add` | Member role / Manager / Admin | Add a new counter (defending + attacking leader) |
| `/tw_lookup` | Everyone | Look up all counters for a defending leader |
| `/tw_report` | Member role / Manager / Admin | Report a win or loss for an existing counter |
| `/tw_delete` | Manager / Admin only | Delete a counter (with inline confirmation button) |
| `/tw_help` | Everyone | List all commands |

### `/tw_add`
- Parameters: `defending_leader` (autocomplete), `attacking_leader` (autocomplete)
- On duplicate matchup: ephemeral error suggesting `/tw_report` instead
- On success: ephemeral confirmation + public message in invoking channel

### `/tw_lookup`
- Parameter: `defending_leader` (autocomplete)
- Returns embed with all counters sorted by win rate descending
- Each entry shows: `[attacking_leader] — W/L: 12/3 (80%) — Added by Username`
- Counters with 0 reports show `No reports yet` and sort to the bottom
- Both percentage and absolute numbers are always shown (confidence is visible)
- No minimum report threshold — volume is expected to be sufficient in a single-guild context

### `/tw_report`
- Parameters: `defending_leader` (autocomplete), `attacking_leader` (autocomplete, filtered to existing entries for that defender), `result` (Win / Loss)
- Increments `wins` or `losses` on the matching row
- Ephemeral confirmation with updated stats

### `/tw_delete`
- Manager/admin only
- Inline confirmation button before deletion

---

## Relic Bucketing

Results are categorized at report time into three buckets based on `relic_delta` (attacker_relic − defender_relic):

| Bucket | Delta range | Meaning |
|---|---|---|
| `under` (stark unterlegen) | ≤ −3 | Attacker significantly weaker — high reliability signal |
| `even` (ausgeglichen) | −2 to +2 | Comparable relic levels — the representative case |
| `over` (stark überlegen) | ≥ +3 | Attacker significantly stronger — win may reflect relic advantage, not counter quality |

Bucket boundaries use ±99 as open-ended sentinels in code — the conditionals are open-ended, so any future relic cap increase falls into the correct bucket automatically. Individual relic input values are validated to 0–20 as a typo guard.

`/tw_lookup` sorts primarily by `even` bucket win rate. Counters with zero `even` results sink to the bottom, ordered among themselves by whatever bucket data they do have. A blended/average-rate sort across all buckets is earmarked as a future improvement once real usage data exists.

---

## Character List

- Source: SWGOH.gg API (exact endpoint to be verified at implementation time)
- Fetched on startup via `tasks.loop(hours=168)` (weekly refresh)
- Stored in memory as sorted list, persisted to `characters.json` as startup fallback
- Discord autocomplete filters the in-memory list against current input, returns up to 25 matches

---

## Deployment (Docker / Portainer)

Same pattern as TB bot:

```yaml
services:
  tw-counter:
    build:
      context: https://github.com/<repo>/TW_Counter.git
      dockerfile: Dockerfile
    environment:
      - DISCORD_TOKEN=...
      - MEMBER_ROLE_ID=...
      - MANAGER_IDS=...
      - GUILD_ID=...
      - DATA_DIR=/app/data
    volumes:
      - tw-counter-data:/app/data

volumes:
  tw-counter-data:
    external: true
    name: tw_counter_tw-counter-data
```

---

## Dev Setup

- WSL2 Ubuntu 24.04, Python 3.12.3
- VS Code with: Remote WSL, Python (ms-python), Ruff (charliermarsh.ruff), Claude extensions
- `.venv` at `~/tw-bot/.venv`
- `.vscode/settings.json` with format-on-save via Ruff

---

## Decisions Log

| Decision | Rationale |
|---|---|
| SQLite over JSON | Relational lookups and per-row updates don't fit flat JSON well |
| No omicron field in v1 | Self-explanatory in context; avoids complexity |
| No datacron field | Change too frequently to be worth tracking |
| No required attackers field | Out of scope for v1 |
| No minimum report threshold | Single-guild volume expected to be sufficient |
| Show % + absolute numbers | Transparent confidence without artificial thresholds |
| Relic input validated 0–20 | Typo guard; well above any plausible current cap |
| Bucket sentinels ±99 | Open-ended conditionals — future relic cap increases need no code change |
| Store attacker_relic, defender_relic, relic_delta | Raw values retained for future re-bucketing or absolute-relic queries; delta stored for query performance |
| No denormalized wins/losses on counters table | Always derived from results table; avoids sync issues |
| Leader-only matching | Full team tracking is out of scope for v1 |
| Sort by `even` bucket win rate | Most representative case; counters with no even data sink to bottom |
