import datetime as dt
import os
import smtplib
import ssl
import urllib.request
import xml.etree.ElementTree as ET
from email.mime.text import MIMEText

SEC_UA = os.getenv("SEC_USER_AGENT", "Form4Scanner/1.0 (contact: your_email@example.com)")
LOOKBACK_HOURS = int(os.getenv("LOOKBACK_HOURS", "24"))

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
    send_email(
        "TEST: Form 4 Scanner",
        "If you received this email, your GitHub Action + Gmail SMTP setup works ✅"
    )

if __name__ == "__main__":
    main()
