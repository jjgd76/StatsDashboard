# GenAI Adoption Dashboard

A self-hosted dashboard that aggregates usage statistics from **Claude (Anthropic)**, **ChatGPT (OpenAI)**, and **Microsoft 365 Copilot** by uploading CSV exports from each platform's admin console.

> **No live API keys required.** Export stats manually from each tool, upload the CSV, and the dashboard renders instantly. Live API polling can be added later — see [Adding a New Data Source](#adding-a-new-data-source).

---

## Architecture

```
frontend/index.html          ← single self-contained HTML page (Chart.js, vanilla JS)
backend/
  main.py                    ← FastAPI: /upload/{platform}, /metrics/summary, /metrics/trend
  storage.py                 ← Azure Table Storage (MetricsCache + MetricsRaw)
  parsers/
    anthropic_parser.py      ← parses Anthropic Console CSV exports
    openai_parser.py         ← parses OpenAI Platform CSV exports
    copilot_parser.py        ← parses M365 Admin Center CSV exports
sample_data/                 ← sample CSV files to test with
infra/
  deploy.sh                  ← one-shot Azure deployment (Container App + Static Web App)
  keyvault.sh                ← populate Key Vault with future API keys
```

---

## Prerequisites

- Python 3.11+
- Docker Desktop (for local container testing or deployment)
- Azure CLI (`az`) — for deployment only
- An Azure subscription — for deployment only

---

## How to Export Stats From Each Platform

### Claude — Anthropic Console
1. Go to [console.anthropic.com](https://console.anthropic.com)
2. Navigate to **Settings → Usage**
3. Select your date range (up to 90 days)
4. Click **Export CSV**
5. Upload the downloaded file to the dashboard under **Claude (Anthropic)**

Expected columns: `Date, Model, Input Tokens, Output Tokens, Requests, Cost`

---

### ChatGPT — OpenAI Platform
1. Go to [platform.openai.com](https://platform.openai.com)
2. Navigate to **Usage** in the left sidebar
3. Select your date range
4. Click **Export** → download CSV
5. Upload under **ChatGPT (OpenAI)**

Expected columns: `Date, Model, Requests, Input Tokens, Output Tokens, Cost ($)`

---

### M365 Copilot — Microsoft 365 Admin Center
1. Go to [admin.microsoft.com](https://admin.microsoft.com)
2. Navigate to **Reports → Microsoft 365 Reports → Microsoft 365 Copilot**
   (or search "Copilot" in the Reports section)
3. Select your period (7, 30, 90, or 180 days)
4. Click **Export** to download the CSV
5. Upload under **M365 Copilot**

Two report formats are supported:
- **Summary report** — one row with aggregate active users per app
- **User-level report** — one row per user with Yes/No per app (auto-detected)

---

## Local Development

### Option A — Docker Compose (recommended, one command)

```bash
cd genai-dashboard
docker compose up --build
```

- Frontend: http://localhost:3000
- Backend API: http://localhost:8000
- API docs: http://localhost:8000/docs
- Azure Storage is emulated locally by [Azurite](https://github.com/Azure/Azurite) — no Azure account needed

### Option B — Python + Azurite

**1. Start Azurite (local Azure Storage emulator)**
```bash
# via npm
npx azurite --tableHost 0.0.0.0 --silent

# or via Docker
docker run -p 10002:10002 mcr.microsoft.com/azure-storage/azurite azurite-table --tableHost 0.0.0.0
```

**2. Configure environment**
```bash
cd genai-dashboard
cp .env.example .env
```
Edit `.env` and set:
```
AZURE_STORAGE_CONNECTION_STRING=DefaultEndpointsProtocol=http;AccountName=devstoreaccount1;AccountKey=Eby8vdM02xNOcqFlqUwJPLlmEtlCDXJ1OGLjX+N4oYp9MVaGYO+YDWF7LtJ3VXQZ7DlB2IyQ==;TableEndpoint=http://127.0.0.1:10002/devstoreaccount1;
```

**3. Install dependencies and run**
```bash
cd backend
pip install -r requirements.txt
uvicorn main:app --reload --port 8000
```

**4. Serve the frontend**
```bash
cd ../frontend
python -m http.server 3000
```
Open http://localhost:3000

**5. Upload sample data**
```bash
# Test with included sample files
curl -X POST http://localhost:8000/upload/anthropic -F "file=@../sample_data/anthropic_sample.csv"
curl -X POST http://localhost:8000/upload/openai    -F "file=@../sample_data/openai_sample.csv"
curl -X POST http://localhost:8000/upload/copilot   -F "file=@../sample_data/copilot_sample_summary.csv"
```

Or drag-and-drop the files directly in the browser UI.

---

## Deployment to Azure

**1. Populate Key Vault** (optional — for future live API keys)
```bash
bash infra/keyvault.sh
```

**2. Deploy everything**
```bash
bash infra/deploy.sh
```

This creates:
- Azure Container Registry (hosts the backend Docker image)
- Azure Storage Account (two tables: `MetricsCache`, `MetricsRaw`)
- Azure Key Vault (secrets)
- Azure Container App (backend API, external ingress)
- Azure Static Web App (frontend)

**3. Update BACKEND_URL in the frontend**

After deploy.sh finishes it prints the backend URL. Edit `frontend/index.html`:
```js
const BACKEND_URL = 'https://your-backend.azurecontainerapps.io';
```
Then redeploy the frontend to the Static Web App.

---

## API Reference

| Method | Endpoint | Description |
|--------|----------|-------------|
| `GET`  | `/health` | Health check |
| `POST` | `/upload/{platform}` | Upload CSV export. `platform`: `anthropic`, `openai`, `copilot` |
| `GET`  | `/metrics/summary?period=30` | Latest snapshot for all platforms. `period`: 7, 30, 90 |
| `GET`  | `/metrics/trend?platform=all&days=30` | Daily trend series for charting |
| `GET`  | `/uploads/history` | Last upload timestamp per platform |

Interactive docs: http://localhost:8000/docs

---

## Adding a New Data Source

1. **Create a parser** in `backend/parsers/new_parser.py`:
   ```python
   def parse_new_platform_csv(content: bytes) -> dict:
       # Return dict with keys: platform, period_days, total_requests,
       # total_cost_usd, daily_trend, model_breakdown, fetched_at
       ...
   ```

2. **Register it** in `backend/parsers/__init__.py` and `backend/main.py`:
   ```python
   # main.py — add to _PLATFORM_PARSERS
   "newplatform": ("new_platform", parse_new_platform_csv),
   ```

3. **Add an upload card** in `frontend/index.html`:
   ```html
   <div class="upload-zone" id="zone-newplatform" data-platform="newplatform">
     ...
   </div>
   ```

4. **Add KPI cards and chart datasets** in the `PLATFORMS` array in `index.html`.

---

## Troubleshooting

### "No data yet" on first load
Upload at least one CSV export. The dashboard shows this message until data exists in storage.

### Backend unreachable
- Confirm `uvicorn` is running: `curl http://localhost:8000/health`
- Check `CORS_ORIGINS` in `.env` includes your frontend origin
- If using Docker Compose, ensure all containers are up: `docker compose ps`

### Upload returns 422 Unprocessable Entity
The parser could not recognise the column names in your CSV.
- Open the CSV in a text editor and check the first row (headers)
- Compare with the expected formats in each `parsers/*.py` file header comment
- Try the sample files in `sample_data/` to verify the pipeline works end-to-end

### Azure Storage connection errors
- Local: confirm Azurite is running on port 10002
- Azure: confirm `AZURE_STORAGE_CONNECTION_STRING` is set correctly in `.env` or Key Vault

### M365 Copilot shows zeroes for app breakdown
The summary report format varies by tenant configuration. Try exporting the **user-level** report instead (`copilot_sample_users.csv` format). Both are auto-detected.
