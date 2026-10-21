# Kleinanzeigen Deal Sniper & Valuation Bot: Detailed Architecture & Specification

## 1. Executive Summary & Objective
The objective is an automated, real-time pipeline monitoring German classified listings on **Kleinanzeigen.de** for specific high-value categories (e.g., custom gaming PCs). The system intercepts new listings, extracts unstructured hardware configurations, matches individual components against dynamic baseline pricing tables, and flags statistically significant undervalued assets via Discord webhooks while screening out fraudulent or defective entries.

---

## 2. End-to-End System Architecture

```text
[ Kleinanzeigen Ingestion Layer ]
        │  (Polled every 45–90s; TLS session persistence)
        ▼
[ In-Memory Deduplication & Fast Regex Pre-Filter ]
        │  (Drops seen IDs, non-matching terms, e.g., explicit DDR3/DDR4)
        ▼
[ Semantic Extraction Layer (Structured LLM Parsing) ]
        │  (Extracts strict component specs: CPU, GPU, RAM, Storage, PSU)
        ▼
[ Valuation Engine & Risk Heuristics ]
        │  (Component table lookup + depreciation + outlier math)
        ▼
[ Dispatcher & Discord Webhook Notification ]
```

---

## 3. Ingestion & Anti-Bot Infrastructure

### 3.1 Scraping Vector & Session State
* **Transport Emulation:** Standard HTTP clients (e.g., vanilla Python `urllib` or `requests`) fail against Kleinanzeigen due to Cloudflare JA3/JA4 fingerprint analysis. Requests must be sent through a client capable of complete browser signature emulation (HTTP/2 framing, TLS cipher suite ordering, matching ALPN parameters).
* **Session Persistence:** TCP connections and HTTP cookies must be reused across consecutive polling cycles. Forcing new TLS handshakes on every request accelerates behavioral rate-limiting.
* **Geographical Constraints:** Deploy the ingestion process exclusively on IP ranges within the European Union (preferably Germany). Foreign or data-center IP blocks outside the DACH region experience significantly higher challenge rates.

### 3.2 Polling Strategy & Target Query Construction
Instead of crawling generic top-level categories, query URLs should encode pre-filters directly into the URL parameters to conserve extraction compute:
* Category scoping: Narrow down strictly to target hardware nodes (e.g., `PCs` or `Grafikkarten`).
* Price boundaries: Set explicit floor (`min_price`) and ceiling (`max_price`) values to eliminate peripheral clutter and irrelevant ultra-budget configurations.
* Sorting: Query with `sortType=SORTING_DATE` (newest first) to restrict ingestion scanning strictly to page 1.
* Polling cadence: Randomized jittered polling (e.g., every 45 to 85 seconds) to avoid rigid temporal patterns.

### 3.3 Deduplication State Store
* Maintain a persistent set of visited listing IDs (`/s-anzeige/<id>`).
* Utilize a fast local key-value store (SQLite in WAL mode or Redis) with a 30-day Time-To-Live (TTL).
* Drop already processed IDs before parsing the page DOM or downloading listing descriptions.

---

## 4. Extraction & Normalization Pipeline

### 4.1 Tier-1 Coarse Keyword Rejection
To minimize latency and LLM token usage, incoming listings pass through immediate deterministic checks:
* **Exclusion Keywords:** Discard titles containing negative terms (e.g., *"Suche"*, *"Defekt"*, *"Tausch"*, *"Nur Gehäuse"*).
* **Negative Spec Filter:** If the target configuration requires current-generation hardware (e.g., DDR5 platforms), reject listings with obvious older architectural markers in the title (e.g., `DDR4`, `AM4`, `LGA1200`, `GTX`, `FX-`).

### 4.2 Tier-2 Semantic Extraction (Constrained LLM Schema)
Surviving candidates are processed via an extraction model using strict JSON schema enforcement.

#### JSON Target Schema
```json
{
  "is_complete_system": true,
  "cpu_model": "string or null",
  "gpu_model": "string or null",
  "ram_generation": "DDR4 | DDR5 | UNKNOWN",
  "ram_capacity_gb": "integer or null",
  "storage_capacity_gb": "integer or null",
  "motherboard_chipset": "string or null",
  "psu_wattage": "integer or null",
  "operating_system_included": "boolean",
  "defects_or_missing_parts": ["string"],
  "pickup_only": "boolean"
}
```

#### Extraction Rules
* If `ram_generation` is evaluated as anything other than `DDR5`, discard the listing immediately.
* If `is_complete_system` is false and the target is a pre-built PC, terminate processing.
* Flag any values populated in `defects_or_missing_parts` for downstream risk adjustments.

---

## 5. Valuation Engine & Mathematical Model

The valuation model avoids pure LLM pricing (which is susceptible to hallucination and data staleness) and instead computes values deterministically from structured data.

### 5.1 Component Price Lookup Table
Maintain an offline JSON lookup matrix representing the rolling second-hand fair market value (FMV) of individual parts:

| Hardware Type | Canonical Identifier | Baseline Value (EUR) |
|---|---|---|
| **CPU** | `AMD_RYZEN_5_7600X` | €155 |
| **CPU** | `AMD_RYZEN_7_7800X3D` | €310 |
| **GPU** | `NVIDIA_RTX_4070_12GB` | €440 |
| **GPU** | `NVIDIA_RTX_4080_16GB` | €820 |
| **RAM** | `DDR5_32GB_STANDARD` | €85 |
| **Storage** | `NVME_GEN4_1TB` | €50 |
| **Motherboard**| `B650_STANDARD` | €105 |
| **Case/Cooler/PSU** | `PERIPHERAL_BUNDLE_GENERIC` | €90 |

### 5.2 Bundle Valuation Formula
Total fair market value is calculated by aggregating recognized components, incorporating platform adjustments, and discounting for overall bundle depreciation:

$$\text{FMV}_{\text{gross}} = \sum_{i \in \text{components}} \text{Price}(i)$$

$$\text{FMV}_{\text{net}} = (\text{FMV}_{\text{gross}} \times (1 - \delta_{\text{bundle}})) - \sum \text{Cost}(\text{defects})$$

Where:
* $\delta_{\text{bundle}}$ = Standard bundle discount (typically $0.10$ to $0.15$), representing the discount expected when buying a complete pre-built system versus individual standalone parts.
* $\text{Cost}(\text{defects})$ = Replacement cost for missing essentials (e.g., missing storage or damaged cooler).

### 5.3 Deal Ratio & Anomaly Classification
Compute the relative margin against the seller's asking price:

$$\text{Margin \%} = \frac{\text{FMV}_{\text{net}} - \text{Price}_{\text{asking}}}{\text{FMV}_{\text{net}}} \times 100$$

* **Actionable Deal Range ($30\% \le \text{Margin} \le 55\%$):** Optimal purchase candidates. Represents sellers clearing space, mispricing slightly, or prioritizing quick liquidation.
* **Extreme Outlier Range ($\text{Margin} > 60\%$):** High risk of compromise, fraudulent listings, or defective components not captured in text. Flags must display explicit warnings.

---

## 6. Risk Management & Fraud Detection

Because Kleinanzeigen has a high prevalence of fraudulent hardware offerings, the bot extracts auxiliary trust signals:

1. **Account Registration Date:** Accounts created within the last 30 days selling high-end hardware (e.g., RTX 4080/4090) are flagged as suspicious.
2. **Badge Metadata:** Ingest user reputation ratings (`TOP`, `Freundlich`, `Zuverlässig`). Listings from accounts without rating history trigger secondary review warnings.
3. **Payment & Delivery Mode:** "Nur Abholung" (Collection only) significantly decreases fraud probability. "Versand möglich" (Shipping available) paired with an extreme outlier score automatically attaches an elevated risk label.

---

## 7. Dispatcher & Discord Integration

When a deal meets the target criteria, an alert is formatted as an embed and dispatched via Discord webhook.

### Alert Payload Contents
* **Header:** Listing Title and direct hyperlinked URL.
* **Pricing Metrics:** Asking Price, Calculated FMV, Potential Margin (€ and %), and Outlier Rating.
* **Extracted Hardware Table:** Formatted breakdown of detected components (CPU, GPU, RAM, Storage, Motherboard).
* **Seller Context:** Profile creation year, seller rating badges, location (City + Postal Code), and shipping vs. collection terms.
* **Fraud Risk Badge:** Color-coded border indicator:
  * 🟢 **Green:** Verified account + Collection available + 30–45% margin.
  * 🟡 **Yellow:** Recent account or shipping-only with 40–55% margin.
  * 🔴 **Red:** Extreme outlier (>60% margin) or known scam signature.