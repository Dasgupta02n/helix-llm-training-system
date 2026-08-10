# Helix

**Multi-tenant agent ecosystem** for influencer-marketing research data and LLM training pipelines.  
Host on a **VPS with your custom domain**, powered by **OpenRouter** (Grok and other models).

Not tied to Paperclip or any third-party agent marketplace — Helix is a standalone platform.

## What you get

| Layer | Details |
|--------|---------|
| **Gold-data mission** | Mine high-quality training data for LLMs; one **tenant** (or Research Brief) per training product line |
| **Research Brief** | Domain, mission, questions, categories, sources, phase targets, agent instructions — drives Research Director + all agents |
| **Schema Studio** | Per-topic JSON Schema + sample gold row; synthetic examples must match |
| **Dataset export** | Snapshot approved pool; download JSONL/JSON by dataset version |
| **15 agents** | Domain-agnostic prompts; Research Brief injected at runtime |
| **Tool use** | 80+ tenant-scoped tools (SQLite local / Postgres on VPS) |
| **Multi-tenant** | Isolated data, budgets, memberships, optional per-tenant OpenRouter keys |
| **Gather** | **Apify only** (batch, cache, dedupe; code filters before any AI) |
| **Judge** | **OpenRouter only** — never invents scrape/post content |
| **LLM** | OpenRouter for judgment; optional direct xAI local fallback |
| **Deploy** | Docker Compose + Nginx, custom domain + TLS ready |

### Console tabs (http://localhost:8000/app)

1. **Home** — status, questions, activity  
2. **Plan** — what to mine (research brief)  
3. **Formats** — gold example shapes  
4. **AI helpers** — run the 15-agent pipeline  
5. **Download** — snapshot + JSONL export  

### Accounts & email (Resend)

| Feature | Endpoint |
|---------|----------|
| Create account | `POST /api/auth/register` |
| Sign in | `POST /api/auth/login` |
| Confirm email | `GET /api/auth/verify-email?token=` |
| Resend confirmation | `POST /api/auth/resend-verification` |
| Forgot password | `POST /api/auth/forgot-password` |
| Reset / set password | `POST /api/auth/reset-password` · `POST /api/auth/set-password` |
| Profile | `GET/PATCH /api/auth/me` |
| Admin list/invite | `GET /api/users` · `POST /api/users/invite` |

Add to `.env`:

```env
RESEND_API_KEY=re_...
RESEND_FROM_EMAIL=Helix <you@your-domain.com>
HELIX_BASE_URL=http://localhost:8000
ALLOW_PUBLIC_SIGNUP=true
REQUIRE_EMAIL_VERIFICATION=true
```

Without `RESEND_API_KEY`, emails are skipped and **dev links** are returned in API responses (local only) so you can still test flows.

## Org chart

```
Research Director (CEO)
├── Discovery Agent
├── Evidence Collector
├── Duplicate Resolver
├── Fact Verification Agent
├── Knowledge Extraction Agent
├── Knowledge Graph Agent
├── Campaign Strategist
├── Trainer
├── Scope Guardian
└── Dataset Curator (manager)
    ├── Synthetic Dataset Generator
    ├── Adversarial Reviewer
    └── Benchmark Builder

Operations Dashboard — reports to the board (you), outside the RD chain
```

## Quick start (local)

```bash
cd "LLM Training System"
python -m venv .venv

# Windows
.venv\Scripts\activate
# macOS/Linux
# source .venv/bin/activate

pip install -r requirements.txt
copy .env.example .env   # or: cp .env.example .env
```

Edit `.env`:

```env
HELIX_ENV=local
HELIX_SECRET_KEY=dev-secret-change-me
DATABASE_URL=sqlite:///./data/helix.db

# Preferred for VPS / hosted:
OPENROUTER_API_KEY=sk-or-v1-...
OPENROUTER_MODEL=x-ai/grok-4.5
OPENROUTER_SITE_URL=http://localhost:8000
OPENROUTER_SITE_NAME=Helix

# Optional local fallback if OpenRouter unset:
# XAI_API_KEY=...

BOOTSTRAP_ADMIN_EMAIL=admin@example.com
BOOTSTRAP_ADMIN_PASSWORD=admin12345
```

```bash
# init DB + bootstrap admin + demo tenant
python -m helix.cli init

# start API + web UI
python -m helix.cli serve
# → http://localhost:8000
# → Console: http://localhost:8000/app
# → API docs: http://localhost:8000/docs
```

Login with the bootstrap admin. Demo tenant slug: **`demo`**.

### CLI agent run

```bash
python -m helix.cli list-agents
python -m helix.cli run discovery --tenant demo
python -m helix.cli run operations_dashboard --tenant demo
```

## Multi-tenant model

- **Platform superadmin** — creates tenants, can access all
- **Tenant members** — `owner` | `admin` | `member` via memberships
- All pipeline tables carry `tenant_id` (hard isolation)
- Each tenant has `monthly_budget_usd` / `spent_usd`
- Optional **per-tenant** `openrouter_api_key` + `openrouter_model` (else platform key)

### Create a tenant (API)

```bash
TOKEN=$(curl -s -X POST http://localhost:8000/api/auth/login \
  -H 'Content-Type: application/json' \
  -d '{"email":"admin@example.com","password":"admin12345"}' | jq -r .access_token)

curl -X POST http://localhost:8000/api/tenants \
  -H "Authorization: Bearer $TOKEN" \
  -H 'Content-Type: application/json' \
  -d '{
    "slug": "acme",
    "name": "Acme Growth",
    "monthly_budget_usd": 100,
    "owner_email": "owner@acme.com",
    "owner_password": "strong-password"
  }'
```

### Run an agent for a tenant

```bash
curl -X POST http://localhost:8000/api/t/acme/agents/discovery/run \
  -H "Authorization: Bearer $TOKEN" \
  -H 'Content-Type: application/json' \
  -d '{}'
```

## VPS + custom domain (OpenRouter)

### 1. Server prerequisites

- Ubuntu 22.04+ (or similar)
- Docker + Docker Compose plugin
- DNS **A record**: `your-domain.com` → VPS public IP

### 2. Deploy

```bash
git clone <your-repo> helix && cd helix
cp .env.example .env
nano .env
```

Production `.env` essentials:

```env
HELIX_ENV=production
HELIX_SECRET_KEY=<long-random-string>
HELIX_BASE_URL=https://your-domain.com
DATABASE_URL=postgresql+psycopg2://helix:STRONG_PASSWORD@db:5432/helix
POSTGRES_PASSWORD=STRONG_PASSWORD

OPENROUTER_API_KEY=sk-or-v1-...
OPENROUTER_MODEL=x-ai/grok-4.5
OPENROUTER_SITE_URL=https://your-domain.com
OPENROUTER_SITE_NAME=Helix

BOOTSTRAP_ADMIN_EMAIL=admin@your-domain.com
BOOTSTRAP_ADMIN_PASSWORD=<strong-password>
```

Edit `deploy/nginx.conf`: replace `YOUR_DOMAIN` with your domain.

```bash
mkdir -p deploy/certs
docker compose up -d --build
```

App is reachable on port 80 via Nginx → FastAPI.

### 3. TLS (Let's Encrypt)

On the host (example with certbot standalone or webroot):

```bash
# After DNS propagates — example using certbot docker once HTTP works:
docker run --rm -v "$(pwd)/deploy/certs:/etc/letsencrypt/live/your-domain.com" \
  -v "$(pwd)/deploy/certbot-www:/var/www/certbot" \
  -p 80:80 certbot/certbot certonly --standalone \
  -d your-domain.com -d www.your-domain.com \
  --agree-tos -m admin@your-domain.com --non-interactive
```

Copy `fullchain.pem` + `privkey.pem` into `deploy/certs/`, switch to `deploy/nginx.tls.conf` (or uncomment HTTPS block), then:

```bash
docker compose restart nginx
```

Set `HELIX_BASE_URL=https://your-domain.com` and redeploy the app container if needed.

### 4. OpenRouter

1. Create a key at [openrouter.ai/keys](https://openrouter.ai/keys)
2. Pick a model id, e.g. `x-ai/grok-4.5` (or any OpenRouter chat model)
3. Put key + model in `.env` (platform-wide) or set per-tenant via API when creating tenants

Helix uses the **OpenAI-compatible** Chat Completions API against OpenRouter’s base URL, with tool/function calling for every agent.

## API surface

| Method | Path | Purpose |
|--------|------|---------|
| POST | `/api/auth/login` | JWT login |
| GET | `/api/auth/me` | Profile + tenants |
| GET/POST | `/api/tenants` | List / create tenants |
| GET | `/api/t/{slug}/agents` | List 15 agents |
| POST | `/api/t/{slug}/agents/{key}/run` | Run one agent |
| POST | `/api/t/{slug}/pipeline/run` | Run ordered pipeline |
| GET | `/api/t/{slug}/dashboard` | Metrics, health, escalations |
| POST | `/api/t/{slug}/escalations/{id}/resolve` | Route human decision |
| GET | `/api/t/{slug}/runs` | Agent run history |
| GET | `/api/health` | Liveness + provider info |

## Project layout

```
helix/
  api/           FastAPI app + routes
  agents/        15 agent defs + OpenRouter tool loop
  tools/         schemas + tenant-scoped handlers
  db/            SQLAlchemy models, bootstrap seed
  llm/           OpenRouter / xAI client
  web/           Console UI
deploy/          Nginx configs for custom domain
docker-compose.yml
Dockerfile
```

## Security notes

- Change `HELIX_SECRET_KEY` and bootstrap password before any public deploy
- Prefer HTTPS on the custom domain
- Do not expose Postgres ports publicly
- Treat OpenRouter keys as secrets (platform or per-tenant)
- Agent tools are **allow-listed per agent** — no cross-role tool access

## License

Private / your use — adapt as needed for your product.
