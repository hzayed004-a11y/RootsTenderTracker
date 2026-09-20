# Deploying the operational system

The system is a web app in a Docker container. It needs no Python installed
locally and no GitHub account. Pick whichever route suits you.

---

## A. Run it on your own machine (fastest, no account needed)

```bash
docker build -t tender-screening .
docker run -p 8000:8000 -v tenderdata:/data -e APP_TOKEN=pick-a-password tender-screening
```

Open <http://localhost:8000>. The `-v tenderdata:/data` keeps the database
between restarts.

Without Docker:

```bash
pip install -r requirements.txt
sudo apt install poppler-utils          # macOS: brew install poppler
DATA_DIR=./data python -m uvicorn app:app --port 8000
```

---

## B. Railway CLI — no GitHub repo required

A project named **pharma-tender-screening** already exists in your Railway
workspace. It is empty; these commands create the service and upload this
folder directly.

```bash
npm i -g @railway/cli
railway login
cd pharma-tender-system
railway link            # choose pharma-tender-screening
railway up              # uploads this folder and builds the Dockerfile
```

Then, in the Railway dashboard for that service:

1. **Variables** -> add `APP_TOKEN` = a password of your choosing.
   Without it the URL is open to anyone who has it.
2. **Settings -> Volumes** -> add a volume mounted at `/data`.
   Skip this and the database is wiped on every redeploy.
3. **Settings -> Networking** -> Generate Domain.

---

## C. GitHub, if you want version history

```bash
cd pharma-tender-system
git init && git add . && git commit -m "Tender screening system"
gh repo create pharma-tender-screening --private --source=. --push
```

Then in Railway: New Service -> GitHub Repo -> pick it. Add the `APP_TOKEN`
variable and the `/data` volume as in route B.

---

## First run, in the browser

1. **Dashboard** — the reference data loads by itself on first boot
   (about 40 seconds: 8,691 registered products and 214 Roots products).
2. **Portal setup** -> **Probe the portal now**. The server fetches
   etenders.moh.gov.kw and reports its form fields, department options and
   results table. This is the step that could not be done from the chat
   sandbox, which blocks that host.
3. Fill in the department field name and the Medical Store option value from
   the probe output, confirm the therapeutic-area labels, and **Save**.
4. **Dashboard** -> **Run new screening cycle**.
5. **Export tracker** -> download the filled workbook.

If the probe reports a login wall, the portal needs your company
registration credentials. Add them as Railway variables `MOH_KW_USER` and
`MOH_KW_PASS` — the scraper reads them from the environment and they are
never written into any config file.

---

## Scheduling

Railway: add a second service from the same source with start command
`python run.py run` and a cron schedule, sharing the `/data` volume.

Local cron:

```cron
0 7 * * 1-5  cd /path/to/pharma-tender-system && python run.py run
```
