from fastapi import FastAPI, UploadFile, File, Depends, HTTPException, Security
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from pydantic import BaseModel
import requests
import re
import time
import random
import itertools
import threading
import asyncio
from typing import List, Dict, Optional, Tuple
from datetime import datetime
from pymongo import MongoClient
import json
import os
import base64
import xml.etree.ElementTree as ET
import hmac
import hashlib
from pathlib import Path
import urllib.parse

app = FastAPI(title="Live Checker + Balance Sorter API")
security = HTTPBearer()
BASE_DIR = Path(__file__).resolve().parent
API_METHODS_FILE = BASE_DIR / "api_methods.json"

# ================== AUTH (SABIT) ==================
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

# ================== MONGO DB (SABIT) ==================
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

# ================== STRIPE KONFİGÜRASYONU ==================

# ================== NMI KONFİGÜRASYONU ==================
NMI_CONFIG = {
    "api_username": os.getenv("NMI_API_USERNAME", "bygreenllc"),
    "api_password": os.getenv("NMI_API_PASSWORD", "Ak1f1987@..."),
    "security_key": os.getenv("NMI_SECURITY_KEY", "v4_secret_4A9387r9Kc44xHm3p2g2V28Qu9t3vb8X"),
    "api_url": os.getenv("NMI_API_URL", "https://api.nmi.com/api/v1/transaction")
}

# ================== CLOVER KONFİGÜRASYONU ==================
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
    card_str = card_str.strip()
    parts = card_str.split("|")
    
    if len(parts) == 3:
        pan = parts[0].strip()
        expiry = parts[1].strip()
        cvv = parts[2].strip()
        if "/" in expiry:
            exp_parts = expiry.split("/")
            month = exp_parts[0].strip().zfill(2)
            year = exp_parts[1].strip()
            if len(year) == 2:
                year = f"20{year}"
        else:
            return None
    elif len(parts) == 4:
        pan = parts[0].strip()
        month = parts[1].strip().zfill(2)
        year = parts[2].strip()
        if len(year) == 2:
            year = f"20{year}"
        cvv = parts[3].strip()
    else:
        return None
    
    if not pan or len(pan) < 13 or len(pan) > 19:
        return None
    if not month or not year or not cvv:
        return None
    if len(cvv) < 3 or len(cvv) > 4:
        return None
        
    return {
        "pan": pan,
        "month": month,
        "year": year,
        "cvv": cvv,
        "expiry": f"{month}/{year}"
    }

# ================== STRIPE CARD VERIFY ==================
def stripe_verify_card(card_data: Dict) -> Dict:
    """Stripe API ile kart doğrulama"""
    bin_info = get_bin_info(card_data["pan"])
    
    try:
        # Stripe Payment Intent oluştur
        payload = {
            "amount": 50,  # 0.50 USD in cents
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
    """NMI Account Verification ile kart doğrulama"""
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
    """Clover API ile kart doğrulama (Tokenize + Charge)"""
    bin_info = get_bin_info(card_data["pan"])
    
    try:
        # 1. Kartı Tokenize Et
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
        
        # 2. Token ile Charge (0.50 test)
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

# ================== LIVE CHECK (Stripe + NMI + Clover) ==================
def live_check_single(card_data: Dict) -> Dict:
    """
    Önce Stripe, sonra NMI, sonra Clover dene
    """
    
    # 1. Stripe ile dene
    stripe_result = stripe_verify_card(card_data)
    if stripe_result.get("live", False):
        return stripe_result
    
    # 2. NMI ile dene
    nmi_result = nmi_verify_card(card_data)
    if nmi_result.get("live", False):
        return nmi_result
    
    # 3. Clover ile dene
    clover_result = clover_verify_card(card_data)
    if clover_result.get("live", False):
        return clover_result
    
    # Hepsi başarısız
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
        parsed = parse_card(card_str)
        if parsed:
            parsed_cards.append(parsed)
    
    if not parsed_cards:
        return [{"error": "Geçerli kart bulunamadı"}]
    
    for i, card in enumerate(parsed_cards):
        result = live_check_single(card)
        results.append(result)
        if i < len(parsed_cards) - 1:
            delay = random.uniform(0.5, 1.0)
            time.sleep(delay)
    
    return results

# ================== API ENDPOINTLER ==================

@app.get("/")
async def home():
    return {
        "status": "API aktif (Stripe + NMI + Clover)",
        "endpoints": [
            "/livecheck",
            "/balancesort",
            "/bulklive",
            "/balancebybin",
            "/stripe/verify",
            "/nmi/verify",
            "/clover/verify",
            "/docs"
        ],
        "auth_required": "Bearer token ile",
        "stripe_enabled": True,
        "nmi_enabled": True,
        "clover_enabled": True
    }

@app.post("/livecheck")
async def livecheck(cards: List[str], auth: str = Depends(verify_auth)):
    require_card_checks_enabled()
    results = bulk_live_check(cards)
    return {"total": len(results), "results": results}

@app.post("/stripe/verify")
async def stripe_verify(card: str, auth: str = Depends(verify_auth)):
    """Sadece Stripe ile kart doğrulama testi"""
    require_card_checks_enabled()
    card_data = parse_card(card)
    if not card_data:
        raise HTTPException(status_code=400, detail="Geçersiz kart formatı")
    result = stripe_verify_card(card_data)
    return result

@app.post("/nmi/verify")
async def nmi_verify(card: str, auth: str = Depends(verify_auth)):
    """Sadece NMI ile kart doğrulama testi"""
    require_card_checks_enabled()
    card_data = parse_card(card)
    if not card_data:
        raise HTTPException(status_code=400, detail="Geçersiz kart formatı")
    result = nmi_verify_card(card_data)
    return result

@app.post("/clover/verify")
async def clover_verify(card: str, auth: str = Depends(verify_auth)):
    """Sadece Clover ile kart doğrulama testi"""
    require_card_checks_enabled()
    card_data = parse_card(card)
    if not card_data:
        raise HTTPException(status_code=400, detail="Geçersiz kart formatı")
    result = clover_verify_card(card_data)
    return result

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=10000)