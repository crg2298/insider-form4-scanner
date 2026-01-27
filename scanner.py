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
        if not data:
            return None
        return data[0].get("mktCap")
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
        with open(STATE_FILE, "r") as f:
            return json.load(f)
    except:
        return {}

def save_state(state):
    os.makedirs("docs", exist_ok=True)
    with open(STATE_FILE, "w") as f:
        json.dump(state, f)

# ================= FORM 4 ===================

def parse_form4(xml_bytes):
    root = ET.fromstring(xml_bytes)

    issuer = root.find("issuer")
    ticker = issuer.findtext("issuerTradingSymbol", "") if issuer is not None else ""

    owner = root.find("reportingOwner")
    owner_name = owner.find("reportingOwnerId").findtext("rptOwnerName", "Unknown")

    role = "Insider"
    rel = owner.find("reportingOwnerRelationship")
    if rel is not None:
        title = rel.findtext("officerTitle")
        if title:
            role = title

    nd = root.find("nonDerivativeTable")
    if nd is None:
        return None

    total = 0.0
    date = ""

    for tx in nd.findall("nonDerivativeTransaction"):
        code = tx.find("transactionCoding").findtext("transactionCode", "")
        if code != "P":
            continue

        date = tx.find("transactionDate").findtext("value", "")
        shares = float(
            tx.find("transactionAmounts")
              .find("transactionShares")
              .findtext("value", "0")
        )
        price = float(
            tx.find("transactionAmounts")
              .find("transactionPricePerShare")
              .findtext("value", "0") or 0
        )

        total += shares * price

    return {
        "ticker": ticker,
        "owner": owner_name,
        "role": role,
        "total": round(total, 2),
        "date": date
    }

# ================= ANALYSTS ===================

def fetch_analyst_upgrades():
    api_key = os.getenv("FMP_API_KEY")
    if not api_key:
        return []

    url = f"https://financialmodelingprep.com/api/v3/price-target-rss-feed?apikey={api_key}"

    try:
        data = json.loads(http_get(url).decode())
    except:
        return []

    results = []
    for item in data:
        old = item.get("priceTargetPrior")
        new = item.get("priceTarget")
        if not old or not new or new <= old:
            continue

        pct = (new - old) / old
        if pct < 0.07:
            continue

        results.append({
            "symbol": item.get("symbol"),
            "analyst": item.get("analystCompany"),
            "old": old,
            "new": new,
            "pct": round(pct * 100, 1)
        })

    return results[:5]

# ================= MAIN ===================

def main():
    state = load_state()

    rss = "https://www.sec.gov/cgi-bin/browse-edgar?action=getcurrent&type=4&owner=only&output=atom"
    atom_xml = http_get(rss)
    feed = ET.fromstring(atom_xml)
    ns = {"atom": "http://www.w3.org/2005/Atom"}

    cutoff = dt.datetime.utcnow() - dt.timedelta(hours=LOOKBACK_HOURS)
    hits = []
    scanned = 0
    market_caps = {}
    most_recent_filing_ts = None

    for entry in feed.findall("atom:entry", ns):
        updated = entry.findtext("atom:updated", "", ns)
        if not updated:
            continue

        updated_dt = dt.datetime.fromisoformat(updated.replace("Z", "+00:00")).replace(tzinfo=None)

        if most_recent_filing_ts is None or updated_dt > most_recent_filing_ts:
            most_recent_filing_ts = updated_dt

        if updated_dt < cutoff:
            continue

        scanned += 1

        xml_link = None
        for l in entry.findall("atom:link", ns):
            if l.get("type") == "application/xml":
                xml_link = l.get("href")

        if not xml_link:
            continue

        parsed = parse_form4(http_get(xml_link))
        if not parsed:
            continue

        ticker = parsed["ticker"]

        if ticker not in market_caps:
            market_caps[ticker] = fetch_market_cap(ticker)

        mkt_cap = market_caps.get(ticker)
        if not mkt_cap or mkt_cap < MIN_MARKET_CAP:
            continue

        parsed["market_cap"] = mkt_cap
        hits.append(parsed)
        state["last_buy_date"] = parsed["date"]

    save_state(state)

    analysts = fetch_analyst_upgrades()

    # ================= DAILY BRIEF =================

    blocks = []

    today = dt.datetime.now(timezone.utc).astimezone(
        ZoneInfo("America/New_York")
    ).date()

    last_buy = state.get("last_buy_date")
    silence_days = "N/A"
    if last_buy:
        try:
            silence_days = (today - dt.date.fromisoformat(last_buy)).days
        except:
            pass

    if hits and analysts:
        regime = "Mixed"
    elif hits:
        regime = "Insider-led"
    elif analysts:
        regime = "Analyst-led"
    else:
        regime = "Quiet"

    interpretation = {
        "Insider-led": "Insider participation increased, suggesting internally driven conviction.",
        "Analyst-led": "Analyst activity remains elevated without insider confirmation.",
        "Mixed": "Both insider and analyst signals are present, indicating selective risk appetite.",
        "Quiet": "Both insider and analyst activity remain muted, signaling a low-conviction environment."
    }[regime]

    blocks.append(f"""
    <div class="card hero">
      <div class="section-title">🧠 Daily Market Signal Brief</div>
      <div class="item muted">📅 Signal window: Last 14 days ({LOOKBACK_HOURS} hours)</div>
      <div class="item"><strong>Market regime:</strong> {regime}</div>
      <div class="item"><strong>Days since last insider buy:</strong> {silence_days}</div>
      <div class="item"><strong>Interpretation:</strong> {interpretation}</div>
    </div>
    """)

    # ================= SYSTEM STATUS =================

    last_checked_str = (
        most_recent_filing_ts
        .replace(tzinfo=timezone.utc)
        .astimezone(ZoneInfo("America/New_York"))
        .strftime("%Y-%m-%d %I:%M %p ET")
        if most_recent_filing_ts else "N/A"
    )

    blocks.append(f"""
    <div class="card">
      <div class="section-title">🛠 System Status</div>
      <div class="item">Form 4 filings scanned: {scanned}</div>
      <div class="item">Valid insider buys detected: {len(hits)}</div>
      <div class="item">Analyst upgrades detected: {len(analysts)}</div>
      <div class="item">Last SEC Form 4 filing checked: {last_checked_str}</div>
      <div class="item muted">Coverage notes:</div>
      <div class="item muted">• Filings are sourced directly from the SEC current Form 4 Atom feed</div>
      <div class="item muted">• Filings outside the rolling {LOOKBACK_HOURS}-hour window are not included</div>
      <div class="item muted">• Companies under $1B market cap are excluded</div>
    </div>
    """)

    # ================= INSIDER BUYING =================

    if hits:
        grouped = defaultdict(list)
        for h in hits:
            grouped[h["ticker"]].append(h)

        for ticker, items in grouped.items():
            total = sum(i["total"] for i in items)
            mkt_cap = items[0]["market_cap"]
            pct_of_mkt_cap = (total / mkt_cap) * 100

            blocks.append(f"""
            <div class="card">
              <div class="section-title">🔥 Insider Buying — {ticker}</div>
              <div class="item muted">
                {len(items)} insiders · ${total:,.0f} · {pct_of_mkt_cap:.3f}% of market cap
              </div>
            """)

            for i in items:
                blocks.append(
                    f"<div class='item'>• {i['owner']} ({i['role']}) — ${i['total']:,.0f} on {i['date']}</div>"
                )

            blocks.append("</div>")

    # ================= ANALYST UPGRADES =================

    blocks.append("<div class='card'><div class='section-title'>📊 Analyst Upgrades</div>")

    if analysts:
        for a in analysts:
            blocks.append(
                f"<div class='item'><strong>{a['symbol']}</strong> — {a['analyst']}<br>"
                f"Target ${a['old']} → ${a['new']} (+{a['pct']}%)</div>"
            )
    else:
        blocks.append("<div class='empty'>No material analyst upgrades detected.</div>")

    blocks.append("</div>")

    write_daily_update_html("\n".join(blocks))

# ================= RUN ====================

if __name__ == "__main__":
    main()
