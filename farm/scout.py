"""Pest scout: watches the sticky traps in every greenhouse.

When a greenhouse's whitefly count crosses the threshold, it asks the
agronomist in town what to do. The agronomist then orders the sprayer.
"""

import asyncio

from farm.greenhouse import BUG_THRESHOLD
from farm.kit import FarmAgent, Payload, setup_logging

GREENHOUSES = ["gh1", "gh2"]
CHECK_EVERY = 1.5


async def main() -> None:
    setup_logging()
    traps: dict[str, int | None] = dict.fromkeys(GREENHOUSES)
    open_cases: dict[str, str] = {}

    async def handle(payload: Payload) -> Payload:
        if payload.get("action") != "status":
            raise ValueError("the scout only reports its status")
        return {
            "kind": "scout",
            "traps": traps,
            "threshold": BUG_THRESHOLD,
            "open_cases": open_cases,
        }

    agent = FarmAgent("scout", "Pest scout", handle, location="greenhouse camera")
    await agent.start()

    async def ask_agronomist(gh: str, count: int) -> None:
        agent.notify(
            f"{count} whiteflies in {gh.upper()}, what should we do?", to="agronomist"
        )
        try:
            advice = await agent.call(
                "agronomist",
                {
                    "action": "advise",
                    "greenhouse": gh,
                    "pest": "whitefly",
                    "count": count,
                },
                timeout=8,
            )
        except Exception as e:
            open_cases.pop(gh, None)  # retry on the next check
            agent.notify(f"agronomist unreachable ({type(e).__name__})", level="warn")
            return
        if not advice.get("ok"):
            # The treatment was not ordered (e.g. the sprayer was restarting).
            # Closing the case makes the next check ask again.
            open_cases.pop(gh, None)
            return
        open_cases[gh] = advice.get("advice", "treatment ordered")

    async def watch() -> None:
        while True:
            for gh in GREENHOUSES:
                try:
                    status = await agent.call(gh, {"action": "status"}, timeout=1.5)
                except Exception:
                    traps[gh] = None
                    continue
                count = status["whiteflies"]
                traps[gh] = count
                if count >= BUG_THRESHOLD and gh not in open_cases:
                    open_cases[gh] = "asking the agronomist..."
                    agent.spawn(ask_agronomist(gh, count))
                elif count < BUG_THRESHOLD / 3 and gh in open_cases:
                    del open_cases[gh]
                    agent.notify(
                        f"{gh.upper()} traps are clean again ({count} whiteflies)"
                    )
            await asyncio.sleep(CHECK_EVERY)

    await agent.run_until_stopped(watch())


if __name__ == "__main__":
    asyncio.run(main())
