from fastapi import FastAPI, UploadFile, File, Depends, HTTPException, Security, Query
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from pydantic import BaseModel
import requests
import re
import time
import random
import itertools
import threading
import asyncio
from typing import List, Dict, Optional, Tuple, Any
from datetime import datetime, timedelta
from pymongo import MongoClient
import json
import os
import base64
import xml.etree.ElementTree as ET
import hmac
import hashlib
from pathlib import Path
import urllib.parse

app = FastAPI(
    title="Live Checker + Balance Sorter API",
    description="Kredi kartı doğrulama, BIN lookup, balance kontrol ve API katalog sistemi",
    version="2.0.0"
)
security = HTTPBearer()
BASE_DIR = Path(__file__).resolve().parent
API_METHODS_FILE = BASE_DIR / "api_methods.json"

# ================== AUTH ==================
AUTH_TOKEN = os.getenv("AUTH_TOKEN", "b9f3k7m2v8t3w5z1q6p9c4b7n2v8m2025")
STRIPE_SECRET_KEY = os.getenv("STRIPE_SECRET_KEY")
CARD_CHECKS_ENABLED = os.getenv("ENABLE_CARD_CHECKS", "true").lower() in {"1", "true", "yes", "on"}

def verify_auth(credentials: HTTPAuthorizationCredentials = Security(security)):
    if credentials.credentials != AUTH_TOKEN:
        raise HTTPException(status_code=401, detail="Geçersiz token")
    return credentials.credentials

def require_card_checks_enabled():
    if not CARD_CHECKS_ENABLED:
        raise HTTPException(status_code=403, detail="Card check endpointleri deploy ortamında kapalı")

# ================== CARD CHECKER MODULE ==================

class CardChecker:
    """CardChecker sınıfı - kart doğrulama işlemleri"""
    
    BIN_CACHE_MAX_AGE_MS = 30 * 24 * 60 * 60 * 1000  # 30 gün
    
    def __init__(self):
        self.cache = {}
    
    def digits_only(self, value: Any) -> str:
        """Sadece rakamları döndür"""
        return re.sub(r'\D', '', str(value or ""))
    
    def normalize_expiry(self, value: Any) -> Optional[Dict]:
        """Expiry değerini normalize et"""
        text = str(value or "").strip()
        compact = self.digits_only(text)
        
        month = ""
        year = ""
        
        if "/" in text or "-" in text:
            parts = re.split(r'[/-]', text)
            month = self.digits_only(parts[0]) if len(parts) > 0 else ""
            year = self.digits_only(parts[1]) if len(parts) > 1 else ""
        elif len(compact) == 4:
            month = compact[:2]
            year = compact[2:]
        elif len(compact) == 6:
            month = compact[:2]
            year = compact[2:]
        
        month = month.zfill(2)
        if len(year) == 2:
            year = f"20{year}"
        
        if not re.match(r'^(0[1-9]|1[0-2])$', month) or not re.match(r'^\d{4}$', year):
            return None
        
        return {
            "month": month,
            "year": year,
            "label": f"{month}/{year[-2:]}"
        }
    
    def normalize_card_input(self, payload: Dict = None) -> Dict:
        """Kart girdisini normalize et"""
        if payload is None:
            payload = {}
        
        pan = self.digits_only(payload.get("pan") or payload.get("cardNumber") or 
                               payload.get("cardnumber") or payload.get("number"))
        
        expiry = self.normalize_expiry(
            payload.get("exp") or 
            payload.get("expiry") or 
            f"{payload.get('expMonth', '')}/{payload.get('expYear', '')}"
        )
        
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
            "holderName": str(payload.get("holderName") or payload.get("cardholderName") or payload.get("name") or "").strip(),
            "address": str(payload.get("address") or payload.get("billingAddress") or "").strip(),
            "phone": str(payload.get("phone") or "").strip()
        }
    
    def parse_card_line(self, line: str) -> Dict:
        """Kart satırını parse et"""
        parts = str(line or "").strip().split("|")
        
        def field_after_cvv(index: int) -> Dict:
            value = str(parts[index] if index < len(parts) else "").strip()
            is_zip = bool(re.match(r'^\d{5}$', value))
            return {
                "zip": value if is_zip else "00000",
                "holderName": parts[index + 1] if is_zip and index + 1 < len(parts) else value if not is_zip else "",
                "address": "|".join(parts[index + 2:]) if is_zip else "|".join(parts[index + 1:])
            }
        
        month = self.digits_only(parts[1]) if len(parts) > 1 else ""
        year = self.digits_only(parts[2]) if len(parts) > 2 else ""
        cvv_after_year = str(parts[3] if len(parts) > 3 else "").strip()
        
        if len(parts) >= 4 and re.match(r'^(0?[1-9]|1[0-2])$', month) and re.match(r'^(\d{2}|\d{4})$', year) and cvv_after_year:
            after_cvv = field_after_cvv(4)
            return self.normalize_card_input({
                "cardNumber": parts[0],
                "exp": f"{month}/{year}",
                "cvv": parts[3],
                "zip": after_cvv["zip"],
                "holderName": after_cvv["holderName"],
                "address": after_cvv["address"]
            })
        
        after_cvv = field_after_cvv(3)
        return self.normalize_card_input({
            "cardNumber": parts[0],
            "exp": parts[1] if len(parts) > 1 else "",
            "cvv": parts[2] if len(parts) > 2 else "",
            "zip": after_cvv["zip"],
            "holderName": after_cvv["holderName"],
            "address": after_cvv["address"]
        })
    
    def mask_pan(self, pan: str) -> Optional[str]:
        """PAN'i maskele"""
        digits = self.digits_only(pan)
        if len(digits) < 10:
            return None
        return f"{digits[:6]}******{digits[-4:]}"
    
    def record_hash(self, card: Dict) -> str:
        """Kart hash'i oluştur"""
        data = f"{self.digits_only(card['pan'])}|{card['expMonth']}|{card['expYear']}"
        return hashlib.sha256(data.encode()).hexdigest()[:24]

card_checker = CardChecker()

# ================== MONGO DB ==================
MONGODB_URI = os.getenv("MONGODB_URI", "")

try:
    if MONGODB_URI:
        client = MongoClient(
            MONGODB_URI,
            tls=True,
            tlsAllowInvalidCertificates=True,
            tlsAllowInvalidHostnames=True
        )
        db = client[os.getenv("MONGODB_DB", "paymentmanger")]
        collection = db[os.getenv("MONGODB_COLLECTION", "live_balance_results")]
        print("[+] MongoDB bağlantısı başarılı")
    else:
        collection = None
        print("[!] MONGODB_URI tanımlı değil, MongoDB kayıtları kapalı")
except Exception as e:
    print(f"[!] MongoDB hatası: {e}")
    collection = None

# ================== CONFIGURATIONS ==================
NMI_CONFIG = {
    "api_username": os.getenv("NMI_API_USERNAME", "bygreenllc"),
    "api_password": os.getenv("NMI_API_PASSWORD", "Ak1f1987@..."),
    "security_key": os.getenv("NMI_SECURITY_KEY", "v4_secret_4A9387r9Kc44xHm3p2g2V28Qu9t3vb8X"),
    "api_url": os.getenv("NMI_API_URL", "https://api.nmi.com/api/v1/transaction")
}

CLOVER_CONFIG = {
    "merchant_id": os.getenv("CLOVER_MERCHANT_ID", "518993421163932"),
    "public_token": os.getenv("CLOVER_PUBLIC_TOKEN", "cc5f1f800dad9399d3e46aca8da49d8f"),
    "private_token": os.getenv("CLOVER_PRIVATE_TOKEN", "c7ee250b-e9ae-ab59-ba52-616ecc63ed29"),
    "api_url": os.getenv("CLOVER_API_URL", "https://api.clover.com/v1/charges"),
    "token_url": os.getenv("CLOVER_TOKEN_URL", "https://token.clover.com/v1/tokens")
}

# ================== BIN LOOKUP ==================
def get_bin_info(bin_number: str) -> Dict:
    try:
        r = requests.get(f"https://lookup.binlist.net/{bin_number[:6]}", timeout=8)
        if r.status_code == 200:
            data = r.json()
            return {
                "bin": bin_number[:6],
                "brand": data.get("scheme", "").upper(),
                "type": data.get("type", "").upper(),
                "level": data.get("brand", "").upper(),
                "bank": data.get("bank", {}).get("name", "Unknown"),
                "country": data.get("country", {}).get("alpha2", "XX"),
                "country_name": data.get("country", {}).get("name", "Unknown")
            }
    except:
        pass
    return {
        "bin": bin_number[:6],
        "brand": "UNKNOWN",
        "type": "UNKNOWN",
        "level": "UNKNOWN",
        "bank": "Unknown",
        "country": "XX",
        "country_name": "Unknown"
    }

# ================== KART FORMATLAMA ==================
def parse_card(card_str: str) -> Optional[Dict]:
    return card_checker.parse_card_line(card_str) if "|" in card_str else card_checker.normalize_card_input({"cardNumber": card_str})

# ================== STRIPE CARD VERIFY ==================
def stripe_verify_card(card_data: Dict) -> Dict:
    if not STRIPE_SECRET_KEY:
        return {
            "status": "dead",
            "live": False,
            "balance": "0.00",
            "gateway": "Stripe_Live",
            "error": "STRIPE_SECRET_KEY env tanımlı değil",
            "bin": get_bin_info(card_data["pan"]),
            "card": card_data
        }
    
    bin_info = get_bin_info(card_data["pan"])
    
    try:
        payload = {
            "amount": 50,
            "currency": "usd",
            "payment_method_types[]": "card",
            "payment_method_data[type]": "card",
            "payment_method_data[card][number]": card_data["pan"],
            "payment_method_data[card][exp_month]": card_data["month"],
            "payment_method_data[card][exp_year]": card_data["year"],
            "payment_method_data[card][cvc]": card_data["cvv"],
            "confirm": "true",
            "return_url": "https://example.com/return"
        }
        
        headers = {
            "Authorization": f"Bearer {STRIPE_SECRET_KEY}",
            "Content-Type": "application/x-www-form-urlencoded"
        }
        
        response = requests.post(
            "https://api.stripe.com/v1/payment_intents",
            data=payload,
            headers=headers,
            timeout=15
        )
        
        if response.status_code in [200, 201]:
            data = response.json()
            status = data.get("status")
            is_live = status in ["succeeded", "requires_capture", "requires_confirmation"]
            
            return {
                "status": "live" if is_live else "dead",
                "live": is_live,
                "balance": "0.00",
                "gateway": "Stripe_Live",
                "proxy": "none",
                "bin": bin_info,
                "card": card_data,
                "transaction_id": data.get("id"),
                "stripe_status": status,
                "client_secret": data.get("client_secret", "")
            }
        else:
            return {
                "status": "dead",
                "live": False,
                "balance": "0.00",
                "gateway": "Stripe_Live",
                "error": f"HTTP {response.status_code}: {response.text[:100]}",
                "bin": bin_info,
                "card": card_data
            }
            
    except Exception as e:
        return {
            "status": "dead",
            "live": False,
            "balance": "0.00",
            "gateway": "Stripe_Live",
            "error": str(e)[:100],
            "bin": bin_info,
            "card": card_data
        }

# ================== NMI CARD VERIFY ==================
def nmi_verify_card(card_data: Dict) -> Dict:
    bin_info = get_bin_info(card_data["pan"])
    
    xml_request = f"""<?xml version="1.0" encoding="utf-8"?>
    <sale>
        <api-username>{NMI_CONFIG['api_username']}</api-username>
        <api-password>{NMI_CONFIG['api_password']}</api-password>
        <security-key>{NMI_CONFIG['security_key']}</security-key>
        <type>verify</type>
        <cc-number>{card_data['pan']}</cc-number>
        <cc-exp>{card_data['month']}{card_data['year'][-2:]}</cc-exp>
        <cc-cvv>{card_data['cvv']}</cc-cvv>
        <amount>0.00</amount>
        <currency>USD</currency>
        <order-description>Card Verification</order-description>
        <billing-firstname>Test</billing-firstname>
        <billing-lastname>User</billing-lastname>
        <billing-address1>123 Test St</billing-address1>
        <billing-city>Test City</billing-city>
        <billing-state>TS</billing-state>
        <billing-zip>12345</billing-zip>
        <billing-country>US</billing-country>
    </sale>"""
    
    headers = {
        "Content-Type": "application/xml",
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"
    }
    
    try:
        r = requests.post(
            NMI_CONFIG['api_url'],
            data=xml_request,
            headers=headers,
            timeout=15
        )
        
        if r.status_code in [200, 201, 202]:
            root = ET.fromstring(r.text)
            result_code = root.findtext('result_code', '')
            result_text = root.findtext('result_text', '')
            result = root.findtext('result', '')
            
            is_live = result_code in ['100', '200'] or result.upper() in ['SUCCESS', 'APPROVED']
            transaction_id = root.findtext('transaction_id', '')
            
            return {
                "status": "live" if is_live else "dead",
                "live": is_live,
                "balance": "0.00",
                "gateway": "NMI_AccountVerification",
                "proxy": "none",
                "bin": bin_info,
                "card": card_data,
                "result_code": result_code,
                "result_text": result_text,
                "transaction_id": transaction_id
            }
        else:
            return {
                "status": "dead",
                "live": False,
                "balance": "0.00",
                "gateway": "NMI_AccountVerification",
                "error": f"HTTP {r.status_code}",
                "bin": bin_info,
                "card": card_data
            }
            
    except ET.ParseError as e:
        return {
            "status": "dead",
            "live": False,
            "balance": "0.00",
            "gateway": "NMI_AccountVerification",
            "error": f"XML Parse Error: {str(e)}",
            "bin": bin_info,
            "card": card_data
        }
    except Exception as e:
        return {
            "status": "dead",
            "live": False,
            "balance": "0.00",
            "gateway": "NMI_AccountVerification",
            "error": str(e)[:100],
            "bin": bin_info,
            "card": card_data
        }

# ================== CLOVER CARD VERIFY ==================
def clover_verify_card(card_data: Dict) -> Dict:
    bin_info = get_bin_info(card_data["pan"])
    
    try:
        token_payload = {
            "card": {
                "number": card_data["pan"],
                "exp_month": int(card_data["month"]),
                "exp_year": int(card_data["year"]),
                "cvv": card_data["cvv"]
            }
        }
        
        token_headers = {
            "Content-Type": "application/json",
            "apikey": CLOVER_CONFIG["public_token"]
        }
        
        token_response = requests.post(
            CLOVER_CONFIG["token_url"],
            json=token_payload,
            headers=token_headers,
            timeout=10
        )
        
        if token_response.status_code != 200:
            return {
                "status": "dead",
                "live": False,
                "balance": "0.00",
                "gateway": "Clover_Tokenization",
                "error": f"Tokenization failed: HTTP {token_response.status_code}",
                "bin": bin_info,
                "card": card_data
            }
        
        token_data = token_response.json()
        token_id = token_data.get("id")
        
        if not token_id:
            return {
                "status": "dead",
                "live": False,
                "balance": "0.00",
                "gateway": "Clover_Tokenization",
                "error": "No token ID received",
                "bin": bin_info,
                "card": card_data
            }
        
        charge_payload = {
            "amount": 50,
            "currency": "usd",
            "source": token_id,
            "capture": False,
            "metadata": {"test": "true"}
        }
        
        charge_headers = {
            "Content-Type": "application/json",
            "Authorization": f"Bearer {CLOVER_CONFIG['private_token']}"
        }
        
        charge_response = requests.post(
            CLOVER_CONFIG["api_url"],
            json=charge_payload,
            headers=charge_headers,
            timeout=10
        )
        
        if charge_response.status_code in [200, 201, 202]:
            charge_data = charge_response.json()
            is_live = charge_data.get("status") in ["succeeded", "approved", "authorized"]
            
            return {
                "status": "live" if is_live else "dead",
                "live": is_live,
                "balance": "0.00",
                "gateway": "Clover_Charge",
                "proxy": "none",
                "bin": bin_info,
                "card": card_data,
                "transaction_id": charge_data.get("id"),
                "clover_token": token_id
            }
        else:
            return {
                "status": "dead",
                "live": False,
                "balance": "0.00",
                "gateway": "Clover_Charge",
                "error": f"Charge failed: HTTP {charge_response.status_code}",
                "bin": bin_info,
                "card": card_data
            }
            
    except Exception as e:
        return {
            "status": "dead",
            "live": False,
            "balance": "0.00",
            "gateway": "Clover_Error",
            "error": str(e)[:100],
            "bin": bin_info,
            "card": card_data
        }

# ================== LIVE CHECK ==================
def live_check_single(card_data: Dict) -> Dict:
    stripe_result = stripe_verify_card(card_data)
    if stripe_result.get("live", False):
        return stripe_result
    
    nmi_result = nmi_verify_card(card_data)
    if nmi_result.get("live", False):
        return nmi_result
    
    clover_result = clover_verify_card(card_data)
    if clover_result.get("live", False):
        return clover_result
    
    return {
        "status": "dead",
        "live": False,
        "balance": "0.00",
        "gateway": "none",
        "error": "Tüm gateway'ler başarısız",
        "bin": get_bin_info(card_data["pan"]),
        "card": card_data
    }

# ================== TOPLU LIVE CHECK ==================
def bulk_live_check(cards: List[str]) -> List[Dict]:
    results = []
    parsed_cards = []
    
    for card_str in cards:
        try:
            parsed = parse_card(card_str)
            if parsed:
                parsed_cards.append(parsed)
        except Exception as e:
            results.append({"error": str(e), "card": card_str})
    
    if not parsed_cards:
        return [{"error": "Geçerli kart bulunamadı"}]
    
    for i, card in enumerate(parsed_cards):
        result = live_check_single(card)
        results.append(result)
        if i < len(parsed_cards) - 1:
            delay = random.uniform(0.5, 1.0)
            time.sleep(delay)
    
    return results

# ================== API METHOD CATALOG ==================
def load_api_methods() -> Dict:
    try:
        if API_METHODS_FILE.exists():
            return json.loads(API_METHODS_FILE.read_text(encoding="utf-8"))
    except:
        pass
    
    # Built-in catalog
    return {
        "source": API_METHODS_FILE.name,
        "collection": "Live Checker API",
        "total": 12,
        "methods": [
            {"id": "livecheck", "method": "POST", "path": "/livecheck", "group": "Card Checks", "description": "Toplu kart live check"},
            {"id": "balancesort", "method": "POST", "path": "/balancesort", "group": "Card Checks", "description": "Balance sıralama ve ortalama"},
            {"id": "bulklive", "method": "POST", "path": "/bulklive", "group": "Card Checks", "description": "Dosyadan toplu live check"},
            {"id": "balancebybin", "method": "POST", "path": "/balancebybin", "group": "Card Checks", "description": "BIN'e göre balance gruplama"},
            {"id": "stripe_verify", "method": "POST", "path": "/stripe/verify", "group": "Card Checks", "description": "Stripe ile kart doğrulama"},
            {"id": "nmi_verify", "method": "POST", "path": "/nmi/verify", "group": "Card Checks", "description": "NMI ile kart doğrulama"},
            {"id": "clover_verify", "method": "POST", "path": "/clover/verify", "group": "Card Checks", "description": "Clover ile kart doğrulama"},
            {"id": "gatewaystats", "method": "GET", "path": "/gatewaystats", "group": "System", "description": "Gateway istatistikleri"},
            {"id": "api_methods", "method": "GET", "path": "/api-methods", "group": "System", "description": "API method kataloğu"},
            {"id": "api_methods_by_group", "method": "GET", "path": "/api-methods/groups/{group_name}", "group": "System", "description": "Grup bazlı API methodları"},
            {"id": "parse_cards", "method": "POST", "path": "/parse-cards", "group": "Utility", "description": "Kart formatlarını parse et"},
            {"id": "mask_pan", "method": "POST", "path": "/mask-pan", "group": "Utility", "description": "PAN maskeleme"}
        ]
    }

# ================== API ENDPOINTLER ==================

@app.get("/")
async def home():
    return {
        "status": "API aktif (Stripe + NMI + Clover)",
        "version": "2.0.0",
        "endpoints": [
            "/livecheck",
            "/balancesort", 
            "/bulklive",
            "/balancebybin",
            "/stripe/verify",
            "/nmi/verify",
            "/clover/verify",
            "/api-methods",
            "/docs"
        ],
        "auth_required": "Bearer token ile",
        "stripe_enabled": bool(STRIPE_SECRET_KEY),
        "nmi_enabled": bool(NMI_CONFIG.get("api_username")),
        "clover_enabled": bool(CLOVER_CONFIG.get("public_token"))
    }

@app.get("/gatewaystats")
async def get_gateway_stats(auth: str = Depends(verify_auth)):
    return {
        "Stripe_Live": {"status": "🟢 Active" if STRIPE_SECRET_KEY else "🔴 Disabled"},
        "NMI_AccountVerification": {"status": "🟢 Active" if NMI_CONFIG.get("api_username") else "🔴 Disabled"},
        "Clover_Charge": {"status": "🟢 Active" if CLOVER_CONFIG.get("public_token") else "🔴 Disabled"}
    }

@app.get("/api-methods")
async def api_methods(auth: str = Depends(verify_auth)):
    """API method kataloğu"""
    return load_api_methods()

@app.get("/api-methods/groups/{group_name}")
async def api_methods_by_group(group_name: str, auth: str = Depends(verify_auth)):
    """Grup bazlı API methodları"""
    catalog = load_api_methods()
    group_key = group_name.strip().lower()
    methods = [
        method for method in catalog.get("methods", [])
        if method.get("group", "").lower() == group_key
    ]
    return {
        "source": catalog.get("source"),
        "collection": catalog.get("collection"),
        "group": group_name,
        "total": len(methods),
        "methods": methods
    }

@app.post("/livecheck")
async def livecheck(cards: List[str], auth: str = Depends(verify_auth)):
    require_card_checks_enabled()
    results = bulk_live_check(cards)
    if collection:
        try:
            collection.insert_one({"type": "livecheck", "cards": len(cards), "results": results, "timestamp": datetime.utcnow()})
        except:
            pass
    return {"total": len(results), "results": results}

@app.post("/balancesort")
async def balancesort(cards: List[str], auth: str = Depends(verify_auth)):
    require_card_checks_enabled()
    results = bulk_live_check(cards)
    live_results = [r for r in results if r.get("live", False)]
    dead_results = [r for r in results if not r.get("live", False)]
    
    return {
        "total_cards": len(results),
        "live_count": len(live_results),
        "dead_count": len(dead_results),
        "success_rate": f"{(len(live_results)/len(results)*100):.1f}%" if results else "0%",
        "live_cards": [
            {
                "pan": card_checker.mask_pan(r.get("card", {}).get("pan", "")),
                "gateway": r.get("gateway", "unknown"),
                "brand": r.get("bin", {}).get("brand", "UNKNOWN"),
                "country": r.get("bin", {}).get("country_name", "UNKNOWN")
            }
            for r in live_results
        ],
        "dead_cards": [
            {
                "pan": card_checker.mask_pan(r.get("card", {}).get("pan", "")),
                "brand": r.get("bin", {}).get("brand", "UNKNOWN")
            }
            for r in dead_results
        ]
    }

@app.post("/bulklive")
async def bulklive(file: UploadFile = File(...), auth: str = Depends(verify_auth)):
    require_card_checks_enabled()
    content = await file.read()
    cards = content.decode("utf-8").splitlines()
    cards = [c.strip() for c in cards if c.strip()]
    if not cards:
        return {"error": "Dosya boş"}
    results = bulk_live_check(cards)
    if collection:
        try:
            collection.insert_one({"type": "bulklive", "cards": len(cards), "results": results, "timestamp": datetime.utcnow()})
        except:
            pass
    return {"total": len(results), "results": results}

@app.post("/stripe/verify")
async def stripe_verify(card: str, auth: str = Depends(verify_auth)):
    require_card_checks_enabled()
    try:
        card_data = parse_card(card)
        if not card_data:
            raise HTTPException(status_code=400, detail="Geçersiz kart formatı")
        result = stripe_verify_card(card_data)
        return result
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))

@app.post("/nmi/verify")
async def nmi_verify(card: str, auth: str = Depends(verify_auth)):
    require_card_checks_enabled()
    try:
        card_data = parse_card(card)
        if not card_data:
            raise HTTPException(status_code=400, detail="Geçersiz kart formatı")
        result = nmi_verify_card(card_data)
        return result
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))

@app.post("/clover/verify")
async def clover_verify(card: str, auth: str = Depends(verify_auth)):
    require_card_checks_enabled()
    try:
        card_data = parse_card(card)
        if not card_data:
            raise HTTPException(status_code=400, detail="Geçersiz kart formatı")
        result = clover_verify_card(card_data)
        return result
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))

@app.post("/parse-cards")
async def parse_cards(cards: List[str], auth: str = Depends(verify_auth)):
    """Kart formatlarını parse et"""
    results = []
    for card in cards:
        try:
            parsed = parse_card(card)
            if parsed:
                results.append({
                    "original": card,
                    "parsed": {
                        "pan": parsed.get("pan"),
                        "expMonth": parsed.get("expMonth"),
                        "expYear": parsed.get("expYear"),
                        "expiry": parsed.get("exp"),
                        "masked": card_checker.mask_pan(parsed.get("pan", "")),
                        "hash": card_checker.record_hash(parsed)
                    }
                })
        except Exception as e:
            results.append({"original": card, "error": str(e)})
    return {"total": len(results), "results": results}

@app.post("/mask-pan")
async def mask_pan_endpoint(pan: str, auth: str = Depends(verify_auth)):
    """PAN maskeleme"""
    return {"original": pan, "masked": card_checker.mask_pan(pan)}

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=10000)