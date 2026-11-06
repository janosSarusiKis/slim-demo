"""Sprayer: the spray rig that applies treatments in a greenhouse.

It takes orders, then drives the greenhouse's spray nozzles.
"""

import asyncio

from farm.kit import FarmAgent, Payload, now, setup_logging

SPRAY_SECONDS = 8


async def main() -> None:
    setup_logging()
    jobs: list[Payload] = []
    busy_until = 0.0

    async def handle(payload: Payload) -> Payload:
        nonlocal busy_until
        match payload.get("action"):
            case "status":
                return {
                    "kind": "sprayer",
                    "busy_for": max(0, round(busy_until - now())),
                    "last_job": jobs[-1] if jobs else None,
                    "jobs": len(jobs),
                }
            case "spray":
                gh = payload["greenhouse"]
                product = payload.get("product", "neem oil")
                by = payload.get("ordered_by", "unknown")
                if now() < busy_until:
                    return {"ok": False, "say": "busy with another job"}
                answer = await agent.call(
                    gh, {"action": "spray", "product": product}, timeout=3
                )
                busy_until = now() + SPRAY_SECONDS
                jobs.append(
                    {
                        "greenhouse": gh,
                        "product": product,
                        "ordered_by": by,
                        "at": now(),
                    }
                )
                agent.notify(f"spraying {gh.upper()} with {product}", to=gh)
                return {
                    "ok": True,
                    "say": f"spraying {gh.upper()}: {answer.get('say')}",
                }
            case other:
                raise ValueError(f"unknown action {other!r}")

    agent = FarmAgent("sprayer", "Sprayer", handle, location="farm shed")
    await agent.start()
    await agent.run_until_stopped()


if __name__ == "__main__":
    asyncio.run(main())
