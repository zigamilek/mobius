from __future__ import annotations

import argparse
import os
import re
import shlex
import socket
import subprocess
from pathlib import Path

import uvicorn

from mobius import __version__
from mobius.config import AppConfig, load_config
from mobius.onboarding import default_config_path, default_env_path, run_onboarding

SERVICE_NAME = "mobius"
DEFAULT_REPO_URL = "https://github.com/zigamilek/mobius.git"
DEFAULT_RAW_REPO_PATH = "zigamilek/mobius"
DEFAULT_REPO_REF = "master"
GITHUB_HTTPS_RE = re.compile(
    r"^https://github\.com/(?P<path>[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+?)(?:\.git)?/?$"
)
GITHUB_SSH_RE = re.compile(
    r"^git@github\.com:(?P<path>[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+?)(?:\.git)?$"
)
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


def _env_values_from_file(path: Path) -> dict[str, str]:
    values: dict[str, str] = {}
    if not path.exists():
        return values
    for line in path.read_text(encoding="utf-8").splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        match = ENV_LINE_RE.match(stripped)
        if not match:
            continue
        key = match.group("key")
        value = match.group("value")
        if key:
            values[key] = value
    return values


def _try_load_config(
    path: Path, *, env_path: Path | None = None
) -> tuple[AppConfig | None, str | None]:
    injected_keys: list[str] = []
    if env_path is not None:
        for key, value in _env_values_from_file(env_path).items():
            if key in os.environ:
                continue
            os.environ[key] = value
            injected_keys.append(key)
    try:
        return load_config(path), None
    except Exception as exc:
        return None, f"{exc.__class__.__name__}: {exc}"
    finally:
        for key in injected_keys:
            os.environ.pop(key, None)


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
    else:
        prompts_dir = Path("/etc/mobius/system_prompts")
        log_dir = Path("/var/log/mobius")
        log_file = log_dir / "mobius.log"

    print("")
    print("Mobius Paths")
    print("============")
    print(f"Config YAML:        {cfg_path} ({_path_state(cfg_path)})")
    print(f"Env file:           {env_path} ({_path_state(env_path)})")
    print(f"System prompts dir: {prompts_dir} ({_path_state(prompts_dir)})")
    print(f"Logs directory:     {log_dir} ({_path_state(log_dir)})")
    print(f"Log file:           {log_file} ({_path_state(log_file)})")
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
    config, error = _try_load_config(cfg_path, env_path=env_path)
    _print_runtime_paths(
        cfg_path=cfg_path,
        env_path=env_path,
        config=config,
        config_error=error,
    )
    return 0


def _cmd_diagnostics(args: argparse.Namespace) -> int:
    cfg_path = _resolve_config_path(getattr(args, "config_path", None))
    env_path = _resolve_env_path(getattr(args, "env_file", None))
    config, error = _try_load_config(cfg_path, env_path=env_path)

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
        env_path = _resolve_env_path(getattr(args, "env_file", None))
        config, _ = _try_load_config(cfg_path, env_path=env_path)
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
    diagnostics_parser.add_argument("--env-file", dest="env_file", default=None)

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
    logs_parser.add_argument("--env-file", dest="env_file", default=None)

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
