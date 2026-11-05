"""The "home agent": an A2A agent served over SLIM.

It never listens on a port. It connects out to a SLIM node and registers under
a SLIM name (agntcy/demo/home_agent by default). Callers address that name, so
the agent can run on a laptop behind NAT today and in a container tomorrow
without any caller noticing.

Each answer says where it came from and reads data that exists only on the
machine the agent is running on (the notes directory).
"""

import argparse
import asyncio
import contextlib
import logging
import os
import signal
import socket
from pathlib import Path

import slim_bindings
from a2a.helpers import new_task_from_user_message
from a2a.server.agent_execution import AgentExecutor, RequestContext
from a2a.server.events import EventQueue
from a2a.server.request_handlers import DefaultRequestHandler
from a2a.server.tasks import InMemoryTaskStore
from a2a.server.tasks.task_updater import TaskUpdater
from a2a.types import Message, Part, Role
from a2a.types.a2a_pb2 import (
    AgentCapabilities,
    AgentCard,
    AgentInterface,
    AgentSkill,
)
from slima2a import setup_slim_client
from slima2a.handler import SRPCHandler
from slima2a.types.v1.a2a_pb2_slimrpc import add_A2AServiceServicer_to_server

from slim_demo import common

logger = logging.getLogger("home_agent")


class HomeAgent:
    def __init__(self, location: str, notes_dir: Path) -> None:
        self.location = location
        self.notes_dir = notes_dir
        self.host = socket.gethostname()

    def answer(self, question: str) -> str:
        notes = sorted(
            (p for p in self.notes_dir.glob("*") if p.is_file()),
            key=lambda p: p.stat().st_mtime,
        )
        if notes:
            latest = notes[-1].read_text(encoding="utf-8").strip().splitlines()[0]
            local = f'{len(notes)} notes, latest: "{latest}"'
        else:
            local = "no notes"
        return f"{self.location} ({self.host}, Python) · {local}"


class HomeAgentExecutor(AgentExecutor):
    def __init__(self, agent: HomeAgent) -> None:
        self.agent = agent

    async def execute(self, context: RequestContext, event_queue: EventQueue) -> None:
        if not context.message:
            raise ValueError("missing message")

        # The request handler requires a Task before any update events.
        task = context.current_task
        if task is None:
            task = new_task_from_user_message(context.message)
            await event_queue.enqueue_event(task)

        updater = TaskUpdater(
            event_queue=event_queue, task_id=task.id, context_id=task.context_id
        )

        question = "".join(
            part.text
            for part in context.message.parts
            if part.WhichOneof("content") == "text"
        )
        logger.info("question: %s", question)
        result = self.agent.answer(question)

        response = Message(
            role=Role.ROLE_AGENT,
            message_id=context.message.message_id,
            parts=[Part(text=result)],
        )
        await updater.add_artifact(parts=list(response.parts), name="answer")
        await updater.complete(message=response)

    async def cancel(self, context: RequestContext, event_queue: EventQueue) -> None:
        raise NotImplementedError("cancel not supported")


def agent_card(name: str) -> AgentCard:
    return AgentCard(
        name="Home Agent",
        description="Answers questions using data that lives only where it runs",
        # Over SLIM the agent is addressed by its SLIM name, not a URL.
        supported_interfaces=[
            AgentInterface(
                url=f"{common.NAMESPACE}/{common.GROUP}/{name}",
                protocol_binding="slimrpc",
                protocol_version="1.0",
            )
        ],
        version="0.1.0",
        default_input_modes=["text"],
        default_output_modes=["text"],
        capabilities=AgentCapabilities(streaming=False),
        skills=[
            AgentSkill(
                id="local-notes",
                name="local notes",
                description="Summarises the notes stored next to the agent",
                tags=["notes"],
                examples=["What's new at home?"],
            )
        ],
    )


async def main() -> None:
    args = parse_arguments()
    logging.basicConfig(level=args.log_level, format="%(asctime)s %(name)s %(message)s")

    agent = HomeAgent(args.location, Path(args.notes_dir))
    card = agent_card(args.name)
    request_handler = DefaultRequestHandler(
        agent_executor=HomeAgentExecutor(agent),
        task_store=InMemoryTaskStore(),
        agent_card=card,
    )

    service, local_app, local_name, conn_id = await setup_slim_client(
        namespace=common.NAMESPACE,
        group=common.GROUP,
        name=args.name,
        slim_url=args.slim_url,
        secret=common.shared_secret(),
        log_level="warn",
    )

    server = slim_bindings.Server.new_with_connection(local_app, local_name, conn_id)
    add_A2AServiceServicer_to_server(SRPCHandler(card, request_handler), server)

    print(
        f"home agent up: {common.NAMESPACE}/{common.GROUP}/{args.name} "
        f"via {args.slim_url} · location={agent.location} host={agent.host}",
        flush=True,
    )

    # Stop cleanly on Ctrl-C and on `docker stop` (SIGTERM), so open sessions
    # are closed and callers reconnect at once instead of waiting for a timeout.
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, stop.set)

    serving = asyncio.create_task(server.serve_async())
    await stop.wait()

    print("home agent stopping...", flush=True)
    await server.shutdown_async()
    with contextlib.suppress(Exception):
        await local_app.unsubscribe_async(local_name, conn_id)
    service.disconnect(conn_id)
    with contextlib.suppress(asyncio.CancelledError, Exception):
        await asyncio.wait_for(serving, timeout=2)


def parse_arguments() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--name", default=common.AGENT_NAME)
    parser.add_argument("--slim-url", default=common.slim_url())
    parser.add_argument(
        "--location",
        default=os.environ.get("AGENT_LOCATION", "laptop"),
        help="Label shown in every answer, e.g. laptop or cloud",
    )
    parser.add_argument(
        "--notes-dir",
        default=os.environ.get("NOTES_DIR", "notes/laptop"),
        help="Directory with local data the agent reports on",
    )
    parser.add_argument("--log-level", default="WARNING")
    return parser.parse_args()


if __name__ == "__main__":
    asyncio.run(main())
