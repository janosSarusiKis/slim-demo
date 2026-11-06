"""Farm assistant: the farmer's dashboard, served on http://localhost:8080.

It is a SLIM agent like the others. It asks every agent for its status once a
second, sends the farmer's button presses to the right agent, and collects the
events the other agents report into a live conversation log.
"""

import asyncio
import contextlib
import json
import os
import time
from collections import deque
from pathlib import Path

import uvicorn
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import FileResponse, JSONResponse, StreamingResponse
from starlette.routing import Route

from farm.kit import AGENTS, FarmAgent, Payload, now, setup_logging

SHOWN = [a for a in AGENTS if a != "assistant"]
POLL_EVERY = 1.0
STATIC = Path(__file__).parent / "static"

ACTIONS = {  # what the buttons may ask, and how the log describes it
    "water": "water zone A for {seconds} s",
    "release_bugs": "release whiteflies (demo)",
}


class QuietServer(uvicorn.Server):
    """Leave Ctrl-C to FarmAgent, which shuts SLIM down cleanly."""

    def capture_signals(self) -> contextlib.AbstractContextManager[None]:
        return contextlib.nullcontext()


async def main() -> None:
    setup_logging()
    agents: dict[str, Payload] = {a: {"online": False, "status": None} for a in SHOWN}
    log: deque[Payload] = deque(maxlen=60)

    def add_log(
        sender: str, text: str, to: str | None = None, level: str = "info"
    ) -> None:
        log.appendleft(
            {"t": now(), "from": sender, "to": to, "text": text, "level": level}
        )

    async def handle(payload: Payload) -> Payload:
        match payload.get("action"):
            case "event":
                add_log(
                    payload["from"],
                    payload["text"],
                    payload.get("to"),
                    payload.get("level", "info"),
                )
                return {"ok": True}
            case "status":
                return {"kind": "assistant", "watching": SHOWN}
            case other:
                raise ValueError(f"unknown action {other!r}")

    agent = FarmAgent("assistant", "Farm assistant", handle)
    await agent.start()

    async def poll_one(target: str) -> None:
        started = time.perf_counter()
        try:
            status = await agent.call(target, {"action": "status"}, timeout=1.5)
        except Exception:
            if agents[target]["online"]:
                add_log(
                    "assistant",
                    f"{target.upper()} stopped answering",
                    to=target,
                    level="error",
                )
            agents[target] = {**agents[target], "online": False, "ms": None}
            return
        ms = round((time.perf_counter() - started) * 1000)
        before = agents[target]
        if not before["online"] and before["status"] is not None:
            add_log(
                "assistant",
                f"{target.upper()} answering again from {status.get('location')}",
                to=target,
            )
        elif before["status"] and before["status"].get("host") != status.get("host"):
            add_log(
                "assistant",
                f"{target.upper()} is now answered from {status.get('location')}",
                to=target,
                level="warn",
            )
        agents[target] = {"online": True, "ms": ms, "status": status, "seen": now()}

    async def poll() -> None:
        while True:
            await asyncio.gather(*(poll_one(a) for a in SHOWN))
            await asyncio.sleep(POLL_EVERY)

    async def index(_: Request) -> FileResponse:
        return FileResponse(STATIC / "index.html")

    async def stream(request: Request) -> StreamingResponse:
        async def events():
            while not await request.is_disconnected():
                data = json.dumps({"agents": agents, "log": list(log), "now": now()})
                yield f"data: {data}\n\n"
                await asyncio.sleep(0.5)

        return StreamingResponse(events(), media_type="text/event-stream")

    async def action(request: Request) -> JSONResponse:
        body = await request.json()
        target, name = body.get("target"), body.get("action")
        if target not in SHOWN or name not in ACTIONS:
            return JSONResponse({"ok": False, "error": "unknown target or action"}, 400)
        payload = {k: v for k, v in body.items() if k != "target"} | {"by": "assistant"}
        add_log("assistant", ACTIONS[name].format(**payload), to=target)
        try:
            answer = await agent.call(target, payload, timeout=3)
        except Exception as e:
            add_log(
                "assistant",
                f"{target.upper()} did not answer ({type(e).__name__})",
                to=target,
                level="error",
            )
            return JSONResponse({"ok": False, "error": type(e).__name__}, 504)
        if answer.get("say"):
            add_log(target, answer["say"], to="assistant")
        return JSONResponse(answer)

    app = Starlette(
        routes=[
            Route("/", index),
            Route("/api/stream", stream),
            Route("/api/action", action, methods=["POST"]),
        ]
    )
    port = int(os.environ.get("DASHBOARD_PORT", "8080"))
    web = QuietServer(
        uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning")
    )
    print(f"dashboard: http://localhost:{port}", flush=True)

    async def serve_web() -> None:
        await web.serve()

    try:
        await agent.run_until_stopped(poll(), serve_web())
    finally:
        web.should_exit = True


if __name__ == "__main__":
    asyncio.run(main())
