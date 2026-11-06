"""Shared plumbing for the farm agents.

Every agent is one SLIM app that can both serve A2A requests and call other
agents by their SLIM name. Requests and answers are small JSON objects carried
in an A2A text part, e.g. {"action": "water", "seconds": 10}.

Agents also report what they do to the farm assistant (the dashboard) with
notify(), so its live log can show conversations it is not part of.
"""

import asyncio
import contextlib
import json
import logging
import os
import signal
import socket
import time
from collections.abc import Awaitable, Callable
from typing import Any

import slim_bindings
from a2a.client import Client, minimal_agent_card
from a2a.helpers import new_task_from_user_message, new_text_message
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
    SendMessageRequest,
)
from slima2a import setup_slim_client
from slima2a.client_transport import (
    ClientConfig,
    MultiAgentClientFactory,
    slimrpc_channel_factory,
)
from slima2a.handler import SRPCHandler
from slima2a.types.v1.a2a_pb2_slimrpc import add_A2AServiceServicer_to_server

from slim_demo import common

logger = logging.getLogger(__name__)

# Every agent on the farm, by short id, with its SLIM name.
AGENTS = {
    "gh1": "greenvalley/farm/gh1",
    "gh2": "greenvalley/farm/gh2",
    "scout": "greenvalley/farm/scout",
    "agronomist": "greenvalley/town/agronomist",
    "sprayer": "greenvalley/farm/sprayer",
    "guardian": "greenvalley/farm/guardian",
    "assistant": "greenvalley/farm/assistant",
}

Payload = dict[str, Any]
Handler = Callable[[Payload], Awaitable[Payload]]


class _JsonExecutor(AgentExecutor):
    """Decodes the JSON request, runs the agent's handler, encodes the answer."""

    def __init__(self, agent: "FarmAgent") -> None:
        self.agent = agent

    async def execute(self, context: RequestContext, event_queue: EventQueue) -> None:
        if not context.message:
            raise ValueError("missing message")

        task = context.current_task
        if task is None:
            task = new_task_from_user_message(context.message)
            await event_queue.enqueue_event(task)
        updater = TaskUpdater(
            event_queue=event_queue, task_id=task.id, context_id=task.context_id
        )

        text = "".join(
            p.text for p in context.message.parts if p.WhichOneof("content") == "text"
        )
        try:
            answer = await self.agent.handle(json.loads(text))
        except Exception as e:  # report bad requests to the caller, keep serving
            logger.warning("request failed: %s", e)
            answer = {"ok": False, "error": str(e)}

        response = Message(
            role=Role.ROLE_AGENT,
            message_id=context.message.message_id,
            parts=[Part(text=json.dumps(answer))],
        )
        await updater.add_artifact(parts=list(response.parts), name="answer")
        await updater.complete(message=response)

    async def cancel(self, context: RequestContext, event_queue: EventQueue) -> None:
        raise NotImplementedError("cancel not supported")


class FarmAgent:
    """One farm agent: a SLIM identity that serves requests and calls others."""

    def __init__(
        self,
        agent_id: str,
        title: str,
        handler: Handler | None = None,
        location: str | None = None,
    ) -> None:
        self.id = agent_id
        self.name = AGENTS[agent_id]
        self.handler = handler
        self.meta = {
            "id": agent_id,
            "title": title,
            "slim_name": self.name,
            "location": location or os.environ.get("AGENT_LOCATION", "laptop"),
            "host": socket.gethostname(),
            "lang": "Python",
        }
        self._clients: dict[str, Client] = {}
        self._background: set[asyncio.Task] = set()

    async def handle(self, payload: Payload) -> Payload:
        if self.handler is None:
            raise ValueError(f"{self.id} accepts no requests")
        answer = await self.handler(payload)
        if payload.get("action") == "status":
            # Every status answer says who and where the agent is, so the
            # dashboard can show it moving between machines.
            answer = {**self.meta, **answer}
        return answer

    async def start(self) -> None:
        namespace, group, name = self.name.split("/")
        self._service, self._app, local_name, self._conn = await setup_slim_client(
            namespace=namespace,
            group=group,
            name=name,
            slim_url=common.slim_url(),
            secret=common.shared_secret(),
            log_level="error",
        )
        self._local_name = local_name
        self._factory = MultiAgentClientFactory(
            ClientConfig(
                supported_protocol_bindings=["slimrpc"],
                streaming=False,
                slimrpc_channel_factory=slimrpc_channel_factory(self._app, self._conn),
            )
        )

        self._server = None
        if self.handler is not None:
            card = AgentCard(
                name=self.meta["title"],
                description=f"Green Valley farm agent {self.id}",
                supported_interfaces=[
                    AgentInterface(
                        url=self.name,
                        protocol_binding="slimrpc",
                        protocol_version="1.0",
                    )
                ],
                version="0.1.0",
                default_input_modes=["text"],
                default_output_modes=["text"],
                capabilities=AgentCapabilities(streaming=False),
            )
            request_handler = DefaultRequestHandler(
                agent_executor=_JsonExecutor(self),
                task_store=InMemoryTaskStore(),
                agent_card=card,
            )
            self._server = slim_bindings.Server.new_with_connection(
                self._app, local_name, self._conn
            )
            add_A2AServiceServicer_to_server(
                SRPCHandler(card, request_handler), self._server
            )
            self.spawn(self._server.serve_async())

        print(
            f"{self.meta['title']} up: {self.name} via {common.slim_url()} · "
            f"location={self.meta['location']} host={self.meta['host']}",
            flush=True,
        )

    async def call(
        self, target: str, payload: Payload, timeout: float = 2.0
    ) -> Payload:
        """Send a JSON request to another agent by short id; raise on failure."""
        client = self._clients.get(target)
        if client is None:
            client = self._factory.create(
                card=minimal_agent_card(AGENTS[target], ["slimrpc"])
            )
            self._clients[target] = client
        try:
            return await asyncio.wait_for(self._send(client, payload), timeout)
        except BaseException:
            # Drop the channel: the next call opens a fresh session to the
            # name, which SLIM routes to wherever that agent is now.
            self._clients.pop(target, None)
            raise

    @staticmethod
    async def _send(client: Client, payload: Payload) -> Payload:
        request = SendMessageRequest(
            message=new_text_message(json.dumps(payload), role=Role.ROLE_USER)
        )
        text = ""
        async for response in client.send_message(request=request):
            # Python and Go agents answer with a task holding an artifact; the
            # .NET agent answers with a plain message. Accept both.
            match response.WhichOneof("payload"):
                case "task":
                    parts = [p for a in response.task.artifacts for p in a.parts]
                case "message":
                    parts = list(response.message.parts)
                case _:
                    continue
            text = "".join(p.text for p in parts if p.WhichOneof("content") == "text")
        return json.loads(text) if text else {}

    def notify(self, text: str, to: str | None = None, level: str = "info") -> None:
        """Tell the dashboard what just happened; never blocks, never fails."""
        if self.id == "assistant":
            return
        event = {
            "action": "event",
            "from": self.id,
            "to": to,
            "text": text,
            "level": level,
        }
        self.spawn(self._notify(event))

    async def _notify(self, event: Payload) -> None:
        with contextlib.suppress(Exception):
            await self.call("assistant", event, timeout=1.5)

    def spawn(self, coro: Awaitable[Any]) -> asyncio.Task:
        task = asyncio.ensure_future(coro)
        self._background.add(task)
        task.add_done_callback(self._background.discard)
        return task

    async def run_until_stopped(self, *loops: Awaitable[Any]) -> None:
        """Run background loops until Ctrl-C or SIGTERM, then shut down cleanly.

        A clean shutdown closes open sessions, so callers switch to another
        copy of this agent at once instead of waiting for a timeout.
        """
        loop_tasks = [self.spawn(loop_coro) for loop_coro in loops]
        stop = asyncio.Event()
        event_loop = asyncio.get_running_loop()
        for sig in (signal.SIGINT, signal.SIGTERM):
            event_loop.add_signal_handler(sig, stop.set)
        await stop.wait()

        print(f"{self.meta['title']} stopping...", flush=True)
        # Stop acting first, e.g. so a stopping guardian starts no backup box.
        for task in loop_tasks:
            task.cancel()
        if self._server is not None:
            await self._server.shutdown_async()
        with contextlib.suppress(Exception):
            await self._app.unsubscribe_async(self._local_name, self._conn)
        self._service.disconnect(self._conn)
        for task in list(self._background):
            task.cancel()


def now() -> float:
    return time.time()


def setup_logging() -> None:
    logging.basicConfig(level=os.environ.get("LOG_LEVEL", "WARNING"))
    # a2a-sdk warns about optional agent card fields on every start.
    logging.getLogger("a2a.utils.proto_utils").setLevel(logging.ERROR)
