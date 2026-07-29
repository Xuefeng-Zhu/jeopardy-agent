"""Agent Jeopardy competition harness.

The controller polls the full stacked board, solves tiles concurrently with a
bounded Anthropic tool loop, verifies candidates, and sends every answer through
one globally rate-limited submission lane.

Local one-pass smoke:

    RUN_ONCE=1 MAX_TILES=3 python main.py

Inspect candidates without submitting:

    RUN_ONCE=1 MAX_TILES=3 DRY_RUN=1 VERBOSE=1 python main.py

The hosted agent should use the defaults: it remains alive, follows phase
changes, and attempts every open tile.
"""

from __future__ import annotations

import asyncio

import jeopardy as jp
from orchestrator import AgentConfig, AgentController


async def run() -> None:
    config = AgentConfig.from_env()
    jp.log(
        "starting agent: "
        f"workers={config.model_workers} max_turns={config.max_turns} "
        f"max_tiles={config.max_tiles or 'all'} "
        f"run_once={config.run_once} dry_run={config.dry_run}"
    )
    controller = AgentController(jp, config)
    await controller.run()


def main() -> None:
    try:
        asyncio.run(run())
    except jp.AuthError as exc:
        raise SystemExit(f"[auth] {exc}") from exc
    except KeyboardInterrupt:
        jp.log("stopped")


if __name__ == "__main__":
    main()
