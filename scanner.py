import datetime as dt
import os
import json
import urllib.request
import xml.etree.ElementTree as ET
from collections import defaultdict
from datetime import timezone
from zoneinfo import ZoneInfo

# ================= CONFIG =================

LOOKBACK_HOURS = int(os.getenv("LOOKBACK_HOURS", "72"))
SEC_USER_AGENT = "Form4Scanner/1.0 (contact: ginsbergcaleb71@gmail.com)"

# Toggle paid features
PAID_MODE = os.getenv("PAID_MODE", "false").lower() == "true"

QUIET_STREAK_FILE = "docs/quiet_streak.json"

# ================= HTTP ===================

def http_get(url: str) -> bytes:
    req = urllib.request.Request(url)
    req.add_header("User-Agent", SEC_USER_AGENT)
    req.add_header("Accept", "*/*")
    with urllib.request.urlopen(req, timeout=30) as resp:
        return resp.read()

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

# ================= HELPERS =================

def infer_sector(ticker: str) -> str:
    if not ticker:
        return "Unknown"
    t = ticker.upper()

    if t.startswith(("XOM", "CVX", "BP", "COP")):
        return "Energy"
    if t.startswith(("MRNA", "BIIB", "PFE", "JNJ")):
        return "Biotech / Pharma"
    if t.startswith(("AAPL", "MSFT", "NVDA", "AMD", "GOOG")):
        return "Technology"
    if t.startswith(("JPM", "BAC", "GS", "WFC")):
        return "Financials"

    return "Other"

# ================= SIGNAL SCORING =================

def signal_strength(insider_count, total_dollars, analyst_count):
    score = 0
    score += min(insider_count * 2, 4)
    score += min(total_dollars / 250_000, 4)
    score += min(analyst_count, 2)
    return round(min(score, 10), 1)

# ================= QUIET STREAK =================

def load_quiet_streak():
    if not os.path.exists(QUIET_STREAK_FILE):
        return 0
    try:
        with open(QUIET_STREAK_FILE, "r") as f:
            return json.load(f).get("days", 0)
    except:
        return 0

def save_quiet_streak(days):
    with open(QUIET_STREAK_FILE, "w") as f:
        json.dump({"days": days}, f)

# ================= FORM 4 =================

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

    if total < 15000:
        return None

    return {
        "ticker": ticker,
        "owner": owner_name,
        "role": role,
        "total": round(total, 2),
        "date": date
    }

# ================= ANALYSTS =================

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
    rss = "https://www.sec.gov/cgi-bin/browse-edgar?action=getcurrent&type=4&owner=only&output=atom"
    feed = ET.fromstring(http_get(rss).decode("utf-8", "ignore"))
    ns = {"atom": "http://www.w3.org/2005/Atom"}

    cutoff = dt.datetime.utcnow() - dt.timedelta(hours=LOOKBACK_HOURS)
    hits = []
    sector_counts = defaultdict(int)
    total_dollars = 0

    for entry in feed.findall("atom:entry", ns):
        updated = entry.findtext("atom:updated", "", ns)
        if not updated:
            continue

        updated_dt = dt.datetime.fromisoformat(updated.replace("Z", "+00:00")).replace(tzinfo=None)
        if updated_dt < cutoff:
            continue

        link = None
        for l in entry.findall("atom:link", ns):
            if l.get("rel") == "alternate":
                link = l.get("href")

        if not link:
            continue

        page = http_get(link).decode("utf-8", "ignore")

        xml_url = None
        for line in page.splitlines():
            if ".xml" in line and "form4" in line.lower():
                start = line.find("https://")
                end = line.find(".xml") + 4
                xml_url = line[start:end]
                break

        if not xml_url:
            continue

        parsed = parse_form4(http_get(xml_url))
        if parsed:
            hits.append(parsed)
            total_dollars += parsed["total"]
            sector_counts[infer_sector(parsed["ticker"])] += 1

    analysts = fetch_analyst_upgrades()

    # Quiet streak
    quiet_days = load_quiet_streak()
    if hits:
        quiet_days = 0
    else:
        quiet_days += 1
    save_quiet_streak(quiet_days)

    strength = signal_strength(len(hits), total_dollars, len(analysts))

    blocks = []

    blocks.append(f"""
    <div class="card">
      <div class="section-title">📈 Signal Strength</div>
      <div class="item">Overall signal score: <strong>{strength} / 10</strong></div>
      <div class="item muted">Combines insider activity, dollar conviction, and analyst momentum.</div>
    </div>
    """)

    blocks.append(f"""
    <div class="card">
      <div class="section-title">⏳ Market Quiet Streak</div>
      <div class="item">Days without notable insider buying: <strong>{quiet_days}</strong></div>
    </div>
    """)

    blocks.append("<div class='card'><div class='section-title'>🧭 Sector Rotation</div>")
    for sector, count in sorted(sector_counts.items(), key=lambda x: -x[1]):
        blocks.append(f"<div class='item'>{sector}: {count} insider events</div>")
    if not sector_counts:
        blocks.append("<div class='item muted'>No sector concentration detected.</div>")
    blocks.append("</div>")

    if not PAID_MODE:
        blocks.append("""
        <div class="card">
          <div class="section-title">🔒 Premium Signals</div>
          <div class="item muted">
            Detailed ticker-level analysis and historical performance tracking
            are available to paid subscribers.
          </div>
        </div>
        """)

    write_daily_update_html("\n".join(blocks))

# ================= RUN ====================

if __name__ == "__main__":
    main()
