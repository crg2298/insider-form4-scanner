import datetime as dt
import os
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

def main():
    rss_url = "https://www.sec.gov/cgi-bin/browse-edgar?action=getcurrent&CIK=&type=4&company=&dateb=&owner=only&start=0&count=100&output=atom"
    atom = http_get(rss_url).decode("utf-8", errors="ignore")
    feed = ET.fromstring(atom)
    ns = {"atom": "http://www.w3.org/2005/Atom"}
    now = dt.datetime.utcnow()
    cutoff = now - dt.timedelta(hours=LOOKBACK_HOURS)
    body_lines = []
    subject = "Daily Insider Activity Update"

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
        body = "\n\n".join(body_lines)

    write_daily_update_html(body, "docs/index.html")

    # OPTIONAL EMAIL (leave commented if not needed)
    # send_email(subject, body)

    

    for entry in feed.findall("atom:entry", ns):
        updated = entry.findtext("atom:updated", default="", namespaces=ns)
        if not updated:
            continue

        updated_dt = dt.datetime.fromisoformat(updated.replace("Z", "+00:00")).replace(tzinfo=None)
        if updated_dt < cutoff:
            continue

        # filing details page
        link = None
        for l in entry.findall("atom:link", ns):
            if l.get("rel") == "alternate":
                link = l.get("href")
        if not link:
            continue

        # pull filing page HTML, then find XML file link
        try:
            detail_html = http_get(link).decode("utf-8", errors="ignore")
        except:
            continue

        idx = detail_html.lower().find(".xml")
        if idx == -1:
            continue

        href_start = detail_html.rfind('href="', 0, idx)
        if href_start == -1:
            continue

        href_start += len('href="')
        href_end = detail_html.find('"', href_start)
        xml_path = detail_html[href_start:href_end]

        if not xml_path.lower().endswith(".xml"):
            continue

        xml_url = xml_path if xml_path.startswith("http") else "https://www.sec.gov" + xml_path

        try:
            xml_bytes = http_get(xml_url)
            parsed = parse_form4_xml(xml_bytes)
            if parsed:
                parsed["filing_url"] = link
                hits.append(parsed)
        except:
            continue

    # Email results
    if not body_lines:
        # OPTIONAL: comment this out if you ONLY want emails on purchases
        send_email("Form 4 Scanner: No purchase alerts", "No Form 4 open-market purchases (code P) found in the last lookback window.")
        return

    lines = []
    for h in hits:
        lines.append(f"{h['issuer']} ({h['ticker']})")
        lines.append(f"Insider: {h['owner']} | Role: {h['role']} | 10% owner: {h['is_10_percent_owner']}")
        lines.append(f"Total $: ${h['total_dollars']}")
        for t in h["transactions"]:
            lines.append(f"  - {t['date']} | P | {t['shares']} shares @ ${t['price']} = ${t['dollars']}")
        lines.append(f"Filing: {h['filing_url']}")
        lines.append("")

    send_email(f"Form 4 Scanner: {len(hits)} purchase alert(s)", "\n".join(lines).strip())


if __name__ == "__main__":
    main()
