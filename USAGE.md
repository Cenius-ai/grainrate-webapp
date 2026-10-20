# Using GrainRate

Every example below uses `python3 grainrate.py`; after `bash install.sh` you can
write `grainrate` instead. Commands print results on **stdout**, errors on
**stderr**, and never prompt unless a passphrase is being typed at a terminal.

## 1. Register bins

```bash
python3 grainrate.py bin add --name "North 1" --grain corn --moisture 14.5
# Added bin North 1  corn  14.5% moisture  (safe limit 15.0%)

python3 grainrate.py bin add --name "South 2" --grain soybean --moisture 14.2
python3 grainrate.py bin add --name "West 3" --grain wheat --moisture 13.0
```

Rules enforced on `add` **and** `update` (identical validation code):

- `--moisture` must be a number between 0 and 100, stored to 0.1 %:
  `error: moisture must be a number between 0 and 100` (exit 1, nothing saved).
- Names are unique, case-insensitively trimmed: `error: bin name already exists: North 1`.
- `--grain` must be one of the built-in grains, named in the error:
  `error: unknown grain 'rye'; supported grains: barley, canola, corn, oats, rice, sorghum, soybean, sunflower, wheat`.

```bash
python3 grainrate.py bin list
```

```
Bins · 3
NAME     GRAIN    MOISTURE  UPDATED
───────  ───────  ────────  ──────────
North 1  corn        14.5%  2026-10-18
South 2  soybean     14.2%  2026-10-18
West 3   wheat       13.0%  2026-10-18
3 bins · /home/you/.grainrate/store.json
```

With no bins yet, `bin list` prints the next command to run:

```
No bins yet. Add one with: grainrate.py bin add --name "North 1" --grain corn --moisture 14.5
```

## 2. Update a bin as conditions change

```bash
python3 grainrate.py bin update --name "North 1" --moisture 15.8
# Updated bin North 1  moisture 14.5% -> 15.8%

python3 grainrate.py bin update --name "West 3" --moisture 13.2 --grain rye   # rejected: unknown grain
python3 grainrate.py bin update --name "West 3" --moisture 13.2 --grain barley
```

`bin update` needs at least one of `--moisture` / `--grain`; an unknown bin name
is `error: bin not found: West 9 (see: grainrate.py bin list)`.

## 3. Check the risk report

```bash
python3 grainrate.py report
```

```
Risk report · 4 bins
NAME     GRAIN    MOISTURE  LIMIT  STATUS      MARGIN
───────  ───────  ────────  ─────  ──────────  ──────
East 4   canola      10.4%  10.0%  !! AT RISK    +0.4
North 1  corn        14.5%  15.0%  SAFE          -0.5
South 2  soybean     14.2%  13.0%  !! AT RISK    +1.2
West 3   wheat       13.2%  13.5%  SAFE          -0.3
4 bins · 2 at risk · 2 safe
limits: max safe moisture % per grain (approximate equilibrium moisture content …)
```

Each bin is compared **to its own grain's limit** — a corn bin at 14.5 % is safe
(limit 15.0 %), a soybean bin at 14.2 % is not (limit 13.0 %).

Exit codes make it scriptable:

```bash
if ! python3 grainrate.py report > /tmp/risk.txt; then
  # $? == 2 → at least one bin is at risk
  mail -s "GrainRate: bins at risk" you@example.com < /tmp/risk.txt
fi
```

Empty store:

```
No bins to report. Add a bin first.
```

Exit `2` means "the report ran and found risk"; exit `1` means "the command
failed" (bad input, locked tool, corrupt store). Never mix them.

Machine-readable:

```bash
python3 grainrate.py --json report
```

```json
{
  "at_risk": 1,
  "bins": [
    {"grain": "soybean", "limit_pct": 13.0, "margin_pct": 1.2,
     "moisture_pct": 14.2, "name": "South 2", "status": "AT RISK"}
  ],
  "count": 1,
  "safe": 0,
  "table_basis": "approximate equilibrium moisture content at 25 C / 70-75% relative humidity (…)"
}
```

## 4. Log daily silo temperatures

```bash
python3 grainrate.py temp log --bin "North 1" --date 2026-10-18 --temp 18.5
# Logged 2026-10-18 · North 1  18.5 C  (1 reading on file)

python3 grainrate.py temp log --bin "North 1" --date 2026-10-19 --temp 19.2
python3 grainrate.py temp history --bin "North 1"
```

```
Temperature history · North 1 (corn)
DATE          TEMP
──────────  ──────
2026-10-18  18.5 C
2026-10-19  19.2 C
2 readings · 2026-10-18 to 2026-10-19
```

Validation happens **before** anything is written:

| Input | Message on stderr |
| --- | --- |
| `--temp abc` | `error: temperature must be numeric` |
| `--date 2026/10/18` | `error: date must be YYYY-MM-DD` |
| `--date 2026-13-01` | `error: date must be a real calendar date in YYYY-MM-DD (got 2026-13-01)` |
| `--bin Ghost` | `error: bin not found: Ghost (see: grainrate.py bin list)` |
| same bin + same date twice | `error: bin 'North 1' already has a reading for 2026-10-18 (18.5 C); one reading per bin per day` |

Readings are stored against the bin's internal id, so renaming or adding a
second bin can never attach a reading to the wrong silo, and no orphaned
readings are possible. A bin with no readings says so and shows the next command:

```
No readings for North 1. Log one with: grainrate.py temp log --bin "North 1" --date 2026-10-20 --temp 18.5
```

## 5. Lock the tool with a local passphrase (optional)

```bash
python3 grainrate.py auth set-passphrase     # prompts twice; piped input works too
python3 grainrate.py auth login              # verifies, writes ~/.grainrate/session
python3 grainrate.py auth status
python3 grainrate.py auth logout             # deletes the session token
```

Non-interactive use (scripts, the demo):

```bash
printf 'silo-2026\nsilo-2026\n' | python3 grainrate.py auth set-passphrase
printf 'silo-2026\n' | python3 grainrate.py auth login
GRAINRATE_PASSPHRASE=silo-2026 python3 grainrate.py auth login
python3 grainrate.py auth login --passphrase silo-2026   # visible in shell history
```

Once a passphrase is set, `bin`, `report`, and `temp` refuse until a session
exists:

```
error: locked: run auth login to unlock GrainRate (a passphrase is configured; try: grainrate.py auth login)
```

- The passphrase is never stored or logged: the store keeps only a
  PBKDF2-HMAC-SHA256 digest, a per-store random salt, and the iteration count
  (200 000).
- Any wrong passphrase produces exactly one identical message:
  `error: invalid passphrase` — it does not reveal whether a passphrase exists.
- `auth logout` deletes the token, so the next data command is locked again.
- Sessions do not expire (single local user, no network). Delete
  `~/.grainrate/session` to force a lock.
- With no passphrase configured everything is open; `auth login` then explains
  that the tool is already open instead of failing silently.

## 6. Where the data lives

| Path | Contents |
| --- | --- |
| `~/.grainrate/store.json` | `bins[]`, `readings[]`, `auth` — written atomically, mode `0600` |
| `~/.grainrate/session` | session token written by `auth login` |

Point `GRAINRATE_HOME` at another directory to keep several independent
registries (test data, a farm laptop, a shared drive):

```bash
GRAINRATE_HOME=./demo-data python3 grainrate.py bin list
```

## 7. Help

```bash
python3 grainrate.py --help
python3 grainrate.py bin --help
python3 grainrate.py temp log --help
python3 grainrate.py --version
```

`--help` on the root command lists the four command groups and a set of
copy-pasteable examples; every subcommand documents its required flags.

## Deliberately not included

No real-time sensors or IoT probes, no weather data, no aeration-fan modelling,
no spoilage forecasting or time-to-risk estimates, no Fahrenheit conversion,
no bin capacity/geometry maths, no multi-user accounts or roles, no remote
identity or email reset, no daemon or scheduling, no cloud sync or outbound
alerts, and no GUI/web/mobile surface. Risk is driven by moisture only, and the
safe-storage table is a fixed constant in this version.
