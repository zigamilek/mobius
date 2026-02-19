from __future__ import annotations

import argparse
import os
import re
import secrets
import shlex
import socket
import subprocess
from pathlib import Path
from urllib.parse import quote

import uvicorn
import yaml

from mobius import __version__
from mobius.config import AppConfig, load_config
from mobius.onboarding import default_config_path, default_env_path, run_onboarding

SERVICE_NAME = "mobius"
DEFAULT_REPO_URL = "https://github.com/zigamilek/mobius.git"
DEFAULT_RAW_REPO_PATH = "zigamilek/mobius"
DEFAULT_REPO_REF = "master"
PGDG_KEY_URL = "https://www.postgresql.org/media/keys/ACCC4CF8.asc"
PGDG_KEYRING_PATH = Path("/usr/share/keyrings/postgresql.gpg")
PGDG_SOURCES_LIST_PATH = Path("/etc/apt/sources.list.d/pgdg.list")
GITHUB_HTTPS_RE = re.compile(
    r"^https://github\.com/(?P<path>[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+?)(?:\.git)?/?$"
)
GITHUB_SSH_RE = re.compile(
    r"^git@github\.com:(?P<path>[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+?)(?:\.git)?$"
)
SAFE_DB_IDENTIFIER_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]{0,62}$")
ENV_LINE_RE = re.compile(r"^(?P<key>[A-Z0-9_]+)=(?P<value>.*)$")


def _resolve_config_path(config_path: str | None) -> Path:
    if config_path:
        return Path(config_path)
    env_config = os.getenv("MOBIUS_CONFIG", "").strip()
    if env_config:
        return Path(env_config)
    return default_config_path()


def _resolve_env_path(env_file: str | None) -> Path:
    if env_file:
        return Path(env_file)
    return default_env_path()


def _try_load_config(path: Path) -> tuple[AppConfig | None, str | None]:
    try:
        return load_config(path), None
    except Exception as exc:
        return None, f"{exc.__class__.__name__}: {exc}"


def _path_state(path: Path) -> str:
    return "exists" if path.exists() else "missing"


def _detect_local_ip() -> str:
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
            sock.connect(("8.8.8.8", 80))
            ip = sock.getsockname()[0]
            if ip:
                return ip
    except OSError:
        pass

    try:
        ip = socket.gethostbyname(socket.gethostname())
        if ip and not ip.startswith("127."):
            return ip
    except OSError:
        pass
    return "127.0.0.1"


def _run_command(command: list[str]) -> int:
    try:
        return subprocess.run(command, check=False).returncode
    except FileNotFoundError:
        print(f"Command not found: {command[0]}")
        return 127


def _run_capture(command: list[str]) -> tuple[int, str, str]:
    try:
        completed = subprocess.run(
            command,
            check=False,
            capture_output=True,
            text=True,
        )
    except FileNotFoundError:
        return 127, "", f"Command not found: {command[0]}"
    return completed.returncode, completed.stdout.strip(), completed.stderr.strip()


def _run_or_fail(command: list[str], *, label: str, env: dict[str, str] | None = None) -> None:
    print(f"-> {label}: {shlex.join(command)}")
    try:
        completed = subprocess.run(command, env=env, check=False)
    except FileNotFoundError as exc:
        raise RuntimeError(f"{label} failed: missing command ({exc}).") from exc
    if completed.returncode != 0:
        raise RuntimeError(f"{label} failed with exit code {completed.returncode}.")


def _is_safe_db_identifier(value: str) -> bool:
    return bool(SAFE_DB_IDENTIFIER_RE.match(value.strip()))


def _sql_quote_literal(value: str) -> str:
    return value.replace("'", "''")


def _sql_quote_ident(value: str) -> str:
    return '"' + value.replace('"', '""') + '"'


def _psql_as_postgres(sql: str, *, db: str | None = None) -> tuple[int, str, str]:
    command = ["runuser", "-u", "postgres", "--", "psql", "-v", "ON_ERROR_STOP=1"]
    if db:
        command.extend(["-d", db])
    command.extend(["-tAc", sql])
    return _run_capture(command)


def _state_dsn(*, db_user: str, db_password: str, db_host: str, db_port: int, db_name: str) -> str:
    quoted_password = quote(db_password, safe="")
    return (
        f"postgresql://{db_user}:{quoted_password}@{db_host}:{db_port}/{db_name}"
    )


def _linux_codename() -> str | None:
    os_release = Path("/etc/os-release")
    if not os_release.exists():
        return None
    try:
        lines = os_release.read_text(encoding="utf-8").splitlines()
    except Exception:
        return None
    values: dict[str, str] = {}
    for line in lines:
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        values[key.strip()] = value.strip().strip('"').strip("'")
    codename = values.get("VERSION_CODENAME") or values.get("UBUNTU_CODENAME")
    if not codename:
        return None
    cleaned = codename.strip().lower()
    return cleaned or None


def _install_first_available_package(packages: list[str]) -> tuple[bool, str | None]:
    for package in packages:
        show_rc, _stdout, _stderr = _run_capture(["apt-cache", "show", package])
        if show_rc != 0:
            continue
        _run_or_fail(
            ["apt-get", "install", "-y", package],
            label=f"package install ({package})",
        )
        return True, package
    return False, None


def _ensure_pgdg_repo(codename: str) -> None:
    _run_or_fail(
        ["apt-get", "install", "-y", "ca-certificates", "curl", "gnupg"],
        label="pgdg repository prerequisites",
    )
    _run_or_fail(
        [
            "bash",
            "-lc",
            f"rm -f {shlex.quote(str(PGDG_KEYRING_PATH))} && "
            f"curl -fsSL {shlex.quote(PGDG_KEY_URL)} | gpg --dearmor > {shlex.quote(str(PGDG_KEYRING_PATH))}",
        ],
        label="pgdg repository key import",
    )
    PGDG_KEYRING_PATH.chmod(0o644)
    repo_line = (
        f"deb [signed-by={PGDG_KEYRING_PATH}] "
        f"https://apt.postgresql.org/pub/repos/apt {codename}-pgdg main\n"
    )
    PGDG_SOURCES_LIST_PATH.write_text(repo_line, encoding="utf-8")
    _run_or_fail(["apt-get", "update"], label="apt index refresh (pgdg)")


def _load_env_lines(path: Path) -> list[str]:
    if not path.exists():
        return []
    return path.read_text(encoding="utf-8").splitlines()


def _upsert_env_lines(lines: list[str], updates: dict[str, str]) -> list[str]:
    remaining = list(updates.keys())
    result: list[str] = []
    for line in lines:
        match = ENV_LINE_RE.match(line.strip())
        if not match:
            result.append(line)
            continue
        key = match.group("key")
        if key not in updates:
            result.append(line)
            continue
        result.append(f"{key}={updates[key]}")
        if key in remaining:
            remaining.remove(key)
    for key in remaining:
        result.append(f"{key}={updates[key]}")
    return result


def _write_env_lines(path: Path, lines: list[str]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    text = "\n".join(lines).rstrip() + "\n"
    path.write_text(text, encoding="utf-8")
    try:
        path.chmod(0o600)
    except OSError:
        pass


def _enable_state_in_config(path: Path) -> None:
    loaded: object = {}
    if path.exists():
        loaded = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    data: dict[str, object] = loaded if isinstance(loaded, dict) else {}
    state = data.setdefault("state", {})
    if not isinstance(state, dict):
        state = {}
        data["state"] = state
    state["enabled"] = True
    database = state.setdefault("database", {})
    if not isinstance(database, dict):
        database = {}
        state["database"] = database
    database["dsn"] = "${ENV:MOBIUS_STATE_DSN}"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        yaml.safe_dump(data, sort_keys=False, allow_unicode=False),
        encoding="utf-8",
    )


def _service_exists(service_name: str) -> bool:
    rc, _stdout, _stderr = _run_capture(
        ["systemctl", "status", service_name, "--no-pager", "-l"]
    )
    return rc in {0, 3}


def _cmd_db_bootstrap_local(args: argparse.Namespace) -> int:
    if hasattr(os, "geteuid") and os.geteuid() != 0:
        print("mobius db bootstrap-local should be run as root (or with sudo).")
        return 1

    db_name = str(getattr(args, "db_name", "mobius") or "mobius").strip()
    db_user = str(getattr(args, "db_user", "mobius") or "mobius").strip()
    db_host = str(getattr(args, "db_host", "127.0.0.1") or "127.0.0.1").strip()
    db_port = int(getattr(args, "db_port", 5432) or 5432)
    db_password = str(getattr(args, "db_password", "") or "").strip()
    skip_install = bool(getattr(args, "skip_install", False))
    no_restart = bool(getattr(args, "no_restart", False))
    dry_run = bool(getattr(args, "dry_run", False))
    assume_yes = bool(getattr(args, "yes", False))

    if not _is_safe_db_identifier(db_name):
        print(
            "Invalid --db-name. Use letters/digits/underscore and start with letter/underscore."
        )
        return 2
    if not _is_safe_db_identifier(db_user):
        print(
            "Invalid --db-user. Use letters/digits/underscore and start with letter/underscore."
        )
        return 2
    if db_port < 1 or db_port > 65535:
        print("Invalid --db-port. Expected 1..65535.")
        return 2
    if not db_password:
        db_password = f"mobius-{secrets.token_urlsafe(18)}"

    cfg_path = _resolve_config_path(getattr(args, "config_path", None))
    env_path = _resolve_env_path(getattr(args, "env_file", None))
    dsn = _state_dsn(
        db_user=db_user,
        db_password=db_password,
        db_host=db_host,
        db_port=db_port,
        db_name=db_name,
    )

    print("")
    print("Mobius Local DB Bootstrap")
    print("=========================")
    print(f"Config file:     {cfg_path}")
    print(f"Env file:        {env_path}")
    print(f"DB name:         {db_name}")
    print(f"DB user:         {db_user}")
    print(f"DB host:         {db_host}")
    print(f"DB port:         {db_port}")
    print(f"Install packages:{'no' if skip_install else 'yes'}")
    print(f"Restart service: {'no' if no_restart else 'yes'}")
    print("")

    if dry_run:
        print("Dry run only. No database/bootstrap changes were applied.")
        return 0

    if not assume_yes:
        confirm = input("Proceed with local PostgreSQL bootstrap? [Y/n]: ").strip().lower()
        if confirm in {"n", "no"}:
            print("Bootstrap cancelled.")
            return 0

    try:
        if not skip_install:
            _run_or_fail(["apt-get", "update"], label="apt index refresh")
            _run_or_fail(
                ["apt-get", "install", "-y", "postgresql", "postgresql-contrib"],
                label="postgresql package install",
            )

        _run_or_fail(
            ["systemctl", "enable", "-q", "--now", "postgresql"],
            label="postgresql service enable/start",
        )

        rc, version_text, _stderr = _run_capture(["psql", "--version"])
        if rc != 0:
            raise RuntimeError("Could not detect PostgreSQL version via psql.")
        match = re.search(r"(\d+)(?:\.\d+)?", version_text)
        major = match.group(1) if match else ""
        pgvector_candidates = []
        if major:
            pgvector_candidates.append(f"postgresql-{major}-pgvector")
        pgvector_candidates.append("postgresql-pgvector")
        pgvector_installed, installed_pkg = _install_first_available_package(
            pgvector_candidates
        )
        if not pgvector_installed:
            codename = _linux_codename()
            if codename:
                print(
                    "No pgvector package found in default repositories; "
                    f"trying PostgreSQL APT repository ({codename}-pgdg)."
                )
                _ensure_pgdg_repo(codename)
                pgvector_installed, installed_pkg = _install_first_available_package(
                    pgvector_candidates
                )
        if not pgvector_installed:
            raise RuntimeError(
                "Could not install a pgvector package for this system. "
                "Install pgvector manually, then rerun 'mobius db bootstrap-local'."
            )
        if installed_pkg:
            print(f"Installed pgvector package: {installed_pkg}")

        quoted_user_ident = _sql_quote_ident(db_user)
        quoted_name_ident = _sql_quote_ident(db_name)
        quoted_user_literal = _sql_quote_literal(db_user)
        quoted_name_literal = _sql_quote_literal(db_name)
        quoted_password = _sql_quote_literal(db_password)

        role_exists_sql = (
            f"SELECT 1 FROM pg_roles WHERE rolname = '{quoted_user_literal}' LIMIT 1;"
        )
        role_rc, role_exists, role_err = _psql_as_postgres(role_exists_sql)
        if role_rc != 0:
            raise RuntimeError(f"Failed to query PostgreSQL roles: {role_err}")
        if role_exists.strip() != "1":
            create_role_sql = (
                f"CREATE ROLE {quoted_user_ident} LOGIN PASSWORD '{quoted_password}';"
            )
            create_role_rc, _out, create_role_err = _psql_as_postgres(create_role_sql)
            if create_role_rc != 0:
                raise RuntimeError(f"Failed to create DB role: {create_role_err}")
        else:
            alter_role_sql = (
                f"ALTER ROLE {quoted_user_ident} WITH LOGIN PASSWORD '{quoted_password}';"
            )
            alter_role_rc, _out, alter_role_err = _psql_as_postgres(alter_role_sql)
            if alter_role_rc != 0:
                raise RuntimeError(f"Failed to alter DB role password: {alter_role_err}")

        db_exists_sql = (
            f"SELECT 1 FROM pg_database WHERE datname = '{quoted_name_literal}' LIMIT 1;"
        )
        db_rc, db_exists, db_err = _psql_as_postgres(db_exists_sql)
        if db_rc != 0:
            raise RuntimeError(f"Failed to query PostgreSQL databases: {db_err}")
        if db_exists.strip() != "1":
            create_db_sql = f"CREATE DATABASE {quoted_name_ident} OWNER {quoted_user_ident};"
            create_db_rc, _out, create_db_err = _psql_as_postgres(create_db_sql)
            if create_db_rc != 0:
                raise RuntimeError(f"Failed to create database: {create_db_err}")

        for extension_sql in (
            "CREATE EXTENSION IF NOT EXISTS pgcrypto;",
            "CREATE EXTENSION IF NOT EXISTS vector;",
        ):
            ext_rc, _out, ext_err = _psql_as_postgres(extension_sql, db=db_name)
            if ext_rc != 0:
                raise RuntimeError(
                    f"Failed to ensure extension in '{db_name}': {ext_err}"
                )

        env_lines = _load_env_lines(env_path)
        updated_env = _upsert_env_lines(
            env_lines,
            {
                "MOBIUS_STATE_DSN": dsn,
            },
        )
        _write_env_lines(env_path, updated_env)
        _enable_state_in_config(cfg_path)

        if not no_restart and _service_exists(SERVICE_NAME):
            _run_or_fail(
                ["systemctl", "restart", SERVICE_NAME],
                label=f"{SERVICE_NAME} restart",
            )

    except Exception as exc:
        print(f"Bootstrap failed: {exc}")
        return 1

    print("")
    print("Local PostgreSQL bootstrap complete.")
    print(f"- MOBIUS_STATE_DSN written to: {env_path}")
    print(f"- state.enabled ensured in:    {cfg_path}")
    if not no_restart and _service_exists(SERVICE_NAME):
        print(f"- Service restarted:           {SERVICE_NAME}")
    print("")
    return 0


def _raw_repo_path_from_origin_url(origin_url: str | None) -> str | None:
    if not origin_url:
        return None
    value = origin_url.strip()
    if not value:
        return None
    https_match = GITHUB_HTTPS_RE.match(value)
    if https_match:
        return str(https_match.group("path"))
    ssh_match = GITHUB_SSH_RE.match(value)
    if ssh_match:
        return str(ssh_match.group("path"))
    return None


def _detect_origin_url_from_checkout() -> str | None:
    repo_root = Path(__file__).resolve().parents[2]
    git_dir = repo_root / ".git"
    if not git_dir.exists():
        return None
    try:
        completed = subprocess.run(
            ["git", "-C", str(repo_root), "config", "--get", "remote.origin.url"],
            capture_output=True,
            text=True,
            check=False,
        )
    except Exception:
        return None
    if completed.returncode != 0:
        return None
    value = completed.stdout.strip()
    return value or None


def _resolve_update_sources(
    *,
    explicit_raw_repo_path: str | None,
    explicit_repo_url: str | None,
    explicit_repo_ref: str | None,
) -> tuple[str, str, str]:
    detected_origin = _detect_origin_url_from_checkout()
    detected_raw_path = _raw_repo_path_from_origin_url(detected_origin)

    raw_repo_path = (
        (explicit_raw_repo_path or "").strip()
        or os.getenv("RAW_REPO_PATH", "").strip()
        or detected_raw_path
        or DEFAULT_RAW_REPO_PATH
    )
    repo_url = (
        (explicit_repo_url or "").strip()
        or os.getenv("REPO_URL", "").strip()
        or detected_origin
        or f"https://github.com/{raw_repo_path}.git"
        or DEFAULT_REPO_URL
    )
    repo_ref = (
        (explicit_repo_ref or "").strip()
        or os.getenv("REPO_REF", "").strip()
        or DEFAULT_REPO_REF
    )
    return raw_repo_path, repo_url, repo_ref


def _cmd_update(args: argparse.Namespace) -> int:
    if hasattr(os, "geteuid") and os.geteuid() != 0:
        print("mobius update should be run as root (or with sudo) inside LXC.")
        return 1

    raw_repo_path, repo_url, repo_ref = _resolve_update_sources(
        explicit_raw_repo_path=getattr(args, "raw_repo_path", None),
        explicit_repo_url=getattr(args, "repo_url", None),
        explicit_repo_ref=getattr(args, "repo_ref", None),
    )
    script_url = f"https://raw.githubusercontent.com/{raw_repo_path}/{repo_ref}/ct/mobius.sh"
    command = ["bash", "-c", f"curl -fsSL \"{script_url}\" | bash"]

    print("")
    print("Mobius Update")
    print("=============")
    print(f"Repo URL:      {repo_url}")
    print(f"Repo ref:      {repo_ref}")
    print(f"Raw repo path: {raw_repo_path}")
    print(f"Installer URL: {script_url}")
    print("")
    print(f"Running: {shlex.join(command)}")

    if bool(getattr(args, "dry_run", False)):
        print("Dry run only. No update executed.")
        return 0

    env = os.environ.copy()
    env["REPO_URL"] = repo_url
    env["REPO_REF"] = repo_ref
    env["RAW_REPO_PATH"] = raw_repo_path

    try:
        return subprocess.run(command, env=env, check=False).returncode
    except FileNotFoundError as exc:
        print(f"Update failed: missing command ({exc}).")
        return 127


def _print_runtime_paths(
    *,
    cfg_path: Path,
    env_path: Path,
    config: AppConfig | None,
    config_error: str | None,
) -> None:
    if config is not None:
        prompts_dir = config.specialists.prompts_directory
        log_dir = config.logging.directory
        log_file = config.logging.directory / config.logging.filename
        state_dir = config.state.projection.output_directory
    else:
        prompts_dir = Path("/etc/mobius/system_prompts")
        log_dir = Path("/var/log/mobius")
        log_file = log_dir / "mobius.log"
        state_dir = Path("/var/lib/mobius/state")

    print("")
    print("Mobius Paths")
    print("============")
    print(f"Config YAML:        {cfg_path} ({_path_state(cfg_path)})")
    print(f"Env file:           {env_path} ({_path_state(env_path)})")
    print(f"System prompts dir: {prompts_dir} ({_path_state(prompts_dir)})")
    print(f"Logs directory:     {log_dir} ({_path_state(log_dir)})")
    print(f"Log file:           {log_file} ({_path_state(log_file)})")
    print(f"State directory:    {state_dir} ({_path_state(state_dir)})")
    if config_error:
        print("")
        print(f"Config load note: {config_error}")
    print("")


def _cmd_version() -> int:
    print(f"mobius {__version__}")
    return 0


def _cmd_paths(args: argparse.Namespace) -> int:
    cfg_path = _resolve_config_path(getattr(args, "config_path", None))
    env_path = _resolve_env_path(getattr(args, "env_file", None))
    config, error = _try_load_config(cfg_path)
    _print_runtime_paths(
        cfg_path=cfg_path,
        env_path=env_path,
        config=config,
        config_error=error,
    )
    return 0


def _cmd_diagnostics(args: argparse.Namespace) -> int:
    cfg_path = _resolve_config_path(getattr(args, "config_path", None))
    config, error = _try_load_config(cfg_path)

    host = config.server.host if config is not None else "0.0.0.0"
    port = config.server.port if config is not None else 8080
    ip = _detect_local_ip()
    public_host = ip if host in {"0.0.0.0", "::", ""} else host

    local_base = f"http://127.0.0.1:{port}"
    public_base = f"http://{public_host}:{port}"

    print("")
    print("Mobius Diagnostics")
    print("==================")
    print(f"Detected IP: {ip}")
    print(f"Configured bind host: {host}")
    print(f"Configured port: {port}")
    if error:
        print(f"Config load note: {error}")
    print("")
    print("Quick checks:")
    print(f"  curl -sS --max-time 3 {local_base}/healthz")
    print(f"  curl -sS --max-time 3 {local_base}/readyz")
    print(f"  curl -sS --max-time 3 {local_base}/diagnostics")
    print("")
    print("From another machine on your LAN:")
    print(f"  curl -sS --max-time 5 {public_base}/healthz")
    print(f"  curl -sS --max-time 5 {public_base}/readyz")
    print(f"  curl -sS --max-time 5 {public_base}/diagnostics")
    print("")
    return 0


def _cmd_service(action: str) -> int:
    if action == "status":
        command = ["systemctl", "status", SERVICE_NAME, "--no-pager", "-l"]
    else:
        command = ["systemctl", action, SERVICE_NAME]

    print(f"Running: {shlex.join(command)}")
    rc = _run_command(command)
    if rc != 0 and action in {"start", "stop", "restart"}:
        print("Tip: this may require elevated permissions (try with sudo).")
    return rc


def _cmd_logs(args: argparse.Namespace) -> int:
    follow = bool(getattr(args, "follow", False))
    lines = int(getattr(args, "lines", 200))
    use_file = bool(getattr(args, "file", False))
    use_journal = bool(getattr(args, "journal", False))
    if use_file and use_journal:
        print("Choose only one logs source: --file or --journal.")
        return 2

    if use_file:
        cfg_path = _resolve_config_path(getattr(args, "config_path", None))
        config, _ = _try_load_config(cfg_path)
        log_file = (
            config.logging.directory / config.logging.filename
            if config is not None
            else Path("/var/log/mobius/mobius.log")
        )
        command = ["tail", "-n", str(lines)]
        if follow:
            command.append("-f")
        command.append(str(log_file))
        print(f"Running: {shlex.join(command)}")
        return _run_command(command)

    command = ["journalctl", "-u", SERVICE_NAME, "-n", str(lines), "--no-pager"]
    if follow:
        command.append("-f")
    print(f"Running: {shlex.join(command)}")
    rc = _run_command(command)
    if rc == 127:
        print("journalctl is unavailable; try 'mobius logs --file' instead.")
    return rc


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="mobius")
    parser.add_argument(
        "--version",
        action="version",
        version=f"%(prog)s {__version__}",
    )
    subparsers = parser.add_subparsers(dest="command")

    serve_parser = subparsers.add_parser("serve", help="Run API server")
    serve_parser.add_argument("--config", dest="config_path", default=None)
    serve_parser.add_argument("--host", default=None)
    serve_parser.add_argument("--port", type=int, default=None)

    onboarding_parser = subparsers.add_parser(
        "onboarding", help="Interactive setup for env/config"
    )
    onboarding_parser.add_argument("--config", dest="config_path", default=None)
    onboarding_parser.add_argument("--env-file", dest="env_file", default=None)
    onboarding_parser.add_argument(
        "--force",
        action="store_true",
        help="Overwrite existing onboarding values without confirmation",
    )

    subparsers.add_parser("version", help="Print Mobius version")

    paths_parser = subparsers.add_parser("paths", help="Print runtime file paths")
    paths_parser.add_argument("--config", dest="config_path", default=None)
    paths_parser.add_argument("--env-file", dest="env_file", default=None)

    diagnostics_parser = subparsers.add_parser(
        "diagnostics", help="Print diagnostics curl commands and detected IP"
    )
    diagnostics_parser.add_argument("--config", dest="config_path", default=None)

    subparsers.add_parser("start", help="Start systemd service")
    subparsers.add_parser("stop", help="Stop systemd service")
    subparsers.add_parser("restart", help="Restart systemd service")
    subparsers.add_parser("status", help="Show systemd service status")

    logs_parser = subparsers.add_parser(
        "logs", help="Show service logs (journalctl by default)"
    )
    logs_parser.add_argument("--follow", action="store_true", help="Follow live logs")
    logs_parser.add_argument(
        "--lines", type=int, default=200, help="Number of log lines to show"
    )
    logs_parser.add_argument(
        "--journal",
        action="store_true",
        help="Read logs from systemd journal (default)",
    )
    logs_parser.add_argument("--file", action="store_true", help="Read logs from file")
    logs_parser.add_argument("--config", dest="config_path", default=None)

    update_parser = subparsers.add_parser(
        "update", help="Update Mobius from inside LXC"
    )
    update_parser.add_argument(
        "--repo-ref",
        default=None,
        help="Git ref (branch/tag) to update from (default: master)",
    )
    update_parser.add_argument(
        "--repo-url",
        default=None,
        help="Git repository URL used by updater",
    )
    update_parser.add_argument(
        "--raw-repo-path",
        default=None,
        help="GitHub owner/repo path for raw installer URL",
    )
    update_parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Print resolved update command without running it",
    )

    db_parser = subparsers.add_parser("db", help="Database utilities")
    db_subparsers = db_parser.add_subparsers(dest="db_command")
    db_bootstrap_parser = db_subparsers.add_parser(
        "bootstrap-local",
        help="Bootstrap local PostgreSQL + pgvector and enable state mode",
    )
    db_bootstrap_parser.add_argument("--config", dest="config_path", default=None)
    db_bootstrap_parser.add_argument("--env-file", dest="env_file", default=None)
    db_bootstrap_parser.add_argument("--db-name", default="mobius")
    db_bootstrap_parser.add_argument("--db-user", default="mobius")
    db_bootstrap_parser.add_argument("--db-password", default=None)
    db_bootstrap_parser.add_argument("--db-host", default="127.0.0.1")
    db_bootstrap_parser.add_argument("--db-port", type=int, default=5432)
    db_bootstrap_parser.add_argument(
        "--skip-install",
        action="store_true",
        help="Skip apt installation steps for PostgreSQL packages",
    )
    db_bootstrap_parser.add_argument(
        "--no-restart",
        action="store_true",
        help="Do not restart mobius service after bootstrap",
    )
    db_bootstrap_parser.add_argument(
        "--yes",
        action="store_true",
        help="Run non-interactively without confirmation prompt",
    )
    db_bootstrap_parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Print plan only, no changes applied",
    )
    return parser


def main() -> None:
    parser = _build_parser()
    args = parser.parse_args()
    command = args.command or "serve"

    if command == "onboarding":
        run_onboarding(
            config_path=getattr(args, "config_path", None),
            env_file=getattr(args, "env_file", None),
            force=bool(getattr(args, "force", False)),
        )
        return
    if command == "version":
        raise SystemExit(_cmd_version())
    if command == "paths":
        raise SystemExit(_cmd_paths(args))
    if command == "diagnostics":
        raise SystemExit(_cmd_diagnostics(args))
    if command == "start":
        raise SystemExit(_cmd_service("start"))
    if command == "stop":
        raise SystemExit(_cmd_service("stop"))
    if command == "restart":
        raise SystemExit(_cmd_service("restart"))
    if command == "status":
        raise SystemExit(_cmd_service("status"))
    if command == "logs":
        raise SystemExit(_cmd_logs(args))
    if command == "update":
        raise SystemExit(_cmd_update(args))
    if command == "db":
        db_command = getattr(args, "db_command", None)
        if db_command == "bootstrap-local":
            raise SystemExit(_cmd_db_bootstrap_local(args))
        print("Usage: mobius db bootstrap-local [options]")
        raise SystemExit(2)

    # When no subcommand is passed, argparse does not populate serve-only fields
    # like host/port. Fall back to config values in that case.
    config = load_config(getattr(args, "config_path", None))
    host = getattr(args, "host", None) or config.server.host
    port = getattr(args, "port", None) or config.server.port
    uvicorn.run(
        "mobius.main:app",
        host=host,
        port=port,
        reload=False,
    )


if __name__ == "__main__":
    main()
