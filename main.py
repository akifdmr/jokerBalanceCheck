from fastapi import FastAPI, UploadFile, File, Depends, HTTPException, Security, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from fastapi.responses import JSONResponse, FileResponse
from pydantic import BaseModel, Field
from typing import Dict, List, Optional, Any
from datetime import datetime
import requests
import re
import time
import random
import hashlib
import csv
import json
import os
import logging
from pathlib import Path
from pymongo import MongoClient

# ================== LOGGING ==================
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

app = FastAPI(
    title="Clover Live Card Checker API",
    description="""
    ## Clover ile Kart Doğrulama ve BIN Sorgulama API'si
    
    Bu API ile:
    - Kartları normalize edip Clover üzerinden doğrulayabilirsiniz
    - BIN sorgulama yapabilirsiniz
    - Toplu kart işleme yapabilirsiniz
    - Live kartları listeleyebilirsiniz
    
    ### Kart Formatları
    - `PAN|MM/YYYY/CCVV` → `PAN|MM/YYYY|CCVV`
    - `PAN|MM|YYYY|CCVV|...` → `PAN|MM/YYYY|CCVV`
    - `PAN|MM/YYYY|CCVV` → aynen kalır
    """,
    version="2.0.0",
    contact={
        "name": "API Support",
        "email": "info@internationalliaison.com"
    }
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
    generated_cards_collection = db["generatedCards"]
    logger.info("[+] MongoDB bağlantısı başarılı")
except Exception as e:
    logger.error(f"[!] MongoDB hatası: {e}")
    client = None
    generated_cards_collection = None

# ================== MODELS ==================
class CardCheckRequest(BaseModel):
    """Kart doğrulama isteği"""
    pan: Optional[str] = Field(None, description="Kart numarası", example="4514011614153896")
    cardNumber: Optional[str] = Field(None, description="Kart numarası (alternatif)", example="4514011614153896")
    exp: Optional[str] = Field(None, description="Son kullanma tarihi (MM/YYYY)", example="07/2026")
    expMonth: Optional[str] = Field(None, description="Son kullanma ayı", example="07")
    expYear: Optional[str] = Field(None, description="Son kullanma yılı", example="2026")
    cvv: Optional[str] = Field(None, description="CVV kodu", example="234")
    zip: Optional[str] = Field(None, description="Posta kodu", example="00000")
    billingZip: Optional[str] = Field(None, description="Fatura posta kodu", example="00000")

class CardProcessRequest(BaseModel):
    """Tek kart işleme isteği"""
    card: str = Field(..., description="Kart string'i", example="4514011614153896|07/2026|234")

class BatchProcessRequest(BaseModel):
    """Toplu kart işleme isteği"""
    cards: List[str] = Field(..., description="Kart listesi", example=["4514011614153896|07/2026|234", "5348690004625057|11/2026|469"])
    delay: Optional[float] = Field(2.0, description="Kart arası bekleme süresi (saniye)", ge=0.5, le=10)

class ProcessResult(BaseModel):
    """İşlem sonucu"""
    original: str
    normalized: Optional[str] = None
    verified: bool = False
    isLive: bool = False
    error: Optional[str] = None
    binInfo: Optional[Dict] = None
    verification: Optional[Dict] = None

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

def normalize_card_line(card_str: str) -> Optional[str]:
    """
    Kart formatını normalize eder:
    - 4514011614153896|07/2026/20234 → 4514011614153896|07/2026|234
    - 4514011614153896|07/2026|234 → 4514011614153896|07/2026|234
    - 5183230102242436|09|2026|978|XX|UNKNOWN|UNKNOWN|NMI → 5183230102242436|09/2026|978
    """
    parts = card_str.strip().split('|')
    
    if len(parts) < 3:
        return None
    
    pan = parts[0].strip()
    if not pan or len(pan) < 13 or len(pan) > 19:
        return None
    
    numbers = []
    for part in parts[1:]:
        clean = re.sub(r'\D', '', part)
        if clean:
            numbers.append(clean)
    
    if len(numbers) < 2:
        return None
    
    month = None
    for num in numbers:
        if len(num) in [1, 2]:
            month = num.zfill(2)
            break
    
    if not month:
        return None
    
    year = None
    for num in numbers:
        if num != month and len(num) in [2, 4]:
            year = num
            break
    
    if not year:
        return None
    
    if len(year) == 2:
        year = f"20{year}"
    elif len(year) != 4:
        return None
    
    cvv = ""
    for num in numbers:
        if num not in [month, year] and len(num) in [3, 4]:
            cvv = num
            break
    
    if not cvv:
        return None
    
    return f"{pan}|{month}/{year}|{cvv}"

def parse_card_line(line: str) -> Optional[Dict]:
    normalized = normalize_card_line(line)
    if not normalized:
        return None
    
    parts = normalized.split('|')
    if len(parts) < 3:
        return None
    
    pan = parts[0]
    expiry = parts[1]
    cvv = parts[2]
    
    if '/' in expiry:
        exp_parts = expiry.split('/')
        month = exp_parts[0].zfill(2)
        year = exp_parts[1]
    else:
        return None
    
    return {
        "pan": pan,
        "month": month,
        "year": year,
        "cvv": cvv,
        "expiry": expiry
    }

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
            "isLive": is_live
        }
    
    try:
        zip_code = card_data.get("zip", "00000")
        if not zip_code or len(zip_code) < 5:
            zip_code = "00000"
        
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

# ================== FILE OPERATIONS ==================

def append_formatted_card(card_line: str):
    file_path = Path("formatted_cards.txt")
    with open(file_path, 'a', encoding='utf-8') as f:
        f.write(card_line + '\n')

def append_live_card(card_line: str):
    file_path = Path("live_cards.txt")
    with open(file_path, 'a', encoding='utf-8') as f:
        f.write(card_line + '\n')

# ================== CORE PROCESS ==================

def process_card(card_str: str) -> ProcessResult:
    result = ProcessResult(
        original=card_str,
        verified=False,
        isLive=False
    )
    
    normalized = normalize_card_line(card_str)
    if not normalized:
        result.error = "Normalization failed"
        return result
    
    result.normalized = normalized
    append_formatted_card(normalized)
    
    card_data = parse_card_line(normalized)
    if not card_data:
        result.error = "Parse failed"
        return result
    
    card_data["zip"] = "00000"
    
    verification = clover_verify_card(card_data)
    result.verified = True
    result.isLive = verification.get("isLive", False)
    result.verification = verification
    
    if not result.isLive:
        return result
    
    bin_info = bin_lookup.get_bin_info(card_data["pan"])
    result.binInfo = bin_info
    
    brand = bin_info.get("brand", "UNKNOWN")
    card_type = bin_info.get("type", "UNKNOWN")
    level = bin_info.get("level", "STANDARD")
    country = bin_info.get("country", "XX")
    bank = bin_info.get("bank", "Unknown")
    
    live_line = f"{card_data['pan']}|{card_data['expiry']}|{card_data['cvv']}|{country}|{bank}|{card_type}|{level}"
    append_live_card(live_line)
    
    if generated_cards_collection:
        try:
            doc = {
                "pan": card_data["pan"],
                "expiry": card_data["expiry"],
                "cvv": card_data["cvv"],
                "brand": brand,
                "type": card_type,
                "level": level,
                "country": country,
                "bank": bank,
                "transactionId": verification.get("transactionId", ""),
                "isLive": True,
                "createdAt": datetime.now().isoformat()
            }
            generated_cards_collection.insert_one(doc)
        except Exception as e:
            logger.error(f"[DB] Kayıt hatası: {e}")
    
    return result

# ================== API ENDPOINTS ==================

@app.get("/", tags=["System"])
async def root():
    """API ana sayfası"""
    return {
        "name": "Clover Live Card Checker API",
        "version": "2.0.0",
        "status": "active",
        "mock_mode": MOCK_MODE,
        "endpoints": [
            {"path": "/docs", "method": "GET", "description": "Swagger dokümantasyonu"},
            {"path": "/health", "method": "GET", "description": "Sağlık kontrolü"},
            {"path": "/process", "method": "POST", "description": "Tek kart işle"},
            {"path": "/process/batch", "method": "POST", "description": "Toplu kart işle"},
            {"path": "/process/file", "method": "POST", "description": "Dosyadan kart işle"},
            {"path": "/bin/lookup", "method": "POST", "description": "BIN sorgulama"},
            {"path": "/cards/live", "method": "GET", "description": "Live kartları listele"},
            {"path": "/cards/stats", "method": "GET", "description": "Live kart istatistikleri"},
            {"path": "/cards/export", "method": "GET", "description": "Live kartları dışa aktar"}
        ]
    }

@app.get("/health", tags=["System"])
async def health_check():
    """Sağlık kontrolü"""
    return {
        "status": "healthy",
        "mock_mode": MOCK_MODE,
        "mongodb": generated_cards_collection is not None,
        "timestamp": datetime.now().isoformat()
    }

@app.post(
    "/process",
    tags=["Card Processing"],
    response_model=ProcessResult,
    summary="Tek kart işle",
    description="""
    Tek bir kartı normalize eder, Clover ile doğrular ve live ise kaydeder.
    
    **Desteklenen formatlar:**
    - `PAN|MM/YYYY/CCVV` → `PAN|MM/YYYY|CCVV`
    - `PAN|MM|YYYY|CCVV|...` → `PAN|MM/YYYY|CCVV`
    - `PAN|MM/YYYY|CCVV` → aynen kalır
    """
)
async def process_single_card(
    request: CardProcessRequest,
    auth: str = Depends(verify_auth)
):
    try:
        result = process_card(request.card)
        return result
    except Exception as e:
        logger.error(f"[API] Hata: {e}")
        raise HTTPException(status_code=500, detail=str(e))

@app.post(
    "/process/batch",
    tags=["Card Processing"],
    summary="Toplu kart işle",
    description="Birden fazla kartı toplu olarak işler. Her kart arasında belirtilen süre kadar bekler."
)
async def process_batch(
    request: BatchProcessRequest,
    auth: str = Depends(verify_auth)
):
    try:
        results = []
        total = len(request.cards)
        live_count = 0
        
        for i, card in enumerate(request.cards, 1):
            logger.info(f"[BATCH] {i}/{total} kart işleniyor...")
            result = process_card(card)
            results.append(result)
            if result.isLive:
                live_count += 1
            if i < total:
                time.sleep(request.delay)
        
        return {
            "total": total,
            "live": live_count,
            "dead": total - live_count,
            "results": results
        }
    except Exception as e:
        logger.error(f"[API] Hata: {e}")
        raise HTTPException(status_code=500, detail=str(e))

@app.post(
    "/process/file",
    tags=["Card Processing"],
    summary="Dosyadan kart işle",
    description="Bir dosyadan kartları okuyarak toplu işlem yapar. Her kart arasında 2 saniye bekler."
)
async def process_file(
    file: UploadFile = File(..., description="Kart listesi içeren dosya (.txt)"),
    auth: str = Depends(verify_auth)
):
    try:
        content = await file.read()
        lines = [line.strip() for line in content.decode('utf-8').split('\n') if line.strip()]
        
        if not lines:
            raise HTTPException(status_code=400, detail="Dosya boş")
        
        results = []
        total = len(lines)
        live_count = 0
        
        for i, line in enumerate(lines, 1):
            logger.info(f"[FILE] {i}/{total} kart işleniyor...")
            result = process_card(line)
            results.append(result)
            if result.isLive:
                live_count += 1
            if i < total:
                time.sleep(2)
        
        return {
            "total": total,
            "live": live_count,
            "dead": total - live_count,
            "file": file.filename,
            "results": results
        }
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"[API] Hata: {e}")
        raise HTTPException(status_code=500, detail=str(e))

@app.post(
    "/bin/lookup",
    tags=["BIN Lookup"],
    summary="BIN sorgulama",
    description="Kart numarasının ilk 6 hanesine göre BIN bilgilerini getirir."
)
async def lookup_bin(
    request: CardCheckRequest,
    auth: str = Depends(verify_auth)
):
    try:
        pan = digits_only(request.bin or request.pan or request.cardNumber or "")
        if len(pan) < 6:
            raise HTTPException(status_code=400, detail="En az 6 hane gerekli")
        
        bin_info = bin_lookup.get_bin_info(pan[:6])
        return {
            "bin": bin_info["bin"],
            "brand": bin_info["brand"],
            "type": bin_info["type"],
            "level": bin_info["level"],
            "bank": bin_info["bank"],
            "country": bin_info["country"],
            "country_name": bin_info["country_name"],
            "currency": bin_info["currency"],
            "valid": bin_info["valid"],
            "timestamp": datetime.now().isoformat()
        }
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"[API] BIN lookup hatası: {e}")
        raise HTTPException(status_code=500, detail=str(e))

@app.get(
    "/cards/live",
    tags=["Cards"],
    summary="Live kartları listele",
    description="Veritabanına kaydedilmiş live kartları listeler."
)
async def list_live_cards(
    limit: int = 50,
    brand: Optional[str] = None,
    auth: str = Depends(verify_auth)
):
    if not generated_cards_collection:
        raise HTTPException(status_code=503, detail="MongoDB bağlantısı yok")
    
    query = {"isLive": True}
    if brand:
        query["brand"] = brand.upper()
    
    try:
        cursor = generated_cards_collection.find(query).sort("createdAt", -1).limit(limit)
        cards = []
        for doc in cursor:
            doc["_id"] = str(doc["_id"])
            cards.append(doc)
        return {"total": len(cards), "cards": cards}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@app.get(
    "/cards/stats",
    tags=["Cards"],
    summary="Live kart istatistikleri",
    description="Veritabanındaki live kartların istatistiklerini gösterir."
)
async def card_statistics(
    auth: str = Depends(verify_auth)
):
    if not generated_cards_collection:
        raise HTTPException(status_code=503, detail="MongoDB bağlantısı yok")
    
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

@app.get(
    "/cards/export",
    tags=["Cards"],
    summary="Live kartları dışa aktar",
    description="Tüm live kartları CSV veya JSON formatında dışa aktarır."
)
async def export_cards(
    format: str = "csv",
    auth: str = Depends(verify_auth)
):
    if not generated_cards_collection:
        raise HTTPException(status_code=503, detail="MongoDB bağlantısı yok")
    
    try:
        cards = list(generated_cards_collection.find({"isLive": True}))
        
        if format.lower() == "csv":
            header = "PAN,Expiry,CVV,Brand,Type,Level,Country,Bank,TransactionId,CreatedAt\n"
            lines = []
            for card in cards:
                lines.append(f"{card['pan']},{card['expiry']},{card['cvv']},{card['brand']},{card['type']},{card['level']},{card['country']},{card['bank']},{card.get('transactionId', '')},{card.get('createdAt', '')}")
            return JSONResponse(
                content={
                    "format": "csv",
                    "content": header + "\n".join(lines),
                    "count": len(cards)
                },
                headers={"Content-Disposition": f"attachment; filename=live_cards_{datetime.now().strftime('%Y%m%d_%H%M%S')}.csv"}
            )
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