import datetime as dt
import os
import json
import urllib.request
import ssl
import smtplib
import xml.etree.ElementTree as ET
from email.mime.text import MIMEText
from collections import defaultdict

# -------------------------
# CONFIG
# -------------------------
LOOKBACK_HOURS = int(os.getenv("LOOKBACK_HOURS", "72"))

SEC_UA = os.getenv(
    "SEC_USER_AGENT",
    "Form4Scanner/1.0 (contact: ginsbergcaleb71@gmail.com)"
).strip()  # IMPORTANT: prevents invalid header crash

FMP_API_KEY = os.getenv("FMP_API_KEY")

# -------------------------
# HTTP
# -------------------------
def http_get(url: str) -> bytes:
    req = urllib.request.Request(
        url,
        headers={"User-Agent": SEC_UA}
    )
    with urllib.request.urlopen(req, timeout=30) as resp:
        return resp.read()

# -------------------------
# HTML OUTPUT
# -------------------------
def write_html(body: str, out_path="docs/index.html"):
    now = dt.datetime.utcnow().strftime("%Y-%m-%d %H:%M UTC")

    html = f"""<!DOCTYPE html>
<html>
<head>
<meta charset="utf-8">
<title>Daily Insider Log</title>
<style>
body {{
  font-family: -apple-system, BlinkMacSystemFont, sans-serif;
  background: #0f172a;
  color: #e5e7eb;
  padding: 40px;
}}
.container {{
  max-width: 900px;
  margin: auto;
  background: #020617;
  padding: 32px;
  border-radius: 12px;
}}
h1 {{
  font-size: 36px;
  margin-bottom: 6px;
}}
.subtitle {{
  font-size: 18px;
  color: #94a3b8;
  margin-bottom: 24px;
}}
.updated {{
  font-style: italic;
  margin-bottom: 24px;
  color: #cbd5f5;
}}
pre {{
  white-space: pre-wrap;
  line-height: 1.5;
}}
</style>
</head>
<body>
<div class="container">
<h1>Daily Insider Log</h1>
<div class="subtitle">Rare insider buys & analyst upgrades — last {LOOKBACK_HOURS} hours</div>
<div class="updated">Updated {now}</div>
<pre>{body}</pre>
</div>
</body>
</html>
"""

    os.makedirs(os.path.dirname(out_path), exist_ok=True)
    with open(out_path, "w", encoding="utf-8") as f:
        f.write(html)

# -------------------------
# FORM 4 PARSER
# -------------------------
def parse_form4(xml_bytes: bytes):
    root = ET.fromstring(xml_bytes)

    issuer = root.find("issuer")
    if issuer is None:
        return None

    ticker = issuer.findtext("issuerTradingSymbol", "").strip()

    owner = root.find("reportingOwner")
    if owner is None:
        return None

    name = owner.find("reportingOwnerId").findtext("rptOwnerName", "Unknown")

    rel = owner.find("reportingOwnerRelationship")
    role = rel.findtext("officerTitle", "") if rel is not None else ""

    table = root.find("nonDerivativeTable")
    if table is None:
        return None

    total = 0
    date = ""

    for tx in table.findall("nonDerivativeTransaction"):
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
        "owner": name,
        "role": role or "Insider",
        "total": round(total, 2),
        "date": date
    }

# -------------------------
# ANALYST UPGRADES
# -------------------------
def fetch_analyst_upgrades():
    if not FMP_API_KEY:
        return []

    url = f"https://financialmodelingprep.com/api/v3/price-target-rss-feed?apikey={FMP_API_KEY}"
    req = urllib.request.Request(
        url,
        headers={
            "User-Agent": SEC_UA,
            "Accept": "application/json"
        }
    )

    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            data = json.loads(r.read().decode())
    except Exception:
        return []

    signals = []
    for d in data:
        old = d.get("priceTargetPrior")
        new = d.get("priceTarget")
        if not old or not new or new <= old:
            continue

        pct = (new - old) / old
        if pct < 0.07:
            continue

        signals.append(
            f"{d.get('symbol')} — {d.get('analystCompany')}\n"
            f"{d.get('ratingPrior')} → {d.get('ratingCurrent')}\n"
            f"Target ${old} → ${new} (+{round(pct*100,1)}%)\n"
        )

    return signals[:5]

# -------------------------
# MAIN
# -------------------------
def main():
    feed_url = (
        "https://www.sec.gov/cgi-bin/browse-edgar"
        "?action=getcurrent&type=4&owner=only&count=100&output=atom"
    )

    atom = http_get(feed_url).decode("utf-8", "ignore")
    feed = ET.fromstring(atom)
    ns = {"atom": "http://www.w3.org/2005/Atom"}

    cutoff = dt.datetime.utcnow() - dt.timedelta(hours=LOOKBACK_HOURS)

    hits = []

    for entry in feed.findall("atom:entry", ns):
        updated = entry.findtext("atom:updated", "", ns)
        if not updated:
            continue

        updated_dt = dt.datetime.fromisoformat(updated.replace("Z", "+00:00")).replace(tzinfo=None)
        if updated_dt < cutoff:
            continue

        link = next(
            (l.get("href") for l in entry.findall("atom:link", ns) if l.get("rel") == "alternate"),
            None
        )
        if not link:
            continue

        page = http_get(link).decode("utf-8", "ignore")
        xml_url = next(
            (line[line.find("https://"):line.find(".xml")+4]
             for line in page.splitlines()
             if ".xml" in line and "form4" in line.lower()),
            None
        )
        if not xml_url:
            continue

        parsed = parse_form4(http_get(xml_url))
        if not parsed or parsed["total"] < 25_000:
            continue

        hits.append(parsed)

    # -------------------------
    # CLUSTER BUY DETECTION
    # -------------------------
    groups = defaultdict(list)
    for h in hits:
        groups[h["ticker"]].append(h)

    body = ""

    clusters = {k: v for k, v in groups.items() if len(v) >= 2}

    if clusters:
        body += "🔥 CLUSTER INSIDER BUYING\n\n"
        for ticker, items in clusters.items():
            total = sum(i["total"] for i in items)
            body += f"{ticker} — {len(items)} buys (${total:,.0f})\n"
            for i in items:
                body += f"  • {i['owner']} ({i['role']}) bought ${i['total']:,.0f} on {i['date']}\n"
            body += "-" * 30 + "\n"
    else:
        body += f"No notable insider buying activity in the last {LOOKBACK_HOURS} hours.\n"

    # -------------------------
    # ANALYST SECTION
    # -------------------------
    analyst = fetch_analyst_upgrades()
    body += "\n📊 ANALYST UPGRADES\n\n"
    if analyst:
        body += "\n".join(analyst)
    else:
        body += "No significant analyst upgrades today."

    write_html(body)

# -------------------------
if __name__ == "__main__":
    main()
