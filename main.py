from fastapi import FastAPI, UploadFile, File, Depends, HTTPException, Security
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
import requests
import re
import time
import random
import hashlib
import xml.etree.ElementTree as ET
from typing import Dict, List, Optional, Any
from datetime import datetime
import json
import os
import logging
from pymongo import MongoClient
from pydantic import BaseModel

# ================== LOGGING ==================
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

app = FastAPI(title="Advanced Live Checker + BIN API")
security = HTTPBearer()

# ================== AUTH ==================
AUTH_TOKEN = os.getenv("AUTH_TOKEN", "b9f3k7m2v8t3w5z1q6p9c4b7n2v8m2025")
MOCK_MODE = os.getenv("MOCK_MODE", "true").lower() in {"1", "true", "yes", "on"}
MONGO_URI = os.getenv("MONGODB_URI", "mongodb+srv://paymentmanger.gvaavzc.mongodb.net/?authSource=%24external&authMechanism=MONGODB-X509&appName=paymentmanger")

# ================== MONGODB ==================
try:
    client = MongoClient(MONGO_URI, tls=True, tlsAllowInvalidCertificates=True)
    db = client["paymentmanger"]
    collection = db["card_checks"]
    logger.info("[+] MongoDB bağlantısı başarılı")
except Exception as e:
    logger.error(f"[!] MongoDB hatası: {e}")
    collection = None

def save_to_mongodb(data: Dict):
    if collection:
        try:
            collection.insert_one({**data, "timestamp": datetime.utcnow()})
        except Exception as e:
            logger.error(f"[!] MongoDB kayıt hatası: {e}")

# ================== MODELS ==================
class CardCheckRequest(BaseModel):
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

# ================== PROVIDER LIST ==================
PROVIDERS = [
    {"name": "clover", "status": "untested", "last_check": None},
    {"name": "authorizenet", "status": "untested", "last_check": None},
    {"name": "paypal", "status": "untested", "last_check": None},
    {"name": "amazonpay", "status": "untested", "last_check": None},
]

PROVIDER_STATS = {p["name"]: {"success": 0, "fail": 0, "total": 0} for p in PROVIDERS}
PROVIDER_INDEX = 0

# ================== PROVIDER HEALTH CHECK ==================
async def test_provider(provider_name: str) -> bool:
    """Provider'ın çalışıp çalışmadığını test et"""
    try:
        test_card = {
            "pan": "4111111111111111",
            "expMonth": "12",
            "expYear": "2030",
            "cvv": "123"
        }
        
        result = await provider_service.verify_card(test_card, provider_name)
        is_healthy = result.get("status") in ["approved", "declined", "error"]
        
        # Provider'ı güncelle
        for p in PROVIDERS:
            if p["name"] == provider_name:
                p["status"] = "healthy" if is_healthy else "unhealthy"
                p["last_check"] = datetime.now().isoformat()
                break
        
        logger.info(f"[HEALTH] {provider_name}: {'✅' if is_healthy else '❌'}")
        return is_healthy
        
    except Exception as e:
        logger.warning(f"[HEALTH] {provider_name} test hatası: {e}")
        for p in PROVIDERS:
            if p["name"] == provider_name:
                p["status"] = "unhealthy"
                p["last_check"] = datetime.now().isoformat()
                break
        return False

async def test_all_providers():
    """Tüm provider'ları başlangıçta test et"""
    logger.info("[HEALTH] Tüm provider'lar test ediliyor...")
    for p in PROVIDERS:
        await test_provider(p["name"])
        time.sleep(1)
    logger.info("[HEALTH] Test tamamlandı")

def get_next_healthy_provider() -> str:
    """Bir sonraki sağlıklı provider'ı döndür (rotasyon)"""
    global PROVIDER_INDEX
    
    healthy_providers = [p["name"] for p in PROVIDERS if p["status"] == "healthy"]
    
    if not healthy_providers:
        # Hiç sağlıklı yoksa clover'ı dene
        logger.warning("[PROVIDER] Sağlıklı provider yok, clover deneniyor")
        return "clover"
    
    # Rotasyon
    provider = healthy_providers[PROVIDER_INDEX % len(healthy_providers)]
    PROVIDER_INDEX += 1
    
    return provider

# ================== BIN CHECK ==================
async def check_bin(bin_number: str) -> Dict:
    """BIN kontrolü - bin-check API veya binlist"""
    bin_clean = re.sub(r'\D', '', str(bin_number))[:6]
    
    if len(bin_clean) < 6:
        return {
            "success": False,
            "error": "Invalid BIN (need 6 digits)",
            "bin": bin_clean
        }
    
    try:
        # Önce bin-check API'yi dene
        try:
            url = f"https://bin-check-dr4g.herokuapp.com/api/{bin_clean}"
            response = requests.get(url, timeout=5)
            
            if response.status_code == 200:
                data = response.json()
                if data.get("result") != "false":
                    return {
                        "success": True,
                        "data": {
                            "bin": data.get("data", {}).get("bin", bin_clean),
                            "vendor": data.get("data", {}).get("vendor", "UNKNOWN"),
                            "type": data.get("data", {}).get("type", "UNKNOWN"),
                            "level": data.get("data", {}).get("level", "STANDARD"),
                            "bank": data.get("data", {}).get("bank", "Unknown"),
                            "country": data.get("data", {}).get("country", "XX")
                        },
                        "source": "bin-check-api"
                    }
        except:
            pass
        
        # Fallback: binlist.net
        response = requests.get(f"https://lookup.binlist.net/{bin_clean}", 
                               headers={"Accept-Version": "3"}, timeout=5)
        
        if response.status_code == 200:
            data = response.json()
            bank = data.get("bank", {})
            country = data.get("country", {})
            brand = data.get("scheme", "UNKNOWN").upper()
            brand_name = data.get("brand", "").upper()
            
            # Level belirle
            level = "STANDARD"
            if "PLATINUM" in brand_name:
                level = "PLATINUM"
            elif "GOLD" in brand_name:
                level = "GOLD"
            elif "SIGNATURE" in brand_name:
                level = "SIGNATURE"
            elif "INFINITE" in brand_name:
                level = "INFINITE"
            elif "WORLD" in brand_name:
                level = "WORLD"
            
            return {
                "success": True,
                "data": {
                    "bin": bin_clean,
                    "vendor": brand,
                    "type": data.get("type", "UNKNOWN").upper(),
                    "level": level,
                    "bank": bank.get("name", "Unknown"),
                    "country": country.get("alpha2", "XX")
                },
                "source": "binlist.net"
            }
        
        return {
            "success": False,
            "error": "No data found",
            "bin": bin_clean
        }
        
    except Exception as e:
        logger.warning(f"[BIN] Hata: {e}")
        return {
            "success": False,
            "error": str(e),
            "bin": bin_clean
        }

# ================== HELPER FUNCTIONS ==================
def digits_only(value: Any) -> str:
    if value is None:
        return ""
    return re.sub(r'\D', '', str(value))

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
    
    return {
        "month": month,
        "year": year,
        "label": f"{month}/{year[-2:]}"
    }

def normalize_card_input(payload: Dict) -> Dict:
    pan = digits_only(
        payload.get("pan") or 
        payload.get("cardNumber") or 
        payload.get("cardnumber") or 
        payload.get("number") or 
        ""
    )
    
    expiry = None
    if payload.get("exp"):
        expiry = normalize_expiry(payload.get("exp"))
    elif payload.get("expiry"):
        expiry = normalize_expiry(payload.get("expiry"))
    elif payload.get("expMonth") and payload.get("expYear"):
        expiry = normalize_expiry(f"{payload.get('expMonth')}/{payload.get('expYear')}")
    
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
        holder = parts[4] if len(parts) > 4 else ""
    else:
        month = parts[1].zfill(2)
        year = parts[2]
        if len(year) == 2:
            year = f"20{year}"
        expiry = f"{month}/{year}"
        cvv = parts[3] if len(parts) > 3 else ""
        zip_code = parts[4] if len(parts) > 4 else "00000"
        holder = parts[5] if len(parts) > 5 else ""
    
    return normalize_card_input({
        "cardNumber": pan,
        "exp": expiry,
        "cvv": cvv,
        "zip": zip_code,
        "holderName": holder
    })

def mask_pan(pan: str) -> str:
    return pan
    # digits = digits_only(pan)
    # if len(digits) < 10:
    #     return digits
    # return f"{digits[:6]}******{digits[-4:]}"

def format_response(card_data: Dict, live_result: Dict, bin_result: Dict) -> Dict:
    """İstenen formatta response oluştur"""
    return {
        "pan": card_data.get("pan", ""),
        "exp": card_data.get("exp", ""),
        "cvv": card_data.get("cvv", ""),
        "status": "LIVE" if live_result.get("isLive") else "DEAD",
        "isLive": live_result.get("isLive", False),
        "provider": live_result.get("provider", "unknown"),
        "gateway": live_result.get("provider", "unknown"),
        "transactionId": live_result.get("transactionId", ""),
        "responseCode": live_result.get("responseCode", ""),
        "responseText": live_result.get("responseText", ""),
        "bin": bin_result.get("data", {}).get("bin", ""),
        "vendor": bin_result.get("data", {}).get("vendor", "UNKNOWN"),
        "type": bin_result.get("data", {}).get("type", "UNKNOWN"),
        "level": bin_result.get("data", {}).get("level", "STANDARD"),
        "bank": bin_result.get("data", {}).get("bank", "Unknown"),
        "country": bin_result.get("data", {}).get("country", "XX"),
        "bin_source": bin_result.get("source", "unknown"),
        "timestamp": datetime.now().isoformat()
    }

# ================== PROVIDER SERVICES ==================
class ProviderService:
    def __init__(self):
        self.nmi_config = {
            "username": os.getenv("NMI_API_USERNAME", "bygreenllc"),
            "password": os.getenv("NMI_API_PASSWORD", "Ak1f1987@..."),
            "api_key": os.getenv("NMI_API_SECURITY_KEY", "v4_secret_4A9387r9Kc44xHm3p2g2V28Qu9t3vb8X"),
            "url": "https://secure.nmi.com/api/transact.php"
        }
        
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
            return {
                "status": "approved" if is_live else "declined",
                "transactionId": f"mock_{hashlib.md5(card_data['pan'].encode()).hexdigest()[:16]}",
                "provider": provider,
                "isLive": is_live
            }
        
        if provider == "clover":
            return await self._nmi_verify(card_data)
        elif provider == "authorizenet":
            return await self._authorize_verify(card_data)
        elif provider == "paypal":
            return await self._paypal_verify(card_data)
        elif provider == "amazonpay":
            return {"status": "declined", "isLive": False, "provider": "amazonpay"}
        else:
            return {"status": "error", "isLive": False, "error": "Unknown provider"}
    
    async def _nmi_verify(self, card_data: Dict) -> Dict:
        try:
            data = {
                "username": self.nmi_config["username"],
                "password": self.nmi_config["password"],
                "ccnumber": card_data["pan"],
                "ccexp": f"{card_data['expMonth']}{card_data['expYear'][-2:]}",
                "cvv": card_data["cvv"],
                "type": "verify",
                "amount": "0.00"
            }
            
            response = requests.post(
                self.nmi_config["url"],
                data=data,
                timeout=15,
                headers={"Content-Type": "application/x-www-form-urlencoded"}
            )
            
            params = dict(x.split('=') for x in response.text.split('&'))
            response_code = params.get('response', '0')
            
            return {
                "status": "approved" if response_code == '1' else "declined",
                "transactionId": params.get('transactionid', ''),
                "authCode": params.get('authcode', ''),
                "responseCode": response_code,
                "responseText": params.get('responsetext', ''),
                "provider": "clover",
                "isLive": response_code == '1'
            }
        except Exception as e:
            return {"status": "error", "isLive": False, "error": str(e)}
    
    async def _authorize_verify(self, card_data: Dict) -> Dict:
        try:
            xml_request = f"""<?xml version="1.0" encoding="utf-8"?>
            <createTransactionRequest xmlns="AnetApi/xml/v1/schema/AnetApiSchema.xsd">
                <merchantAuthentication>
                    <name>{self.authorize_config['login_id']}</name>
                    <transactionKey>{self.authorize_config['transaction_key']}</transactionKey>
                </merchantAuthentication>
                <transactionRequest>
                    <transactionType>authOnly</transactionType>
                    <amount>0.00</amount>
                    <payment>
                        <creditCard>
                            <cardNumber>{card_data['pan']}</cardNumber>
                            <expirationDate>{card_data['expYear']}-{card_data['expMonth']}</expirationDate>
                            <cardCode>{card_data['cvv']}</cardCode>
                        </creditCard>
                    </payment>
                </transactionRequest>
            </createTransactionRequest>"""
            
            response = requests.post(
                self.authorize_config["url"],
                data=xml_request,
                headers={"Content-Type": "application/xml"},
                timeout=15
            )
            
            root = ET.fromstring(response.text)
            for elem in root.getiterator():
                if '}' in elem.tag:
                    elem.tag = elem.tag.split('}', 1)[1]
            
            trans_response = root.find('.//transactionResponse')
            if trans_response is not None:
                response_code = trans_response.findtext('responseCode', '0')
                return {
                    "status": "approved" if response_code == '1' else "declined",
                    "transactionId": trans_response.findtext('transId', ''),
                    "authCode": trans_response.findtext('authCode', ''),
                    "responseCode": response_code,
                    "provider": "authorizenet",
                    "isLive": response_code == '1'
                }
            
            return {"status": "declined", "isLive": False, "provider": "authorizenet"}
        except Exception as e:
            return {"status": "error", "isLive": False, "error": str(e)}
    
    async def _paypal_verify(self, card_data: Dict) -> Dict:
        try:
            data = {
                "METHOD": "DoDirectPayment",
                "VERSION": "124.0",
                "USER": self.paypal_config["username"],
                "PWD": self.paypal_config["password"],
                "SIGNATURE": self.paypal_config["signature"],
                "PAYMENTACTION": "Authorization",
                "AMT": "0.00",
                "CREDITCARDTYPE": "Visa",
                "ACCT": card_data["pan"],
                "EXPDATE": f"{card_data['expMonth']}{card_data['expYear'][-2:]}",
                "CVV2": card_data["cvv"],
                "FIRSTNAME": "Test",
                "LASTNAME": "User",
                "STREET": "123 Main St",
                "CITY": "New York",
                "STATE": "NY",
                "ZIP": card_data.get("zip", "10001"),
                "COUNTRYCODE": "US"
            }
            
            response = requests.post(
                self.paypal_config["nvp_url"],
                data=data,
                timeout=15
            )
            
            params = dict(x.split('=') for x in response.text.split('&'))
            ack = params.get('ACK', 'Failure')
            
            return {
                "status": "approved" if ack.upper() == "SUCCESS" else "declined",
                "transactionId": params.get('TRANSACTIONID', ''),
                "correlationId": params.get('CORRELATIONID', ''),
                "provider": "paypal",
                "isLive": ack.upper() == "SUCCESS"
            }
        except Exception as e:
            return {"status": "error", "isLive": False, "error": str(e)}

provider_service = ProviderService()

# ================== CARD CHECK FUNCTIONS ==================

async def check_card(card_input: Dict, provider: str = "auto") -> Dict:
    try:
        card_data = normalize_card_input(card_input)
    except ValueError as e:
        return {"status": "error", "error": str(e)}
    
    # Provider seçimi
    if provider == "auto":
        provider = get_next_healthy_provider()
        logger.info(f"[PROVIDER] Seçilen: {provider}")
    
    # BIN Check
    bin_result = await check_bin(card_data["pan"])
    
    # Live Check
    live_result = await provider_service.verify_card(card_data, provider)
    
    # Response format
    response = format_response(card_data, live_result, bin_result)
    
    # MongoDB'ye kaydet
    save_to_mongodb({
        "card": card_data["pan"],
        "provider": provider,
        "live": live_result.get("isLive", False),
        "response": response
    })
    
    return response

async def check_card_batch(cards: List[str], provider: str = "auto") -> List[Dict]:
    results = []
    for card_line in cards:
        try:
            card_data = parse_card_line(card_line)
            result = await check_card(card_data, provider)
            results.append(result)
        except Exception as e:
            results.append({
                "error": str(e),
                "raw": card_line,
                "status": "error"
            })
        time.sleep(0.3)  # Rate limiting
    return results

# ================== API ENDPOINTS ==================

@app.on_event("startup")
async def startup_event():
    """Uygulama başlarken provider'ları test et"""
    await test_all_providers()

@app.get("/")
async def home():
    return {
        "status": "API aktif",
        "mock_mode": MOCK_MODE,
        "providers": [
            {"name": p["name"], "status": p["status"]} 
            for p in PROVIDERS
        ],
        "endpoints": [
            "/check (POST)",
            "/check/batch (POST)",
            "/check/file (POST)",
            "/bin/lookup (POST)",
            "/provider/status (GET)",
            "/health (GET)"
        ]
    }

@app.get("/health")
async def health():
    return {
        "status": "healthy",
        "mock_mode": MOCK_MODE,
        "providers": [
            {"name": p["name"], "status": p["status"]} 
            for p in PROVIDERS
        ],
        "timestamp": datetime.now().isoformat()
    }

@app.get("/provider/status")
async def provider_status(auth: str = Depends(verify_auth)):
    return {"providers": PROVIDERS}

@app.post("/check")
async def check_single_card(
    request: CardCheckRequest,
    auth: str = Depends(verify_auth)
):
    try:
        payload = request.dict(exclude_none=True)
        provider = payload.pop("provider", "auto")
        result = await check_card(payload, provider)
        return result
    except Exception as e:
        logger.error(f"[API] Hata: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=str(e))

@app.post("/check/batch")
async def check_batch_cards(
    request: BatchCheckRequest,
    auth: str = Depends(verify_auth)
):
    try:
        provider = request.provider or "auto"
        results = await check_card_batch(request.cards, provider)
        
        total = len(results)
        live = sum(1 for r in results if r.get("isLive") is True)
        dead = sum(1 for r in results if r.get("isLive") is False)
        errors = sum(1 for r in results if r.get("status") == "error")
        
        return {
            "total": total,
            "live": live,
            "dead": dead,
            "errors": errors,
            "provider": provider,
            "results": results
        }
    except Exception as e:
        logger.error(f"[API] Hata: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=str(e))

@app.post("/check/file")
async def check_file(
    file: UploadFile,
    provider: str = "auto",
    auth: str = Depends(verify_auth)
):
    try:
        content = await file.read()
        text = content.decode('utf-8')
        lines = [line.strip() for line in text.split('\n') if line.strip() and not line.startswith('#')]
        
        results = []
        for line in lines:
            try:
                card_data = parse_card_line(line)
                result = await check_card(card_data, provider)
                results.append(result)
            except Exception as e:
                results.append({"error": str(e), "raw": line})
            time.sleep(0.3)
        
        total = len(results)
        live = sum(1 for r in results if r.get("isLive") is True)
        dead = sum(1 for r in results if r.get("isLive") is False)
        
        return {
            "total": total,
            "live": live,
            "dead": dead,
            "provider": provider,
            "results": results
        }
    except Exception as e:
        logger.error(f"[API] Hata: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=str(e))

@app.post("/bin/lookup")
async def lookup_bin(
    bin_number: str,
    auth: str = Depends(verify_auth)
):
    try:
        result = await check_bin(bin_number)
        return result
    except Exception as e:
        logger.error(f"[API] BIN lookup hatası: {e}")
        raise HTTPException(status_code=500, detail=str(e))

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=10000)