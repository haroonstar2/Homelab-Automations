# Haroon's Homelab (`10.0.0.118` & `10.0.0.117`)

This repository contains the Docker Compose stacks, n8n workflows, and configuration for the two-node homelab setup.

---

## 1. Main Compute, SaaS Staging & AI Server (`10.0.0.118` — `haroonpcserver`)
- **Hardware**: Dell OptiPlex 5060 | Intel Core i7-8700 (6C/12T) | 32 GB RAM | 116 GB NVMe (` / `) + 1.8 TB HDD (`/mnt/Storage1`)
- **Stack Path on Server**: `~/homelab-stack/docker-compose.yml` ([server-10.0.0.118/docker-compose.yml](server-10.0.0.118/docker-compose.yml))

| Service | Local URL / Connection | `.home` DNS URL | Purpose |
| :--- | :--- | :--- | :--- |
| **Open WebUI** | `http://10.0.0.118:3000` | `http://ai.home:3000` | Local ChatGPT UI + Master's Course PDF RAG Knowledge Base |
| **n8n** | `http://10.0.0.118:5678` | `http://n8n.home:5678` | Visual AI workflows (Job Radar, ArXiv Digest, Tech News, Research Agent) |
| **SearXNG** | `http://10.0.0.118:8088` | `http://server.home:8088` | Private metasearch engine + JSON search API (`/search?q=...&format=json`) for coding agents |
| **Ollama API** | `http://10.0.0.118:11434` | `http://ollama.home:11434` | Local LLM (`qwen2.5:3b`) & embedding server (`nomic-embed-text`) |
| **Windmill** | `http://10.0.0.118:8100` | `http://windmill.home:8100` | 24/7 developer task runner for Python/TS/Bash scripts & cron jobs |
| **Code-Server** | `http://10.0.0.118:8443` | `http://code.home:8443` | Browser-based VS Code IDE with `/mnt/Storage1/data` mounted |
| **SaaS Postgres (`pgvector`)** | `postgresql://saas:saas_dev_pass@10.0.0.118:5432/saas_dev` | `server.home:5432` | Dedicated PostgreSQL 16 + `pgvector` database for SaaS projects |
| **SaaS Redis** | `redis://10.0.0.118:6379` | `server.home:6379` | Dedicated Redis 7 server for SaaS queues, caching & rate-limiting |
| **Adminer DB UI** | `http://10.0.0.118:8082` | `http://server.home:8082` | Web GUI for inspecting `saas-postgres` (`saas` / `saas_dev_pass` / `saas_dev`) |
| **Jellyfin** | `http://10.0.0.118:8096` | `http://jellyfin.home:8096` | Media server (`~/docker-compose.yml`) |
| **OpenSpeedTest** | `http://10.0.0.118:3004` | `http://speed.home:3004` | Local LAN/Wi-Fi speed test |
| **CasaOS** | `http://10.0.0.118:80` | `http://server.home` | Host OS dashboard |
| **Portainer** | `https://10.0.0.118:9443` | `https://server.home:9443` | Docker container manager |

---

## 2. Pre-Built n8n Workflows (`server-10.0.0.118/workflows/`)
All 4 workflows are pre-imported into n8n (`http://10.0.0.118:5678`) and wired to your Discord Webhook:
1. **[1. Daily AI Tech & News Digest -> Discord](server-10.0.0.118/workflows/daily-ai-tech-digest.json)**: Summarizes top Hacker News stories every morning at 8 AM.
2. **[2. Private Web Research AI Agent (SearXNG + Ollama) -> Discord](server-10.0.0.118/workflows/private-web-research-agent.json)**: Searches the live web via SearXNG on any topic and synthesizes a cited report.
3. **[3. SWE & AI Job Hunt Radar -> Discord](server-10.0.0.118/workflows/job-hunt-radar.json)**: Scans tech/YC job feeds twice daily (10 AM & 4 PM), filters for SWE/AI/Full-Stack roles using Ollama, and sends direct apply links to Discord.
4. **[4. Master's CS & AI ArXiv Paper Digest -> Discord](server-10.0.0.118/workflows/arxiv-paper-digest.json)**: Summarizes the top 4 new ArXiv papers in `cs.AI`, `cs.LG`, and `cs.SE` on weekdays at 9 AM.

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
