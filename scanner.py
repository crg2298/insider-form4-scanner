import datetime as dt
import os
import json
import urllib.request
import xml.etree.ElementTree as ET
from collections import defaultdict

LOOKBACK_HOURS = int(os.getenv("LOOKBACK_HOURS", "72"))
SEC_UA = "Form4Scanner/1.0 (contact: ginsbergcaleb71@gmail.com)"

# ---------------- HTTP ----------------
def http_get(url: str) -> bytes:
    req = urllib.request.Request(url, headers={"User-Agent": SEC_UA})
    with urllib.request.urlopen(req, timeout=30) as resp:
        return resp.read()

# ---------------- HTML ----------------
def write_html(body: str):
    now_utc = dt.datetime.utcnow().strftime("%Y-%m-%d %H:%M UTC")

    html = f"""<!DOCTYPE html>
<html>
<head>
<title>Daily Insider Log</title>
<style>
body {{
  font-family: -apple-system, BlinkMacSystemFont, sans-serif;
  background: #f3f4f6;
  margin: 0;
  padding: 40px;
}}
.container {{
  max-width: 960px;
  margin: auto;
}}
.card {{
  background: white;
  padding: 24px;
  margin-bottom: 24px;
  border-radius: 12px;
  box-shadow: 0 4px 12px rgba(0,0,0,0.06);
}}
h1 {{
  margin-top: 0;
}}
.section-title {{
  font-size: 18px;
  font-weight: 600;
  margin-bottom: 12px;
}}
.small {{
  color: #555;
  font-size: 14px;
}}
.score {{
  font-weight: 700;
  color: #16a34a;
}}
.soft {{
  color: #888;
}}
</style>
</head>
<body>
<div class="container">

<div class="card">
<h1>Daily Insider Log</h1>
<div class="small">Rare insider buys & analyst upgrades — last {LOOKBACK_HOURS} hours</div>
<div class="small">Updated {now_utc}</div>
</div>

{body}

</div>
</body>
</html>"""

    os.makedirs("docs", exist_ok=True)
    with open("docs/index.html", "w", encoding="utf-8") as f:
        f.write(html)

# ---------------- FORM 4 PARSER ----------------
def parse_form4(xml_bytes):
    root = ET.fromstring(xml_bytes)

    issuer = root.find("issuer")
    ticker = issuer.findtext("issuerTradingSymbol", "")

    owner = root.find("reportingOwner")
    owner_name = owner.find("reportingOwnerId").findtext("rptOwnerName", "Unknown")

    rel = owner.find("reportingOwnerRelationship")
    role = rel.findtext("officerTitle", "Insider")

    table = root.find("nonDerivativeTable")
    if table is None:
        return None

    total = 0
    for tx in table.findall("nonDerivativeTransaction"):
        code = tx.find("transactionCoding").findtext("transactionCode", "")
        if code != "P":
            continue

        shares = float(tx.find("transactionAmounts")
                       .find("transactionShares")
                       .findtext("value", "0") or 0)
        price = float(tx.find("transactionAmounts")
                      .find("transactionPricePerShare")
                      .findtext("value", "0") or 0)
        total += shares * price

    if total <= 0:
        return None

    return {
        "ticker": ticker,
        "owner": owner_name,
        "role": role,
        "total": round(total, 2),
    }

# ---------------- ANALYST UPGRADES ----------------
def fetch_analyst_upgrades():
    api_key = os.getenv("FMP_API_KEY")
    if not api_key:
        return {}

    url = f"https://financialmodelingprep.com/api/v3/price-target-rss-feed?apikey={api_key}"
    data = json.loads(http_get(url).decode())

    upgrades = defaultdict(list)
    for item in data:
        if not item.get("priceTargetPrior") or not item.get("priceTarget"):
            continue
        if item["priceTarget"] <= item["priceTargetPrior"]:
            continue

        pct = (item["priceTarget"] - item["priceTargetPrior"]) / item["priceTargetPrior"]
        if pct < 0.07:
            continue

        upgrades[item["symbol"]].append(item)

    return upgrades

# ---------------- CONVICTION SCORE ----------------
def conviction_score(hit, cluster, analyst):
    score = 0
    score += min(hit["total"] / 10000, 30)
    if "CEO" in hit["role"]:
        score += 25
    elif "CFO" in hit["role"]:
        score += 18
    else:
        score += 10
    score += min(cluster * 10, 20)
    if analyst:
        score += 15
    return min(int(score), 100)

# ---------------- MAIN ----------------
def main():
    rss = "https://www.sec.gov/cgi-bin/browse-edgar?action=getcurrent&type=4&output=atom"
    feed = ET.fromstring(http_get(rss))
    ns = {"atom": "http://www.w3.org/2005/Atom"}

    now = dt.datetime.utcnow()
    cutoff = now - dt.timedelta(hours=LOOKBACK_HOURS)

    hits = []

    for entry in feed.findall("atom:entry", ns):
        updated = entry.findtext("atom:updated", "", ns)
        if not updated:
            continue

        updated_dt = dt.datetime.fromisoformat(updated.replace("Z","+00:00")).replace(tzinfo=None)
        if updated_dt < cutoff:
            continue

        link = next((l.get("href") for l in entry.findall("atom:link", ns)
                     if l.get("rel") == "alternate"), None)
        if not link:
            continue

        page = http_get(link).decode("utf-8", "ignore")
        xml_url = next(
            (line[line.find("https://"):line.find(".xml")+4]
             for line in page.splitlines()
             if ".xml" in line.lower() and "form4" in line.lower()),
            None
        )
        if not xml_url:
            continue

        parsed = parse_form4(http_get(xml_url))
        if not parsed:
            continue

        hits.append(parsed)

    analyst_map = fetch_analyst_upgrades()
    grouped = defaultdict(list)
    for h in hits:
        grouped[h["ticker"]].append(h)

    body = ""

    # ---- HIGH CONVICTION ----
    body += "<div class='card'><div class='section-title'>🔥 High-Conviction Signals</div>"
    for ticker, g in grouped.items():
        large = [h for h in g if h["total"] >= 25000]
        if not large:
            continue
        score = conviction_score(large[0], len(large), ticker in analyst_map)
        body += f"<p><b>{ticker}</b> — Conviction <span class='score'>{score}</span></p>"
        for h in large:
            body += f"<div class='small'>• {h['owner']} ({h['role']}) bought ${h['total']:,.0f}</div>"
        body += "<br>"
    body += "</div>"

    # ---- SOFT SIGNALS ----
    body += "<div class='card'><div class='section-title'>🟡 Notable Insider Activity (Smaller Buys)</div>"
    for ticker, g in grouped.items():
        soft = [h for h in g if 5000 <= h["total"] < 25000]
        for h in soft:
            body += f"<div class='small soft'>• {ticker} — {h['owner']} bought ${h['total']:,.0f}</div>"
    body += "</div>"

    # ---- CONFLUENCE ----
    body += "<div class='card'><div class='section-title'>🔗 Insider + Analyst Confluence</div>"
    for ticker in grouped:
        if ticker in analyst_map:
            body += f"<div class='small'><b>{ticker}</b> — Insider buying + analyst upgrades</div>"
    body += "</div>"

    if not hits:
        body = """<div class="card">
        <div class="section-title">No Major Insider Activity</div>
        <div class="small">Markets are quiet — monitoring continues.</div>
        </div>"""

    write_html(body)

if __name__ == "__main__":
    main()
