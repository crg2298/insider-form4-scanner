import datetime as dt
import os
import json
import urllib.request
import xml.etree.ElementTree as ET
from collections import defaultdict

# =====================
# CONFIG
# =====================

LOOKBACK_HOURS = int(os.getenv("LOOKBACK_HOURS", "72"))

SEC_USER_AGENT = (
    os.getenv("SEC_USER_AGENT", "")
    .strip()
    .replace("\n", "")
)

if not SEC_USER_AGENT:
    raise RuntimeError("SEC_USER_AGENT is required")

FMP_API_KEY = os.getenv("FMP_API_KEY", "").strip()

# =====================
# HTTP
# =====================

def http_get(url: str) -> bytes:
    req = urllib.request.Request(
        url,
        headers={"User-Agent": SEC_USER_AGENT}
    )
    with urllib.request.urlopen(req, timeout=30) as resp:
        return resp.read()

# =====================
# HTML OUTPUT
# =====================

def write_daily_update_html(body: str):
    os.makedirs("docs", exist_ok=True)

    now_utc = dt.datetime.utcnow().strftime("%Y-%m-%d %H:%M UTC")

    html = f"""<!doctype html>
<html>
<head>
<meta charset="utf-8">
<title>Daily Insider Log</title>
<style>
body {{ font-family: Arial, sans-serif; max-width: 800px; margin: auto; }}
pre {{ white-space: pre-wrap; }}
</style>
</head>
<body>
<h1>Daily Insider Log</h1>
<h3>Rare insider buys & analyst upgrades — last {LOOKBACK_HOURS} hours</h3>
<p><em>Updated {now_utc}</em></p>
<pre>{body}</pre>
</body>
</html>
"""

    with open("docs/index.html", "w", encoding="utf-8") as f:
        f.write(html)

# =====================
# FORM 4 PARSER
# =====================

def parse_form4_xml(xml_bytes: bytes):
    root = ET.fromstring(xml_bytes)

    issuer = root.find("issuer")
    ticker = issuer.findtext("issuerTradingSymbol", default="").strip()
    issuer_name = issuer.findtext("issuerName", default="Unknown")

    owner = root.find("reportingOwner")
    rel = owner.find("reportingOwnerRelationship")

    owner_name = owner.find("reportingOwnerId").findtext("rptOwnerName", default="Unknown")
    role = rel.findtext("officerTitle", default="Insider")

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
        shares = float(tx.find("transactionAmounts")
                         .find("transactionShares")
                         .findtext("value", "0") or 0)

        price = float(
            tx.find("transactionAmounts")
              .find("transactionPricePerShare")
              .findtext("value", "0") or 0
        )

        total += shares * price

    if total <= 0:
        return None

    return {
        "ticker": ticker,
        "issuer": issuer_name,
        "owner": owner_name,
        "role": role,
        "total_dollars": round(total, 2),
        "date": date
    }

# =====================
# ANALYST UPGRADES
# =====================

def fetch_analyst_upgrades():
    if not FMP_API_KEY:
        return []

    url = f"https://financialmodelingprep.com/api/v3/price-target-rss-feed?apikey={FMP_API_KEY}"
    req = urllib.request.Request(
        url,
        headers={"User-Agent": SEC_USER_AGENT, "Accept": "application/json"}
    )

    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            data = json.loads(resp.read().decode())
    except Exception:
        return []

    signals = []
    for item in data:
        old = item.get("priceTargetPrior")
        new = item.get("priceTarget")
        if not old or not new or new <= old:
            continue

        pct = (new - old) / old
        if pct < 0.07:
            continue

        signals.append({
            "symbol": item.get("symbol"),
            "analyst": item.get("analystCompany"),
            "pct": round(pct * 100, 1),
            "date": item.get("publishedDate", "")[:10]
        })

    return signals

# =====================
# MAIN
# =====================

def main():
    rss = "https://www.sec.gov/cgi-bin/browse-edgar?action=getcurrent&type=4&owner=only&output=atom"
    feed = ET.fromstring(http_get(rss).decode("utf-8", "ignore"))
    ns = {"atom": "http://www.w3.org/2005/Atom"}

    cutoff = dt.datetime.utcnow() - dt.timedelta(hours=LOOKBACK_HOURS)
    insider_hits = []

    for entry in feed.findall("atom:entry", ns):
        updated = entry.findtext("atom:updated", "", ns)
        if not updated:
            continue

        updated_dt = dt.datetime.fromisoformat(updated.replace("Z", "+00:00")).replace(tzinfo=None)
        if updated_dt < cutoff:
            continue

        link = next((l.get("href") for l in entry.findall("atom:link", ns) if l.get("rel") == "alternate"), None)
        if not link:
            continue

        page = http_get(link).decode("utf-8", "ignore")

        xml_url = None
        for line in page.splitlines():
            if ".xml" in line and "form4" in line.lower():
                xml_url = line[line.find("https://"):line.find(".xml") + 4]
                break

        if not xml_url:
            continue

        parsed = parse_form4_xml(http_get(xml_url))
        if not parsed or parsed["total_dollars"] < 25_000:
            continue

        insider_hits.append(parsed)

    body = []

    # Phase 1 — Cluster buys
    groups = defaultdict(list)
    for h in insider_hits:
        groups[h["ticker"]].append(h)

    clusters = {k: v for k, v in groups.items() if len(v) >= 2}

    if clusters:
        body.append("🔥 CLUSTER INSIDER BUYING\n")
        for ticker, hits in clusters.items():
            total = sum(h["total_dollars"] for h in hits)
            body.append(f"{ticker} — {len(hits)} buys, ${total:,.0f}\n")
            for h in hits:
                body.append(f"  • {h['owner']} ({h['role']}) ${h['total_dollars']:,.0f} on {h['date']}\n")
            body.append("-" * 30 + "\n")

    # Analyst signals
    signals = fetch_analyst_upgrades()
    if signals:
        body.append("\n📊 ANALYST UPGRADES\n")
        for s in signals[:5]:
            body.append(f"{s['symbol']} — +{s['pct']}% target raise ({s['date']})\n")

    if not body:
        body.append(f"No notable insider buying activity in the last {LOOKBACK_HOURS} hours.")

    write_daily_update_html("".join(body))

if __name__ == "__main__":
    main()
