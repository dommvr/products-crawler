# Products Crawler

Crawls company websites, extracts product data using an LLM, and writes everything to a Google Sheets spreadsheet.

---

## What it does

1. Reads a list of company names or URLs from `names.txt`
2. Finds the official website if only a name is given (via Serper search)
3. Crawls the entire site (follows pagination, subdomains, product listings)
4. Clicks size/variant selectors on product pages to capture each size as a separate row
5. Uses OpenAI to extract only products matching your configured categories
6. Writes results to Google Sheets (and optionally a local CSV backup)

---

## Setup

### 1. Clone and install dependencies

```bash
git clone https://github.com/dommvr/products-crawler.git
cd products-crawler

python -m venv .venv
source .venv/bin/activate        # Windows: .venv\Scripts\activate

pip install -r requirements.txt
playwright install chromium
```

### 2. Get API keys

**OpenAI API key** — from https://platform.openai.com/api-keys

**Serper API key** — from https://serper.dev (free tier: 2500 searches/month)

Set them as environment variables (recommended — keeps secrets out of files):

```bash
# Linux / macOS
export OPENAI_API_KEY="sk-..."
export SERPER_API_KEY="..."

# Windows (PowerShell)
$env:OPENAI_API_KEY="sk-..."
$env:SERPER_API_KEY="..."
```

Or paste them directly into `config.yaml` under `openai.api_key` and `search.api_key`.

### 3. Set up Google Cloud (one-time)

#### a) Create a project
- Go to https://console.cloud.google.com
- Click **New Project**, give it a name, note the **Project ID**

#### b) Enable APIs
- Go to **APIs & Services → Library**
- Search for and enable: **Google Sheets API**
- Search for and enable: **Google Drive API**

#### c) Create a service account
- Go to **APIs & Services → Credentials**
- Click **Create Credentials → Service Account**
- Give it a name (e.g. `sheets-agent`) → click through to Done
- Click the service account → **Keys** tab → **Add Key → Create New Key → JSON**
- Save the downloaded file as **`credentials.json`** in the project root

#### d) Share your spreadsheet with the service account
- Open `credentials.json` and copy the `client_email` value
  (looks like `sheets-agent@your-project.iam.gserviceaccount.com`)
- Open Google Sheets, create a sheet (or the crawler creates one automatically)
- Click **Share**, paste the email, give it **Editor** access

> **Important:** `credentials.json` is in `.gitignore` — never commit it.

### 4. Configure

Edit `config.yaml` to match your needs. Key settings:

```yaml
input:
  names_file: "names.txt"
  company_category_hint: "materace"   # helps search find the right site

products:
  categories:
    - "materace"                       # only extract these product types

crawling:
  max_pages: 20                        # per company
  delay_between_requests: 2.0          # seconds between requests

output:
  spreadsheet_name: "products_data"
  sheet_mode: "new"                    # new / append / overwrite
  save_csv_backup: false
```

### 5. Add your companies

Edit `names.txt` — one entry per line:

```
# You can use names, domains, or full URLs
jysk.pl
janpol.pl
sealy
https://www.hilding.com
```

---

## Run

```bash
python -m src.main
```

With debug logging:

```bash
python -m src.main --debug
```

Custom config file:

```bash
python -m src.main --config my_config.yaml
```

---

## Output spreadsheet columns

| Column | Description |
|---|---|
| Company Name | Company display name |
| Category | Matched product category |
| Product Name | Full name including size variant |
| Description | All available info: materials, dimensions, firmness, features, price |
| URL | Page where the product was found |
| Photo URL | Direct image URL (if available) |
| Confidence | 0.0–1.0 — LLM certainty this is a matching product |

---

## Project structure

```
products-crawler/
├── config.yaml         # all settings
├── names.txt           # input: companies to crawl
├── credentials.json    # Google service account (you provide, gitignored)
├── requirements.txt
├── .gitignore
├── README.md
└── src/
    ├── main.py         # entry point
    ├── config.py       # loads config.yaml
    ├── resolver.py     # URL detection + website search
    ├── crawler.py      # Playwright crawler (pagination, subdomains, variants)
    ├── extractor.py    # OpenAI product extraction
    ├── sheets.py       # Google Sheets + CSV writer
    └── utils.py        # helpers, skip list, logging
```

---

## Tips

- **Rate limiting:** Increase `delay_between_requests` if sites start blocking you (try 3–5 seconds).
- **Debugging:** Set `headless: false` in config to watch the browser crawl in real time.
- **Cost:** With `gpt-4o-mini` at default settings, crawling ~20 pages per company costs roughly $0.01–0.05 per company.
- **Many companies:** For batches of 50+, consider setting `max_pages: 10` to stay within budget.
- **CSV backup:** Set `save_csv_backup: true` in config to also get a local `.csv` in the `outputs/` folder.
