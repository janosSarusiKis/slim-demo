# slim-demo: your agent, anywhere

A short demo of [A2A](https://a2a-protocol.org) over [SLIM](https://github.com/agntcy/slim).
You call an agent by its **name**, not its address. That means:

1. **No open ports.** The agent on your laptop sits behind NAT and a firewall that
   blocks all incoming connections, and the cloud can still call it.
2. **It can move live.** The agent moves from the laptop to a container while it is
   being called, and the caller changes nothing.

Takes about 4 minutes on stage.

```
  ┌──────────── Cloud VM (public IP) ────────────┐
  │  SLIM node :46357   ← the only open port      │
  │  cloud assistant (ticker)                     │
  │  agent container (Act 3)                      │
  └───────────▲───────────────────────▲───────────┘
              │ outbound only          │ outbound only
  ┌───────────┴───────────┐  ┌────────┴──────────────┐
  │ Laptop, home Wi-Fi     │  │ ...or any other host  │
  │ agntcy/demo/home_agent │  │ agntcy/demo/home_agent│
  └───────────────────────┘  └───────────────────────┘
```

## What's in here

| Path | What it is |
|---|---|
| `slim_demo/agent.py` | The home agent: an A2A agent served over SLIM. It answers with where it runs and data that exists only on that machine (`notes/`). |
| `slim_demo/ticker.py` | The cloud assistant: asks the agent a question every second and prints the round-trip time and who answered. It retries on failure, so a move shows up as at most one missed line. |
| `go_agent/` | The same agent rewritten in Go (same SLIM name, same answers), for the encore. |
| `compose.yaml` | SLIM node, the agent containers (Python and Go) and the ticker. |
| `Taskfile.yaml` | Shortcuts for every step (`task --list`). |

Requirements: [uv](https://docs.astral.sh/uv/), [Task](https://taskfile.dev), Docker.

## Rehearse locally (one machine)

```bash
task secret        # writes a random SLIM_SHARED_SECRET into .env
task node          # SLIM node in Docker on :46357
task agent         # terminal 1: the "laptop" agent
task ticker        # terminal 2: the caller
task agent:cloud   # terminal 3: the container copy, then Ctrl-C terminal 1
```

The ticker goes from cyan `laptop (...)` lines to magenta `cloud (...)` lines.
For the Go encore, use `task agent:go` instead of `task agent:cloud`: the lines
switch to `cloud (…, Go)`.

## The real setup

**Cloud VM** (any small Linux VM with Docker; open TCP 46357 inbound):

```bash
git clone https://github.com/janosSarusiKis/slim-demo.git && cd slim-demo
cp .env.example .env    # paste the same SLIM_SHARED_SECRET you use on the laptop
docker compose up -d slim
docker compose build
```

**Laptop:** in `.env`, set `SLIM_URL=http://<vm-ip>:46357` and use the same secret.

## Demo script

**Act 1: The problem (30 s)**
"I want a cloud assistant to ask an agent on my laptop a question. Normally that
means port forwarding, ngrok, or a public URL."
- Show the macOS firewall: *System Settings › Network › Firewall › Options ›
  Block all incoming connections*.

**Act 2: It just works (1 min)**
- Laptop: `task agent`
- `task ports`: the agent isn't listening on any port. It only has one outbound
  connection to the SLIM node.
- VM, over SSH: `docker compose run --rm ticker`
  ```
  #1   ✔    41 ms  │ laptop (my-macbook, Python) · 3 notes, latest: "Water the tomatoes"
  ```
- The notes come from the laptop's disk and never leave it except as the answer.
- Talking point: the session is end-to-end encrypted with MLS. The SLIM node
  routes messages but can't read them.

**Act 3: Move it live (1.5 min)**
- "Now I'm going to close my laptop."
- VM: `docker compose up -d agent`. Same name, new home.
- Laptop: Ctrl-C the agent.
- The ticker switches to `cloud (...)` lines without missing a call. Same
  name, same client, no reconfiguration.

**Encore: a different language (1 min)**
- VM: `docker compose up -d agent-go`, then stop the Python copy (Ctrl-C on the
  laptop, or `docker compose stop agent`).
- The ticker now shows `cloud (…, Go)`. The caller is still the same Python
  program, using the same name. It never learned the agent was rewritten.

**Act 4: Wrap-up (30 s)**
"No public endpoint, no DNS, no reconfiguration. Agents are addressed by identity,
not location."

## How the move behaves

Measured locally with the ticker at 1 call per second:

| How the old copy goes away | What the ticker shows |
|---|---|
| New copy started first, then Ctrl-C or `docker stop` on the old one | No missed calls |
| New copy started first, then `kill -9` on the old one | One `timeout, retrying…` line (3 s), then the new copy answers |
| Old copy stopped and nothing running until the new one starts | Timeouts until the new copy is up |

Pulling the laptop's Wi-Fi hasn't been tested. Unlike `kill -9`, the
TCP connection isn't closed, so the node may take longer to notice that the
laptop is gone. Rehearse it on the venue network before relying on it, or use Ctrl-C.

## Notes and caveats

- **Shared secret.** Every component needs the same `SLIM_SHARED_SECRET`
  (32+ characters). There is no built-in default, because the node is reachable
  from the internet.
- **No TLS.** The connection to the node is plain TCP (`tls.insecure: true` in
  `slim/config.yaml`). Message contents are still MLS-encrypted end to end. For a
  production-style setup, enable TLS on the node and pass a `client_config` to
  `setup_slim_client`.
- **Venue networks** sometimes block outbound traffic to unusual ports. Keep a phone
  hotspot as a backup, or move the node to port 443.
- The node logs `WARN ... subscription_table: not found` when an agent
  disconnects. This is harmless.

## Clean up

```bash
task down
```
