import datetime as dt
import os
import json
import urllib.request
import xml.etree.ElementTree as ET
from collections import defaultdict

LOOKBACK_HOURS = int(os.getenv("LOOKBACK_HOURS", "72"))

SEC_UA = os.getenv(
    "SEC_USER_AGENT",
    "Form4Scanner/1.0 (contact: ginsbergcaleb71@gmail.com)"
)

# -------------------------
# Helpers
# -------------------------

def http_get(url: str) -> bytes:
    req = urllib.request.Request(url, headers={"User-Agent": SEC_UA})
    with urllib.request.urlopen(req, timeout=30) as resp:
        return resp.read()


def write_daily_update_html(body: str, out_path: str = "docs/index.html"):
    with open("docs/template.html", "r", encoding="utf-8") as f:
        tpl = f.read()

    now_utc = dt.datetime.utcnow().strftime("%Y-%m-%d %H:%M UTC")

    safe_body = (
        body.replace("&", "&amp;")
            .replace("<", "&lt;")
            .replace(">", "&gt;")
    )

    html = (
        tpl.replace("{{TITLE}}", "Daily Insider Log")
           .replace("{{H1}}", "Daily Insider Log")
           .replace("{{SUBTITLE}}", f"Rare insider buys & analyst upgrades — last {LOOKBACK_HOURS} hours")
           .replace("{{UPDATED}}", now_utc)
           .replace("{{BODY}}", safe_body)
    )

    os.makedirs("docs", exist_ok=True)
    with open(out_path, "w", encoding="utf-8") as f:
        f.write(html)


def parse_form4_xml(xml_bytes: bytes):
    root = ET.fromstring(xml_bytes)

    issuer = root.find("issuer")
    if issuer is None:
        return None

    ticker = issuer.findtext("issuerTradingSymbol", default="")
    issuer_name = issuer.findtext("issuerName", default="")

    owner = root.find("reportingOwner")
    rel = owner.find("reportingOwnerRelationship") if owner is not None else None

    owner_name = owner.find("reportingOwnerId").findtext("rptOwnerName", default="Unknown") if owner is not None else "Unknown"
    role = rel.findtext("officerTitle", default="Insider") if rel is not None else "Insider"

    nd_table = root.find("nonDerivativeTable")
    if nd_table is None:
        return None

    total = 0
    for tx in nd_table.findall("nonDerivativeTransaction"):
        code = tx.find("transactionCoding").findtext("transactionCode", "")
        if code != "P":
            continue

        shares = float(tx.find("transactionAmounts/transactionShares/value").text or 0)
        price_txt = tx.find("transactionAmounts/transactionPricePerShare/value")
        price = float(price_txt.text) if price_txt is not None else 0
        total += shares * price

    if total <= 0:
        return None

    date = root.findtext("periodOfReport", default="")

    return {
        "ticker": ticker,
        "issuer": issuer_name,
        "owner": owner_name,
        "role": role,
        "total_dollars": round(total, 2),
        "date": date,
    }


def fetch_analyst_upgrades(api_key):
    url = f"https://financialmodelingprep.com/api/v3/price-target-rss-feed?apikey={api_key}"
    req = urllib.request.Request(url, headers={"User-Agent": SEC_UA})

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
            "old": old,
            "new": new,
            "pct": round(pct * 100, 1),
            "date": item.get("publishedDate", "")[:10],
        })

    return signals


# -------------------------
# Main
# -------------------------

def main():
    rss = "https://www.sec.gov/cgi-bin/browse-edgar?action=getcurrent&type=4&owner=only&count=100&output=atom"
    feed = ET.fromstring(http_get(rss).decode("utf-8", "ignore"))
    ns = {"atom": "http://www.w3.org/2005/Atom"}

    cutoff = dt.datetime.utcnow() - dt.timedelta(hours=LOOKBACK_HOURS)
    insider_hits = []

    for entry in feed.findall("atom:entry", ns):
        updated = entry.findtext("atom:updated", "", namespaces=ns)
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

        parsed = parse_form4_xml(http_get(xml_url))
        if not parsed or parsed["total_dollars"] < 15000:
            continue

        insider_hits.append(parsed)

    body_lines = []

    # 🔥 Cluster Buys
    groups = defaultdict(list)
    for h in insider_hits:
        groups[h["ticker"]].append(h)

    clusters = {k: v for k, v in groups.items() if len(v) >= 2}
    if clusters:
        body_lines.append("\n🔥 CLUSTER INSIDER BUYING\n")
        for t, hits in clusters.items():
            total = sum(h["total_dollars"] for h in hits)
            body_lines.append(f"{t} — {len(hits)} buys, ${total:,.0f}\n")
            for h in hits:
                body_lines.append(f"  • {h['owner']} ({h['role']}) ${h['total_dollars']:,.0f}\n")
            body_lines.append("-" * 30 + "\n")

    # ⭐ Single Buys
    singles = [h for h in insider_hits if h["total_dollars"] >= 15000]
    if singles:
        body_lines.append("\n⭐ NOTABLE INSIDER BUYS\n")
        for h in singles[:10]:
            body_lines.append(
                f"{h['ticker']} — {h['owner']} ({h['role']}) ${h['total_dollars']:,.0f} on {h['date']}\n"
            )
        body_lines.append("-" * 30 + "\n")

    # 📊 Analyst Upgrades
    api_key = os.getenv("FMP_API_KEY")
    if api_key:
        upgrades = fetch_analyst_upgrades(api_key)
        if upgrades:
            body_lines.append("\n📊 ANALYST UPGRADES\n")
            for u in upgrades[:5]:
                body_lines.append(
                    f"{u['symbol']} — target ${u['old']} → ${u['new']} (+{u['pct']}%)\n"
                )

    if not body_lines:
        body = f"No notable insider buying activity in the last {LOOKBACK_HOURS} hours."
    else:
        body = "".join(body_lines)

    write_daily_update_html(body)


if __name__ == "__main__":
    main()
