# Haroon's Homelab (`10.0.0.118` & `10.0.0.117`)

This repository contains the Docker Compose stacks, the **devbot** Discord assistant, and configuration for the two-node homelab setup.

---

## 1. Main Compute, SaaS Staging & AI Server (`10.0.0.118` — `haroonpcserver`)
- **Hardware**: Dell OptiPlex 5060 | Intel Core i7-8700 (6C/12T) | 32 GB RAM | 116 GB NVMe (` / `) + 1.8 TB HDD (`/mnt/Storage1`)
- **Stack Path on Server**: `~/homelab-stack/docker-compose.yml` ([server-10.0.0.118/docker-compose.yml](server-10.0.0.118/docker-compose.yml))

| Service | Local URL / Connection | `.home` DNS URL | Purpose |
| :--- | :--- | :--- | :--- |
| **devbot** | Discord | — | Developer assistant: `/ask`, `/log`, `/status` (more in later phases) |
| **LiteLLM** | internal `http://litellm:4000` | — | Model-alias proxy (`smart` / `fast` / `embed`) in front of Ollama |
| **Open WebUI** | `http://10.0.0.118:3000` | `http://ai.home:3000` | Local ChatGPT UI + Master's Course PDF RAG Knowledge Base |
| **SearXNG** | `http://10.0.0.118:8088` | `http://server.home:8088` | Private metasearch engine + JSON search API (`/search?q=...&format=json`) for coding agents |
| **Ollama API** | `http://10.0.0.118:11434` | `http://ollama.home:11434` | Local CPU LLMs (`qwen3-coder:30b`, `qwen3:4b`) & embeddings (`nomic-embed-text`) |
| **Code-Server** | `http://10.0.0.118:8443` | `http://code.home:8443` | Browser-based VS Code IDE with `/mnt/Storage1/data` mounted |
| **SaaS Postgres (`pgvector`)** | `postgresql://saas:saas_dev_pass@10.0.0.118:5432/saas_dev` | `server.home:5432` | Dedicated PostgreSQL 16 + `pgvector` database for SaaS projects |
| **SaaS Redis** | `redis://10.0.0.118:6379` | `server.home:6379` | Dedicated Redis 7 server for SaaS queues, caching & rate-limiting |
| **Adminer DB UI** | `http://10.0.0.118:8082` | `http://server.home:8082` | Web GUI for inspecting `saas-postgres` (`saas` / `saas_dev_pass` / `saas_dev`) |
| **Jellyfin** | `http://10.0.0.118:8096` | `http://jellyfin.home:8096` | Media server (`~/docker-compose.yml`) |
| **OpenSpeedTest** | `http://10.0.0.118:3004` | `http://speed.home:3004` | Local LAN/Wi-Fi speed test |
| **CasaOS** | `http://10.0.0.118:80` | `http://server.home` | Host OS dashboard |
| **Portainer** | `https://10.0.0.118:9443` | `https://server.home:9443` | Docker container manager |

---

## 2. devbot: Discord Developer Assistant (`server-10.0.0.118/devbot/`)
Runs entirely on `10.0.0.118` with local models and no API costs. Nothing is ever added to project repos.

| Command | What it does |
| :--- | :--- |
| `/note <topic> [project]` | Opens a thread for raw tech notes/links/snippets; click **Synthesize & Save to Notion** (or `/note_save`) to format and upload to Notion (`Type = Tech`). |
| `/conventions <project> <rules> [repo_url]` | Saves per-project coding rules in Notion's **Projects** database (automatically pulled by `/ask` and `/code`). |
| `/ask <question> [project]` | Opens a thread for architecture / implementation Q&A. Follow up, paste docs, or attach text/source files in the thread. |
| `/doc <name> [question] [file] [text]` | Chunks & embeds documentation into `pgvector` (`nomic-embed-text`) and opens a grounded Q&A thread citing `[chunk N]`. |
| `/docs` | Lists all documentation sets indexed in `pgvector`. |
| `/code <repo> <task>` | Clones `<repo>`, drafts an implementation plan, waits for your ✅ approval in Discord, runs Aider on `agent/<slug>`, verifies syntax, pushes the branch, and opens a Draft PR. |
| `/review <pr>` | Fetches any GitHub PR diff (`url` or `repo#12`) and posts a senior-engineer code review in a Discord thread. |
| `/checkin` / `/wrapup` / `/retro` | Triggers the 8:00 AM Git-aware check-in, 6:00 PM wrap-up, or Sunday 6:00 PM weekly retro on demand. |
| `/resume [repo]` | Shows where you left off: last handoff, latest plan, blockers, recent GitHub commits, open PRs, and active `agent/*` branches. |
| `/research <query>` | Searches the web via SearXNG, synthesizes fixes/sources, and saves a `Research` note in Notion. |
| `/log <text>` | Appends a progress entry to Postgres, local archive, and today's Notion Journal page. |
| `/status` | Health check for LiteLLM, Postgres (`pgvector`), Notion, GitHub, and SearXNG. |

Only the user in `DISCORD_OWNER_ID` can use it. Replies are slow by design (CPU inference): expect minutes, not seconds.

### First-time setup
1. **Discord bot**: at https://discord.com/developers/applications, create an app, then under **Bot** copy the token and enable **Message Content Intent**. Invite it with OAuth2 URL Generator scopes `bot` + `applications.commands` and permissions *Send Messages, Send Messages in Threads, Create Public Threads, Read Message History, Embed Links, Attach Files*.
2. **Env file** (on the server, in `~/homelab-stack/`): `cp .env.example .env` and fill in the Discord IDs and a random `LITELLM_MASTER_KEY`.
3. **Data dir**: `sudo mkdir -p /mnt/Storage1/devbot && sudo chown 1000:1000 /mnt/Storage1/devbot`
4. **Remove retired containers and pull models**:
   ```bash
   docker compose up -d --remove-orphans ollama        # applies new Ollama settings, removes n8n/windmill/sandbox
   docker exec ollama ollama pull qwen3-coder:30b
   docker exec ollama ollama pull qwen3:4b
   docker exec ollama ollama pull nomic-embed-text
   ```
5. **Evaluate models**: use `python3 devbot/scripts/benchmark_models.py` for throughput, then run
   `python3 devbot/scripts/evaluate_code_models.py smart` through LiteLLM for repository-edit correctness.
   Only select a model after its acceptance cases pass; then edit `litellm/config.yaml` if needed.
6. **Start**: `docker compose up -d --build litellm devbot && docker logs -f devbot`. Then run `/status` in Discord.

---

## 3. Edge, DNS & Monitoring Node (`10.0.0.117` — `raspberrypi`)
- **Hardware**: Raspberry Pi 4 Model B (8 GB RAM) | 128 GB Storage
- **Stack Path on Pi**: `~/pi-stack/docker-compose.yml` ([pi-10.0.0.117/docker-compose.yml](pi-10.0.0.117/docker-compose.yml))

| Service | Local URL | `.home` DNS URL | Purpose |
| :--- | :--- | :--- | :--- |
| **Homepage** | `http://10.0.0.117:3002` | `http://dash.home:3002` | Unified start page & live status monitor for both nodes |
| **Uptime Kuma** | `http://10.0.0.117:3001` | `http://status.home:3001` | 24/7 uptime monitoring & notifications |
| **Pi-hole** | `http://10.0.0.117/admin` | `http://pihole.home/admin` | Network-wide ad blocker & `*.home` DNS resolver |
| **Portainer** | `https://10.0.0.117:9443` | `https://pi.home:9443` | Docker container manager on Pi |
