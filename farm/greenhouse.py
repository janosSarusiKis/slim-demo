"""Greenhouse agent: the small computer wired to a greenhouse's sensors and valves.

The sensors are simulated: soil dries out over time, watering wets it, and
whiteflies multiply until the greenhouse is sprayed.

Run with --role backup to play the spare box that takes over when the primary
goes offline. The backup starts from the last readings the guardian saved.
"""

import argparse
import asyncio
import contextlib
import json
import os
import random
from pathlib import Path

from farm.kit import FarmAgent, Payload, now, setup_logging

TICK = 0.5
DRY_BELOW = 25.0
BUG_THRESHOLD = 30
PRIMARY_HEARTBEAT = 1.0


class Greenhouse:
    def __init__(self, gh_id: str, role: str, state_file: Path | None) -> None:
        self.id = gh_id
        self.role = role
        self.moisture = 21.0 if gh_id == "gh1" else 42.0  # GH1 starts dry
        self.base_temp = 24.0 if gh_id == "gh1" else 22.0
        self.temp = self.base_temp
        self.whiteflies = 3.0
        self.watering_until = 0.0
        self.spraying_until = 0.0
        self.spray_product: str | None = None
        self.took_over_at: float | None = None
        self.data_as_of: float | None = None

        if role == "backup":
            self.took_over_at = now()
            if state_file and state_file.exists():
                saved = json.loads(state_file.read_text())
                self.moisture = saved["moisture"]
                self.temp = saved["temp"]
                self.whiteflies = saved["whiteflies"]
                self.data_as_of = saved["saved_at"]

    def tick(self, dt: float) -> None:
        t = now()
        if t < self.watering_until:
            self.moisture = min(65.0, self.moisture + 2.5 * dt)
        else:
            self.moisture = max(5.0, self.moisture - 0.08 * dt)
        self.temp += (self.base_temp - self.temp) * 0.1 + random.uniform(-0.15, 0.15)

        if t < self.spraying_until:
            self.whiteflies = max(2.0, self.whiteflies * (1 - 0.3 * dt))
        elif self.whiteflies >= BUG_THRESHOLD:
            self.whiteflies += 0.8 * dt  # an untreated infestation keeps growing
        else:
            self.whiteflies = min(
                5.0, max(1.0, self.whiteflies + random.uniform(-0.3, 0.3))
            )

    def status(self) -> Payload:
        t = now()
        return {
            "kind": "greenhouse",
            "role": self.role,
            "moisture": round(self.moisture, 1),
            "dry": self.moisture < DRY_BELOW,
            "temp": round(self.temp, 1),
            "whiteflies": round(self.whiteflies),
            "watering_for": max(0, round(self.watering_until - t)),
            "spraying_for": max(0, round(self.spraying_until - t)),
            "spray_product": self.spray_product,
            "took_over_at": self.took_over_at,
            "data_as_of": self.data_as_of,
        }


async def main() -> None:
    args = parse_arguments()
    setup_logging()
    gh = Greenhouse(
        args.id, args.role, Path(args.state_file) if args.state_file else None
    )
    title = f"Greenhouse {args.id[-1]}" + (
        " (backup box)" if args.role == "backup" else ""
    )

    async def handle(payload: Payload) -> Payload:
        match payload.get("action"):
            case "status":
                return gh.status()
            case "water":
                seconds = int(payload.get("seconds", 10))
                gh.watering_until = now() + seconds
                return {"ok": True, "say": f"valve A open for {seconds} s"}
            case "release_bugs":
                gh.whiteflies = max(gh.whiteflies, 62.0)
                return {"ok": True, "say": "whiteflies released 🪲"}
            case "spray":
                product = payload.get("product", "neem oil")
                gh.spray_product = product
                gh.spraying_until = now() + 8
                return {"ok": True, "say": f"spray nozzles on: {product}"}
            case other:
                raise ValueError(f"unknown action {other!r}")

    agent = FarmAgent(args.id, title, handle)
    await agent.start()
    if args.role == "backup":
        agent.notify("backup box took over", level="warn")
    else:
        agent.spawn(announce_primary(agent))

    async def simulate() -> None:
        while True:
            await asyncio.sleep(TICK)
            gh.tick(TICK)

    await agent.run_until_stopped(simulate())


async def announce_primary(agent: FarmAgent) -> None:
    """Keep telling the guardian the real box is alive.

    The guardian retires a running backup box when it hears this, and holds off
    a takeover while the heartbeat is fresh, so a quick restart of the main box
    never ends with both boxes running under the same name.
    """
    while True:
        with contextlib.suppress(Exception):
            await agent.call(
                "guardian", {"action": "primary_alive", "greenhouse": agent.id}
            )
        await asyncio.sleep(PRIMARY_HEARTBEAT)


def parse_arguments() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "--id", default=os.environ.get("GREENHOUSE", "gh1"), choices=["gh1", "gh2"]
    )
    parser.add_argument(
        "--role",
        default=os.environ.get("ROLE", "primary"),
        choices=["primary", "backup"],
    )
    parser.add_argument("--state-file", default=os.environ.get("STATE_FILE"))
    return parser.parse_args()


if __name__ == "__main__":
    asyncio.run(main())
