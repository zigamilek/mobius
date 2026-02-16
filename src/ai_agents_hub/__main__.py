from __future__ import annotations

import argparse

import uvicorn

from ai_agents_hub.config import load_config
from ai_agents_hub.onboarding import run_onboarding


def main() -> None:
    parser = argparse.ArgumentParser(prog="ai-agents-hub")
    subparsers = parser.add_subparsers(dest="command")

    serve_parser = subparsers.add_parser("serve", help="Run API server")
    serve_parser.add_argument("--config", dest="config_path", default=None)
    serve_parser.add_argument("--host", default=None)
    serve_parser.add_argument("--port", type=int, default=None)

    onboard_parser = subparsers.add_parser(
        "onboard", help="Interactive setup for env/config"
    )
    onboard_parser.add_argument("--config", dest="config_path", default=None)
    onboard_parser.add_argument("--env-file", dest="env_file", default=None)

    args = parser.parse_args()
    command = args.command or "serve"

    if command == "onboard":
        run_onboarding(config_path=args.config_path, env_file=args.env_file)
        return

    config = load_config(getattr(args, "config_path", None))
    host = args.host or config.server.host
    port = args.port or config.server.port
    uvicorn.run(
        "ai_agents_hub.main:app",
        host=host,
        port=port,
        reload=False,
    )


if __name__ == "__main__":
    main()
