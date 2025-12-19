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
               f"Insider buying & analyst conviction — last {LOOKBACK_HOURS} hours"
           )
           .replace("{{UPDATED}}", now_et)
           .replace("{{HOURS}}", str(LOOKBACK_HOURS))
           .replace("{{BODY}}", body_html)
    )

    os.makedirs("docs", exist_ok=True)
    with open("docs/index.html", "w", encoding="utf-8") as f:
        f.write(html)

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

# ================= SNAPSHOT =================

def daily_market_snapshot(hits, analysts):
    insider_state = (
        "Insider participation is elevated, suggesting growing internal conviction."
        if hits else
        "Insider activity remains subdued, indicating a wait-and-see posture."
    )

    analyst_state = (
        "Analyst sentiment shows selective optimism through price target increases."
        if analysts else
        "Analyst revisions are muted, signaling stable consensus expectations."
    )

    return f"""
    <div class="card">
      <div class="section-title">🧠 Daily Market Intelligence</div>
      <div class="item">{insider_state}</div>
      <div class="item">{analyst_state}</div>
      <div class="item muted">
        Quiet periods often precede volatility expansion. Monitoring insider behavior
        and analyst conviction during these windows can surface early inflection points
        before price momentum becomes obvious.
      </div>
    </div>
    """

# ================= MAIN ===================

def main():
    rss = "https://www.sec.gov/cgi-bin/browse-edgar?action=getcurrent&type=4&owner=only&output=atom"
    feed = ET.fromstring(http_get(rss).decode("utf-8", "ignore"))
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

    blocks = []

    # ===== INSIDER BUYING =====
    if hits:
        grouped = defaultdict(list)
        for h in hits:
            grouped[h["ticker"]].append(h)

        for ticker, items in grouped.items():
            total = sum(i["total"] for i in items)

            blocks.append(f"""
            <div class="card">
              <div class="section-title">🔥 Insider Buying — {ticker}</div>
              <div class="item muted">{len(items)} insiders · ${total:,.0f}</div>
            """)

            for i in items:
                blocks.append(
                    f"<div class='item'>• {i['owner']} ({i['role']}) — ${i['total']:,.0f} on {i['date']}</div>"
                )

            blocks.append("</div>")

    # ===== ANALYST UPGRADES (ALWAYS) =====
    analysts = fetch_analyst_upgrades()

    blocks.append("<div class='card'><div class='section-title'>📊 Analyst Upgrades</div>")

    if analysts:
        for a in analysts:
            blocks.append(
                f"<div class='item'><strong>{a['symbol']}</strong> — {a['analyst']}<br>"
                f"Target ${a['old']} → ${a['new']} (+{a['pct']}%)</div>"
            )
    else:
        blocks.append("<div class='empty'>No strong analyst upgrades detected.</div>")

    blocks.append("</div>")

    # ===== DAILY SNAPSHOT (ALWAYS) =====
    blocks.append(daily_market_snapshot(hits, analysts))

    write_daily_update_html("\n".join(blocks))

# ================= RUN ====================

if __name__ == "__main__":
    main()
