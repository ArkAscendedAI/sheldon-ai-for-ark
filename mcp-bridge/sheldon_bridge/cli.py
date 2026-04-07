"""Command-line interface for the Sheldon Bridge."""

from __future__ import annotations

import argparse
import asyncio
import logging
import sys
from pathlib import Path

from dotenv import load_dotenv


def main():
    parser = argparse.ArgumentParser(
        prog="sheldon-bridge",
        description="Sheldon AI Bridge — LLM-powered assistant for ARK: Survival Ascended",
    )
    subparsers = parser.add_subparsers(dest="command")

    # init command
    init_parser = subparsers.add_parser("init", help="Create a new config file")
    init_parser.add_argument(
        "--path", default="config.json", help="Config file path (default: config.json)"
    )

    # run command
    run_parser = subparsers.add_parser("run", help="Start the bridge server")
    run_parser.add_argument(
        "--config", default="config.json", help="Config file path (default: config.json)"
    )
    run_parser.add_argument(
        "--env-file", default=None, help="Path to .env file for API keys"
    )
    run_parser.add_argument(
        "--verbose", "-v", action="store_true", help="Enable debug logging"
    )

    # secret command
    secret_parser = subparsers.add_parser("secret", help="Generate a new shared secret")

    args = parser.parse_args()

    if args.command == "init":
        from sheldon_bridge.config import initialize_config
        initialize_config(args.path)

    elif args.command == "run":
        # Load .env file if specified
        if args.env_file:
            load_dotenv(args.env_file)
        else:
            # Try common locations
            for env_path in [".env", Path.home() / ".sheldon-bridge.env"]:
                if Path(env_path).exists():
                    load_dotenv(env_path)
                    break

        # Configure logging
        log_level = logging.DEBUG if args.verbose else logging.INFO
        logging.basicConfig(
            level=log_level,
            format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
            datefmt="%Y-%m-%d %H:%M:%S",
        )

        # Load config
        from sheldon_bridge.config import load_config
        config = load_config(args.config)

        # Load knowledge base
        from sheldon_bridge.tools.knowledge import load_data
        load_data(config.data_dirs)

        # Run server
        from sheldon_bridge.server import run_server
        asyncio.run(run_server(config))

    elif args.command == "secret":
        from sheldon_bridge.auth import TokenAuthenticator
        secret = TokenAuthenticator.generate_secret()
        print(f"Generated secret: {secret}")
        print("Add this to both your bridge config.json and your mod's GameUserSettings.ini")

    else:
        parser.print_help()
        sys.exit(1)


if __name__ == "__main__":
    main()
