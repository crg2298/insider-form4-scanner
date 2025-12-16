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

