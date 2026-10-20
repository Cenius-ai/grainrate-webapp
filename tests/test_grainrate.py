"""End-to-end and unit tests for the GrainRate CLI.

Every CLI test runs ``grainrate.py`` in a subprocess against a throwaway
``GRAINRATE_HOME`` directory, so the suite proves the real store-on-disk
behaviour (including cross-process reload) rather than in-memory shortcuts.
"""

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

import grainrate

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "grainrate.py"

EMPTY_BINS_HINT = (
    'No bins yet. Add one with: '
    'grainrate.py bin add --name "North 1" --grain corn --moisture 14.5'
)


class Result:
    """A finished CLI invocation."""

    def __init__(self, completed: subprocess.CompletedProcess):
        self.code = completed.returncode
        self.stdout = completed.stdout
        self.stderr = completed.stderr

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return (
            f"Result(code={self.code!r}, stdout={self.stdout!r}, "
            f"stderr={self.stderr!r})"
        )


@pytest.fixture()
def home(tmp_path: Path) -> Path:
    return tmp_path / "grainrate-home"


def cli(home: Path, *args: str, stdin: str = None, env: dict = None) -> Result:
    environment = dict(os.environ)
    environment["GRAINRATE_HOME"] = str(home)
    environment["NO_COLOR"] = "1"
    environment.pop("GRAINRATE_PASSPHRASE", None)
    if env:
        environment.update(env)
    completed = subprocess.run(
        [sys.executable, str(SCRIPT), *args],
        input=stdin,
        capture_output=True,
        text=True,
        env=environment,
        timeout=120,
    )
    return Result(completed)


def store_file(home: Path) -> Path:
    return home / "store.json"


def read_store(home: Path) -> dict:
    return json.loads(store_file(home).read_text(encoding="utf-8"))


def add_bin(home: Path, name: str, grain: str, moisture: str) -> Result:
    return cli(home, "bin", "add", "--name", name, "--grain", grain, "--moisture", moisture)


def configure_passphrase(home: Path, passphrase: str = "silo-2026") -> Result:
    return cli(home, "auth", "set-passphrase", stdin=f"{passphrase}\n{passphrase}\n")


# ---------------------------------------------------------------------------
# T0: skeleton, empty states, error path
# ---------------------------------------------------------------------------


def test_help_lists_every_command_group(home):
    result = cli(home, "--help")
    assert result.code == 0
    for group in ("bin", "report", "temp", "auth"):
        assert group in result.stdout
    assert "--version" in result.stdout


def test_bare_invocation_prints_help_and_exits_zero(home):
    result = cli(home)
    assert result.code == 0
    assert "bin add" in result.stdout


def test_unknown_command_is_a_usage_error(home):
    result = cli(home, "frobnicate")
    assert result.code == 1
    assert result.stdout == ""
    assert "invalid choice" in result.stderr
    assert "valid command groups" in result.stderr


def test_bin_list_empty_prints_the_add_hint(home):
    result = cli(home, "bin", "list")
    assert result.code == 0
    assert result.stdout.strip() == EMPTY_BINS_HINT


def test_corrupt_store_is_reported_clearly(home):
    home.mkdir(parents=True)
    store_file(home).write_text("{not json", encoding="utf-8")
    result = cli(home, "bin", "list")
    assert result.code == 1
    assert "store is unreadable or corrupt" in result.stderr


def test_store_with_wrong_shape_is_reported_as_corrupt(home):
    home.mkdir(parents=True)
    store_file(home).write_text("[]", encoding="utf-8")
    result = cli(home, "report")
    assert result.code == 1
    assert "store is unreadable or corrupt" in result.stderr


# ---------------------------------------------------------------------------
# F1: bin registry with shared 0-100 moisture validation
# ---------------------------------------------------------------------------


def test_add_then_list_from_a_separate_process(home):
    added = add_bin(home, "North 1", "corn", "14.5")
    assert added.code == 0, added.stderr
    listed = cli(home, "bin", "list")
    assert listed.code == 0
    assert "North 1" in listed.stdout
    assert "corn" in listed.stdout
    assert "14.5%" in listed.stdout
    # the store really was reloaded from disk by the second process
    assert read_store(home)["bins"][0]["name"] == "North 1"


def test_duplicate_bin_name_is_rejected(home):
    assert add_bin(home, "North 1", "corn", "14.5").code == 0
    duplicate = add_bin(home, "North 1", "corn", "13.0")
    assert duplicate.code == 1
    assert "bin name already exists" in duplicate.stderr
    assert len(read_store(home)["bins"]) == 1


@pytest.mark.parametrize("bad_moisture", ["abc", "120", "-1", "nan"])
def test_add_rejects_bad_moisture(home, bad_moisture):
    result = add_bin(home, "West 3", "corn", bad_moisture)
    assert result.code == 1
    assert "moisture must be a number between 0 and 100" in result.stderr
    assert "West 3" not in cli(home, "bin", "list").stdout


@pytest.mark.parametrize("bad_moisture", ["abc", "120"])
def test_update_shares_the_moisture_rule_and_keeps_the_stored_value(home, bad_moisture):
    assert add_bin(home, "North 1", "corn", "14.5").code == 0
    result = cli(home, "bin", "update", "--name", "North 1", "--moisture", bad_moisture)
    assert result.code == 1
    assert "moisture must be a number between 0 and 100" in result.stderr
    assert read_store(home)["bins"][0]["moisture_pct"] == 14.5


def test_update_changes_moisture_and_optionally_grain(home):
    assert add_bin(home, "North 1", "corn", "14.5").code == 0
    updated = cli(home, "bin", "update", "--name", "North 1",
                  "--moisture", "15.8", "--grain", "wheat")
    assert updated.code == 0, updated.stderr
    recorded = read_store(home)["bins"][0]
    assert recorded["moisture_pct"] == 15.8
    assert recorded["grain"] == "wheat"
    assert recorded["updated_at"] >= recorded["created_at"]


def test_update_requires_a_change_and_an_existing_bin(home):
    assert add_bin(home, "North 1", "corn", "14.5").code == 0
    nothing = cli(home, "bin", "update", "--name", "North 1")
    assert nothing.code == 1
    assert "nothing to update" in nothing.stderr
    missing = cli(home, "bin", "update", "--name", "Ghost", "--moisture", "12")
    assert missing.code == 1
    assert "bin not found" in missing.stderr


def test_unknown_grain_names_the_supported_grains(home):
    result = add_bin(home, "East 4", "rye", "12.0")
    assert result.code == 1
    assert "unknown grain 'rye'" in result.stderr
    assert "corn" in result.stderr and "soybean" in result.stderr
    assert not store_file(home).exists() or read_store(home)["bins"] == []


# ---------------------------------------------------------------------------
# F2: per-grain safe-storage report
# ---------------------------------------------------------------------------


def test_report_compares_each_bin_to_its_own_grain_limit(home):
    assert add_bin(home, "North 1", "corn", "14.0").code == 0
    assert add_bin(home, "South 2", "soybean", "14.0").code == 0
    report = cli(home, "report")
    assert report.code == 2
    assert "SAFE" in report.stdout
    assert "AT RISK" in report.stdout
    north = [line for line in report.stdout.splitlines() if line.startswith("North 1")][0]
    south = [line for line in report.stdout.splitlines() if line.startswith("South 2")][0]
    assert "SAFE" in north and "15.0%" in north
    assert "AT RISK" in south and "13.0%" in south
    assert "1 at risk" in report.stdout


def test_report_flags_at_risk_with_exit_code_two(home):
    assert add_bin(home, "North 1", "corn", "16.0").code == 0
    report = cli(home, "report")
    assert report.code == 2
    assert "AT RISK" in report.stdout
    assert "1 at risk" in report.stdout


def test_report_all_safe_exits_zero(home):
    assert add_bin(home, "North 1", "corn", "13.0").code == 0
    assert add_bin(home, "South 2", "soybean", "12.0").code == 0
    report = cli(home, "report")
    assert report.code == 0
    assert "AT RISK" not in report.stdout
    assert "0 at risk" in report.stdout


def test_report_empty_store(home):
    report = cli(home, "report")
    assert report.code == 0
    assert report.stdout.strip() == "No bins to report. Add a bin first."


def test_report_json_contract(home):
    assert add_bin(home, "North 1", "corn", "16.0").code == 0
    report = cli(home, "--json", "report")
    assert report.code == 2
    payload = json.loads(report.stdout)
    assert payload["at_risk"] == 1
    assert payload["safe"] == 0
    assert payload["bins"][0]["status"] == "AT RISK"
    assert payload["bins"][0]["limit_pct"] == 15.0
    assert payload["bins"][0]["margin_pct"] == 1.0


def test_safe_storage_table_documents_at_least_two_differing_grains():
    assert grainrate.SAFE_STORAGE_TABLE["corn"].max_safe_moisture_pct == 15.0
    assert grainrate.SAFE_STORAGE_TABLE["soybean"].max_safe_moisture_pct == 13.0
    limits = {rule.max_safe_moisture_pct for rule in grainrate.SAFE_STORAGE_TABLE.values()}
    assert len(limits) >= 2
    for rule in grainrate.SAFE_STORAGE_TABLE.values():
        assert 0 < rule.max_safe_moisture_pct < 100
        assert rule.basis


# ---------------------------------------------------------------------------
# F3: daily temperature readings
# ---------------------------------------------------------------------------


def test_temp_log_then_history(home):
    assert add_bin(home, "North 1", "corn", "14.5").code == 0
    logged = cli(home, "temp", "log", "--bin", "North 1", "--date", "2026-01-15",
                 "--temp", "18.5")
    assert logged.code == 0, logged.stderr
    history = cli(home, "temp", "history", "--bin", "North 1")
    assert history.code == 0
    assert "2026-01-15" in history.stdout
    assert "18.5" in history.stdout


def test_temp_history_is_sorted_by_date(home):
    assert add_bin(home, "North 1", "corn", "14.5").code == 0
    for reading_date, temp in (("2026-01-17", "21.0"), ("2026-01-15", "18.5"),
                               ("2026-01-16", "19.5")):
        assert cli(home, "temp", "log", "--bin", "North 1", "--date", reading_date,
                   "--temp", temp).code == 0
    history = cli(home, "temp", "history", "--bin", "North 1")
    dates = [line.split()[0] for line in history.stdout.splitlines()
             if line[:2] == "20"]
    assert dates == ["2026-01-15", "2026-01-16", "2026-01-17"]


@pytest.mark.parametrize("bad_temp", ["abc", "", "nan"])
def test_temp_log_rejects_non_numeric_before_saving(home, bad_temp):
    assert add_bin(home, "North 1", "corn", "14.5").code == 0
    result = cli(home, "temp", "log", "--bin", "North 1", "--date", "2026-01-15",
                 "--temp", bad_temp)
    assert result.code == 1
    assert "temperature must be numeric" in result.stderr
    assert read_store(home)["readings"] == []


def test_temp_log_rejects_missing_bin_without_saving(home):
    assert add_bin(home, "North 1", "corn", "14.5").code == 0
    result = cli(home, "temp", "log", "--bin", "Ghost", "--date", "2026-01-15",
                 "--temp", "18.5")
    assert result.code == 1
    assert "bin not found" in result.stderr
    assert read_store(home)["readings"] == []


@pytest.mark.parametrize("bad_date", ["2026/01/15", "15-01-2026", "2026-13-01", ""])
def test_temp_log_rejects_bad_dates(home, bad_date):
    assert add_bin(home, "North 1", "corn", "14.5").code == 0
    result = cli(home, "temp", "log", "--bin", "North 1", "--date", bad_date,
                 "--temp", "18.5")
    assert result.code == 1
    assert "date must be" in result.stderr
    assert read_store(home)["readings"] == []


def test_readings_are_scoped_to_the_resolved_bin(home):
    assert add_bin(home, "North 1", "corn", "14.5").code == 0
    assert add_bin(home, "South 2", "soybean", "12.0").code == 0
    assert cli(home, "temp", "log", "--bin", "South 2", "--date", "2026-01-16",
               "--temp", "12.0").code == 0
    north_history = cli(home, "temp", "history", "--bin", "North 1")
    south_history = cli(home, "temp", "history", "--bin", "South 2")
    assert "2026-01-16" not in north_history.stdout
    assert "2026-01-16" in south_history.stdout

    store = read_store(home)
    south_id = [record["id"] for record in store["bins"] if record["name"] == "South 2"][0]
    assert store["readings"][0]["bin_id"] == south_id  # resolved to the real Bin.id


def test_temp_history_empty_state_points_at_the_log_command(home):
    assert add_bin(home, "North 1", "corn", "14.5").code == 0
    history = cli(home, "temp", "history", "--bin", "North 1")
    assert history.code == 0
    assert "No readings for North 1" in history.stdout
    assert "temp log" in history.stdout


def test_second_reading_for_the_same_day_is_refused(home):
    assert add_bin(home, "North 1", "corn", "14.5").code == 0
    assert cli(home, "temp", "log", "--bin", "North 1", "--date", "2026-01-15",
               "--temp", "18.5").code == 0
    duplicate = cli(home, "temp", "log", "--bin", "North 1", "--date", "2026-01-15",
                    "--temp", "19.0")
    assert duplicate.code == 1
    assert "already has a reading" in duplicate.stderr
    assert len(read_store(home)["readings"]) == 1


# ---------------------------------------------------------------------------
# F4: local passphrase gate
# ---------------------------------------------------------------------------


def test_set_passphrase_stores_a_pbkdf2_hash_never_the_passphrase(home):
    assert add_bin(home, "North 1", "corn", "14.5").code == 0
    result = configure_passphrase(home, "turn-the-fans-on")
    assert result.code == 0, result.stderr
    raw = store_file(home).read_text(encoding="utf-8")
    assert "turn-the-fans-on" not in raw
    auth = read_store(home)["auth"]
    assert len(auth["passphrase_hash"]) == 64
    assert len(auth["salt"]) >= 16
    assert auth["iterations"] >= 100_000
    # and the stored hash really verifies through the app's own flow
    assert grainrate.verify_passphrase(auth, "turn-the-fans-on")
    assert not grainrate.verify_passphrase(auth, "wrong")


def test_mismatched_confirmation_sets_no_passphrase(home):
    result = cli(home, "auth", "set-passphrase", stdin="alpha\nbeta\n")
    assert result.code == 1
    assert "do not match" in result.stderr
    assert not store_file(home).exists() or read_store(home)["auth"] is None


def test_data_commands_are_locked_once_a_passphrase_is_set(home):
    assert add_bin(home, "North 1", "corn", "14.5").code == 0
    assert configure_passphrase(home).code == 0
    for args in (
        ("bin", "list"),
        ("report",),
        ("temp", "log", "--bin", "North 1", "--date", "2026-01-15", "--temp", "18.5"),
        ("temp", "history", "--bin", "North 1"),
        ("bin", "add", "--name", "West 3", "--grain", "corn", "--moisture", "13"),
    ):
        result = cli(home, *args)
        assert result.code == 1, args
        assert "locked: run auth login" in result.stderr, args
    store = read_store(home)
    assert store["readings"] == []
    assert len(store["bins"]) == 1


def test_auth_status_reports_locked_and_unlocked(home):
    assert cli(home, "auth", "status").stdout.find("not set") != -1
    assert configure_passphrase(home).code == 0
    locked = cli(home, "auth", "status")
    assert "locked" in locked.stdout
    assert cli(home, "auth", "login", stdin="silo-2026\n").code == 0
    unlocked = cli(home, "auth", "status")
    assert "unlocked" in unlocked.stdout


def test_wrong_passphrase_is_one_non_enumerating_error(home):
    assert configure_passphrase(home).code == 0
    first = cli(home, "auth", "login", stdin="nope\n")
    second = cli(home, "auth", "login", stdin="also-nope\n")
    assert first.code == 1 and second.code == 1
    assert first.stderr.strip() == "error: invalid passphrase"
    assert first.stderr == second.stderr  # identical for any wrong passphrase
    assert cli(home, "bin", "list").code == 1


def test_correct_passphrase_unlocks_then_logout_locks_again(home):
    assert add_bin(home, "North 1", "corn", "14.5").code == 0
    assert configure_passphrase(home).code == 0
    login = cli(home, "auth", "login", stdin="silo-2026\n")
    assert login.code == 0, login.stderr
    assert cli(home, "bin", "list").code == 0
    assert cli(home, "report").code == 0
    logout = cli(home, "auth", "logout")
    assert logout.code == 0
    assert not (home / "session").exists()
    locked = cli(home, "report")
    assert locked.code == 1
    assert "locked: run auth login" in locked.stderr


def test_session_token_is_random_per_login(home):
    assert configure_passphrase(home).code == 0
    assert cli(home, "auth", "login", stdin="silo-2026\n").code == 0
    first = json.loads((home / "session").read_text(encoding="utf-8"))["token"]
    assert cli(home, "auth", "logout").code == 0
    assert cli(home, "auth", "login", stdin="silo-2026\n").code == 0
    second = json.loads((home / "session").read_text(encoding="utf-8"))["token"]
    assert first != second
    assert len(first) == 64


def test_login_without_a_configured_passphrase_is_a_clear_error(home):
    result = cli(home, "auth", "login", stdin="silo-2026\n")
    assert result.code == 1
    assert "no passphrase is set" in result.stderr


def test_store_write_is_atomic_and_leaves_no_temp_files(home):
    assert add_bin(home, "North 1", "corn", "14.5").code == 0
    assert add_bin(home, "South 2", "soybean", "12.0").code == 0
    leftovers = [path.name for path in home.iterdir() if path.name.endswith(".tmp")]
    assert leftovers == []
    assert json.loads(store_file(home).read_text(encoding="utf-8"))["bins"]


# ---------------------------------------------------------------------------
# T5: docs / stdlib-only smoke checks and validator units
# ---------------------------------------------------------------------------


def test_runs_with_site_packages_disabled(home):
    environment = dict(os.environ)
    environment["GRAINRATE_HOME"] = str(home)
    completed = subprocess.run(
        [sys.executable, "-S", str(SCRIPT), "report"],
        capture_output=True, text=True, env=environment, timeout=120,
    )
    assert completed.returncode == 0
    assert "No bins to report. Add a bin first." in completed.stdout


def test_temp_log_help_documents_required_arguments(home):
    result = cli(home, "temp", "log", "--help")
    assert result.code == 0
    for flag in ("--bin", "--date", "--temp"):
        assert flag in result.stdout


@pytest.mark.parametrize("raw,expected", [("0", 0.0), ("14.5", 14.5), ("100", 100.0)])
def test_parse_moisture_accepts_the_valid_range(raw, expected):
    assert grainrate.parse_moisture(raw) == expected


@pytest.mark.parametrize("raw", ["abc", "-0.1", "100.1", "nan", "inf", ""])
def test_parse_moisture_rejects_invalid_values(raw):
    with pytest.raises(grainrate.UserError) as caught:
        grainrate.parse_moisture(raw)
    assert str(caught.value) == "moisture must be a number between 0 and 100"


def test_parse_date_and_temp_rules():
    assert grainrate.parse_date("2026-01-15") == "2026-01-15"
    with pytest.raises(grainrate.UserError):
        grainrate.parse_date("2026/01/15")
    assert grainrate.parse_temp("18.5") == 18.5
    with pytest.raises(grainrate.UserError):
        grainrate.parse_temp("warm")


def test_main_without_arguments_returns_zero(capsys):
    assert grainrate.main([]) == 0
    captured = capsys.readouterr()
    assert "bin add" in captured.out


def test_render_table_aligns_columns():
    lines = grainrate.render_table(["NAME", "MOISTURE"], [["North 1", "14.5%"]],
                                   ["left", "right"])
    assert lines[0].split() == ["NAME", "MOISTURE"]
    assert lines[2].endswith("14.5%")
    assert len(lines[1]) >= len("MOISTURE")
