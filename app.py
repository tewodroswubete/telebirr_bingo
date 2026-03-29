import re
import sqlite3
from datetime import datetime

import requests
from bs4 import BeautifulSoup
from flask import Flask, render_template, request, jsonify

app = Flask(__name__)

# Configure your telebirr account details here
MY_ACCOUNT_NAME = "Tewodros Wubete Desta"
MY_PHONE = "+251905028140"
MY_PHONE_MASKED = "2519****8140"  # How it appears in receipts

RECEIPT_BASE_URL = "https://transactioninfo.ethiotelecom.et/receipt/"
DB_PATH = "transactions.db"


def init_db():
    """Initialize the SQLite database for storing validated transactions."""
    conn = sqlite3.connect(DB_PATH)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS transactions (
            transaction_number TEXT PRIMARY KEY,
            amount REAL,
            payer_name TEXT,
            payer_phone TEXT,
            payment_date TEXT,
            validated_at TEXT,
            validation_source TEXT
        )
    """)
    conn.commit()
    conn.close()


def is_duplicate(transaction_number):
    """Check if a transaction has already been validated."""
    conn = sqlite3.connect(DB_PATH)
    row = conn.execute(
        "SELECT transaction_number, amount, payer_name, payment_date, validated_at "
        "FROM transactions WHERE transaction_number = ?",
        (transaction_number,)
    ).fetchone()
    conn.close()
    if row:
        return {
            "transaction_number": row[0],
            "amount": row[1],
            "payer_name": row[2],
            "payment_date": row[3],
            "validated_at": row[4],
        }
    return None


def save_transaction(txn_number, amount, payer_name, payer_phone, payment_date, source):
    """Save a successfully validated transaction to the database."""
    conn = sqlite3.connect(DB_PATH)
    conn.execute(
        "INSERT OR IGNORE INTO transactions "
        "(transaction_number, amount, payer_name, payer_phone, payment_date, validated_at, validation_source) "
        "VALUES (?, ?, ?, ?, ?, ?, ?)",
        (txn_number, amount, payer_name, payer_phone, payment_date,
         datetime.now().strftime("%Y-%m-%d %H:%M:%S"), source)
    )
    conn.commit()
    conn.close()


# Initialize DB on startup
init_db()


def extract_transaction_info(text):
    """Extract transaction details from telebirr SMS text."""
    result = {
        "transaction_number": None,
        "amount": None,
        "sender": None,
        "receiver": None,
        "type": None,
        "date": None,
        "balance": None,
        "receipt_url": None,
        "account_holder": None,
    }

    if "You have transferred" in text:
        result["type"] = "sent"
        amt_match = re.search(r"transferred ETB ([\d,]+\.\d{2})", text)
        if amt_match:
            result["amount"] = float(amt_match.group(1).replace(",", ""))
        recv_match = re.search(r"to (.+?)\s*\((\d[\d*]+)\)", text)
        if recv_match:
            result["receiver"] = recv_match.group(1).strip()
            result["receiver_phone"] = recv_match.group(2)

    elif "You have received" in text:
        result["type"] = "received"
        amt_match = re.search(r"received ETB ([\d,]+\.\d{2})", text)
        if amt_match:
            result["amount"] = float(amt_match.group(1).replace(",", ""))
        send_match = re.search(r"from (.+?)\((\d[\d*]+)\)", text)
        if send_match:
            result["sender"] = send_match.group(1).strip()
            result["sender_phone"] = send_match.group(2)

    txn_match = re.search(r"transaction number is ([A-Z0-9]+)", text)
    if txn_match:
        result["transaction_number"] = txn_match.group(1)

    # Extract receipt URL directly from text if present
    url_match = re.search(r"(https?://transactioninfo\.ethiotelecom\.et/receipt/([A-Z0-9]+))", text)
    if url_match:
        result["receipt_url"] = url_match.group(1)
        if not result["transaction_number"]:
            result["transaction_number"] = url_match.group(2)

    # If still no transaction number, check if the entire input is just a transaction number
    # Telebirr transaction numbers are 10 alphanumeric characters (uppercase + digits)
    if not result["transaction_number"]:
        bare_match = re.match(r"^\s*([A-Z0-9]{10})\s*$", text)
        if bare_match:
            result["transaction_number"] = bare_match.group(1)

    if result["transaction_number"] and not result["receipt_url"]:
        result["receipt_url"] = RECEIPT_BASE_URL + result["transaction_number"]

    date_match = re.search(r"on (\d{2}/\d{2}/\d{4} \d{2}:\d{2}:\d{2})", text)
    if date_match:
        result["date"] = date_match.group(1)

    bal_match = re.search(r"balance is ETB ([\d,]+\.\d{2})", text)
    if bal_match:
        result["balance"] = bal_match.group(1)

    dear_match = re.search(r"Dear (\w+)", text)
    if dear_match:
        result["account_holder"] = dear_match.group(1)

    return result


def parse_receipt_text(text):
    """Parse key fields from telebirr receipt page text (HTML or plain text)."""
    receipt = {}

    patterns = {
        "payer_name": r"(?:Payer Name|የከፋይ ስም)\s+(.+?)(?:\n|$)",
        "payer_phone": r"(?:Payer telebirr no|የከፋይ ቴሌብር ቁ)[.\s/]+(\d[\d*]+)",
        "credited_party_name": r"(?:Credited Party name|የገንዘብ ተቀባይ ስም)\s+(.+?)(?:\n|$)",
        "credited_party_phone": r"(?:Credited party account no|የገንዘብ ተቀባይ ቴሌብር ቁ)[.\s/]+(\d[\d*]+)",
        "transaction_status": r"(?:transaction status|የክፍያው ሁኔታ)\s+(\S+)",
        "invoice_no": r"(?:Invoice No|የክፍያ ቁጥር)[.\s]+(?:የክፍያ ቀን/Payment date\s+የተከፈለው መጠን/Settled Amount\s+)?([A-Z0-9]+)",
        "payment_date": r"([A-Z0-9]+)\s+(\d{2}-\d{2}-\d{4} \d{2}:\d{2}:\d{2})",
        "settled_amount": r"(?:Settled Amount\s+)?(\d{2}-\d{2}-\d{4} \d{2}:\d{2}:\d{2})\s+([\d,.]+)\s*Birr",
        "service_fee": r"(?:Service fee|የአገልግሎት ክፍያ)\s+([\d,.]+)\s*Birr",
        "service_fee_vat": r"(?:Service fee VAT|የአገልግሎት ክፍያ ተ\.እ\.ታ)\s+([\d,.]+)\s*Birr",
        "total_paid_amount": r"(?:Total Paid Amount|ጠቅላላ የተከፈለ)\s+([\d,.]+)\s*Birr",
        "payment_reason": r"(?:Payment Reason|የክፍያ ምክንያት)\s+(.+?)(?:\n|$)",
    }

    # Simple field extraction
    for key in ["payer_name", "payer_phone", "credited_party_name",
                "credited_party_phone", "transaction_status",
                "service_fee", "service_fee_vat", "total_paid_amount",
                "payment_reason"]:
        match = re.search(patterns[key], text, re.IGNORECASE)
        if match:
            receipt[key] = match.group(1).strip()

    # Extract invoice line: "DCD4QNVEX2    13-03-2026 21:00:17    200.00 Birr"
    invoice_match = re.search(r"([A-Z0-9]{8,})\s+(\d{2}-\d{2}-\d{4}\s+\d{2}:\d{2}:\d{2})\s+([\d,.]+)\s*Birr", text)
    if invoice_match:
        receipt["invoice_no"] = invoice_match.group(1)
        receipt["payment_date"] = invoice_match.group(2)
        receipt["settled_amount"] = invoice_match.group(3)

    return receipt


def html_to_text(html):
    """Convert HTML receipt page to plain text for parsing."""
    soup = BeautifulSoup(html, "html.parser")

    # Remove script/style tags
    for tag in soup(["script", "style"]):
        tag.decompose()

    # Get text with separators between table cells
    # Replace <td> and <th> with tab-separated values
    for td in soup.find_all(["td", "th"]):
        td.insert_before("  ")
        td.insert_after("  ")
    for tr in soup.find_all("tr"):
        tr.insert_after("\n")

    text = soup.get_text(separator=" ")
    # Clean up whitespace: collapse multiple spaces but keep newlines
    lines = []
    for line in text.splitlines():
        cleaned = re.sub(r"[ \t]+", " ", line).strip()
        if cleaned:
            lines.append(cleaned)
    return "\n".join(lines)


def fetch_receipt(transaction_number):
    """Fetch the receipt from ethiotelecom and extract text.

    The receipt URL returns a PDF (image-based). We first try to get
    any text from it. If it's HTML, we convert to plain text.
    If it's a PDF, we try OCR or pymupdf text extraction.
    """
    url = RECEIPT_BASE_URL + transaction_number
    try:
        response = requests.get(url, timeout=20)
        if response.status_code != 200:
            return None, None, f"Failed to fetch receipt. HTTP {response.status_code}."

        content_type = response.headers.get("Content-Type", "")
        raw_bytes = response.content

        # Save raw response for debugging
        with open("/tmp/telebirr_receipt_debug.bin", "wb") as f:
            f.write(raw_bytes)

        is_pdf = "pdf" in content_type.lower() or raw_bytes[:4] == b"%PDF"
        is_html = "html" in content_type.lower() or b"<html" in raw_bytes[:500].lower()

        if is_html:
            plain = html_to_text(response.text)
            return plain, "html", None

        if is_pdf:
            # Try PyMuPDF text extraction first
            try:
                import fitz
                doc = fitz.open(stream=raw_bytes, filetype="pdf")
                text = ""
                for page in doc:
                    text += page.get_text()
                doc.close()
                if text.strip():
                    return text, "pdf_text", None
            except Exception:
                pass

            # Try OCR if tesseract is available
            try:
                import fitz
                from PIL import Image
                import pytesseract
                import io

                doc = fitz.open(stream=raw_bytes, filetype="pdf")
                text = ""
                for page in doc:
                    mat = fitz.Matrix(2, 2)
                    pix = page.get_pixmap(matrix=mat)
                    img = Image.open(io.BytesIO(pix.tobytes("png")))
                    text += pytesseract.image_to_string(img)
                doc.close()
                if text.strip():
                    return text, "pdf_ocr", None
            except ImportError:
                pass
            except Exception as e:
                return None, None, f"OCR error: {str(e)}"

            # PDF but can't extract text — return the raw bytes so caller
            # knows the receipt exists (valid transaction)
            return None, "pdf_no_text", None

        # Unknown content type — try as text
        return response.text, "text", None

    except requests.exceptions.Timeout:
        return None, None, "Timeout fetching receipt from ethiotelecom."
    except requests.exceptions.RequestException as e:
        return None, None, f"Network error: {str(e)}"


def validate_receipt(receipt):
    """Validate the parsed receipt against our account."""
    errors = []

    status = receipt.get("transaction_status", "")
    if status and status.lower() != "completed":
        errors.append(f"Transaction status is '{status}', not 'Completed'.")

    credited_name = (receipt.get("credited_party_name") or "").strip()
    credited_phone = (receipt.get("credited_party_phone") or "").strip()

    name_match = (
        MY_ACCOUNT_NAME.lower() in credited_name.lower()
        or credited_name.lower() in MY_ACCOUNT_NAME.lower()
    )
    phone_match = MY_PHONE_MASKED in credited_phone

    if credited_name and not name_match and not phone_match:
        errors.append(
            f"Payment was credited to '{credited_name}' ({credited_phone}), "
            f"NOT to your account '{MY_ACCOUNT_NAME}' ({MY_PHONE_MASKED})."
        )

    amount = None
    if receipt.get("settled_amount"):
        try:
            amount = float(receipt["settled_amount"].replace(",", ""))
        except ValueError:
            errors.append("Could not parse settled amount from receipt.")

    return len(errors) == 0, errors, amount


def validate_sms_only(info):
    """Fallback validation using only SMS text when receipt is unavailable."""
    errors = []

    if info["type"] == "sent":
        errors.append(
            "This is a SENT payment message, not a RECEIVED payment. "
            "The money was sent FROM your account, not TO your account."
        )
        return False, errors, None

    if info["type"] != "received":
        errors.append("Could not determine if this is a sent or received payment.")
        return False, errors, None

    # Check account holder from "Dear Name"
    holder = info.get("account_holder", "")
    first_name = MY_ACCOUNT_NAME.split()[0]
    if holder and holder.lower() != first_name.lower():
        errors.append(
            f"Message addressed to '{holder}', but your account name starts with '{first_name}'. "
            "This payment may not be for your account."
        )
        return False, errors, None

    if not info["amount"]:
        errors.append("Could not extract payment amount.")
        return False, errors, None

    return True, [], info["amount"]


@app.route("/")
def index():
    return render_template("index.html", my_account_name=MY_ACCOUNT_NAME, my_phone=MY_PHONE)


@app.route("/flow")
def flow():
    return render_template("flow.html")


@app.route("/validate", methods=["POST"])
def validate():
    text = request.form.get("sms_text", "").strip()
    if not text:
        return jsonify({"success": False, "errors": ["Please paste the telebirr message text."]})

    # Step 1: Extract transaction info from SMS text
    info = extract_transaction_info(text)

    if not info["transaction_number"]:
        return jsonify({
            "success": False,
            "errors": ["Could not find a transaction number in the text. Please check and try again."],
            "pdf_validated": False,
        })

    receipt_url = info.get("receipt_url") or (RECEIPT_BASE_URL + info["transaction_number"])

    # Step 2: Check for duplicate transaction
    dup = is_duplicate(info["transaction_number"])
    if dup:
        return jsonify({
            "success": False,
            "is_duplicate": True,
            "errors": [
                f"Duplicate transaction! Transaction {dup['transaction_number']} "
                f"was already validated on {dup['validated_at']}."
            ],
            "transaction_number": dup["transaction_number"],
            "receipt_url": receipt_url,
            "original_amount": dup["amount"],
            "original_payer": dup["payer_name"],
            "original_date": dup["payment_date"],
            "validated_at": dup["validated_at"],
        })

    # Step 3: Try to fetch the receipt
    receipt_text, content_type, fetch_error = fetch_receipt(info["transaction_number"])

    if not fetch_error and receipt_text:
        # Step 3: Parse receipt text (works for HTML or OCR'd PDF)
        receipt = parse_receipt_text(receipt_text)

        # Check if we got meaningful data from the receipt
        if receipt.get("credited_party_name") or receipt.get("settled_amount"):
            # Step 4: Validate receipt against our account
            is_valid, errors, amount = validate_receipt(receipt)

            if is_valid:
                save_transaction(
                    info["transaction_number"], amount,
                    receipt.get("payer_name") or info.get("sender"),
                    receipt.get("payer_phone"),
                    receipt.get("payment_date") or info.get("date"),
                    content_type,
                )

            return jsonify({
                "success": is_valid,
                "errors": errors,
                "pdf_validated": True,
                "validation_source": content_type,
                "transaction_number": info["transaction_number"],
                "receipt_url": receipt_url,
                "amount": amount,
                "total_paid": receipt.get("total_paid_amount"),
                "payer_name": receipt.get("payer_name"),
                "payer_phone": receipt.get("payer_phone"),
                "credited_party": receipt.get("credited_party_name"),
                "credited_phone": receipt.get("credited_party_phone"),
                "payment_date": receipt.get("payment_date"),
                "payment_reason": receipt.get("payment_reason"),
                "transaction_status": receipt.get("transaction_status"),
                "service_fee": receipt.get("service_fee"),
                "service_fee_vat": receipt.get("service_fee_vat"),
                "sms_amount": info.get("amount"),
                "sms_type": info.get("type"),
                "sms_sender": info.get("sender"),
                "sms_date": info.get("date"),
            })

    # If we got a PDF but couldn't extract text, the receipt EXISTS
    # (valid transaction on ethiotelecom). Use SMS validation + receipt existence.
    if not fetch_error and content_type == "pdf_no_text":
        is_valid, errors, amount = validate_sms_only(info)
        if is_valid:
            save_transaction(
                info["transaction_number"], amount,
                info.get("sender"), None,
                info.get("date"), "pdf_no_text+sms",
            )
        pdf_note = (
            "Receipt PDF exists on ethiotelecom (transaction is real), "
            "but text could not be extracted. Install tesseract for full PDF validation: "
            "sudo apt-get install -y tesseract-ocr"
        )
        return jsonify({
            "success": is_valid,
            "errors": errors,
            "pdf_validated": False,
            "receipt_exists": True,
            "pdf_note": pdf_note,
            "transaction_number": info["transaction_number"],
            "receipt_url": receipt_url,
            "amount": amount,
            "sms_amount": info.get("amount"),
            "sms_type": info.get("type"),
            "sms_sender": info.get("sender"),
            "sms_date": info.get("date"),
            "account_holder": info.get("account_holder"),
        })

    # Step 5: Fallback — validate from SMS text only
    is_valid, errors, amount = validate_sms_only(info)
    if is_valid:
        save_transaction(
            info["transaction_number"], amount,
            info.get("sender"), None,
            info.get("date"), "sms_only",
        )

    pdf_note = fetch_error or "Could not parse receipt content."

    return jsonify({
        "success": is_valid,
        "errors": errors,
        "pdf_validated": False,
        "receipt_exists": False,
        "pdf_note": pdf_note,
        "transaction_number": info["transaction_number"],
        "receipt_url": receipt_url,
        "amount": amount,
        "sms_amount": info.get("amount"),
        "sms_type": info.get("type"),
        "sms_sender": info.get("sender"),
        "sms_date": info.get("date"),
        "account_holder": info.get("account_holder"),
    })


if __name__ == "__main__":
    app.run(debug=True, port=1111)
