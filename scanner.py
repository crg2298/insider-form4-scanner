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

    now_utc = dt.datetime.utcnow().strftime("%Y-%m-%d %H:%M UTC")

    title = "Upside Discovery"
    h1 = "Upside Discovery"
    subtitle = (
        "Tracking rare insider buying and analyst conviction "
        "to surface overlooked upside before it becomes obvious."
    )

    safe_body = (
        body.replace("&", "&amp;")
            .replace("<", "&lt;")
            .replace(">", "&gt;")
    )

    html = (
        tpl.replace("{{TITLE}}", title)
           .replace("{{H1}}", h1)
           .replace("{{SUBTITLE}}", subtitle)
           .replace("{{UPDATED}}", now_utc)
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
    owner_name = owner.find("reportingOwnerId").findtext("rptOwnerName", default="Unknown")

    rel = owner.find("reportingOwnerRelationship")
    role = rel.findtext("officerTitle", default="Insider")

    table = root.find("nonDerivativeTable")
    if table is None:
        return None

    transactions = []

    for tx in table.findall("nonDerivativeTransaction"):
        code = tx.find("transactionCoding").findtext("transactionCode", "")
        if code != "P":
            continue

        date = tx.find("transactionDate").findtext("value", "")
        shares = float(tx.find("transactionAmounts/transactionShares/value").text or 0)
        price = float(tx.find("transactionAmounts/transactionPricePerShare/value").text or 0)

        transactions.append({
            "date": date,
            "dollars": shares * price
        })

    if not transactions:
        return None

    return {
        "ticker": ticker,
        "owner": owner_name,
        "role": role,
        "transactions": transactions,
        "total_dollars": round(sum(t["dollars"] for t in transactions), 2),
        "date": transactions[-1]["date"]
    }

# =========================
# ANALYST UPGRADES
# =========================
def fetch_analyst_upgrades(api_key):
    url = f"https://financialmodelingprep.com/api/v3/price-target-rss-feed?apikey={api_key}"

    try:
        with urllib.request.urlopen(url, timeout=30) as r:
            data = json.loads(r.read().decode())
    except Exception:
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
# PHASE 3 SCORING
# =========================
def role_score(role: str) -> int:
    role = role.lower()
    if "ceo" in role:
        return 4
    if "cfo" in role:
        return 3
    if "president" in role:
        return 2
    return 1

def confidence_score(hits, analyst=False):
    score = 0
    score += min(len(hits), 3)
    score += min(sum(h["total_dollars"] for h in hits) / 100_000, 3)
    score += max(role_score(h["role"]) for h in hits)
    if analyst:
        score += 2
    return round(min(score, 10), 1)

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

    # -------- CLUSTERS --------
    groups = defaultdict(list)
    for h in insider_hits:
        groups[h["ticker"]].append(h)

    clusters = {k: v for k, v in groups.items() if len(v) >= 2}

    analyst_signals = fetch_analyst_upgrades(os.getenv("FMP_API_KEY"))
    analyst_map = {a["symbol"]: a for a in analyst_signals}

    if clusters:
        body_lines.append(f"\n🔥 HIGH-CONVICTION CLUSTERS (Last {LOOKBACK_HOURS} Hours)\n")

        for ticker, hits in clusters.items():
            analyst = ticker in analyst_map
            score = confidence_score(hits, analyst)

            body_lines.append(
                f"{ticker} — Confidence Score: {score}/10\n"
                f"{len(hits)} insiders | ${sum(h['total_dollars'] for h in hits):,.0f}\n"
            )

            for h in hits:
                body_lines.append(
                    f"  • {h['owner']} ({h['role']}) bought ${h['total_dollars']:,.0f}\n"
                )

            if analyst:
                a = analyst_map[ticker]
                body_lines.append(
                    f"  🚀 Analyst raised target ${a['old']} → ${a['new']} (+{a['pct']}%)\n"
                )

            body_lines.append("-" * 35 + "\n")

    if not body_lines:
        body_lines.append(
            f"No notable high-conviction insider activity found in the last {LOOKBACK_HOURS} hours.\n"
        )

    write_daily_update_html("".join(body_lines))

if __name__ == "__main__":
    main()
