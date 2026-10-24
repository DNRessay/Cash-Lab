# C-Lab (Charlie's Lab)

A personal investing tracker for South African investors: markets, portfolio, benchmarks, property and watchlist, with a read-only MCP server so an AI assistant can query your numbers.

```
backend/    FastAPI + SQLAlchemy on AWS Lambda (SAM, eu-west-1)
frontend/   Plain HTML/CSS/JS on Cloudflare Pages (no build step)
```

## What it does

- **Markets:** USD/ZAR, EUR/ZAR and GBP/ZAR, the JSE Top 40, SA property, gold and the S&P 500, with day, month and year moves.
- **Portfolio:** holdings from buys, sells and dividends (average cost), cash, gain and return, and where the money sits.
  - **Benchmark comparison:** what the same money would be worth if it had gone into the Satrix 40, the Satrix Property ETF or US dollars on the same days.
- **Holdings:** entered by hand or imported from CSV. Anything without a market price (e.g. EasyProperties) can be priced by hand.
- **Watchlist:** JSE shares and REITs with moves, dividend yield and 52-week range. Price alerts are emailed nightly at 18:00 SAST.
- **Property:** value, bond, equity, loan-to-value, gross and net yield, monthly cash flow and growth per year.
- **EasyEquities:** two independent feeds, so one keeps working if the other breaks.
  - **Login sync:** reads every account (including EasyProperties) and its holdings at EasyEquities' own prices, nightly and on "Sync now". It uses an unofficial client kept in `backend/app/invest/ee/platform.py` (adapted from easy-equities-client 0.5.0), so it can be patched here when EasyEquities changes their site. A failed sync keeps the last good read and says which step broke.
  - **Email reader:** "Sign in with Google" gives read-only access; C-Lab reads mail from easyequities.co.za through the Gmail API. Trade invoices, deposits and withdrawals become transactions; EasyProperties orders and corporate actions (dividend choices, schemes) are listed, with the ones needing a decision on top. Every email's text is kept, so after a parser fix "Re-read all emails" applies it to old mail.
  - Passwords are stored encrypted with a key derived from `SECRET_KEY`.

Prices come from Yahoo's public chart feed. JSE quotes arrive in cents and are shown in rand.

**Sign-up:** the first account created owns the app. After that, only emails listed in `SIGNUP_EMAILS` can sign up.

This is a tracker, not financial advice.

## Local dev

```bash
cd backend && pip install -r requirements-dev.txt && cp .env.example .env
uvicorn app.main:app --reload --port 8001
python -m pytest -q
cd ../frontend && npx wrangler pages dev . --port 8789
```

## Deploy

Every push to `main` runs `.github/workflows/deploy.yml`. It runs the tests, deploys the SAM stack `c-lab-api` to eu-west-1 and the Pages project `c-lab`, then smoke-tests the API.

**Repo secrets:**
- Required: `AWS_ACCESS_KEY_ID`, `AWS_SECRET_ACCESS_KEY`, `CLOUDFLARE_API_TOKEN`, `CLOUDFLARE_ACCOUNT_ID`, `CLAB_DATABASE_URL`. `CLAB_DATABASE_URL` should point to its **own** Neon database.
- Optional, for alert emails: `EMAIL_HOST`, `EMAIL_HOST_USER`, `EMAIL_HOST_PASSWORD`.
- Optional, for "Sign in with Google" (EasyEquities emails): `GOOGLE_CLIENT_ID`, `GOOGLE_CLIENT_SECRET` (or `CLAB_`-prefixed). In Google Cloud: enable the Gmail API, add the `gmail.readonly` scope to the OAuth consent screen, and add `<API URL>/api/ee/google/callback` as an authorised redirect URI on a Web OAuth client. Keep the consent screen "In production": in "Testing", Google expires the sign-in after 7 days.

- Optional, for the Refresh buttons on Company deals and the Dividend calendar: `CLAB_GH_DISPATCH_TOKEN`, a fine-grained GitHub token for this repo with **Actions: read and write**. The API uses it to start `.github/workflows/worker.yml`, which reads the articles (spaCy small model) and writes the results into the shared database. Without it the buttons say the worker isn't set up. When you run the API locally the same job runs in a thread instead.

**Optional repo variable:** `SIGNUP_EMAILS`.

The app secret key is created on the first deploy and kept in AWS SSM at `/c-lab/secret-key`.

## License

Proprietary, all rights reserved. Source is published for review only; see [LICENSE](LICENSE).
