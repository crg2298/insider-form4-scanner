import datetime as dt
import os
import json
import urllib.request
import xml.etree.ElementTree as ET
from collections import defaultdict

# =========================
# CONFIG
# =========================
LOOKBACK_HOURS = int(os.getenv("LOOKBACK_HOURS", "72"))
SEC_UA = os.getenv(
    "SEC_USER_AGENT",
    "Form4Scanner/1.0 (contact: your_email@example.com)"
)

# =========================
# HELPERS
# =========================
def http_get(url: str) -> bytes:
    req = urllib.request.Request(url, headers={"User-Agent": SEC_UA})
    with urllib.request.urlopen(req, timeout=30) as resp:
        return resp.read()

def write_daily_update_html(body: str, out_path="docs/index.html"):
    with open("docs/template.html", "r", encoding="utf-8") as f:
        tpl = f.read()

    safe_body = (
        body.replace("&", "&amp;")
            .replace("<", "&lt;")
            .replace(">", "&gt;")
    )

    html = (
        tpl.replace("{{UPDATED}}", dt.datetime.utcnow().strftime("%Y-%m-%d %H:%M UTC"))
           .replace("{{HOURS}}", str(LOOKBACK_HOURS))
           .replace("{{BODY}}", safe_body)
    )

    os.makedirs("docs", exist_ok=True)
    with open(out_path, "w", encoding="utf-8") as f:
        f.write(html)

# =========================
# FORM 4 PARSER
# =========================
def parse_form4_xml(xml_bytes: bytes):
    root = ET.fromstring(xml_bytes)

    issuer = root.find("issuer")
    ticker = issuer.findtext("issuerTradingSymbol", default="").strip()

    owner = root.find("reportingOwner")
    name = owner.find("reportingOwnerId").findtext("rptOwnerName", default="Unknown")

    rel = owner.find("reportingOwnerRelationship")
    role = rel.findtext("officerTitle", default="Insider")

    table = root.find("nonDerivativeTable")
    if table is None:
        return None

    total = 0
    date = None

    for tx in table.findall("nonDerivativeTransaction"):
        code = tx.find("transactionCoding").findtext("transactionCode", "")
        if code != "P":
            continue

        date = tx.find("transactionDate").findtext("value", "")
        shares = float(tx.find("transactionAmounts/transactionShares/value").text or 0)
        price = float(
            tx.find("transactionAmounts/transactionPricePerShare/value").text or 0
        )
        total += shares * price

    if total <= 0:
        return None

    return {
        "ticker": ticker,
        "owner": name,
        "role": role,
        "total_dollars": round(total, 2),
        "date": date
    }

# =========================
# ANALYST UPGRADES (FMP)
# =========================
def fetch_analyst_upgrades(api_key):
    url = f"https://financialmodelingprep.com/api/v3/price-target-rss-feed?apikey={api_key}"

    req = urllib.request.Request(
        url,
        headers={
            "User-Agent": "Mozilla/5.0 (compatible; InsiderScanner/1.0)",
            "Accept": "application/json"
        }
    )

    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            data = json.loads(r.read().decode())
    except Exception as e:
        print("⚠️ Analyst API error:", e)
        return []

    signals = []

    for item in data:
        old_t = item.get("priceTargetPrior")
        new_t = item.get("priceTarget")
        if not old_t or not new_t or new_t <= old_t:
            continue

        pct = (new_t - old_t) / old_t
        if pct < 0.07:
            continue

        signals.append({
            "symbol": item.get("symbol"),
            "old": old_t,
            "new": new_t,
            "pct": round(pct * 100, 1)
        })

    return signals

# =========================
# MAIN
# =========================
def main():
    body_lines = []
    insider_hits = []

    rss = "https://www.sec.gov/cgi-bin/browse-edgar?action=getcurrent&type=4&owner=only&output=atom"
    feed = ET.fromstring(http_get(rss).decode("utf-8", "ignore"))
    ns = {"atom": "http://www.w3.org/2005/Atom"}
    cutoff = dt.datetime.utcnow() - dt.timedelta(hours=LOOKBACK_HOURS)

    # ---------- INSIDER LOOP ----------
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

        filing = http_get(link).decode("utf-8", "ignore")

        xml_url = None
        for line in filing.splitlines():
            if ".xml" in line and "form4" in line.lower():
                xml_url = line[line.find("https://"): line.find(".xml") + 4]
                break

        if not xml_url:
            continue

        parsed = parse_form4_xml(http_get(xml_url))
        if not parsed or parsed["total_dollars"] < 25_000:
            continue

        insider_hits.append(parsed)

    # ---------- CLUSTER BUY DETECTION ----------
    groups = defaultdict(list)
    for hit in insider_hits:
        groups[hit["ticker"]].append(hit)

    clusters = {k: v for k, v in groups.items() if len(v) >= 2}

    if clusters:
        body_lines.append(f"\n🔥 CLUSTER INSIDER BUYING (Last {LOOKBACK_HOURS} Hours)\n")
        for ticker, hits in clusters.items():
            total = sum(h["total_dollars"] for h in hits)
            body_lines.append(f"{ticker} — {len(hits)} buys, ${total:,.0f}\n")
            for h in hits:
                body_lines.append(
                    f"  • {h['owner']} ({h['role']}) bought ${h['total_dollars']:,.0f} on {h['date']}\n"
                )
            body_lines.append("-" * 30 + "\n")

    # ---------- ANALYST SECTION (ALWAYS SHOWN) ----------
    analyst_lines = []
    api_key = os.getenv("FMP_API_KEY")

    analyst_lines.append("\n📊 Analyst Upgrades (Last 24–72 Hours)\n")

    if api_key:
        signals = fetch_analyst_upgrades(api_key)
        print("Analyst signals found:", len(signals))

        if signals:
            for s in signals[:5]:
                analyst_lines.append(
                    f"{s['symbol']} — Target ${s['old']} → ${s['new']} (+{s['pct']}%)\n"
                )
        else:
            analyst_lines.append("No strong analyst upgrades detected.\n")
    else:
        analyst_lines.append("Analyst data unavailable (API key missing).\n")

    # ---------- FINAL OUTPUT ----------
    if not body_lines:
        body_lines.append(
            f"No notable insider buying activity found in the last {LOOKBACK_HOURS} hours.\n"
        )

    final_body = "\n".join(body_lines + analyst_lines)
    write_daily_update_html(final_body)

if __name__ == "__main__":
    main()
