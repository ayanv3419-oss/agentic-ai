# Deployment Guide — Agentic AI Platform

> Backend → Render | Frontend → Vercel
> Last updated: 2025-05-15

---

## Architecture: What goes where

```
GitHub (ayanv3419-oss/agentic-ai)
    │
    ├── Backend  →  Render (Python / FastAPI)
    │                  URL: https://agentic-ai-backend.onrender.com
    │                  Disk: 1 GB persistent (SQLite + uploads)
    │
    └── Frontend →  Vercel (React / Vite)
                       URL: https://agentic-ai-xxx.vercel.app
                       Env: VITE_BACKEND_URL → points at Render URL
```

---

## Prerequisites

| Tool | Version | Check |
|---|---|---|
| Node.js | ≥ 18 | `node --version` |
| npm | ≥ 9 | `npm --version` |
| Python | ≥ 3.11 | `python --version` |
| git | any | `git --version` |
| GitHub account | — | repo: `ayanv3419-oss/agentic-ai` |

---

## Step 1 — Deploy Backend to Render

### 1.1 Create account
1. Go to **[render.com](https://render.com)**
2. Click **Get Started for Free**
3. Sign up with GitHub

### 1.2 Create Web Service
1. Dashboard → **New +** → **Web Service**
2. Connect GitHub → select **`ayanv3419-oss/agentic-ai`**
3. Render auto-detects `render.yaml` → click **Apply**

This creates:
- A **web service** named `agentic-ai-backend`
- A **1 GB persistent disk** mounted at `/data`

### 1.3 Add environment variables
In the Render dashboard → your service → **Environment** tab, add:

| Key | Value | Notes |
|---|---|---|
| `FINANCIAL_DB_PATH` | `/data/financial_records.db` | Set by render.yaml |
| `RESPONSE_STORE_PATH` | `/data/response_store.json` | Set by render.yaml |
| `ALLOWED_ORIGINS` | `https://your-app.vercel.app` | Set AFTER Vercel deploy |
| `GROQ_API_KEY` | `gsk_...` | Optional — users can send their own key |
| `SENTRY_DSN` | `https://...@sentry.io/...` | Optional |

### 1.4 Deploy
Click **Deploy** (or it starts automatically on first connection).
Wait ~3-5 minutes for the first build.

### 1.5 Verify
Once deployed, visit:
```
https://agentic-ai-backend.onrender.com/health
```
Expected response:
```json
{
  "status": "ok",
  "version": "3.1.0-no-auth",
  "database": {"kind": "sqlite", "status": "ok"},
  "sales_rows": 0,
  "purchase_rows": 0
}
```

Copy your Render URL — you need it for Step 2.

---

## Step 2 — Deploy Frontend to Vercel

### 2.1 Create account
1. Go to **[vercel.com](https://vercel.com)**
2. Click **Sign Up** → **Continue with GitHub**

### 2.2 Import project
1. Dashboard → **Add New...** → **Project**
2. Find `ayanv3419-oss/agentic-ai` → click **Import**

### 2.3 Configure build settings
In the "Configure Project" screen:

| Setting | Value |
|---|---|
| **Root Directory** | `frontend` |
| **Framework Preset** | Vite (auto-detected) |
| **Build Command** | `npm run build` |
| **Output Directory** | `dist` |

### 2.4 Add environment variables
Click **Environment Variables** and add:

| Key | Value |
|---|---|
| `VITE_BACKEND_URL` | `https://agentic-ai-backend.onrender.com` |

Use the exact URL from Step 1.5.

### 2.5 Deploy
Click **Deploy**. Wait ~2 minutes.

Vercel gives you a URL like:
```
https://agentic-ai-xxxxx.vercel.app
```

### 2.6 Verify
Open the Vercel URL in a browser.
You should see the login screen.
Default credentials: `Mansuri` / `182012`

---

## Step 3 — Connect Render + Vercel

Now that you have the Vercel URL, update CORS on Render:

1. Render dashboard → your service → **Environment**
2. Set `ALLOWED_ORIGINS` to your Vercel URL:
   ```
   ALLOWED_ORIGINS=https://agentic-ai-xxxxx.vercel.app
   ```
3. Click **Save Changes** → Render restarts the service (~30 seconds)

---

## Step 4 — Smoke Test

1. Open your Vercel URL
2. Log in (`Mansuri` / `182012`)
3. Go to **Shop Info** → enter a shop name + your Groq API key
4. Go to **Upload Data** → upload a CSV file
5. Go to **Dashboard** → verify KPI numbers appear
6. Go to **AI Assistant** → ask "what are my total sales?"
7. Verify you get a streamed answer with tool events

---

## Environment Variables Reference

### Backend (Render)

| Variable | Required | Default | Description |
|---|---|---|---|
| `FINANCIAL_DB_PATH` | Yes | `data/financial_records.db` | SQLite DB path |
| `RESPONSE_STORE_PATH` | No | `data/response_store.json` | Response cache path |
| `ALLOWED_ORIGINS` | Yes (prod) | `*` | Comma-separated allowed frontend origins |
| `GROQ_API_KEY` | No | — | Server-side Groq key (users can send their own) |
| `GROQ_MODEL` | No | `llama-3.3-70b-versatile` | Default LLM model |
| `MAX_LOOP_ITERATIONS` | No | `8` | Max pipeline steps per turn |
| `COST_LIMIT_USD` | No | `1.0` | Max Groq spend per turn |
| `MAX_UPLOAD_BYTES` | No | `52428800` | Max upload size (50 MB) |
| `RATE_LIMIT_PER_MINUTE` | No | `30` | Query rate limit per IP |
| `SENTRY_DSN` | No | — | Sentry error tracking |
| `HOST` | No | `0.0.0.0` | Bind address |
| `PORT` | No | `8000` | Bind port (Render sets this automatically) |

### Frontend (Vercel)

| Variable | Required | Default | Description |
|---|---|---|---|
| `VITE_BACKEND_URL` | Yes | — | Full Render backend URL |
| `VITE_SENTRY_DSN` | No | — | Sentry for frontend errors |

---

## Render Free Tier Limitations

| Limitation | Impact |
|---|---|
| Service spins down after 15 min inactivity | First request after idle takes 30-60s |
| 512 MB RAM | Fine for SQLite + small datasets |
| 1 GB disk (paid add-on) | 1 GB is plenty for MVP data |
| Shared CPU | Analytics on large datasets will be slow |

**To avoid spin-down:** Use a free uptime monitor (UptimeRobot) to ping `/health` every 10 minutes.

---

## Updating the Deployment

Both Render and Vercel auto-deploy on every push to `main`.

```bash
# Make changes locally
git add .
git commit -m "your message"
git push origin main
# → Render rebuilds backend automatically
# → Vercel rebuilds frontend automatically
```

---

## Rollback

### Render
Dashboard → your service → **Deploys** → click any previous deploy → **Rollback to this deploy**

### Vercel
Dashboard → your project → **Deployments** → click `...` on any deploy → **Promote to Production**

---

## Local Development

### Backend
```bash
cd agentic-ai
cp backend/.env.example backend/.env
# Edit backend/.env — set GROQ_API_KEY at minimum
pip install -r backend/requirements.txt
python backend/main.py
# Backend runs at http://localhost:8000
```

### Frontend
```bash
cd agentic-ai/frontend
cp .env.example .env.local
# Edit .env.local — set VITE_BACKEND_URL=http://localhost:8000
npm install
npm run dev
# Frontend runs at http://localhost:5173
```

### Both together (from repo root)
```bash
cd agentic-ai
npm install          # installs concurrently
npm run dev          # starts both in parallel
```

---

## Troubleshooting

### "CORS blocked" in browser console
→ `ALLOWED_ORIGINS` on Render doesn't include your Vercel URL.
Set it exactly: `https://your-app.vercel.app` (no trailing slash).

### Backend returns 503 on first request
→ Render free tier spun down. Wait 30 seconds and retry.

### "Missing Groq API key" error
→ Set your Groq key in the app's **Shop Info** page. Get one free at [console.groq.com](https://console.groq.com).

### Upload fails with "File too large"
→ Default max is 50 MB. Increase `MAX_UPLOAD_BYTES` in Render environment if needed.

### Dashboard shows 0 for all KPIs
→ Upload data first (Upload Data page). Dashboard requires at least one active dataset.

### SQLite data lost after Render restart
→ Verify `FINANCIAL_DB_PATH=/data/financial_records.db` and that the disk is attached in Render.
The disk persists across restarts; the in-memory `/tmp` does not.
