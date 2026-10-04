"""Standalone entry point, same output as ``hermes fleet-policy``::

    python -m fleet_policy status --json            # pip-installed package
    python /path/to/plugins/fleet-policy status --json   # plugin folder (no pip)

Run it with the Python of the Hermes venv (it reads the config through ``hermes_cli``). Needed
where Hermes does not wire plugin CLI commands, e.g. ``plugins.isolation: host`` on recent
Hermes, which skips ``register_cli_command``.
"""
from __future__ import annotations

import argparse
import importlib.util
import sys
from pathlib import Path
from typing import Any


def _package():
    if __package__:  # python -m fleet_policy
        import fleet_policy

        return fleet_policy
    here = Path(__file__).resolve().parent  # python <plugin-dir>: folder name may hold a hyphen
    spec = importlib.util.spec_from_file_location(
        "fleet_policy", here / "__init__.py", submodule_search_locations=[str(here)])
    module = importlib.util.module_from_spec(spec)
    sys.modules["fleet_policy"] = module
    spec.loader.exec_module(module)
    return module


def _plugin_setting(key: str, default: Any = None) -> Any:
    """``plugins.entries.fleet-policy.settings.<key>`` from the effective config (best effort)."""
    try:
        from hermes_cli.config import load_config

        entries = (load_config().get("plugins") or {}).get("entries") or {}
        settings = (entries.get("fleet-policy") or {}).get("settings") or {}
        return settings.get(key, default)
    except Exception:
        return default


def main(argv=None) -> int:
    pkg = _package()
    from fleet_policy import cli  # resolved through sys.modules in both launch modes

    parser = argparse.ArgumentParser(prog="fleet-policy", description=pkg.__doc__.splitlines()[0])
    cli.setup_parser(parser)
    args = parser.parse_args(argv)
    return cli.make_handler(_plugin_setting)(args)


if __name__ == "__main__":
    sys.exit(main())
