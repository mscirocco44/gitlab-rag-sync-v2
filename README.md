# gitlab-rag-sync-v2

Automatically syncs one or more GitLab repositories into a single OpenWebUI knowledge base so you can chat with all of your code at once using a local LLM.

Every sync interval (i.e., 1 hour), it pulls all text-based files from each GitLab project you list and indexes them into a single OpenWebUI knowledge base using your configured embedding model. Binary files are detected and skipped automatically.

This is the v2 successor to [gitlab-rag-sync](https://github.com/mscirocco44/gitlab-rag-sync). The original spun up one container per repo and one knowledge base per repo. v2 is one container, one knowledge base, N repos — driven by a comma-separated list of project IDs in your `.env`. Adding a new repo is a one-line change.

## How it works

1. Reads the list of GitLab project IDs from `GITLAB_PROJECT_IDS`
2. Pulls all files from every listed project via the GitLab API
3. Skips binary files (images, archives, compiled files, LFS pointers, etc.)
4. **Only after all repos are pulled successfully**, resets the OpenWebUI knowledge base and re-uploads everything in one pass
5. Repeats every hour (or however long you configure it to repeat in the ENV settings)

The pull-then-reset ordering matters. If GitLab is unreachable or one of the project IDs is bad, the sync aborts before touching the knowledge base — so a transient outage leaves your existing KB intact instead of wiping it and leaving you with nothing for the next hour. Stale data > no data.

Files from different repos are kept distinct in two ways: each file's flat name gets prefixed with its project ID (so `12__src__main.py` and `42__src__main.py` don't collide), and the chunk header inside each file shows both the project ID and the original path. The LLM can tell where any given snippet came from.

## Requirements

- Docker
- Ollama running with at least one model pulled
- OpenWebUI running and accessible
- A GitLab instance (self-hosted or remote)
- `nomic-embed-text` pulled in Ollama and set as the embedding model in OpenWebUI

To set the embedding model: **OpenWebUI → Admin Panel → Settings → Documents → Embedding Model → nomic-embed-text**

---

## Setup

### 1. Clone the repo

```bash
git clone https://github.com/mscirocco44/gitlab-rag-sync-v2.git
```

### 2. Set up the directory structure

The `gitlab-rag` folder needs to sit alongside your `docker-compose.yml`. Copy it into the same directory as your compose file:

```bash
cp -r gitlab-rag/ /path/to/your/docker-compose-directory/
```

Your project layout should look like this:

```
your-project/
├── docker-compose.yml
└── gitlab-rag/
    ├── sync.py
    ├── Dockerfile
    └── .env
```

> The `build: gitlab-rag` line in the docker-compose service block tells Docker to look for a `Dockerfile` inside a folder called `gitlab-rag` relative to where `docker-compose.yml` lives. If the folder is placed elsewhere, update that path accordingly.

Notice that there is only one `.env` file, regardless of how many repos you sync. That is the whole point of v2.

### 3. Create your .env file

Copy the example and fill in your values. Only one `.env` file is needed no matter how many GitLab projects you index.

Ensure that your GitLab personal token has **read-only** permissions.

```bash
cp .env.example .env
```

```
GITLAB_TOKEN=          # GitLab personal access token (read_api scope)
GITLAB_PROJECT_IDS=    # Comma-separated list of project IDs, e.g. 12,18,42
GITLAB_BRANCH=main
OPENWEBUI_TOKEN=       # OpenWebUI API key (Settings → Account → API Keys)
KNOWLEDGE_NAME=gitlab
```

To add a new repo later, append its project ID to `GITLAB_PROJECT_IDS` and restart the container. No compose edits, no rebuild.

```
GITLAB_PROJECT_IDS=12,18,42        # before
GITLAB_PROJECT_IDS=12,18,42,99     # after — that's it
```

> **Backwards compatibility:** if you are migrating from v1 and still have `GITLAB_PROJECT_ID=12` set, that still works — it gets treated as a single-element list. You don't have to touch your old env to upgrade. Just swap `sync.py` and you're done.

#### Optional env vars

| Variable | Default | Purpose |
| --- | --- | --- |
| `SYNC_INTERVAL` | `3600` | Seconds between syncs |
| `MAX_FILE_BYTES` | `500000` | Files larger than this are skipped |
| `GITLAB_VERIFY_SSL` | `true` | Set to `false` if your GitLab uses a self-signed cert |

### 4. Add to your docker-compose.yml

See `example.docker-compose.yml` for a full reference. Below is the relevant service block.

> **Note on GITLAB_URL:**
> - If GitLab is running in Docker on the **same compose file**, use the container name as the hostname: `http://gitlab`. Docker's internal networking resolves container names automatically — no IP or port needed as long as GitLab is on port 80 inside the container (which it is by default).
> - If GitLab is on a **remote server**, use the actual IP or hostname with the port it is running on: `http://192.168.1.100:80`, `http://gitlab.company.com:8929`, or `https://gitlab.company.com`. If GitLab is on the default port (80 for HTTP, 443 for HTTPS), the port can be omitted.
>
> **Note on OPENWEBUI_URL:**
> - Always `http://open-webui:8080`. This is the internal Docker port for the OpenWebUI container and never changes, regardless of what port OpenWebUI is mapped to on your host machine.

---

#### Local GitLab (running in Docker)

```yaml
  gitlab-rag-sync:
    build: gitlab-rag
    container_name: gitlab-rag-sync
    restart: always
    depends_on:
      open-webui:
        condition: service_healthy
      gitlab:
        condition: service_healthy
    env_file:
      - gitlab-rag/.env
    environment:
      GITLAB_URL: "http://gitlab"
      OPENWEBUI_URL: "http://open-webui:8080"
      SYNC_INTERVAL: "3600"
```

That's it — one block whether you sync one repo or twenty. The list of repos lives in `.env`.

#### Remote GitLab

Remove the `gitlab` depends_on condition since GitLab is not a local container. Set `GITLAB_URL` to your actual GitLab host and port:

```yaml
  gitlab-rag-sync:
    build: gitlab-rag
    container_name: gitlab-rag-sync
    restart: always
    depends_on:
      open-webui:
        condition: service_healthy
    env_file:
      - gitlab-rag/.env
    environment:
      GITLAB_URL: "http://your-gitlab-host:port"
      OPENWEBUI_URL: "http://open-webui:8080"
      SYNC_INTERVAL: "3600"
```

If your remote GitLab runs on the default port 80 (or 443 for HTTPS), the port can be omitted:

```yaml
      GITLAB_URL: "http://your-gitlab-host"
      # or for HTTPS:
      GITLAB_URL: "https://your-gitlab-host"
```

### 5. Start it

```bash
docker compose up -d --build
docker logs -f gitlab-rag-sync
```

---

## Full example docker-compose.yml

The assumed setup runs Ollama, OpenWebUI, and GitLab together in a single compose file. Here is what that looks like with `gitlab-rag-sync` included:

```yaml
services:

  ollama:
    image: ollama/ollama
    container_name: ollama
    runtime: nvidia
    environment:
      - NVIDIA_VISIBLE_DEVICES=all
    volumes:
      - ollama_data:/root/.ollama
    ports:
      - "11434:11434"
    restart: unless-stopped

  open-webui:
    image: ghcr.io/open-webui/open-webui:v0.8.12
    container_name: open-webui
    depends_on:
      - ollama
    environment:
      - OLLAMA_BASE_URL=http://ollama:11434
    volumes:
      - open_webui_data:/app/backend/data
    ports:
      - "3000:8080"
    restart: unless-stopped

  gitlab:
    image: gitlab/gitlab-ce:latest
    container_name: gitlab
    restart: always
    hostname: localhost
    environment:
      GITLAB_OMNIBUS_CONFIG: |
        external_url 'http://localhost'
        gitlab_rails['gitlab_shell_ssh_port'] = 2222
        puma['worker_processes'] = 2
        sidekiq['concurrency'] = 5
        prometheus_monitoring['enable'] = false
    ports:
      - "80:80"
      - "443:443"
      - "2222:22"
    volumes:
      - gitlab_config:/etc/gitlab
      - gitlab_logs:/var/log/gitlab
      - gitlab_data:/var/opt/gitlab
    shm_size: '256m'

  gitlab-rag-sync:
    build: gitlab-rag
    container_name: gitlab-rag-sync
    restart: always
    depends_on:
      open-webui:
        condition: service_healthy
      gitlab:
        condition: service_healthy
    env_file:
      - gitlab-rag/.env
    environment:
      GITLAB_URL: "http://gitlab"
      OPENWEBUI_URL: "http://open-webui:8080"
      SYNC_INTERVAL: "3600"

volumes:
  ollama_data:
  open_webui_data:
  gitlab_config:
  gitlab_logs:
  gitlab_data:
```

> Remove `runtime: nvidia` from the ollama service if you do not have an NVIDIA GPU.
> See `example.docker-compose.yml` for the remote GitLab variation.

---

## Usage

Once the first sync completes, open OpenWebUI and start a chat. Type `#` and select your knowledge base from the dropdown, then ask questions about your code:

```
#gitlab do we have a script that handles authentication?
#gitlab where are API calls being made in project 18?
#gitlab compare how project 12 and project 42 handle config loading
```

Every chunk in the knowledge base has a header showing which project it came from (`# Project: 18`) and the original path (`# File: src/main.py`), so the model can tell you which repo and which file it pulled an answer from. If you want to scope a query to a specific repo, just mention the project ID in your question.

---

## Useful commands

```bash
# Force an immediate resync
docker restart gitlab-rag-sync

# Watch sync output
docker logs -f gitlab-rag-sync

# Add or remove a repo — edit GITLAB_PROJECT_IDS in .env, then:
docker compose up -d gitlab-rag-sync

# Update tokens without rebuilding — edit the .env file, then:
docker compose up -d gitlab-rag-sync

# Rebuild after editing sync.py or Dockerfile
docker compose up -d --build gitlab-rag-sync
```

---

## Notes

- The knowledge base is fully reset on each sync to stay consistent with all listed repos
- A failed pull (network blip, bad project ID, GitLab down) aborts before the reset, so the existing KB is left intact
- Files over 500KB are skipped (override with `MAX_FILE_BYTES` env var, in bytes)
- Any file that fails UTF-8 decoding is treated as binary and skipped
- Git LFS pointer files are detected and skipped — the actual large file content is not pulled
- All listed projects share one knowledge base. Per-file failures during upload are logged and the loop continues
- `GITLAB_PROJECT_IDS` (plural) takes precedence over `GITLAB_PROJECT_ID` (singular). If both are set, the plural one wins
- All projects are pulled on the same `GITLAB_BRANCH`. If your repos use different default branches, set them all to `main` (or whatever) on the GitLab side. Per-repo branch overrides are a future addition
- Container runs as a non-root user (`syncuser`, uid 10001) for STIG/security-baseline compliance
- The script holds all collected file content in memory during a sync. With the default 500KB cap and typical code repos this is fine; if you sync many large repos at once, increase the container's memory limit accordingly

## Migrating from v1

If you're coming from the original `gitlab-rag-sync`:

1. Replace `sync.py` and `Dockerfile` with the v2 versions
2. In your `.env`, rename `GITLAB_PROJECT_ID` to `GITLAB_PROJECT_IDS` (or leave it — v2 reads either)
3. Optionally add more project IDs to the comma-separated list
4. Delete any extra per-repo env files (`project-a.env`, `project-b.env`, etc.) and the duplicate compose service blocks. v2 only needs one `.env` and one service block
5. `docker compose up -d --build`

The knowledge base name will change from `gitlab-repo` (v1 default) to `gitlab` (v2 default) unless you set `KNOWLEDGE_NAME` explicitly. The old KB will still be there in OpenWebUI; you can delete it from the admin panel once you've confirmed the new one looks right.
