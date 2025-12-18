import datetime as dt
import os
import json
import urllib.request
import xml.etree.ElementTree as ET
from collections import defaultdict

# ======================
# CONFIG
# ======================
LOOKBACK_HOURS = int(os.getenv("LOOKBACK_HOURS", "72"))
SEC_UA = os.getenv(
    "SEC_USER_AGENT",
    "Form4Scanner/1.0 (contact: ginsbergcaleb71@gmail.com)"
)

# ======================
# HTTP HELPERS
# ======================
def http_get_sec(url: str) -> bytes:
    """ONLY for sec.gov"""
    req = urllib.request.Request(
        url,
        headers={"User-Agent": SEC_UA}
    )
    with urllib.request.urlopen(req, timeout=30) as resp:
        return resp.read()


def http_get_fmp(url: str) -> bytes:
    """ONLY for Financial Modeling Prep"""
    req = urllib.request.Request(
        url,
        headers={
            "User-Agent": "Mozilla/5.0",
            "Accept": "application/json"
        }
    )
    with urllib.request.urlopen(req, timeout=30) as resp:
        return resp.read()

# ======================
# FORM 4 PARSER
# ======================
def parse_form4_xml(xml_bytes: bytes):
    root = ET.fromstring(xml_bytes)

    issuer = root.find("issuer")
    ticker = issuer.findtext("issuerTradingSymbol", default="")

    owner = root.find("reportingOwner")
    owner_name = owner.find("reportingOwnerId").findtext("rptOwnerName", default="Unknown")

    rel = owner.find("reportingOwnerRelationship")
    role = rel.findtext("officerTitle", default="Insider")

    nd_table = root.find("nonDerivativeTable")
    if nd_table is None:
        return None

    total = 0.0
    dates = []

    for tx in nd_table.findall("nonDerivativeTransaction"):
        code = tx.find("transactionCoding").findtext("transactionCode", "")
        if code != "P":
            continue

        date = tx.find("transactionDate").findtext("value", "")
        shares = float(tx.findtext("transactionAmounts/transactionShares/value", "0") or 0)
        price = float(tx.findtext("transactionAmounts/transactionPricePerShare/value", "0") or 0)

        total += shares * price
        dates.append(date)

    if total <= 0:
        return None

    return {
        "ticker": ticker,
        "owner": owner_name,
        "role": role,
        "total_dollars": round(total, 2),
        "date": max(dates) if dates else ""
    }

# ======================
# ANALYST UPGRADES (FMP)
# ======================
def fetch_analyst_upgrades(api_key: str):
    url = f"https://financialmodelingprep.com/api/v3/price-target-rss-feed?apikey={api_key}"

    try:
        raw = http_get_fmp(url)
        data = json.loads(raw.decode())
    except Exception as e:
        print("⚠️ FMP error:", e)
        return []

    signals = []

    for item in data:
        old = item.get("priceTargetPrior")
        new = item.get("priceTarget")
        if not old or not new or new <= old:
            continue

        pct = (new - old) / old
        if pct < 0.05:
            continue

        signals.append({
            "symbol": item.get("symbol"),
            "analyst": item.get("analystCompany"),
            "old": old,
            "new": new,
            "pct": round(pct * 100, 1),
            "date": item.get("publishedDate", "")[:10]
        })

    return signals

# ======================
# MAIN
# ======================
def main():
    rss = "https://www.sec.gov/cgi-bin/browse-edgar?action=getcurrent&type=4&owner=only&output=atom"
    feed = ET.fromstring(http_get_sec(rss))
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

        link = next(
            (l.get("href") for l in entry.findall("atom:link", ns) if l.get("rel") == "alternate"),
            None
        )
        if not link:
            continue

        filing_page = http_get_sec(link).decode(errors="ignore")
        xml_url = None
        for line in filing_page.splitlines():
            if ".xml" in line and "form4" in line.lower():
                start = line.find("https://")
                end = line.find(".xml") + 4
                xml_url = line[start:end]
                break

        if not xml_url:
            continue

        parsed = parse_form4_xml(http_get_sec(xml_url))
        if not parsed or parsed["total_dollars"] < 25_000:
            continue

        insider_hits.append(parsed)

    body = []
    groups = defaultdict(list)
    for h in insider_hits:
        groups[h["ticker"]].append(h)

    for ticker, hits in groups.items():
        if len(hits) < 2:
            continue

        total = sum(h["total_dollars"] for h in hits)
        body.append(f"🔥 {ticker} — {len(hits)} insider buys (${total:,.0f})")
        for h in hits:
            body.append(f"• {h['owner']} ({h['role']}) bought ${h['total_dollars']:,.0f} on {h['date']}")
        body.append("")

    # Analyst upgrades
    api_key = os.getenv("FMP_API_KEY")
    if api_key:
        upgrades = fetch_analyst_upgrades(api_key)
        if upgrades:
            body.append("📊 Analyst Upgrades")
            for a in upgrades[:5]:
                body.append(
                    f"{a['symbol']} — {a['analyst']} "
                    f"${a['old']} → ${a['new']} (+{a['pct']}%)"
                )

    if not body:
        body.append(f"No notable insider buying activity in the last {LOOKBACK_HOURS} hours.")

    os.makedirs("docs", exist_ok=True)
    with open("docs/index.html", "w", encoding="utf-8") as f:
        f.write("<pre>\n" + "\n".join(body) + "\n</pre>")

if __name__ == "__main__":
    main()
