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
MIN_MARKET_CAP = 100_000_000  # $100M minimum
SEC_USER_AGENT = "Form4Scanner/1.0 (contact: ginsbergcaleb71@gmail.com)"

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

# ================= FORM 4 (ROBUST + REAL) ===================

def parse_form4(xml_bytes):
    root = ET.fromstring(xml_bytes)
    ns = {"ns": root.tag.split("}")[0].strip("{")}

    total_shares = 0.0
    total_value = 0.0
    date = ""
    codes_seen = []

    # --- NON-DERIVATIVE TRANSACTIONS ---
    for tx in root.findall(".//ns:nonDerivativeTransaction", ns):
        code = tx.findtext(".//ns:transactionCode", "", ns)
        shares = float(tx.findtext(".//ns:transactionShares/ns:value", "0", ns))
        price = float(tx.findtext(".//ns:transactionPricePerShare/ns:value", "0", ns) or 0)

        codes_seen.append(code)
        total_shares += shares
        if price > 0:
            total_value += shares * price
        if not date:
            date = tx.findtext(".//ns:transactionDate/ns:value", "", ns)

    # --- DERIVATIVE TRANSACTIONS ---
    for tx in root.findall(".//ns:derivativeTransaction", ns):
        code = tx.findtext(".//ns:transactionCode", "", ns)
        shares = float(tx.findtext(".//ns:transactionShares/ns:value", "0", ns))

        codes_seen.append(code)
        total_shares += shares
        if not date:
            date = tx.findtext(".//ns:transactionDate/ns:value", "", ns)

    if total_shares <= 0:
        return None

    # --- TIER CLASSIFICATION ---
    if "P" in codes_seen:
        tier = "Strong Buy"
    elif any(c in codes_seen for c in ("M", "C")):
        tier = "Accumulation"
    elif "A" in codes_seen:
        tier = "Context"
    else:
        tier = "Other"

    return {
        "ticker": root.findtext(".//ns:issuerTradingSymbol", "", ns),
        "owner": root.findtext(".//ns:rptOwnerName", "Unknown", ns),
        "role": root.findtext(".//ns:officerTitle", "Insider", ns),
        "shares": int(total_shares),
        "total": round(total_value, 2),
        "date": date,
        "tier": tier,
        "codes": list(set(codes_seen))
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

# ================= HTML ===================

def write_daily_update_html(body_html: str):
    tpl = open("docs/template.html", "r", encoding="utf-8").read()

    now_et = dt.datetime.now(timezone.utc).astimezone(
        ZoneInfo("America/New_York")
    ).strftime("%Y-%m-%d %I:%M %p ET")

    html = (
        tpl.replace("{{TITLE}}", "Daily Insider Log")
           .replace("{{H1}}", "Daily Insider Log")
           .replace("{{SUBTITLE}}", f"Insider activity, analyst signals & structure — last {LOOKBACK_HOURS} hours")
           .replace("{{UPDATED}}", now_et)
           .replace("{{HOURS}}", str(LOOKBACK_HOURS))
           .replace("{{BODY}}", body_html)
    )

    os.makedirs("docs", exist_ok=True)
    open("docs/index.html", "w", encoding="utf-8").write(html)

# ================= MAIN ===================

def main():
    tx_summary = defaultdict(int)
    hits = []
    market_caps = {}

    feed = ET.fromstring(http_get(
        "https://www.sec.gov/cgi-bin/browse-edgar?action=getcurrent&type=4&owner=only&output=atom"
    ))
    ns = {"atom": "http://www.w3.org/2005/Atom"}

    cutoff = dt.datetime.utcnow() - dt.timedelta(hours=LOOKBACK_HOURS)
    form4_filings = 0

    for entry in feed.findall("atom:entry", ns):
        form4_filings += 1

        updated = entry.findtext("atom:updated", "", ns)
        if not updated:
            continue
        if dt.datetime.fromisoformat(updated.replace("Z", "+00:00")).replace(tzinfo=None) < cutoff:
            continue

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

        for c in parsed["codes"]:
            tx_summary[c] += 1

        ticker = parsed["ticker"]
        if not ticker:
            continue

        if ticker not in market_caps:
            market_caps[ticker] = fetch_market_cap(ticker)

        if not market_caps[ticker] or market_caps[ticker] < MIN_MARKET_CAP:
            continue

        hits.append(parsed)

    analysts = fetch_analyst_upgrades()

    # ================= RENDER =================

    blocks = []

    blocks.append(f"""
    <div class="card">
      <div class="section-title">🛠 System Status</div>
      <div class="item">Form 4 filings scanned: {form4_filings}</div>
      <div class="item">Open-market buys (P): {tx_summary.get("P",0)}</div>
      <div class="item">Option exercises (M): {tx_summary.get("M",0)}</div>
      <div class="item">Awards (A): {tx_summary.get("A",0)}</div>
    </div>
    """)

    blocks.append("<div class='card'><div class='section-title'>🔥 Insider Activity</div>")
    if hits:
        for h in hits:
            blocks.append(
                f"<div class='item'><strong>{h['ticker']}</strong> — "
                f"{h['tier']} · {h['shares']} shares · {h['owner']} ({h['role']})</div>"
            )
    else:
        blocks.append("<div class='item muted'>No qualifying insider activity.</div>")
    blocks.append("</div>")

    blocks.append("<div class='card'><div class='section-title'>📊 Analyst Activity</div>")
    if analysts:
        for a in analysts:
            blocks.append(f"<div class='item'><strong>{a['symbol']}</strong> — {a['analyst']} (+{a['pct']}%)</div>")
    else:
        blocks.append("<div class='item muted'>No material analyst upgrades.</div>")
    blocks.append("</div>")

    write_daily_update_html("\n".join(blocks))


if __name__ == "__main__":
    main()
