#!/usr/bin/env python3
"""GrainRate - grain moisture vs. safe-storage limits, plus daily silo temperatures.

Single file, standard library only. All data lives in ``~/.grainrate``
(override the directory with the ``GRAINRATE_HOME`` environment variable).
Run ``grainrate.py --help`` or read README.md for the walkthrough.

Design source of truth (calm-precise / terminal dark):
    accent      oklch(0.58 0.12 273)  ==  #6374c1   (primary status + headings)
    danger      #c2564f                              (AT RISK marker)
    ok          #5f9e7a                              (unused as a signal alone)
    muted       #8a8f9c
    text        #e6e7ea
    surface     #12141a   (the terminal's own dark background is the surface)
    type        system monospace, no webfont shipped; aligned columns,
                box-drawing rules, compact density, colour never the only signal.
"""

from __future__ import annotations

import argparse
import hashlib
import hmac
import json
import math
import os
import re
import secrets
import sys
import tempfile
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, List, Optional, Sequence

VERSION = "1.0.0"
PROG = "grainrate.py"

EXIT_OK = 0
EXIT_USER_ERROR = 1
EXIT_AT_RISK = 2

STORE_VERSION = 1
PBKDF2_ITERATIONS = 200_000

EMPTY_BINS_HINT = (
    'No bins yet. Add one with: '
    'grainrate.py bin add --name "North 1" --grain corn --moisture 14.5'
)
EMPTY_REPORT_HINT = "No bins to report. Add a bin first."
MOISTURE_ERROR = "moisture must be a number between 0 and 100"
DATE_ERROR = "date must be YYYY-MM-DD"
TEMP_ERROR = "temperature must be numeric"
TEMP_RANGE = (-80.0, 80.0)
LOCKED_ERROR = (
    "locked: run auth login to unlock GrainRate "
    "(a passphrase is configured; try: grainrate.py auth login)"
)
INVALID_PASSPHRASE = "invalid passphrase"

# --------------------------------------------------------------------------
# Theme (see module docstring)
# --------------------------------------------------------------------------

ACCENT_OKLCH = "oklch(0.58 0.12 273)"
ACCENT_HEX = "#6374c1"
PALETTE_HEX = {
    "accent": ACCENT_HEX,
    "danger": "#c2564f",
    "ok": "#5f9e7a",
    "muted": "#8a8f9c",
    "text": "#e6e7ea",
    "surface": "#12141a",
}


def _rgb(hex_value: str) -> tuple:
    raw = hex_value.lstrip("#")
    return tuple(int(raw[index:index + 2], 16) for index in (0, 2, 4))


@dataclass(frozen=True)
class Palette:
    """ANSI truecolor wrapper that degrades to plain text when colour is off."""

    enabled: bool

    def paint(self, text: str, token: str) -> str:
        if not self.enabled:
            return text
        red, green, blue = _rgb(PALETTE_HEX[token])
        return f"\x1b[38;2;{red};{green};{blue}m{text}\x1b[0m"

    def heading(self, text: str) -> str:
        return self.paint(text, "accent")

    def danger(self, text: str) -> str:
        return self.paint(text, "danger")

    def muted(self, text: str) -> str:
        return self.paint(text, "muted")


def configure_output_streams() -> None:
    """Force UTF-8 on stdout/stderr so box-drawing output never crashes a
    legacy console code page; streams without reconfigure() are left alone."""
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is None:
            continue
        try:
            reconfigure(encoding="utf-8", errors="replace")
        except (ValueError, OSError, LookupError):
            continue


def color_enabled(stream: Any, no_color_flag: bool) -> bool:
    if no_color_flag or os.environ.get("NO_COLOR"):
        return False
    if os.environ.get("TERM", "") == "dumb":
        return False
    return bool(getattr(stream, "isatty", lambda: False)())


def palette_for(args: argparse.Namespace) -> Palette:
    return Palette(color_enabled(sys.stdout, bool(getattr(args, "no_color", False))))


# --------------------------------------------------------------------------
# Embedded safe-storage table (fixed constant, not user editable)
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class SafeStorageRule:
    grain: str
    max_safe_moisture_pct: float
    basis: str


TABLE_BASIS = (
    "approximate equilibrium moisture content at 25 C / 70-75% relative "
    "humidity (extension-service storage guides); not a substitute for local advice"
)

SAFE_STORAGE_TABLE = {
    rule.grain: rule
    for rule in (
        SafeStorageRule("barley", 12.5, TABLE_BASIS),
        SafeStorageRule("canola", 10.0, TABLE_BASIS),
        SafeStorageRule("corn", 15.0, TABLE_BASIS),
        SafeStorageRule("oats", 13.0, TABLE_BASIS),
        SafeStorageRule("rice", 13.0, TABLE_BASIS),
        SafeStorageRule("sorghum", 13.0, TABLE_BASIS),
        SafeStorageRule("soybean", 13.0, TABLE_BASIS),
        SafeStorageRule("sunflower", 9.5, TABLE_BASIS),
        SafeStorageRule("wheat", 13.5, TABLE_BASIS),
    )
}

SUPPORTED_GRAINS = ", ".join(sorted(SAFE_STORAGE_TABLE))


# --------------------------------------------------------------------------
# Errors, time, small helpers
# --------------------------------------------------------------------------


class UserError(Exception):
    """A problem the user can fix: printed to stderr, exit code 1."""


def utc_now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def today_iso() -> str:
    return datetime.now(timezone.utc).date().isoformat()


def emit(lines: Sequence[str]) -> None:
    print("\n".join(lines))


def emit_json(payload: Any) -> None:
    print(json.dumps(payload, indent=2, sort_keys=True))


def format_pct(value: Any) -> str:
    return f"{float(value):.1f}%"


def format_temp(value: Any) -> str:
    return f"{float(value):.1f} C"


def format_margin(value: float) -> str:
    return f"{value:+.1f}"


def normalize_name(raw: Optional[str]) -> str:
    name = (raw or "").strip()
    if not name:
        raise UserError('bin name is required (for example: --name "North 1")')
    if len(name) > 60:
        raise UserError("bin name must be 60 characters or fewer")
    return name


# --------------------------------------------------------------------------
# Filesystem paths and the atomic JSON store
# --------------------------------------------------------------------------


def data_home() -> Path:
    raw = (os.environ.get("GRAINRATE_HOME") or "").strip()
    if raw:
        return Path(raw).expanduser()
    return Path.home() / ".grainrate"


def store_path() -> Path:
    return data_home() / "store.json"


def session_path() -> Path:
    return data_home() / "session"


def empty_store() -> dict:
    return {"version": STORE_VERSION, "bins": [], "readings": [], "auth": None}


def _remove_quietly(path: Path) -> None:
    try:
        path.unlink()
    except FileNotFoundError:
        return
    except OSError:
        return


def write_json_atomic(path: Path, payload: Any, mode: int = 0o600) -> None:
    """Write ``payload`` to ``path`` via a temp file in the same directory."""
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        handle_fd, temp_name = tempfile.mkstemp(
            dir=str(path.parent), prefix=path.name + ".", suffix=".tmp"
        )
    except OSError as exc:
        raise UserError(f"could not write {path}: {exc.strerror or exc}") from exc
    try:
        with os.fdopen(handle_fd, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, indent=2, sort_keys=True)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        try:
            os.chmod(temp_name, mode)
        except OSError:
            pass  # Windows / some filesystems cannot restrict modes
        os.replace(temp_name, path)
    except OSError as exc:
        _remove_quietly(Path(temp_name))
        raise UserError(f"could not write {path}: {exc.strerror or exc}") from exc


BIN_FIELDS = ("id", "name", "grain", "moisture_pct")
READING_FIELDS = ("id", "bin_id", "reading_date", "temp_c")
AUTH_FIELDS = ("passphrase_hash", "salt", "iterations")


def _corrupt(path: Path, detail: str) -> UserError:
    return UserError(f"store is unreadable or corrupt: {path} {detail}")


def _check_store_shape(payload: Any, path: Path) -> dict:
    if not isinstance(payload, dict):
        raise _corrupt(path, "does not contain a JSON object")
    bins = payload.get("bins", [])
    readings = payload.get("readings", [])
    auth = payload.get("auth")
    if not isinstance(bins, list) or not isinstance(readings, list):
        raise _corrupt(path, "has malformed bins/readings lists")
    for record in bins:
        if not isinstance(record, dict) or not all(field in record for field in BIN_FIELDS):
            raise _corrupt(path, "has a malformed bin record")
        if not isinstance(record["moisture_pct"], (int, float)):
            raise _corrupt(path, "has a bin with a non-numeric moisture value")
    for record in readings:
        if not isinstance(record, dict) or not all(field in record for field in READING_FIELDS):
            raise _corrupt(path, "has a malformed temperature reading")
    if auth is not None and (
        not isinstance(auth, dict) or not all(field in auth for field in AUTH_FIELDS)
    ):
        raise _corrupt(path, "has a malformed passphrase record")
    return {
        "version": payload.get("version", STORE_VERSION),
        "bins": bins,
        "readings": readings,
        "auth": auth,
    }


def load_store() -> dict:
    path = store_path()
    if not path.exists():
        return empty_store()
    try:
        raw = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise _corrupt(path, f"cannot be read ({exc.strerror or exc})") from exc
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise _corrupt(path, f"is not valid JSON ({exc.msg} at line {exc.lineno})") from exc
    return _check_store_shape(payload, path)


def save_store(store: dict) -> None:
    store["version"] = STORE_VERSION
    write_json_atomic(store_path(), store)


# --------------------------------------------------------------------------
# Local passphrase + session
# --------------------------------------------------------------------------


def hash_passphrase(passphrase: str, salt_hex: str, iterations: int) -> str:
    digest = hashlib.pbkdf2_hmac(
        "sha256", passphrase.encode("utf-8"), bytes.fromhex(salt_hex), iterations
    )
    return digest.hex()


def verify_passphrase(auth: dict, passphrase: str) -> bool:
    try:
        iterations = int(auth.get("iterations", PBKDF2_ITERATIONS))
        expected = str(auth["passphrase_hash"])
        salt = str(auth["salt"])
    except (KeyError, TypeError, ValueError):
        return False
    computed = hash_passphrase(passphrase, salt, iterations)
    return hmac.compare_digest(computed, expected)


@dataclass(frozen=True)
class SessionState:
    unlocked: bool
    created_at: Optional[str] = None
    problem: Optional[str] = None


def session_state() -> SessionState:
    """Read the session file. Anything unreadable is a locked tool (fail closed)."""
    path = session_path()
    if not path.exists():
        return SessionState(unlocked=False)
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except OSError as exc:
        return SessionState(False, problem=f"session unreadable ({exc.strerror or exc})")
    except json.JSONDecodeError:
        return SessionState(False, problem="session file is not valid JSON")
    if not isinstance(payload, dict) or not isinstance(payload.get("token"), str):
        return SessionState(False, problem="session file has no token")
    if not payload["token"]:
        return SessionState(False, problem="session file has an empty token")
    created_at = payload.get("created_at")
    return SessionState(True, created_at=created_at if isinstance(created_at, str) else None)


def write_session() -> Path:
    path = session_path()
    write_json_atomic(path, {"token": secrets.token_hex(32), "created_at": utc_now()})
    return path


def clear_session() -> bool:
    path = session_path()
    existed = path.exists()
    if existed:
        try:
            path.unlink()
        except FileNotFoundError:
            existed = False
        except OSError as exc:
            raise UserError(f"could not clear the session at {path}: {exc.strerror or exc}") from exc
    return existed


def require_unlocked(store: dict) -> None:
    if store.get("auth") is None:
        return
    if session_state().unlocked:
        return
    raise UserError(LOCKED_ERROR)


def load_unlocked_store() -> dict:
    store = load_store()
    require_unlocked(store)
    return store


def read_secret_line(prompt: str, missing_hint: str = "") -> str:
    """One passphrase line: TTY prompt, else a piped stdin line."""
    hint = missing_hint or "no passphrase given; pass --passphrase or pipe it on stdin"
    stream = sys.stdin
    if stream is None:
        raise UserError(hint)
    try:
        is_tty = stream.isatty()
    except (ValueError, AttributeError):
        is_tty = False
    if is_tty:
        import getpass

        return getpass.getpass(prompt)
    line = stream.readline()
    if line == "":
        raise UserError(hint)
    return line.rstrip("\r\n")


def resolve_passphrase(args: argparse.Namespace, prompt: str) -> str:
    flag_value = getattr(args, "passphrase", None)
    if flag_value:
        return str(flag_value)
    env_value = os.environ.get("GRAINRATE_PASSPHRASE")
    if env_value:
        return env_value
    value = read_secret_line(prompt)
    if not value:
        raise UserError("passphrase must not be empty")
    return value


# --------------------------------------------------------------------------
# Validators
# --------------------------------------------------------------------------


def parse_moisture(raw: Any) -> float:
    try:
        value = float(str(raw).strip())
    except (TypeError, ValueError):
        raise UserError(MOISTURE_ERROR) from None
    if not math.isfinite(value) or value < 0.0 or value > 100.0:
        raise UserError(MOISTURE_ERROR)
    return round(value, 1)


def parse_temp(raw: Any) -> float:
    try:
        value = float(str(raw).strip())
    except (TypeError, ValueError):
        raise UserError(TEMP_ERROR) from None
    if not math.isfinite(value):
        raise UserError(TEMP_ERROR)
    low, high = TEMP_RANGE
    if value < low or value > high:
        raise UserError(f"temperature must be between {low:.0f} and {high:.0f} C")
    return round(value, 1)


DATE_PATTERN = re.compile(r"^\d{4}-\d{2}-\d{2}$")


def parse_date(raw: Any) -> str:
    text = str(raw).strip()
    if not DATE_PATTERN.match(text):
        raise UserError(DATE_ERROR)
    try:
        datetime.strptime(text, "%Y-%m-%d")
    except ValueError:
        raise UserError(f"date must be a real calendar date in YYYY-MM-DD (got {text})") from None
    return text


def resolve_grain(raw: Any) -> str:
    text = str(raw).strip().lower()
    if not text:
        raise UserError(f"grain is required; supported grains: {SUPPORTED_GRAINS}")
    if text in SAFE_STORAGE_TABLE:
        return text
    raise UserError(f"unknown grain '{str(raw).strip()}'; supported grains: {SUPPORTED_GRAINS}")


# --------------------------------------------------------------------------
# Lookups over the store
# --------------------------------------------------------------------------


def find_bin(store: dict, name: str) -> Optional[dict]:
    wanted = name.casefold()
    for record in store["bins"]:
        if str(record.get("name", "")).casefold() == wanted:
            return record
    return None


def sorted_bins(store: dict) -> List[dict]:
    return sorted(store["bins"], key=lambda record: str(record.get("name", "")).casefold())


def readings_for(store: dict, bin_record: dict) -> List[dict]:
    rows = [row for row in store["readings"] if row.get("bin_id") == bin_record["id"]]
    return sorted(rows, key=lambda row: str(row.get("reading_date", "")))


# --------------------------------------------------------------------------
# Table rendering (aligned columns + box-drawing rule)
# --------------------------------------------------------------------------


def render_table(headers: Sequence[str], rows: Sequence[Sequence[str]],
                 align: Optional[Sequence[str]] = None) -> List[str]:
    widths = [len(header) for header in headers]
    for row in rows:
        for index, cell in enumerate(row):
            if index < len(widths):
                widths[index] = max(widths[index], len(cell))
    aligns = list(align or ["left"] * len(headers))

    def render_row(cells: Sequence[str]) -> str:
        parts = []
        for index, cell in enumerate(cells):
            width = widths[index]
            parts.append(cell.rjust(width) if aligns[index] == "right" else cell.ljust(width))
        return "  ".join(parts).rstrip()

    lines = [render_row(headers), "  ".join("\u2500" * width for width in widths)]
    lines.extend(render_row(row) for row in rows)
    return lines


# --------------------------------------------------------------------------
# bin commands
# --------------------------------------------------------------------------


def cmd_bin_add(args: argparse.Namespace) -> int:
    palette = palette_for(args)
    store = load_unlocked_store()
    name = normalize_name(args.name)
    grain = resolve_grain(args.grain)
    moisture = parse_moisture(args.moisture)
    if find_bin(store, name) is not None:
        raise UserError(f"bin name already exists: {name}")
    now = utc_now()
    record = {
        "id": uuid.uuid4().hex,
        "name": name,
        "grain": grain,
        "moisture_pct": moisture,
        "created_at": now,
        "updated_at": now,
    }
    store["bins"].append(record)
    save_store(store)
    rule = SAFE_STORAGE_TABLE[grain]
    if getattr(args, "json", False):
        emit_json({"bin": record, "limit_pct": rule.max_safe_moisture_pct})
        return EXIT_OK
    emit([
        palette.heading(f"Added bin {name}")
        + f"  {grain}  {format_pct(moisture)} moisture"
        + f"  (safe limit {rule.max_safe_moisture_pct:.1f}%)"
    ])
    return EXIT_OK


def cmd_bin_update(args: argparse.Namespace) -> int:
    palette = palette_for(args)
    store = load_unlocked_store()
    name = normalize_name(args.name)
    if args.moisture is None and args.grain is None:
        raise UserError("nothing to update: pass --moisture and/or --grain")
    moisture = parse_moisture(args.moisture) if args.moisture is not None else None
    grain = resolve_grain(args.grain) if args.grain is not None else None
    record = find_bin(store, name)
    if record is None:
        raise UserError(f'bin not found: {name} (see: grainrate.py bin list)')
    changes = []
    if moisture is not None:
        changes.append(f"moisture {format_pct(record['moisture_pct'])} -> {format_pct(moisture)}")
        record["moisture_pct"] = moisture
    if grain is not None:
        changes.append(f"grain {record['grain']} -> {grain}")
        record["grain"] = grain
    record["updated_at"] = utc_now()
    save_store(store)
    if getattr(args, "json", False):
        emit_json({"bin": record})
        return EXIT_OK
    emit([palette.heading(f"Updated bin {record['name']}") + "  " + "  ".join(changes)])
    return EXIT_OK


def cmd_bin_list(args: argparse.Namespace) -> int:
    palette = palette_for(args)
    store = load_unlocked_store()
    bins = sorted_bins(store)
    if getattr(args, "json", False):
        emit_json({"count": len(bins), "bins": bins})
        return EXIT_OK
    if not bins:
        emit([EMPTY_BINS_HINT])
        return EXIT_OK
    rows = [
        [
            str(record["name"]),
            str(record["grain"]),
            format_pct(record["moisture_pct"]),
            str(record.get("updated_at", ""))[:10],
        ]
        for record in bins
    ]
    suffix = "" if len(bins) == 1 else "s"
    lines = [palette.heading(f"Bins \u00b7 {len(bins)}")]
    lines.extend(render_table(
        ["NAME", "GRAIN", "MOISTURE", "UPDATED"], rows, ["left", "left", "right", "left"]
    ))
    lines.append(palette.muted(f"{len(bins)} bin{suffix} \u00b7 {store_path()}"))
    emit(lines)
    return EXIT_OK


# --------------------------------------------------------------------------
# report command
# --------------------------------------------------------------------------


def build_report(store: dict) -> List[dict]:
    rows: List[dict] = []
    for record in sorted_bins(store):
        grain = str(record.get("grain", ""))
        rule = SAFE_STORAGE_TABLE.get(grain)
        if rule is None:
            raise UserError(
                f"bin '{record.get('name')}' stores unknown grain '{grain}'; "
                f"fix it with: grainrate.py bin update --name \"{record.get('name')}\" "
                f"--grain corn"
            )
        moisture = float(record["moisture_pct"])
        margin = round(moisture - rule.max_safe_moisture_pct, 2)
        rows.append({
            "name": str(record.get("name")),
            "grain": grain,
            "moisture_pct": moisture,
            "limit_pct": rule.max_safe_moisture_pct,
            "margin_pct": margin,
            "status": "AT RISK" if margin > 0 else "SAFE",
        })
    return rows


def cmd_report(args: argparse.Namespace) -> int:
    palette = palette_for(args)
    store = load_unlocked_store()
    rows = build_report(store)
    at_risk = sum(1 for row in rows if row["status"] == "AT RISK")
    safe = len(rows) - at_risk
    exit_code = EXIT_AT_RISK if at_risk else EXIT_OK
    if getattr(args, "json", False):
        emit_json({
            "bins": rows,
            "count": len(rows),
            "at_risk": at_risk,
            "safe": safe,
            "table_basis": TABLE_BASIS,
        })
        return exit_code
    if not rows:
        emit([EMPTY_REPORT_HINT])
        return EXIT_OK
    table_rows = []
    for row in rows:
        status = row["status"]
        marker = ("!! " + status) if status == "AT RISK" else status
        table_rows.append([
            row["name"],
            row["grain"],
            format_pct(row["moisture_pct"]),
            f"{row['limit_pct']:.1f}%",
            marker,
            format_margin(row["margin_pct"]),
        ])
    lines = [palette.heading(f"Risk report \u00b7 {len(rows)} bin{'' if len(rows) == 1 else 's'}")]
    lines.extend(render_table(
        ["NAME", "GRAIN", "MOISTURE", "LIMIT", "STATUS", "MARGIN"],
        table_rows,
        ["left", "left", "right", "right", "left", "right"],
    ))
    summary = f"{len(rows)} bin{'' if len(rows) == 1 else 's'} \u00b7 {at_risk} at risk \u00b7 {safe} safe"
    lines.append(palette.danger(summary) if at_risk else palette.muted(summary))
    lines.append(palette.muted(f"limits: max safe moisture % per grain ({TABLE_BASIS})"))
    emit(lines)
    return exit_code


# --------------------------------------------------------------------------
# temp commands
# --------------------------------------------------------------------------


def cmd_temp_log(args: argparse.Namespace) -> int:
    palette = palette_for(args)
    store = load_unlocked_store()
    name = normalize_name(args.bin)
    reading_date = parse_date(args.date)
    temp_c = parse_temp(args.temp)
    record = find_bin(store, name)
    if record is None:
        raise UserError(f'bin not found: {name} (see: grainrate.py bin list)')
    for row in store["readings"]:
        if row.get("bin_id") == record["id"] and row.get("reading_date") == reading_date:
            raise UserError(
                f"bin '{record['name']}' already has a reading for {reading_date} "
                f"({format_temp(row.get('temp_c'))}); one reading per bin per day"
            )
    reading = {
        "id": uuid.uuid4().hex,
        "bin_id": record["id"],
        "reading_date": reading_date,
        "temp_c": temp_c,
        "created_at": utc_now(),
    }
    store["readings"].append(reading)
    save_store(store)
    if getattr(args, "json", False):
        emit_json({"reading": reading, "bin": record["name"]})
        return EXIT_OK
    emit([
        palette.heading(f"Logged {reading_date} \u00b7 {record['name']}")
        + f"  {format_temp(temp_c)}"
        + f"  ({len(readings_for(store, record))} reading"
        f"{'' if len(readings_for(store, record)) == 1 else 's'} on file)"
    ])
    return EXIT_OK


def cmd_temp_history(args: argparse.Namespace) -> int:
    palette = palette_for(args)
    store = load_unlocked_store()
    name = normalize_name(args.bin)
    record = find_bin(store, name)
    if record is None:
        raise UserError(f'bin not found: {name} (see: grainrate.py bin list)')
    readings = readings_for(store, record)
    if getattr(args, "json", False):
        emit_json({
            "bin": str(record["name"]),
            "grain": str(record["grain"]),
            "count": len(readings),
            "readings": readings,
        })
        return EXIT_OK
    if not readings:
        emit([
            f'No readings for {record["name"]}. Log one with: '
            f'grainrate.py temp log --bin "{record["name"]}" '
            f"--date {today_iso()} --temp 18.5"
        ])
        return EXIT_OK
    rows = [
        [str(row["reading_date"]), format_temp(row["temp_c"])]
        for row in readings
    ]
    lines = [
        palette.heading(f"Temperature history \u00b7 {record['name']} ({record['grain']})")
    ]
    lines.extend(render_table(["DATE", "TEMP"], rows, ["left", "right"]))
    lines.append(palette.muted(
        f"{len(readings)} reading{'' if len(readings) == 1 else 's'} "
        f"\u00b7 {rows[0][0]} to {rows[-1][0]}"
    ))
    emit(lines)
    return EXIT_OK


# --------------------------------------------------------------------------
# auth commands
# --------------------------------------------------------------------------


def cmd_auth_set_passphrase(args: argparse.Namespace) -> int:
    palette = palette_for(args)
    store = load_store()
    flag_value = getattr(args, "passphrase", None)
    if flag_value:
        passphrase = str(flag_value)
        if not passphrase:
            raise UserError("passphrase must not be empty")
    else:
        env_value = os.environ.get("GRAINRATE_PASSPHRASE")
        if env_value:
            passphrase = env_value
        else:
            passphrase = read_secret_line("New passphrase: ")
            if not passphrase:
                raise UserError("passphrase must not be empty")
            confirmation = read_secret_line(
                "Confirm passphrase: ",
                "no confirmation passphrase on stdin; pipe two lines "
                "(passphrase then confirmation) or pass --passphrase",
            )
            if confirmation != passphrase:
                raise UserError("passphrases do not match; nothing was saved")
    salt = secrets.token_hex(16)
    store["auth"] = {
        "passphrase_hash": hash_passphrase(passphrase, salt, PBKDF2_ITERATIONS),
        "salt": salt,
        "iterations": PBKDF2_ITERATIONS,
        "created_at": utc_now(),
    }
    save_store(store)
    clear_session()
    if getattr(args, "json", False):
        emit_json({"passphrase_set": True, "iterations": PBKDF2_ITERATIONS, "unlocked": False})
        return EXIT_OK
    emit([
        palette.heading("Passphrase set")
        + f"  PBKDF2-HMAC-SHA256 \u00b7 {PBKDF2_ITERATIONS} iterations \u00b7 salt stored",
        "Data commands are locked until you run: grainrate.py auth login",
    ])
    return EXIT_OK


def cmd_auth_login(args: argparse.Namespace) -> int:
    palette = palette_for(args)
    store = load_store()
    auth = store.get("auth")
    if auth is None:
        raise UserError(
            "no passphrase is set; the tool is already open "
            "(set one with: grainrate.py auth set-passphrase)"
        )
    passphrase = resolve_passphrase(args, "Passphrase: ")
    if not verify_passphrase(auth, passphrase):
        raise UserError(INVALID_PASSPHRASE)
    path = write_session()
    if getattr(args, "json", False):
        emit_json({"unlocked": True, "session_path": str(path)})
        return EXIT_OK
    emit([palette.heading("Unlocked") + f"  session stored at {path}"])
    return EXIT_OK


def cmd_auth_logout(args: argparse.Namespace) -> int:
    palette = palette_for(args)
    existed = clear_session()
    if getattr(args, "json", False):
        emit_json({"unlocked": False, "session_cleared": existed})
        return EXIT_OK
    message = "Logged out" if existed else "Already logged out"
    emit([palette.heading(message) + f"  {session_path()}"])
    return EXIT_OK


def cmd_auth_status(args: argparse.Namespace) -> int:
    palette = palette_for(args)
    store = load_store()
    auth = store.get("auth")
    state = session_state()
    passphrase_set = auth is not None
    unlocked = (not passphrase_set) or state.unlocked
    if getattr(args, "json", False):
        emit_json({
            "passphrase_set": passphrase_set,
            "unlocked": unlocked,
            "iterations": int(auth["iterations"]) if passphrase_set else 0,
            "session_created_at": state.created_at,
            "session_problem": state.problem,
            "data_home": str(data_home()),
        })
        return EXIT_OK
    lines = [palette.heading("Auth status")]
    if not passphrase_set:
        lines.append("passphrase: not set \u2014 data commands are open")
        lines.append("set one with: grainrate.py auth set-passphrase")
    else:
        lines.append(
            f"passphrase: set (PBKDF2-HMAC-SHA256 \u00b7 {int(auth['iterations'])} iterations)"
        )
        if state.unlocked:
            created = state.created_at or "unknown time"
            lines.append(f"session: unlocked (since {created})")
            lines.append("lock again with: grainrate.py auth logout")
        else:
            detail = f" \u00b7 {state.problem}" if state.problem else ""
            lines.append(f"session: locked{detail}")
            lines.append("unlock with: grainrate.py auth login")
    lines.append(palette.muted(f"data home: {data_home()}"))
    emit(lines)
    return EXIT_OK


# --------------------------------------------------------------------------
# Argument parser
# --------------------------------------------------------------------------

EPILOG = f"""\
examples:
  {PROG} bin add --name "North 1" --grain corn --moisture 14.5
  {PROG} bin update --name "North 1" --moisture 15.8
  {PROG} bin list
  {PROG} report                      # exit 2 when any bin is at risk
  {PROG} temp log --bin "North 1" --date 2026-01-15 --temp 18.5
  {PROG} temp history --bin "North 1"
  {PROG} auth set-passphrase         # optional: locks the data commands
  {PROG} auth login

grains and max safe moisture %: {SUPPORTED_GRAINS}
data home: ~/.grainrate (override with GRAINRATE_HOME)
"""

COMMAND_GROUPS = ("auth", "bin", "report", "temp")


class GrainRateParser(argparse.ArgumentParser):
    """Argparse that reports usage errors on stderr with exit code 1."""

    def error(self, message: str) -> None:  # type: ignore[override]
        self.print_usage(sys.stderr)
        print(f"{self.prog}: error: {message}", file=sys.stderr)
        print(
            "valid command groups: " + ", ".join(COMMAND_GROUPS)
            + f"  (run '{PROG} --help' for details)",
            file=sys.stderr,
        )
        raise SystemExit(EXIT_USER_ERROR)


def _common_flags() -> argparse.ArgumentParser:
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument(
        "--json", action="store_true", default=argparse.SUPPRESS,
        help="emit machine-readable JSON on stdout",
    )
    common.add_argument(
        "--no-color", action="store_true", default=argparse.SUPPRESS,
        help="disable ANSI colour (also honours NO_COLOR)",
    )
    return common


def build_parser() -> argparse.ArgumentParser:
    common = _common_flags()
    parser = GrainRateParser(
        prog=PROG,
        description=(
            "GrainRate \u2014 compare stored grain moisture against built-in "
            "safe-storage limits, flag bins at risk of spoilage, and log daily "
            "silo temperature readings."
        ),
        epilog=EPILOG,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("--version", action="version", version=f"GrainRate {VERSION}")
    parser.add_argument(
        "--json", action="store_true",
        help="emit machine-readable JSON on stdout (bin list, report, temp history, auth status)",
    )
    parser.add_argument(
        "--no-color", action="store_true",
        help="disable ANSI colour (also honours the NO_COLOR environment variable)",
    )
    groups = parser.add_subparsers(dest="command", metavar="{bin,report,temp,auth}")
    groups.required = True

    bin_parser = groups.add_parser(
        "bin", help="register, edit, and list grain bins",
        description="Register grain bins with a name, grain type, and stored moisture.",
    )
    bin_commands = bin_parser.add_subparsers(dest="bin_command", metavar="{add,update,list}")
    bin_commands.required = True

    add_parser = bin_commands.add_parser(
        "add", parents=[common], help="register a new bin",
        description="Register a bin. Moisture must be a number between 0 and 100.",
    )
    add_parser.add_argument("--name", required=True, help='bin name, e.g. "North 1"')
    add_parser.add_argument("--grain", required=True, help=f"one of: {SUPPORTED_GRAINS}")
    add_parser.add_argument("--moisture", required=True, help="stored moisture percentage, 0-100")
    add_parser.set_defaults(handler=cmd_bin_add)

    update_parser = bin_commands.add_parser(
        "update", parents=[common], help="change a bin's moisture (and optionally its grain)",
        description="Update a bin looked up by name. Pass --moisture and/or --grain.",
    )
    update_parser.add_argument("--name", required=True, help="existing bin name")
    update_parser.add_argument("--moisture", help="new moisture percentage, 0-100")
    update_parser.add_argument("--grain", help=f"new grain type, one of: {SUPPORTED_GRAINS}")
    update_parser.set_defaults(handler=cmd_bin_update)

    list_parser = bin_commands.add_parser(
        "list", parents=[common], help="list every bin with its grain and moisture",
    )
    list_parser.set_defaults(handler=cmd_bin_list)

    report_parser = groups.add_parser(
        "report", parents=[common],
        help="compare every bin to its grain's safe-storage limit",
        description=(
            "One line per bin comparing stored moisture to that grain's built-in "
            "safe-storage limit. Exit code 2 when any bin is at risk, 0 otherwise."
        ),
    )
    report_parser.set_defaults(handler=cmd_report)

    temp_parser = groups.add_parser(
        "temp", help="log and review daily silo temperature readings",
        description="Log one daily temperature reading per bin and review the history.",
    )
    temp_commands = temp_parser.add_subparsers(dest="temp_command", metavar="{log,history}")
    temp_commands.required = True

    log_parser = temp_commands.add_parser(
        "log", parents=[common], help="log a daily temperature reading for a bin",
        description="Log one reading per bin per day. Date must be YYYY-MM-DD.",
    )
    log_parser.add_argument("--bin", required=True, help="existing bin name")
    log_parser.add_argument("--date", required=True, help="reading date, YYYY-MM-DD")
    log_parser.add_argument("--temp", required=True, help="temperature in degrees Celsius")
    log_parser.set_defaults(handler=cmd_temp_log)

    history_parser = temp_commands.add_parser(
        "history", parents=[common], help="show a bin's readings sorted by date",
    )
    history_parser.add_argument("--bin", required=True, help="existing bin name")
    history_parser.set_defaults(handler=cmd_temp_history)

    auth_parser = groups.add_parser(
        "auth", help="set, unlock, lock, and inspect the local passphrase gate",
        description=(
            "Optional local single-user passphrase. Once set, data commands "
            "(bin, report, temp) need an unlocked session."
        ),
    )
    auth_commands = auth_parser.add_subparsers(
        dest="auth_command", metavar="{set-passphrase,login,logout,status}"
    )
    auth_commands.required = True

    set_parser = auth_commands.add_parser(
        "set-passphrase", parents=[common], help="set the local passphrase (prompts twice)",
        description=(
            "Set the local passphrase: PBKDF2-HMAC-SHA256 with a random salt, "
            "never stored in plaintext. Reads stdin when not attached to a terminal."
        ),
    )
    set_parser.add_argument(
        "--passphrase", help="passphrase (prefer piping it on stdin; flags land in shell history)",
    )
    set_parser.set_defaults(handler=cmd_auth_set_passphrase)

    login_parser = auth_commands.add_parser(
        "login", parents=[common], help="verify the passphrase and store a session token",
    )
    login_parser.add_argument("--passphrase", help="passphrase (or pipe it on stdin)")
    login_parser.set_defaults(handler=cmd_auth_login)

    logout_parser = auth_commands.add_parser(
        "logout", parents=[common], help="clear the local session token",
    )
    logout_parser.set_defaults(handler=cmd_auth_logout)

    status_parser = auth_commands.add_parser(
        "status", parents=[common], help="show whether the tool is locked or unlocked",
    )
    status_parser.set_defaults(handler=cmd_auth_status)

    return parser


# --------------------------------------------------------------------------
# Entry point
# --------------------------------------------------------------------------


def main(argv: Optional[Sequence[str]] = None) -> int:
    configure_output_streams()
    arguments = list(sys.argv[1:] if argv is None else argv)
    parser = build_parser()
    if not arguments:
        parser.print_help()
        print()
        print(f'Start here: {PROG} bin add --name "North 1" --grain corn --moisture 14.5')
        print(f'Then:       {PROG} report')
        return EXIT_OK
    args = parser.parse_args(arguments)
    try:
        return int(args.handler(args))
    except UserError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return EXIT_USER_ERROR
    except BrokenPipeError:
        try:
            sys.stdout.close()
        except OSError:
            pass
        return EXIT_OK
    except KeyboardInterrupt:
        print("aborted", file=sys.stderr)
        return 130


if __name__ == "__main__":
    sys.exit(main())
