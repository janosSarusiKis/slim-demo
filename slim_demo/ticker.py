"""The "cloud assistant": asks the home agent a question every couple of seconds.

It only knows the agent's SLIM name. It does not know, or care, which machine
answers, so the agent can move while the ticker keeps running. Each line shows
the round-trip time and who answered.
"""

import argparse
import asyncio
import contextlib
import itertools
import logging
import time

from a2a.client import Client, minimal_agent_card
from a2a.helpers import new_text_message
from a2a.types.a2a_pb2 import Role, SendMessageRequest
from slima2a import setup_slim_client
from slima2a.client_transport import (
    ClientConfig,
    MultiAgentClientFactory,
    slimrpc_channel_factory,
)

from slim_demo import common

RESET = "\033[0m"
DIM = "\033[2m"
RED = "\033[31m"
GREEN = "\033[32m"
# Fixed colors for the two locations used in the demo, so the move stands out.
LOCATION_COLORS = {"laptop": "\033[1;36m", "cloud": "\033[1;35m"}
OTHER_COLORS = ["\033[1;33m", "\033[1;34m", "\033[1;32m"]


def colorize(answer: str) -> str:
    location = answer.split(" ", 1)[0]
    color = (
        LOCATION_COLORS.get(location)
        or OTHER_COLORS[sum(map(ord, location)) % len(OTHER_COLORS)]
    )
    return f"{color}{answer}{RESET}"


async def ask(client: Client, question: str) -> str:
    request = SendMessageRequest(
        message=new_text_message(question, role=Role.ROLE_USER)
    )
    answer = ""
    async for response in client.send_message(request=request):
        if response.WhichOneof("payload") != "task":
            continue
        for artifact in response.task.artifacts:
            answer = "".join(
                part.text
                for part in artifact.parts
                if part.WhichOneof("content") == "text"
            )
    return answer


async def main() -> None:
    args = parse_arguments()
    logging.basicConfig(level=args.log_level)

    _, local_app, _, conn_id = await setup_slim_client(
        namespace=common.NAMESPACE,
        group=common.GROUP,
        name=args.client_name,
        slim_url=args.slim_url,
        secret=common.shared_secret(),
        log_level="error",
    )

    factory = MultiAgentClientFactory(
        ClientConfig(
            supported_protocol_bindings=["slimrpc"],
            streaming=False,
            slimrpc_channel_factory=slimrpc_channel_factory(local_app, conn_id),
        )
    )
    target = f"{common.NAMESPACE}/{common.GROUP}/{args.agent}"
    card = minimal_agent_card(target, ["slimrpc"])

    def new_client() -> Client:
        client = factory.create(card=card)
        assert isinstance(client, Client)
        return client

    print(f"{DIM}asking {target} via {args.slim_url} every {args.interval}s{RESET}")
    client = new_client()

    for n in itertools.count(1):
        started = time.perf_counter()
        try:
            answer = await asyncio.wait_for(ask(client, args.question), args.timeout)
        except Exception as e:  # noqa: BLE001 - any failure means "try again"
            reason = "timeout" if isinstance(e, TimeoutError) else type(e).__name__
            print(f"{DIM}#{n:<4}{RESET}{RED}✖ {reason}, retrying…{RESET}", flush=True)
            # Drop the old channel: the next call opens a fresh session to the
            # name, which SLIM routes to wherever the agent is now.
            client = new_client()
        else:
            ms = (time.perf_counter() - started) * 1000
            print(
                f"{DIM}#{n:<4}{RESET}{GREEN}✔{RESET} {ms:5.0f} ms  │ {colorize(answer)}",
                flush=True,
            )
        await asyncio.sleep(args.interval)


def parse_arguments() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--agent", default=common.AGENT_NAME)
    parser.add_argument("--client-name", default="cloud_assistant")
    parser.add_argument("--slim-url", default=common.slim_url())
    parser.add_argument("--question", default="What's new at home?")
    parser.add_argument("--interval", type=float, default=1.0)
    parser.add_argument("--timeout", type=float, default=3.0)
    parser.add_argument("--log-level", default="ERROR")
    return parser.parse_args()


if __name__ == "__main__":
    with contextlib.suppress(KeyboardInterrupt):
        asyncio.run(main())
