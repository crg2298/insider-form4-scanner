import datetime as dt
import os
import json
import smtplib
import ssl
import urllib.request
import xml.etree.ElementTree as ET
from email.mime.text import MIMEText
from collections import defaultdict

# ===================== CONFIG =====================
LOOKBACK_HOURS = int(os.getenv("LOOKBACK_HOURS", "72"))
SEC_UA = os.getenv(
    "SEC_USER_AGENT",
    "Form4Scanner/1.0 (contact: your_email@example.com)"
)

# ===================== HELPERS =====================
def http_get(url: str) -> bytes:
    req = urllib.request.Request(url, headers={"User-Agent": SEC_UA})
    with urllib.request.urlopen(req, timeout=30) as resp:
        return resp.read()

def write_daily_update_html(body: str, out_path="docs/index.html"):
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
           .replace("{{SUBTITLE}}", "Rare insider buys, summarized in plain English.")
           .replace("{{UPDATED}}", now_utc)
           .replace("{{HOURS}}", str(LOOKBACK_HOURS))
           .replace("{{BODY}}", safe_body)
    )

    os.makedirs(os.path.dirname(out_path), exist_ok=True)
    with open(out_path, "w", encoding="utf-8") as f:
        f.write(html)

# ===================== FORM 4 PARSER =====================
def parse_form4_xml(xml_bytes: bytes):
    root = ET.fromstring(xml_bytes)

    issuer = root.find("issuer")
    ticker = issuer.findtext("issuerTradingSymbol", default="")
    issuer_name = issuer.findtext("issuerName", default="Unknown")

    owner = root.find("reportingOwner")
    owner_name = owner.find("reportingOwnerId").findtext("rptOwnerName", default="Unknown")
    rel = owner.find("reportingOwnerRelationship")
    role = rel.findtext("officerTitle", default="Insider")

    nd_table = root.find("nonDerivativeTable")
    if nd_table is None:
        return None

    total = 0
    date = ""

    for tx in nd_table.findall("nonDerivativeTransaction"):
        code = tx.find("transactionCoding").findtext("transactionCode", "")
        if code != "P":
            continue

        date = tx.find("transactionDate").findtext("value", "")
        shares = float(tx.find("transactionAmounts")
                       .find("transactionShares")
                       .findtext("value", "0"))
        price = float(tx.find("transactionAmounts")
                      .find("transactionPricePerShare")
                      .findtext("value", "0"))
        total += shares * price

    if total == 0:
        return None

    return {
        "issuer": issuer_name,
        "ticker": ticker,
        "owner": owner_name,
        "role": role,
        "total_dollars": round(total, 2),
        "date": date,
    }

# ===================== ANALYST UPGRADES =====================
def fetch_analyst_upgrades(api_key):
    url = f"https://financialmodelingprep.com/api/v3/price-target-rss-feed?apikey={api_key}"

    req = urllib.request.Request(
        url,
        headers={
            "User-Agent": "Mozilla/5.0 (compatible; InsiderScanner/1.0)",
            "Accept": "application/json",
        },
    )

    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            data = json.loads(r.read().decode())
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

# ===================== MAIN =====================
def main():
    rss_url = (
        "https://www.sec.gov/cgi-bin/browse-edgar?"
        "action=getcurrent&type=4&owner=only&count=100&output=atom"
    )

    feed = ET.fromstring(http_get(rss_url))
    ns = {"atom": "http://www.w3.org/2005/Atom"}
    cutoff = dt.datetime.utcnow() - dt.timedelta(hours=LOOKBACK_HOURS)

    insider_hits = []
    body_lines = []

    for entry in feed.findall("atom:entry", ns):
        updated = entry.findtext("atom:updated", "", ns)
        if not updated:
            continue

        updated_dt = dt.datetime.fromisoformat(updated.replace("Z", "+00:00")).replace(tzinfo=None)
        if updated_dt < cutoff:
            continue

        link = next(
            (l.get("href") for l in entry.findall("atom:link", ns) if l.get("rel") == "alternate"),
            None,
        )
        if not link:
            continue

        filing_page = http_get(link).decode("utf-8", errors="ignore")

        xml_url = None
        for line in filing_page.splitlines():
            if ".xml" in line and "form4" in line.lower():
                start = line.find("https://")
                xml_url = line[start:line.find(".xml") + 4]
                break

        if not xml_url:
            continue

        parsed = parse_form4_xml(http_get(xml_url))
        if not parsed or parsed["total_dollars"] < 25_000:
            continue

        insider_hits.append(parsed)

    # -------- CLUSTER BUY DETECTION --------
    groups = defaultdict(list)
    for h in insider_hits:
        groups[h["ticker"]].append(h)

    clusters = {k: v for k, v in groups.items() if len(v) >= 2}

    if clusters:
        body_lines.append(f"\n🔥 CLUSTER INSIDER BUYING (Last {LOOKBACK_HOURS} Hours)\n")
        for ticker, hits in clusters.items():
            total = sum(h["total_dollars"] for h in hits)
            body_lines.append(f"{ticker} — {len(hits)} buys, total ${total:,.0f}\n")
            for h in hits:
                body_lines.append(
                    f"  • {h['owner']} ({h['role']}) bought "
                    f"${h['total_dollars']:,.0f} on {h['date']}\n"
                )
            body_lines.append("-" * 30 + "\n")

    if not body_lines:
        body_lines.append(f"No notable insider buying activity found in the last {LOOKBACK_HOURS} hours.")

    # -------- ANALYST SECTION --------
    api_key = os.getenv("FMP_API_KEY")
    analyst_lines = []

    if api_key:
        signals = fetch_analyst_upgrades(api_key)
        if signals:
            analyst_lines.append("\n📊 Analyst Upgrades\n")
            for s in signals[:5]:
                analyst_lines.append(
                    f"{s['symbol']} — Target ${s['old']} → ${s['new']} (+{s['pct']}%)\n"
                )

    final_body = "\n".join(body_lines + analyst_lines)
    write_daily_update_html(final_body)

if __name__ == "__main__":
    main()
