# Products Crawler

Crawls mattress retailer / manufacturer websites, extracts each product with an LLM, determines **whether the mattress is made with natural coconut fiber or sisal**, and writes everything to Google Sheets (with the coconut/sisal rows highlighted green) and a local CSV.

Built for Polish and German mattress sites, but the target categories are configurable.

---

## What it does

1. Reads a list of company names / domains / URLs from `names.txt`.
2. For a bare name, finds the official website via Serper search (using `company_category_hint` to disambiguate).
3. Crawls each site with a headless browser, in **two phases**:
   - **Main crawl** — walks category / listing pages (priority queue, pagination, subdomains) and collects every product, plus the direct link to each product's own detail page.
   - **Fiber pass** — visits one detail page per product and reads its full material description to decide `yes` / `no` / `""` for natural coconut fiber / sisal.
4. Filters out non-mattress items (toppers, bed frames, pillows, duvets, furniture), other-language duplicate pages, and category/teaser pseudo-products.
5. Collapses size / firmness / colour variants of the same product into **one** row.
6. Writes results to Google Sheets — **rows containing coconut fiber / sisal are highlighted green** — and to a local CSV backup.

Several companies are crawled concurrently, and within each site the fiber pass runs concurrently, with automatic backoff/retry on OpenAI rate limits.

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

**Serper API key** — from https://serper.dev (free tier: 2500 searches/month). Only needed when `names.txt` contains bare company names that must be searched for; not needed if you only provide URLs.

Set them as environment variables (recommended — keeps secrets out of files):

```bash
# Linux / macOS
export OPENAI_API_KEY="sk-..."
export SERPER_API_KEY="..."

# Windows (PowerShell)
$env:OPENAI_API_KEY="sk-..."
$env:SERPER_API_KEY="..."
```

You can also put them in a `.env` file in the project root (it is gitignored and loaded automatically):

```
OPENAI_API_KEY=sk-...
SERPER_API_KEY=...
```

Or paste them directly into `config.yaml` under `openai.api_key` and `search.api_key`.

> Precedence: system environment variables → `.env` → `config.yaml`. Existing env vars are never overwritten.

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

> **Important:** `credentials.json` and `.env` are in `.gitignore` — never commit them.

### 4. Configure

Edit `config.yaml` to match your needs. Key settings:

```yaml
input:
  names_file: "names.txt"
  company_category_hint: "materace"   # helps search find the right site

products:
  categories:
    - "materace"                       # only extract these product types
  extract_size_variants: false         # false = one row per product (variants collapsed)

crawling:
  max_pages: 20                        # main-crawl page budget per company (0 = unlimited)
  delay_between_requests: 0.5          # seconds between requests
  follow_subdomains: true
  render_wait: 0.8                     # seconds to let client-side JS render
  fiber_pass_concurrency: 6            # detail pages checked at once during the fiber pass
  company_concurrency: 3               # companies crawled at once (each opens its own browser)

llm:
  model: "gpt-4o-mini"
  max_retries: 6                       # backoff/retry on OpenAI 429 rate-limit errors

output:
  spreadsheet_name: "products_data"
  sheet_mode: "overwrite"              # new / append / overwrite
  save_csv_backup: true                # also write a local CSV to outputs/
  csv_output_dir: "outputs"
```

> **Concurrency vs. your OpenAI tier:** total in-flight requests ≈ `company_concurrency × fiber_pass_concurrency`. If you hit frequent 429s, the backoff keeps results complete but throttles you to your tokens-per-minute (TPM) ceiling. On a low TPM tier, lower these (e.g. `2 × 3`) for a smoother run, or raise your OpenAI tier for real speed.

### 5. Add your companies

Edit `names.txt` — one entry per line. Names, domains, or full URLs all work; lines starting with `#` are ignored. Passing a direct category URL (rather than a bare name) is the most reliable.

```
jysk
https://www.janpol.pl/kategoria-produktu/materace/
https://optimum-materace.pl/kategorie-materacow/
hilding.pl
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

## Output columns

| Column | Description |
|---|---|
| Company Name | Company display name |
| Category | Matched product category |
| Product Name | Product name, without the size/firmness/colour variant |
| URL | Direct product page link (falls back to the listing page if no detail page was found) |
| Photo URL | Direct image URL (if available) |
| Confidence | 0.0–1.0 — LLM certainty this is a matching product |
| Contains Natural Fiber | `yes` (coconut fiber / sisal present), `no` (described, none found), or `""` (no description to judge from) |
| Fiber Confidence | 0.0–1.0 — certainty of the fiber verdict |
| Fiber Evidence | Verbatim snippet from the page that backed the verdict |

Rows where **Contains Natural Fiber = `yes`** are highlighted **green** in the spreadsheet.

> **`coconut latex` / `lateks kokosowy` counts as `yes`** (it contains coconut fiber). Pure natural latex, rubber-tree milk, wool, foams, and springs do **not** qualify.

---

## How it works

- **Priority-queue crawl.** Product/category URLs are visited first; utility, blog, and non-mattress pages are deprioritized or skipped. The crawl stops early once the high-priority queue is drained and several pages in a row yield nothing.
- **Detail-link detection.** Product detail URLs are recognized across several site patterns (`/produkty/<slug>`, `/produkt/<category>/<slug>`, German `/produkte/…`, Elementor/HappyAddons JS card links, long descriptive slugs, etc.) and routed to the fiber pass instead of being re-crawled.
- **Fiber pass.** Each unique product's detail page is read once. Fiber detection is **only** done here — never on category pages — so a listing's general text can't contaminate a product's verdict. The model is told the page is about a single product, to avoid "related products" carousels leaking in.
- **Variant collapsing.** Size, firmness, and colour variants are reduced to one product (in both the URL queue and the final output), keeping the record with the direct product link.
- **Filtering.** Accessories (toppers, covers, frames, slats, legs, pillows, duvets), furniture, language-switcher duplicates (`/en/`, `/de/`), and bare category / homepage-teaser pseudo-products are dropped.
- **Global dedup.** After all companies are crawled, duplicates of the same product across multiple `names.txt` entries for the same site are collapsed (keeping the highest-quality record).
- **Rate-limit resilience.** OpenAI 429 / transient errors are retried with exponential backoff that honors the server's `Retry-After`, so extractions aren't silently dropped under load.

---

## Project structure

```
products-crawler/
├── config.yaml         # all settings
├── names.txt           # input: companies to crawl
├── credentials.json    # Google service account (you provide, gitignored)
├── .env                # optional API keys (gitignored)
├── requirements.txt
├── .gitignore
├── README.md
├── outputs/            # local CSV backups
└── src/
    ├── main.py         # entry point: orchestrates concurrent company crawls + global dedup
    ├── config.py       # loads config.yaml + .env
    ├── resolver.py     # bare-name → website resolution (Serper search)
    ├── crawler.py      # Playwright crawler: priority queue, fiber pass, filtering, dedup
    ├── extractor.py    # OpenAI product + coconut-fiber/sisal extraction (with retry/backoff)
    ├── sheets.py       # Google Sheets writer (green highlighting) + CSV fallback
    └── utils.py        # helpers, skip list, logging
```

---

## Tips

- **Speed / cost is bounded by your OpenAI TPM tier.** See the concurrency note in step 4. Raising the tier is the most effective speedup; lowering concurrency reduces 429 thrash.
- **Rate limiting (sites):** increase `delay_between_requests` if a site starts blocking you.
- **Debugging:** set `headless: false` to watch the browser crawl in real time, and run with `--debug` for verbose logs.
- **Cost:** with `gpt-4o-mini`, expect roughly $0.01–0.05 per company at default settings.
- **CSV backup:** `save_csv_backup: true` writes a timestamped `.csv` to `outputs/`. A CSV is also written automatically if the Google Sheets write fails (e.g. Drive quota), so data is never lost.
- **Sheet modes:** `new` creates a fresh timestamped spreadsheet; `overwrite` clears and rewrites the named sheet (and removes stale green highlights); `append` adds to it and highlights only the new rows.
