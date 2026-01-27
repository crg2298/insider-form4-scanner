import datetime as dt
import os
import json
import urllib.request
import xml.etree.ElementTree as ET
from collections import defaultdict
from datetime import timezone
from zoneinfo import ZoneInfo

# ================= CONFIG =================

LOOKBACK_HOURS = int(os.getenv("LOOKBACK_HOURS", "336"))  # 14 days
MIN_MARKET_CAP = 1_000_000_000  # $1B minimum
SEC_USER_AGENT = "Form4Scanner/1.0 (contact: ginsbergcaleb71@gmail.com)"
STATE_FILE = "docs/state.json"

# ================= HTTP ===================

def http_get(url: str) -> bytes:
    req = urllib.request.Request(url)
    req.add_header("User-Agent", SEC_USER_AGENT)
    req.add_header("Accept", "*/*")
    with urllib.request.urlopen(req, timeout=30) as resp:
        return resp.read()

# ================= MARKET CAP ===================

def fetch_market_cap(ticker: str):
    api_key = os.getenv("FMP_API_KEY")
    if not api_key or not ticker:
        return None
    url = f"https://financialmodelingprep.com/api/v3/profile/{ticker}?apikey={api_key}"
    try:
        data = json.loads(http_get(url).decode())
        return data[0].get("mktCap") if data else None
    except:
        return None

# ================= HTML ===================

def write_daily_update_html(body_html: str):
    with open("docs/template.html", "r", encoding="utf-8") as f:
        tpl = f.read()

    now_et = (
        dt.datetime.now(timezone.utc)
        .astimezone(ZoneInfo("America/New_York"))
        .strftime("%Y-%m-%d %I:%M %p ET")
    )

    html = (
        tpl.replace("{{TITLE}}", "Daily Insider Log")
           .replace("{{H1}}", "Daily Insider Log")
           .replace("{{SUBTITLE}}", f"Insider & analyst activity — last {LOOKBACK_HOURS} hours")
           .replace("{{UPDATED}}", now_et)
           .replace("{{BODY}}", body_html)
    )

    os.makedirs("docs", exist_ok=True)
    with open("docs/index.html", "w", encoding="utf-8") as f:
        f.write(html)

# ================= STATE ===================

def load_state():
    if not os.path.exists(STATE_FILE):
        return {}
    try:
        return json.load(open(STATE_FILE))
    except:
        return {}

def save_state(state):
    os.makedirs("docs", exist_ok=True)
    json.dump(state, open(STATE_FILE, "w"))

# ================= FORM 4 ===================

def parse_form4(xml_bytes):
    root = ET.fromstring(xml_bytes)
    ticker = root.findtext("issuer/issuerTradingSymbol", "")
    owner = root.findtext("reportingOwner/reportingOwnerId/rptOwnerName", "Unknown")
    role = root.findtext("reportingOwner/reportingOwnerRelationship/officerTitle", "Insider")

    total = 0.0
    date = ""

    nd = root.find("nonDerivativeTable")
    if nd is None:
        return None

    for tx in nd.findall("nonDerivativeTransaction"):
        if tx.findtext("transactionCoding/transactionCode") != "P":
            continue
        date = tx.findtext("transactionDate/value", "")
        shares = float(tx.findtext("transactionAmounts/transactionShares/value", "0"))
        price = float(tx.findtext("transactionAmounts/transactionPricePerShare/value", "0") or 0)
        total += shares * price

    if total <= 0:
        return None

    return {
        "ticker": ticker,
        "owner": owner,
        "role": role,
        "total": round(total, 2),
        "date": date
    }

# ================= ANALYSTS ===================

def fetch_analyst_upgrades():
    api_key = os.getenv("FMP_API_KEY")
    if not api_key:
        return []
    try:
        data = json.loads(http_get(
            f"https://financialmodelingprep.com/api/v3/price-target-rss-feed?apikey={api_key}"
        ).decode())
    except:
        return []

    return [
        {
            "symbol": i["symbol"],
            "analyst": i["analystCompany"],
            "pct": round((i["priceTarget"] - i["priceTargetPrior"]) / i["priceTargetPrior"] * 100, 1)
        }
        for i in data
        if i.get("priceTarget") and i.get("priceTargetPrior")
        and i["priceTarget"] > i["priceTargetPrior"]
        and (i["priceTarget"] - i["priceTargetPrior"]) / i["priceTargetPrior"] >= 0.07
    ][:5]

# ================= MAIN ===================

def main():
    state = load_state()
    rss = "https://www.sec.gov/cgi-bin/browse-edgar?action=getcurrent&output=atom"
    feed = ET.fromstring(http_get(rss))
    ns = {"atom": "http://www.w3.org/2005/Atom"}

    cutoff = dt.datetime.utcnow() - dt.timedelta(hours=LOOKBACK_HOURS)

    total_filings = 0
    form4_filings = 0
    hits = []
    clusters = defaultdict(int)

    for entry in feed.findall("atom:entry", ns):
        total_filings += 1
        updated = entry.findtext("atom:updated", "", ns)
        if not updated:
            continue
        if dt.datetime.fromisoformat(updated.replace("Z","+00:00")).replace(tzinfo=None) < cutoff:
            continue

        filing_type = ""
        for c in entry.findall("atom:category", ns):
            if c.get("label") == "form type":
                filing_type = c.get("term")

        if filing_type != "4":
            continue
        form4_filings += 1

        xml = None
        for l in entry.findall("atom:link", ns):
            if l.get("type") == "application/xml":
                xml = l.get("href")

        if not xml:
            continue

        parsed = parse_form4(http_get(xml))
        if not parsed:
            continue

        cap = fetch_market_cap(parsed["ticker"])
        if not cap or cap < MIN_MARKET_CAP:
            continue

        parsed["market_cap"] = cap
        hits.append(parsed)
        clusters[parsed["ticker"]] += 1
        state["last_buy_date"] = parsed["date"]

    save_state(state)

    analysts = fetch_analyst_upgrades()

    # ================= TRADER INTELLIGENCE =================

    insider_count = len(hits)
    cluster_max = max(clusters.values()) if clusters else 0

    if insider_count == 0:
        insider_regime = "Silent"
    elif insider_count < 3:
        insider_regime = "Sparse"
    else:
        insider_regime = "Active"

    divergence = "Yes" if analysts and not hits else "No"

    takeaway = (
        "Insider participation remains limited relative to historical norms, suggesting elevated "
        "uncertainty despite ongoing analyst activity."
        if insider_regime == "Silent" else
        "Selective insider accumulation is occurring, with clustering indicating targeted conviction."
    )

    blocks = [f"""
    <div class="card hero">
      <div class="section-title">🧠 Trader Intelligence Summary</div>
      <div class="item"><strong>Insider regime:</strong> {insider_regime}</div>
      <div class="item"><strong>Insider–Analyst divergence:</strong> {divergence}</div>
      <div class="item"><strong>Max cluster score:</strong> {cluster_max}</div>
      <div class="item"><strong>Trader takeaway:</strong> {takeaway}</div>
    </div>
    """]

    blocks.append(f"""
    <div class="card">
      <div class="section-title">🛠 System Status</div>
      <div class="item">SEC filings scanned: {total_filings}</div>
      <div class="item">Form 4 filings scanned: {form4_filings}</div>
      <div class="item">Valid insider buys: {insider_count}</div>
    </div>
    """)

    write_daily_update_html("\n".join(blocks))

if __name__ == "__main__":
    main()
