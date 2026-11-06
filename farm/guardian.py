"""Guardian: makes sure every greenhouse keeps answering.

It checks each greenhouse every second and saves its latest readings. When a
greenhouse misses two checks in a row, the guardian starts that greenhouse's
backup box under the same SLIM name, so every caller reaches the backup
without changing anything. When the real box comes back and says so, the
guardian stops the backup again.

The backup box is a container started with `docker compose`, so the guardian
runs on the machine that hosts it.
"""

import asyncio
import json
import os
from pathlib import Path

from farm.kit import FarmAgent, Payload, now, setup_logging

GREENHOUSES = ["gh1", "gh2"]
BACKUPS = {"gh1": "gh1-backup"}  # greenhouse -> compose service of its backup box
MISSES_BEFORE_TAKEOVER = 2
CHECK_EVERY = 1.0
HEARTBEAT_FRESH = 2.5  # seconds a main box's "I'm alive" counts as current

STATE_DIR = Path(os.environ.get("STATE_DIR", "state"))
COMPOSE_DIR = os.environ.get("COMPOSE_DIR", ".")


async def compose(*args: str) -> bool:
    proc = await asyncio.create_subprocess_exec(
        "docker",
        "compose",
        *args,
        cwd=COMPOSE_DIR,
        stdout=asyncio.subprocess.DEVNULL,
        stderr=asyncio.subprocess.PIPE,
    )
    _, err = await proc.communicate()
    if proc.returncode != 0:
        print(
            f"docker compose {' '.join(args)} failed: {err.decode().strip()}",
            flush=True,
        )
    return proc.returncode == 0


async def compose_running(service: str) -> bool:
    proc = await asyncio.create_subprocess_exec(
        "docker",
        "compose",
        "ps",
        "-q",
        "--status",
        "running",
        service,
        cwd=COMPOSE_DIR,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.DEVNULL,
    )
    out, _ = await proc.communicate()
    return bool(out.strip())


async def main() -> None:
    setup_logging()
    STATE_DIR.mkdir(exist_ok=True)
    misses = dict.fromkeys(GREENHOUSES, 0)
    backup_running = dict.fromkeys(GREENHOUSES, False)
    health: dict[str, str] = dict.fromkeys(GREENHOUSES, "waiting")
    takeovers: list[Payload] = []
    primary_alive_at = dict.fromkeys(GREENHOUSES, 0.0)
    busy: set[str] = set()  # greenhouses with a compose command in flight

    # A backup box may still be running from before the guardian (re)started.
    for gh, service in BACKUPS.items():
        backup_running[gh] = await compose_running(service)

    def primary_alive(gh: str) -> bool:
        return now() - primary_alive_at[gh] < HEARTBEAT_FRESH

    async def retire_backup(gh: str) -> None:
        busy.add(gh)
        try:
            agent.notify(f"{gh.upper()} is back online, stopping its backup box", to=gh)
            if await compose("stop", BACKUPS[gh]):
                backup_running[gh] = False
                agent.notify(f"{gh.upper()} control handed back to the real box")
        finally:
            busy.discard(gh)

    async def handle(payload: Payload) -> Payload:
        match payload.get("action"):
            case "status":
                return {
                    "kind": "guardian",
                    "health": health,
                    "backups": {gh: BACKUPS.get(gh) for gh in GREENHOUSES},
                    "backup_running": backup_running,
                    "takeovers": takeovers[-5:],
                }
            case "primary_alive":
                gh = payload["greenhouse"]
                primary_alive_at[gh] = now()
                if backup_running.get(gh) and gh not in busy:
                    agent.spawn(retire_backup(gh))
                return {"ok": True}
            case other:
                raise ValueError(f"unknown action {other!r}")

    agent = FarmAgent("guardian", "Guardian", handle)
    await agent.start()

    async def take_over(gh: str) -> None:
        busy.add(gh)
        backup_running[gh] = True
        health[gh] = "starting backup box"
        agent.notify(
            f"{gh.upper()} missed {MISSES_BEFORE_TAKEOVER} checks, starting its backup box",
            to=gh,
            level="warn",
        )
        try:
            if await compose("up", "-d", BACKUPS[gh]):
                takeovers.append({"greenhouse": gh, "at": now()})
            else:
                backup_running[gh] = False
        finally:
            busy.discard(gh)

    async def watch() -> None:
        while True:
            for gh in GREENHOUSES:
                try:
                    status = await agent.call(gh, {"action": "status"}, timeout=1.5)
                except Exception:
                    misses[gh] += 1
                    if backup_running[gh]:
                        health[gh] = "waiting for backup box"
                    elif primary_alive(gh):
                        health[gh] = "main box restarting"
                    elif (
                        misses[gh] >= MISSES_BEFORE_TAKEOVER
                        and gh in BACKUPS
                        and gh not in busy
                    ):
                        agent.spawn(take_over(gh))
                    else:
                        health[gh] = f"missed {misses[gh]} check(s)"
                    continue
                misses[gh] = 0
                if status.get("role") == "backup":
                    health[gh] = "running on backup box"
                else:
                    health[gh] = "healthy"
                    # The backup box starts from these readings.
                    snapshot = {
                        k: status[k] for k in ("moisture", "temp", "whiteflies")
                    }
                    snapshot["saved_at"] = now()
                    (STATE_DIR / f"{gh}.json").write_text(json.dumps(snapshot))
            await asyncio.sleep(CHECK_EVERY)

    await agent.run_until_stopped(watch())


if __name__ == "__main__":
    asyncio.run(main())
