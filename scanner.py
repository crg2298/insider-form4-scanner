import datetime as dt
import os
import json
import smtplib
import ssl
import urllib.request
import xml.etree.ElementTree as ET
from email.mime.text import MIMEText

LOOKBACK_HOURS = int(os.getenv("LOOKBACK_HOURS", "24"))

def write_daily_update_html(body: str, out_path: str = "docs/index.html"):
    template_path = "docs/template.html"

    now_utc = dt.datetime.utcnow().strftime("%Y-%m-%d %H:%M UTC")
    title = "Daily Insider Log"
    h1 = "Daily Insider Log"
    subtitle = "Rare insider buys, summarized in plain English."

    with open(template_path, "r", encoding="utf-8") as f:
        tpl = f.read()

    # escape HTML
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
   
    os.makedirs(os.path.dirname(out_path), exist_ok=True)
    with open(out_path, "w", encoding="utf-8") as f:
        f.write(html)

SEC_UA = os.getenv("SEC_USER_AGENT", "Form4Scanner/1.0 (contact: your_email@example.com)")

def http_get(url: str) -> bytes:
    req = urllib.request.Request(url, headers={"User-Agent": SEC_UA})
    with urllib.request.urlopen(req, timeout=30) as resp:
        return resp.read()

def send_email(subject: str, body: str):
    to_addr = os.environ["ALERT_EMAIL_TO"]
    from_addr = os.environ["ALERT_EMAIL_FROM"]
    host = os.environ["SMTP_HOST"]
    port = int(os.environ["SMTP_PORT"])
    user = os.environ["SMTP_USER"]
    pw = os.environ["SMTP_PASS"]

    msg = MIMEText(body, "plain", "utf-8")
    msg["Subject"] = subject
    msg["From"] = from_addr
    msg["To"] = to_addr

    context = ssl.create_default_context()
    with smtplib.SMTP(host, port) as server:
        server.starttls(context=context)
        server.login(user, pw)
        server.sendmail(from_addr, [to_addr], msg.as_string())

def parse_form4_xml(xml_bytes: bytes):
    root = ET.fromstring(xml_bytes)

    issuer = root.find("issuer")
    issuer_name = issuer.findtext("issuerName", default="Unknown")
    ticker = issuer.findtext("issuerTradingSymbol", default="")

    owner = root.find("reportingOwner")
    rel = owner.find("reportingOwnerRelationship")
    title = rel.findtext("officerTitle", default="")
    is_officer = rel.findtext("isOfficer", default="0") == "1"
    is_10 = rel.findtext("isTenPercentOwner", default="0") == "1"
    owner_name = owner.find("reportingOwnerId").findtext("rptOwnerName", default="Unknown")

    transactions = []
    nd_table = root.find("nonDerivativeTable")
    if nd_table is None:
        return None

    for tx in nd_table.findall("nonDerivativeTransaction"):
        code = tx.find("transactionCoding").findtext("transactionCode", default="")
        date = tx.find("transactionDate").findtext("value", default="")
        shares = float(tx.find("transactionAmounts")
                         .find("transactionShares")
                         .findtext("value", default="0") or 0)

        price_txt = tx.find("transactionAmounts") \
                      .find("transactionPricePerShare") \
                      .findtext("value", default="0") or "0"
        try:
            price = float(price_txt)
        except:
            price = 0.0

        if code == "P":
            transactions.append({
                "date": date,
                "shares": shares,
                "price": price,
                "dollars": round(shares * price, 2),
            })

    if not transactions:
        return None

    return {
        "issuer": issuer_name,
        "ticker": ticker,
        "owner": owner_name,
        "role": title or ("Officer" if is_officer else "Insider"),
        "is_10_percent_owner": is_10,
        "transactions": transactions,
        "total_dollars": round(sum(t["dollars"] for t in transactions), 2),
    }
    
    def fetch_analyst_upgrades(api_key):
        url = f"https://financialmodelingprep.com/api/v3/price-target-rss-feed?apikey={api_key}"
        with urllib.request.urlopen(url) as response:
            data = json.loads(response.read().decode())

        signals = []

        for item in data:
            old_rating = (item.get("ratingPrior") or "").lower()
            new_rating = (item.get("ratingCurrent") or "").lower()

            old_target = item.get("priceTargetPrior")
            new_target = item.get("priceTarget")

            # Require upgrade + raised target
            if not old_target or not new_target:
                continue
            if new_target <= old_target:
                continue
            if old_rating == new_rating:
                continue
            if old_target == 0:
                continue

            pct_change = (new_target - old_target) / old_target

            # Require meaningful raise (10%+)
            if pct_change < 0.10:
                continue

            signals.append({
                "symbol": item.get("symbol"),
                "analyst": item.get("analystCompany"),
                "old_rating": item.get("ratingPrior"),
                "new_rating": item.get("ratingCurrent"),
                "old_target": old_target,
                "new_target": new_target,
                "pct": round(pct_change * 100, 1),
                "date": item.get("publishedDate", "")[:10]
            })

        return signals


def main():

    rss_url = "https://www.sec.gov/cgi-bin/browse-edgar?action=getcurrent&CIK=&type=4&company=&dateb=&owner=only&start=0&count=100&output=atom"
    atom = http_get(rss_url).decode("utf-8", errors="ignore")
    feed = ET.fromstring(atom)
    ns = {"atom": "http://www.w3.org/2005/Atom"}
    now = dt.datetime.utcnow()
    cutoff = now - dt.timedelta(hours=LOOKBACK_HOURS)
    body_lines = []
    subject = "Daily Insider Activity Update"
    # --- Analyst upgrades setup ---
    api_key = os.environ.get("FMP_API_KEY")

    if not api_key:
        print("❌ FMP_API_KEY not found")
        analyst_signals = []
    else:
        print("✅ FMP_API_KEY loaded")
        analyst_signals = fetch_analyst_upgrades(api_key)

    for entry in feed.findall("atom:entry", ns):
        updated = entry.findtext("atom:updated", default="", namespaces=ns)
        if not updated:
            continue

        updated_dt = dt.datetime.fromisoformat(
            updated.replace("Z", "+00:00")
        ).replace(tzinfo=None)

        if updated_dt < cutoff:
            continue

        link = None
        for l in entry.findall("atom:link", ns):
            if l.get("rel") == "alternate":
                link = l.get("href")

        if not link:
            continue

        filing_page = http_get(link).decode("utf-8", errors="ignore")

        # Find XML link
        xml_url = None
        for line in filing_page.splitlines():
            if ".xml" in line and "form4" in line.lower():
                start = line.find("https://")
                end = line.find(".xml") + 4
                xml_url = line[start:end]
                break

        if not xml_url:
            continue

        xml_bytes = http_get(xml_url)
        parsed = parse_form4_xml(xml_bytes)

        if not parsed:
            continue
            
        # Minimum purchase threshold: $50,000
        if not parsed.get("total_dollars") or parsed["total_dollars"] < 50_000:
            continue


        body_lines.append(
            f"{parsed['issuer']} ({parsed['ticker']})\n"
            f"Insider: {parsed['owner']} ({parsed['role']})\n"
            f"Total Buy: ${parsed['total_dollars']:,.2f}\n"
            f"Link: {link}\n"
            "----------------------"
        )
        
    if not body_lines:
        body = "No notable insider buying activity found in the last 24 hours."
    else:
        body = "\n".join(body_lines)
    analyst_lines = []

    if analyst_signals:
        analyst_lines.append("📊 Analyst Upgrades (Strong Signals)\n")
        for a in analyst_signals[:5]:  # limit to top 5
            analyst_lines.append(
                f"{a['symbol']} — {a['analyst']}\n"
                f"{a['old_rating']} → {a['new_rating']}\n"
                f"Target: ${a['old_target']} → ${a['new_target']} (+{a['pct']}%)\n"
                f"Date: {a['date']}\n"
                "-----------------------------"
            )
    else:
        analyst_lines.append("No significant analyst upgrades today.")

    final_body = body + "\n\n" + "\n".join(analyst_lines)
    write_daily_update_html(final_body, "docs/index.html")


    # OPTIONAL EMAIL (leave commented if not needed)
    # send_email(subject, body)


if __name__ == "__main__":
    main()
