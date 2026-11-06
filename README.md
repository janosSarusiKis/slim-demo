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
| `go_agent/` | Go agents: `cmd/home_agent` (the home agent rewritten in Go, for the encore) and `cmd/agronomist` (the farm demo's agronomist). |
| `farm/` | The farm demo's Python agents: greenhouse, pest scout, guardian and the dashboard (`farm/assistant.py`, `farm/static/index.html`). `farm/sprayer.py` is a Python spare of the .NET sprayer. |
| `dotnet_sprayer/` | The farm demo's sprayer, written in .NET. |
| `compose.yaml` | SLIM node, the agent containers (Python and Go), the ticker and the farm agents. |
| `Taskfile.yaml` | Shortcuts for every step (`task --list`). |

Requirements: [uv](https://docs.astral.sh/uv/), [Task](https://taskfile.dev), Docker.

## Farm demo: Green Valley Farm

The same SLIM ideas, told as a story. A tomato farm has two greenhouses, each
with a small computer on its own 4G connection, and a pest expert (the
agronomist) working from an office in town. The farmer watches everything on a
dashboard at <http://localhost:8080>.

| Agent | SLIM name | Runs on (local rehearsal) | Language |
|---|---|---|---|
| Greenhouse 1 main box | `greenvalley/farm/gh1` | your laptop (`task gh1`) | Python |
| Greenhouse 2 box | `greenvalley/farm/gh2` | container | Python |
| Pest scout | `greenvalley/farm/scout` | container | Python |
| Agronomist | `greenvalley/town/agronomist` | container | **Go** |
| Sprayer | `greenvalley/farm/sprayer` | container | **.NET** |
| Guardian | `greenvalley/farm/guardian` | your laptop (`task guardian`) | Python |
| Farm assistant (dashboard) | `greenvalley/farm/assistant` | your laptop (`task dashboard`) | Python |
| GH1 backup box | `greenvalley/farm/gh1` (same name!) | container, started by the guardian | Python |

Agents send each other small JSON requests over A2A, for example
`{"action": "water", "seconds": 10}`. The sensors are simulated: soil dries out,
watering wets it, and whiteflies multiply until the greenhouse is sprayed.

### Run it locally

`.env` must point at the node you use. For a local rehearsal, set
`SLIM_URL=http://localhost:46357` and leave `AGENT_SLIM_URL` unset.

```bash
task farm:build    # once, and after every code change
task farm:up       # SLIM node, GH2, pest scout, sprayer, agronomist
task gh1           # terminal 1: Greenhouse 1's main box
task guardian      # terminal 2
task dashboard     # terminal 3, then open http://localhost:8080
```

Stop it all with Ctrl-C in the three terminals and `task farm:down`.

### Demo script (about 4 minutes)

**Act 1: Morning check (1 min)**
"Greenhouse 1 is on a 4G router at the far end of the farm. Nobody opened a port
for it. The dashboard reaches it by name."
- Point at the GH1 card: soil moisture is red, below 25 %.
- Click **💧 Water zone A (10 s)**. The log shows the request and `valve A open`,
  and the moisture bar climbs.

**Act 2: Bugs! (1.5 min)**
- Click **🪲 Release whiteflies** on GH1. The trap count turns red.
- Without anyone clicking anything, the log shows the agents talking:
  pest scout → agronomist ("63 whiteflies in GH1, what should we do?"),
  agronomist → sprayer ("spray GH1 with neem oil"), sprayer → Greenhouse 1.
- Talking point: three languages just worked together. The agronomist is in town,
  on a different network, written in **Go** by "another vendor". The sprayer runs
  the spray rig manufacturer's **.NET** software. The greenhouses and the scout are
  **Python**. Point at the colored language badges. They just call each other by name.
- The count drops while GH1 sprays.

**Act 3: Storm, power cut (1.5 min)**
- Press Ctrl-C in terminal 1 (GH1's main box), or turn off the laptop's Wi-Fi if
  the node is in the cloud.
- The GH1 card turns red: `not answering`. Two seconds later the guardian
  starts GH1's backup box under the **same SLIM name**, and the card turns amber:
  `backup box`, started from the readings saved just before the power cut.
- Click **Water** again: it works. The dashboard never changed anything.
- "Power's back": run `task gh1` again. The guardian stops the backup box, and
  the card is green again with GH1's main box.

**Wrap-up (30 s)**
"No VPN, no open ports, no reconfiguration. Agents in different places and
languages find each other by name, and when one goes down, its replacement
simply takes the same name."

### With the node in the cloud

Put `SLIM_URL=http://<vm-ip>:46357` and `AGENT_SLIM_URL=http://<vm-ip>:46357`
in the laptop's `.env`, then run `task farm:up` without the local node:
`docker compose --profile farm up -d gh2 scout sprayer agronomist`. The guardian
starts the backup box on the machine it runs on, so run the guardian where you
want the backup box to live.

## The home agent demo

The original, shorter demo: one agent and a ticker. The sections below are about it.

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
