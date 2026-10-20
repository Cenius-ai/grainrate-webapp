# Installing GrainRate

GrainRate is one Python file plus an optional console entry point. There is
nothing to compile and no service to configure.

## 1. System prerequisites (you provide these)

| Requirement | Notes |
| --- | --- |
| Python 3.9 or newer (`python3`) | Standard library only. Check with `python3 -V`. |
| A POSIX shell (`bash`) | Only for `install.sh` / `demo.sh`. The CLI itself runs anywhere, including Windows `cmd`. |

Nothing else is needed: no compiler, no `libsqlite3`, no database server, no
network access at runtime.

## 2. Install the project dependencies (one command, then it exits)

```bash
bash install.sh
```

What it does, in order:

1. Verifies `python3` exists.
2. `pip install --upgrade pip setuptools wheel` — old pip silently fails
   editable installs on modern `pyproject.toml` layouts.
3. `pip install -r requirements.txt` — installs `pytest` (the test harness).
   GrainRate itself has **zero** runtime dependencies.
4. `pip install --no-build-isolation -e .` — puts the `grainrate` console script
   on `PATH` (falls back to an isolated build if the offline install fails).
5. `python3 -c "import grainrate; assert grainrate.SAFE_STORAGE_TABLE"` — a cheap
   import self-check so a broken install fails here, not at first use.

It is idempotent, never uses `sudo`, never installs system packages, and never
starts a long-running process.

### Manual install (no pip)

There is no required install step at all:

```bash
python3 grainrate.py --help
```

That is the whole program. Skip `install.sh` if you only want the CLI.

## 3. Verify

```bash
python3 grainrate.py --version          # GrainRate 1.0.0
python3 grainrate.py bin list           # "No bins yet. Add one with: …"
python3 grainrate.py --help             # bin / report / temp / auth
bash demo.sh                            # full walkthrough, exit 0
python3 -m pytest                       # test suite, all green
```

`python3 -m pytest` is the only thing that needs installed packages. The CLI
itself runs even with site packages disabled:

```bash
python3 -S grainrate.py report          # "No bins to report. Add a bin first."
```

## 4. First run

```bash
python3 grainrate.py bin add --name "North 1" --grain corn --moisture 14.5
python3 grainrate.py report
```

The data directory `~/.grainrate/` is created on the first write. To keep data
somewhere else (another drive, a synced folder, a test sandbox), export:

```bash
export GRAINRATE_HOME="$HOME/Documents/grainrate"
```

`GRAINRATE_HOME` is the only environment variable GrainRate needs; it defaults
to `~/.grainrate`. `NO_COLOR` and `--no-color` turn off ANSI colour. See
`.env.example` for the full list.

## Optional: lock the tool with a passphrase

```bash
python3 grainrate.py auth set-passphrase    # prompts twice, stores PBKDF2 hash + salt
python3 grainrate.py auth login
python3 grainrate.py auth logout
```

Not required. Until a passphrase is set, `bin`, `report`, and `temp` are open.

## Troubleshooting

| Symptom | Fix |
| --- | --- |
| `error: store is unreadable or corrupt: …` | Inspect `~/.grainrate/store.json`. A valid store is a JSON object with `bins`, `readings`, `auth`. Move the broken file aside (`mv … … .bak`) to start fresh — bins are not recoverable from a corrupt file. |
| `error: locked: run auth login …` | A passphrase is configured. Run `grainrate auth login`, or delete `~/.grainrate/session` if it went stale. |
| `error: permission denied` writing the store | The data directory is not writable by you. Point `GRAINRATE_HOME` somewhere writable. |
| `grainrate: command not found` | Use `python3 grainrate.py …`, or re-run `bash install.sh` to install the console script. |
| Data looks wrong after a manual edit | GrainRate validates the store on load; malformed records are reported instead of guessed at. |

## Uninstall

```bash
pip uninstall grainrate                # removes the console script
rm -rf ~/.grainrate                    # removes bins, readings, passphrase, session
```

Delete the repository directory last; nothing else was installed.
