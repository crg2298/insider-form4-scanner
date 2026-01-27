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
    try:
        data = json.loads(http_get(
            f"https://financialmodelingprep.com/api/v3/profile/{ticker}?apikey={api_key}"
        ).decode())
        return data[0].get("mktCap") if data else None
    except:
        return None

# ================= TECHNICAL INDICATORS ===================

def fetch_technical_indicators(ticker: str):
    api_key = os.getenv("FMP_API_KEY")
    if not api_key or not ticker:
        return None

    base = "https://financialmodelingprep.com/api/v3/technical_indicator/daily"
    try:
        rsi = json.loads(http_get(
            f"{base}/{ticker}?period=14&type=rsi&apikey={api_key}"
        ).decode())
        atr = json.loads(http_get(
            f"{base}/{ticker}?period=14&type=atr&apikey={api_key}"
        ).decode())
        macd = json.loads(http_get(
            f"{base}/{ticker}?type=macd&apikey={api_key}"
        ).decode())
    except:
        return None

    if not rsi or not atr or not macd:
        return None

    return {
        "rsi": round(rsi[0]["rsi"], 1),
        "atr": round(atr[0]["atr"], 2),
        "macd_cross": macd[0]["macd"] > macd[0]["signal"]
    }

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
           .replace(
               "{{SUBTITLE}}",
               f"Insider buying, analyst conviction & market signals — last {LOOKBACK_HOURS} hours"
           )
           .replace("{{UPDATED}}", now_et)
           .replace("{{HOURS}}", str(LOOKBACK_HOURS))
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
        price = float(tx.findtext(
            "transactionAmounts/transactionPricePerShare/value", "0"
        ) or 0)
        total += shares * price

    if total <= 0:
        return None

    return {
        "ticker": root.findtext("issuer/issuerTradingSymbol", ""),
        "owner": root.findtext("reportingOwner/reportingOwnerId/rptOwnerName", "Unknown"),
        "role": root.findtext(
            "reportingOwner/reportingOwnerRelationship/officerTitle", "Insider"
        ),
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

    out = []
    for i in data:
        if not i.get("priceTargetPrior") or not i.get("priceTarget"):
            continue
        if i["priceTarget"] <= i["priceTargetPrior"]:
            continue
        pct = (i["priceTarget"] - i["priceTargetPrior"]) / i["priceTargetPrior"]
        if pct < 0.07:
            continue
        out.append({
            "symbol": i["symbol"],
            "analyst": i["analystCompany"],
            "pct": round(pct * 100, 1)
        })
    return out[:5]

# ================= MAIN ===================

def main():
    state = load_state()
    technicals_cache = {}

    feed = ET.fromstring(http_get(
        "https://www.sec.gov/cgi-bin/browse-edgar?action=getcurrent&output=atom"
    ))
    ns = {"atom": "http://www.w3.org/2005/Atom"}

    cutoff = dt.datetime.utcnow() - dt.timedelta(hours=LOOKBACK_HOURS)

    hits = []
    market_caps = {}
    cluster_counts = defaultdict(int)

    total_filings = form4_filings = non_form4_filings = 0

    for entry in feed.findall("atom:entry", ns):
        total_filings += 1

        updated = entry.findtext("atom:updated", "", ns)
        if not updated:
            continue
        if dt.datetime.fromisoformat(updated.replace("Z","+00:00")).replace(tzinfo=None) < cutoff:
            continue

        filing_type = next(
            (c.get("term") for c in entry.findall("atom:category", ns)
             if c.get("label") == "form type"),
            ""
        )

        if filing_type != "4":
            non_form4_filings += 1
            continue

        form4_filings += 1

        xml_link = next(
            (l.get("href") for l in entry.findall("atom:link", ns)
             if l.get("type") == "application/xml"),
            None
        )
        if not xml_link:
            continue

        parsed = parse_form4(http_get(xml_link))
        if not parsed:
            continue

        ticker = parsed["ticker"]
        if ticker not in market_caps:
            market_caps[ticker] = fetch_market_cap(ticker)

        if not market_caps[ticker] or market_caps[ticker] < MIN_MARKET_CAP:
            continue

        if ticker not in technicals_cache:
            technicals_cache[ticker] = fetch_technical_indicators(ticker)

        parsed["market_cap"] = market_caps[ticker]
        parsed["technicals"] = technicals_cache.get(ticker)

        hits.append(parsed)
        cluster_counts[ticker] += 1
        state["last_buy_date"] = parsed["date"]

    save_state(state)
    analysts = fetch_analyst_upgrades()

    # ================= RENDER =================

    blocks = []

    # --- SYSTEM STATUS (always)
    blocks.append(f"""
    <div class="card">
      <div class="section-title">🛠 System Status</div>
      <div class="item">SEC filings scanned: {total_filings}</div>
      <div class="item">Form 4 filings scanned: {form4_filings}</div>
      <div class="item">Non-insider filings scanned: {non_form4_filings}</div>
      <div class="item">Valid insider buys detected: {len(hits)}</div>
    </div>
    """)

    # --- MARKET CONTEXT (always)
    if not hits:
        blocks.append("""
        <div class="card">
          <div class="section-title">🧭 Market Context</div>
          <div class="item">
            No qualifying insider purchases were detected in this window for companies above $1B market cap.
            Insider silence is common during earnings blackouts and risk-off regimes and is itself informative.
          </div>
        </div>
        """)

    # --- INSIDER BUYING (conditional)
    for ticker, items in defaultdict(list, {h["ticker"]: [] for h in hits}).items():
        items = [h for h in hits if h["ticker"] == ticker]
        total = sum(i["total"] for i in items)
        mkt_cap = items[0]["market_cap"]
        tech = items[0]["technicals"]

        blocks.append(f"""
        <div class="card">
          <div class="section-title">🔥 Insider Buying — {ticker}</div>
          <div class="item muted">
            {len(items)} insiders · ${total:,.0f} · {(total/mkt_cap)*100:.3f}% of market cap
            · Cluster score: {cluster_counts[ticker]}
          </div>
        """)

        if tech:
            blocks.append(f"""
            <div class="item muted">
              📈 Pre-Move Technical Context —
              RSI(14): {tech['rsi']} · ATR(14): {tech['atr']} ·
              MACD Cross: {"Yes" if tech['macd_cross'] else "No"}
            </div>
            """)

        for i in items:
            blocks.append(
                f"<div class='item'>• {i['owner']} ({i['role']}) — ${i['total']:,.0f} on {i['date']}</div>"
            )

        blocks.append("</div>")

    # --- ANALYSTS (always)
    blocks.append("<div class='card'><div class='section-title'>📊 Analyst Activity</div>")
    if analysts:
        for a in analysts:
            blocks.append(
                f"<div class='item'><strong>{a['symbol']}</strong> — "
                f"{a['analyst']} (+{a['pct']}%)</div>"
            )
    else:
        blocks.append("<div class='item muted'>No material analyst upgrades detected.</div>")
    blocks.append("</div>")

    write_daily_update_html("\n".join(blocks))


if __name__ == "__main__":
    main()
