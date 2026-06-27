from fastapi import FastAPI, HTTPException, Depends, Security, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from pydantic import BaseModel, Field
from typing import Optional, Dict, List
from datetime import datetime
import requests
import os
import logging
from pymongo import MongoClient
from urllib.parse import quote_plus
import hashlib
import json
import random
import re

# ================== LOGGING ==================
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)

# ================== APP ==================
app = FastAPI(
    title="Clover Live Card Checker API",
    description="Clover ile kart doğrulama ve MongoDB'ye kaydetme",
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

# ================== MONGODB CONNECTION ==================
MONGODB_URI = os.getenv("MONGODB_URI", "")
MONGODB_USER = os.getenv("MONGODB_USER", "")
MONGODB_PASS = os.getenv("MONGODB_PASS", "")
MONGODB_DB = os.getenv("MONGODB_DB", "paymentmanger")
MONGODB_COLLECTION = os.getenv("MONGODB_COLLECTION", "liveCards")

# MongoDB bağlantısını kur
try:
    if MONGODB_URI:
        if MONGODB_USER and MONGODB_PASS:
            if "mongodb+srv://" in MONGODB_URI:
                uri_parts = MONGODB_URI.split("://")
                if len(uri_parts) == 2:
                    host_part = uri_parts[1].split("/")
                    if len(host_part) >= 2:
                        host = host_part[0]
                        db_name = host_part[1].split("?")[0]
                        encoded_user = quote_plus(MONGODB_USER)
                        encoded_pass = quote_plus(MONGODB_PASS)
                        MONGODB_URI = f"mongodb+srv://{encoded_user}:{encoded_pass}@{host}/{db_name}"
        
        client = MongoClient(MONGODB_URI, tls=True, tlsAllowInvalidCertificates=True)
        db = client[MONGODB_DB]
        live_cards_collection = db[MONGODB_COLLECTION]
        
        # Index oluştur
        live_cards_collection.create_index("pan", unique=True)
        live_cards_collection.create_index("transactionId")
        live_cards_collection.create_index("verifiedAt")
        
        logger.info(f"[+] MongoDB bağlantısı başarılı - Database: {MONGODB_DB}, Collection: {MONGODB_COLLECTION}")
    else:
        logger.warning("[!] MONGODB_URI environment variable not set - MongoDB devre dışı")
        client = None
        db = None
        live_cards_collection = None
except Exception as e:
    logger.error(f"[!] MongoDB hatası: {e}")
    client = None
    db = None
    live_cards_collection = None

# ================== CONFIG ==================
CLOVER_CONFIG = {
    "merchant_id": os.getenv("CLOVER_MERCHANT_ID", "518993421163932"),
    "public_token": os.getenv("CLOVER_PUBLIC_TOKEN", "cc5f1f800dad9399d3e46aca8da49d8f"),
    "private_token": os.getenv("CLOVER_PRIVATE_TOKEN", "c7ee250b-e9ae-ab59-ba52-616ecc63ed29"),
    "token_url": "https://token.clover.com/v1/tokens",
    "charge_url": "https://api.clover.com/v1/charges"
}

MOCK_MODE = os.getenv("MOCK_MODE", "false").lower() in ["1", "true", "yes", "on"]

# ================== MODELS ==================
class CardInfo(BaseModel):
    """Kart bilgileri modeli"""
    pan: str = Field(..., description="Kart numarası", example="4514011614153896")
    exp: str = Field(..., description="Son kullanma tarihi (MM/YYYY veya MM/YY)", example="07/2026")
    cvv: str = Field(..., description="CVV kodu", example="234")

class CardCheckResponse(BaseModel):
    """Doğrulama sonucu modeli"""
    status: str = Field(..., description="HTTP durumu: success / error")
    verified: bool = Field(False, description="Kart doğrulandı mı?")
    isLive: bool = Field(False, description="Kart live mı?")
    message: str = Field(..., description="Sonuç mesajı")
    card: Dict = Field(..., description="Kart bilgileri (maskesiz)")
    binInfo: Optional[Dict] = Field(None, description="BIN bilgileri")
    verification: Optional[Dict] = Field(None, description="Doğrulama detayları")
    dbSaved: bool = Field(False, description="Veritabanına kaydedildi mi?")
    timestamp: str = Field(..., description="İşlem zamanı")

# ================== HELPER FUNCTIONS ==================

def mask_pan(pan: str) -> str:
    """PAN'i maskeler"""
    if not pan or len(pan) < 10:
        return pan
    return f"{pan[:6]}****{pan[-4:]}"

def normalize_expiry(exp: str) -> Dict:
    """Son kullanma tarihini normalize eder"""
    exp = exp.strip()
    if '/' not in exp:
        raise ValueError("Geçersiz tarih formatı (MM/YYYY veya MM/YY)")
    
    parts = exp.split('/')
    if len(parts) != 2:
        raise ValueError("Geçersiz tarih formatı (MM/YYYY veya MM/YY)")
    
    month = parts[0].strip().zfill(2)
    year = parts[1].strip()
    
    if len(year) == 2:
        year = f"20{year}"
    elif len(year) != 4:
        raise ValueError("Yıl 2 veya 4 haneli olmalı")
    
    if not re.match(r'^(0[1-9]|1[0-2])$', month):
        raise ValueError("Ay 01-12 arasında olmalı")
    
    return {"month": month, "year": year, "expiry": f"{month}/{year}"}

def get_bin_info(pan: str) -> Dict:
    """BIN bilgilerini alır"""
    try:
        # BIN lookup yapmak için basit bir cache
        import requests as req
        bin_6 = pan[:6]
        
        response = req.get(
            f"https://lookup.binlist.net/{bin_6}",
            timeout=5,
            headers={"Accept-Version": "3"}
        )
        
        if response.status_code == 200:
            data = response.json()
            bank = data.get("bank", {})
            country = data.get("country", {})
            brand = data.get("scheme", "UNKNOWN").upper()
            brand_name = data.get("brand", "").upper()
            card_type = data.get("type", "UNKNOWN").upper()
            
            # Level belirleme
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
            
            return {
                "bin": bin_6,
                "brand": brand,
                "type": card_type,
                "level": level,
                "bank": bank.get("name", "Unknown"),
                "country": country.get("alpha2", "XX"),
                "country_name": country.get("name", "Unknown"),
                "currency": country.get("currency", "USD"),
                "valid": True
            }
    except Exception as e:
        logger.warning(f"[BIN] Hata: {e}")
    
    return {
        "bin": pan[:6],
        "brand": "UNKNOWN",
        "type": "UNKNOWN",
        "level": "STANDARD",
        "bank": "Unknown",
        "country": "XX",
        "country_name": "Unknown",
        "currency": "USD",
        "valid": False
    }

# ================== SAVE TO MONGODB ==================

def save_to_mongodb(card_data: Dict, bin_info: Dict, verification: Dict) -> Dict:
    """
    Live kartı MongoDB'ye kaydeder
    """
    if not live_cards_collection:
        return {"saved": False, "error": "MongoDB bağlantısı yok"}
    
    try:
        pan = card_data.get("pan", "")
        expiry = card_data.get("expiry", "")
        cvv = card_data.get("cvv", "")
        
        doc = {
            "pan": pan,
            "masked": mask_pan(pan),
            "expiry": expiry,
            "cvv": cvv,
            "holderName": "",  # Boş
            "zip": "00000",  # Sabit 00000
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
            "status": verification.get("status", "approved"),
            "isLive": True,
            "verifiedAt": datetime.now().isoformat(),
            "raw_data": {
                "card": card_data,
                "verification": verification
            }
        }
        
        # Upsert
        result = live_cards_collection.update_one(
            {"pan": pan},
            {"$set": doc},
            upsert=True
        )
        
        if result.upserted_id:
            logger.info(f"[DB] Yeni kart kaydedildi: {mask_pan(pan)}")
            return {"saved": True, "id": str(result.upserted_id), "action": "inserted"}
        elif result.modified_count > 0:
            logger.info(f"[DB] Kart güncellendi: {mask_pan(pan)}")
            return {"saved": True, "action": "updated"}
        else:
            logger.info(f"[DB] Kart zaten mevcut: {mask_pan(pan)}")
            return {"saved": True, "action": "exists"}
            
    except Exception as e:
        logger.error(f"[DB] Kayıt hatası: {e}")
        return {"saved": False, "error": str(e)}

# ================== CLOVER VERIFY ==================

def clover_verify_card(card_data: Dict) -> Dict:
    """
    Clover API ile kart doğrulama
    """
    if MOCK_MODE:
        is_live = random.random() < 0.15
        return {
            "status": "approved" if is_live else "declined",
            "transactionId": f"mock_{hashlib.md5(card_data.get('pan', '').encode()).hexdigest()[:16]}",
            "provider": "clover",
            "isLive": is_live,
            "mock": True
        }
    
    try:
        pan = card_data.get("pan", "")
        month = card_data.get("month", "")
        year = card_data.get("year", "")
        cvv = card_data.get("cvv", "")
        
        # Tokenization
        token_payload = {
            "card": {
                "number": pan,
                "exp_month": int(month),
                "exp_year": int(year),
                "cvv": cvv
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
        
        # Charge (Authorization) - amount 0 ile test
        charge_payload = {
            "amount": 0,  # 0.00 USD - sadece yetkilendirme
            "currency": "usd",
            "source": token_id,
            "capture": True  # capture true olsun
        }
        
        charge_headers = {
            "Content-Type": "application/json",
            "Authorization": f"Bearer {CLOVER_CONFIG['private_token']}"
        }
        
        charge_response = requests.post(
            CLOVER_CONFIG["charge_url"],
            json=charge_payload,
            headers=charge_headers,
            timeout=20
        )
        
        # 200 veya 201 başarılı demek
        if charge_response.status_code in [200, 201, 202]:
            charge_data = charge_response.json()
            charge_status = charge_data.get("status")
            
            # "succeeded", "approved", "authorized" live
            is_live = charge_status in ["succeeded", "approved", "authorized"]
            
            return {
                "status": "approved" if is_live else "declined",
                "transactionId": charge_data.get("id", ""),
                "provider": "clover",
                "isLive": is_live,
                "token": token_id,
                "charge_data": charge_data
            }
        else:
            # 402, 404 vb. hatalar dead
            return {
                "status": "declined",
                "isLive": False,
                "error": f"Charge failed: HTTP {charge_response.status_code}",
                "charge_response": charge_response.text[:200]
            }
            
    except Exception as e:
        logger.error(f"[CLOVER] Hata: {e}")
        return {
            "status": "error",
            "isLive": False,
            "error": str(e)
        }

# ================== CARD CHECK FUNCTION ==================

def check_card(request: CardInfo) -> CardCheckResponse:
    """
    Kart doğrulama ana fonksiyonu
    """
    try:
        # Kart bilgilerini normalize et
        pan = request.pan.strip()
        
        # Expiry normalize et
        expiry_info = normalize_expiry(request.exp)
        month = expiry_info["month"]
        year = expiry_info["year"]
        expiry = expiry_info["expiry"]
        
        cvv = request.cvv.strip()
        
        # CVV kontrolü
        if len(cvv) < 3 or len(cvv) > 4:
            return CardCheckResponse(
                status="error",
                verified=False,
                isLive=False,
                message="CVV 3 veya 4 haneli olmalı",
                card={"pan": pan, "expiry": expiry, "cvv": cvv},
                binInfo=None,
                verification=None,
                dbSaved=False,
                timestamp=datetime.now().isoformat()
            )
        
        # Kart verisini hazırla
        card_data = {
            "pan": pan,
            "month": month,
            "year": year,
            "cvv": cvv,
            "expiry": expiry
        }
        
        # Card response için (maskesiz)
        card_response = {
            "pan": pan,
            "expiry": expiry,
            "cvv": cvv
        }
        
        # Clover ile doğrula
        verification = clover_verify_card(card_data)
        is_live = verification.get("isLive", False)
        
        if is_live:
            # BIN bilgilerini al
            bin_info = get_bin_info(pan)
            
            # DB'ye kaydet
            save_result = save_to_mongodb(card_data, bin_info, verification)
            
            return CardCheckResponse(
                status="success",
                verified=True,
                isLive=True,
                message="Kart doğrulandı ve live olarak kaydedildi",
                card=card_response,  # Maskesiz
                binInfo=bin_info,
                verification=verification,
                dbSaved=save_result.get("saved", False),
                timestamp=datetime.now().isoformat()
            )
        else:
            error_msg = verification.get('error', 'Doğrulama başarısız')
            return CardCheckResponse(
                status="success",
                verified=False,
                isLive=False,
                message=f"Kart geçersiz: {error_msg}",
                card=card_response,
                binInfo=None,
                verification=verification,
                dbSaved=False,
                timestamp=datetime.now().isoformat()
            )
            
    except ValueError as e:
        return CardCheckResponse(
            status="error",
            verified=False,
            isLive=False,
            message=str(e),
            card={"pan": request.pan, "exp": request.exp, "cvv": request.cvv},
            binInfo=None,
            verification=None,
            dbSaved=False,
            timestamp=datetime.now().isoformat()
        )
    except Exception as e:
        logger.error(f"[CHECK] Hata: {e}")
        return CardCheckResponse(
            status="error",
            verified=False,
            isLive=False,
            message=f"İşlem hatası: {str(e)}",
            card={"pan": request.pan, "exp": request.exp, "cvv": request.cvv},
            binInfo=None,
            verification=None,
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
        "mongodb_connected": live_cards_collection is not None,
        "endpoints": [
            {"path": "/", "method": "GET", "description": "API bilgileri"},
            {"path": "/docs", "method": "GET", "description": "Swagger dokümantasyonu"},
            {"path": "/health", "method": "GET", "description": "Sağlık kontrolü"},
            {"path": "/verify", "method": "POST", "description": "Kart doğrulama"},
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

@app.post("/verify", response_model=CardCheckResponse)
async def verify_card(
    request: CardInfo,
    auth: str = Depends(verify_auth)
):
    """
    Kart doğrulama endpoint'i
    
    Sadece pan, exp (MM/YYYY veya MM/YY), cvv gönderilir.
    holderName ve address boş, zip 00000 olarak otomatik doldurulur.
    Live ise isLive: true ve maskesiz kart bilgileri döner.
    """
    try:
        result = check_card(request)
        return result
    except Exception as e:
        logger.error(f"[API] Hata: {e}")
        raise HTTPException(status_code=500, detail=str(e))

@app.get("/cards/live")
async def list_live_cards(
    limit: int = 50,
    auth: str = Depends(verify_auth)
):
    """
    Live kartları listele (maskeli)
    """
    if not live_cards_collection:
        raise HTTPException(status_code=503, detail="MongoDB bağlantısı yok")
    
    try:
        cursor = live_cards_collection.find(
            {"isLive": True}
        ).sort("verifiedAt", -1).limit(limit)
        
        cards = []
        for doc in cursor:
            doc["_id"] = str(doc["_id"])
            if "cvv" in doc:
                doc["cvv"] = "***"
            if "pan" in doc:
                doc["pan"] = mask_pan(doc["pan"])
            cards.append(doc)
        
        return {"total": len(cards), "cards": cards}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@app.get("/cards/stats")
async def card_statistics(
    auth: str = Depends(verify_auth)
):
    """
    Live kart istatistikleri
    """
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
    port = int(os.getenv("PORT", 10000))
    uvicorn.run(app, host="0.0.0.0", port=port)