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

app = FastAPI(title="Clover Live Checker + BIN API")
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
    zip: Optional[str] = None
    billingZip: Optional[str] = None

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
    - 4514011614153896|07|2026|234 → 4514011614153896|07/2026|234
    - 5183230102242436|09|2026|978|XX|UNKNOWN|UNKNOWN|NMI → 5183230102242436|09/2026|978
    """
    parts = card_str.strip().split('|')
    
    if len(parts) < 3:
        return None
    
    pan = parts[0].strip()
    if not pan or len(pan) < 13 or len(pan) > 19:
        return None
    
    # Tüm parçalardan sayıları topla
    numbers = []
    for part in parts[1:]:
        clean = re.sub(r'\D', '', part)
        if clean:
            numbers.append(clean)
    
    if len(numbers) < 2:
        return None
    
    # AY: ilk 1-2 haneli sayı
    month = None
    for num in numbers:
        if len(num) in [1, 2]:
            month = num.zfill(2)
            break
    
    if not month:
        return None
    
    # YIL: sonraki 2-4 haneli sayı (month değilse)
    year = None
    for num in numbers:
        if num != month and len(num) in [2, 4]:
            year = num
            break
    
    if not year:
        return None
    
    # Yıl'ı 4 haneli yap
    if len(year) == 2:
        year = f"20{year}"
    elif len(year) != 4:
        return None
    
    # CVV: 3-4 haneli sayı (month veya year olmayan)
    cvv = ""
    for num in numbers:
        if num not in [month, year] and len(num) in [3, 4]:
            cvv = num
            break
    
    if not cvv:
        return None
    
    return f"{pan}|{month}/{year}|{cvv}"

def parse_card_line(line: str) -> Optional[Dict]:
    """Normalize edilmiş kartı parse et"""
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
            "level_detail": "Standard Card",
            "bank": "Unknown",
            "country": "XX",
            "country_name": "Unknown",
            "currency": "USD",
            "prepaid": False,
            "commercial": False,
            "source": "none",
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
                level_detail = "Standard Card"
                if "PLATINUM" in brand_name:
                    level = "PLATINUM"
                    level_detail = "Platinum Card"
                elif "GOLD" in brand_name:
                    level = "GOLD"
                    level_detail = "Gold Card"
                elif "SIGNATURE" in brand_name:
                    level = "SIGNATURE"
                    level_detail = "Signature Card"
                elif "INFINITE" in brand_name:
                    level = "INFINITE"
                    level_detail = "Infinite Card"
                elif "WORLD" in brand_name:
                    level = "WORLD"
                    level_detail = "World Card"
                elif "BUSINESS" in brand_name:
                    level = "BUSINESS"
                    level_detail = "Business Card"
                
                result.update({
                    "brand": brand,
                    "type": card_type,
                    "level": level,
                    "level_detail": level_detail,
                    "bank": bank.get("name", "Unknown"),
                    "country": country.get("alpha2", "XX"),
                    "country_name": country.get("name", "Unknown"),
                    "currency": country.get("currency", "USD"),
                    "prepaid": data.get("prepaid", False),
                    "commercial": data.get("commercial", False),
                    "source": "binlist.net",
                    "valid": True
                })
        except Exception as e:
            logger.warning(f"[BIN] Hata: {e}")
        
        self.cache[bin_6] = result
        return result

bin_lookup = BinLookup()

# ================== CLOVER VERIFY ==================

def clover_verify_card(card_data: Dict) -> Dict:
    """Clover ile kart doğrulama"""
    if MOCK_MODE:
        is_live = random.random() < 0.15
        return {
            "status": "approved" if is_live else "declined",
            "transactionId": f"mock_{hashlib.md5(card_data['pan'].encode()).hexdigest()[:16]}",
            "provider": "clover",
            "isLive": is_live
        }
    
    try:
        # ZIP kontrolü
        zip_code = card_data.get("zip", "00000")
        if not zip_code or len(zip_code) < 5:
            zip_code = "00000"
        
        # 1. Tokenize
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
                "error": f"Tokenization failed: HTTP {token_response.status_code}",
                "provider": "clover"
            }
        
        token_data = token_response.json()
        token_id = token_data.get("id")
        
        if not token_id:
            return {
                "status": "error",
                "isLive": False,
                "error": "No token received",
                "provider": "clover"
            }
        
        # 2. Charge (0.50 test)
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
            "error": f"Charge failed: HTTP {charge_response.status_code}",
            "provider": "clover"
        }
        
    except Exception as e:
        logger.error(f"[CLOVER] Hata: {e}")
        return {
            "status": "error",
            "isLive": False,
            "error": str(e),
            "provider": "clover"
        }

# ================== FORMATTED CARDS FILE ==================

def read_formatted_cards_file() -> List[str]:
    """formatted_cards.txt dosyasını okur, yoksa oluşturur"""
    file_path = Path("formatted_cards.txt")
    if not file_path.exists():
        file_path.touch()
        logger.info("[+] formatted_cards.txt oluşturuldu")
        return []
    
    with open(file_path, 'r', encoding='utf-8') as f:
        cards = [line.strip() for line in f if line.strip()]
    return cards

def append_formatted_card(card_line: str):
    """formatted_cards.txt dosyasına kart ekler"""
    file_path = Path("formatted_cards.txt")
    with open(file_path, 'a', encoding='utf-8') as f:
        f.write(card_line + '\n')

def append_live_card(card_line: str):
    """live_cards.txt dosyasına kart ekler"""
    file_path = Path("live_cards.txt")
    with open(file_path, 'a', encoding='utf-8') as f:
        f.write(card_line + '\n')

# ================== PROCESS CARDS ==================

def process_card(card_str: str) -> Dict:
    """Tek bir kartı işle: normalize → format → verify → binCheck → save"""
    result = {
        "original": card_str,
        "normalized": None,
        "verified": False,
        "isLive": False,
        "binInfo": None,
        "error": None
    }
    
    # 1. Normalize et
    normalized = normalize_card_line(card_str)
    if not normalized:
        result["error"] = "Normalization failed"
        return result
    
    result["normalized"] = normalized
    
    # 2. Formatlı dosyaya ekle
    append_formatted_card(normalized)
    
    # 3. Kart verilerini parse et
    card_data = parse_card_line(normalized)
    if not card_data:
        result["error"] = "Parse failed"
        return result
    
    # ZIP kontrolü (formatted_cards.txt'den gelen kartta zip yoksa 00000)
    card_data["zip"] = "00000"
    
    # 4. Clover ile doğrula
    verification = clover_verify_card(card_data)
    result["verified"] = True
    result["isLive"] = verification.get("isLive", False)
    result["verification"] = verification
    
    if not result["isLive"]:
        return result
    
    # 5. BIN Check
    bin_info = bin_lookup.get_bin_info(card_data["pan"])
    result["binInfo"] = bin_info
    
    # 6. Live kartı kaydet - Format: PAN|EXP|CVV|COUNTRY|BANK|TYPE|LEVEL
    brand = bin_info.get("brand", "UNKNOWN")
    card_type = bin_info.get("type", "UNKNOWN")
    level = bin_info.get("level", "STANDARD")
    country = bin_info.get("country", "XX")
    bank = bin_info.get("bank", "Unknown")
    
    live_line = f"{card_data['pan']}|{card_data['expiry']}|{card_data['cvv']}|{country}|{bank}|{card_type}|{level}"
    append_live_card(live_line)
    
    # MongoDB'ye de kaydet
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

@app.get("/")
async def home():
    return {
        "status": "Clover Live Checker API aktif",
        "mock_mode": MOCK_MODE,
        "endpoints": [
            "/process (POST)",
            "/process/file (POST)",
            "/bin/lookup (POST)",
            "/cards/list (GET)",
            "/cards/stats (GET)",
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

@app.post("/process")
async def process_card_endpoint(
    card: str,
    auth: str = Depends(verify_auth)
):
    """Tek kartı işle"""
    try:
        result = process_card(card)
        return result
    except Exception as e:
        logger.error(f"[API] Hata: {e}")
        raise HTTPException(status_code=500, detail=str(e))

@app.post("/process/file")
async def process_file_endpoint(
    file: UploadFile,
    auth: str = Depends(verify_auth)
):
    """Dosyadaki kartları işle"""
    try:
        content = await file.read()
        lines = [line.strip() for line in content.decode('utf-8').split('\n') if line.strip()]
        
        results = []
        total = len(lines)
        live_count = 0
        
        for i, line in enumerate(lines, 1):
            logger.info(f"[PROCESS] {i}/{total} kart işleniyor...")
            result = process_card(line)
            results.append(result)
            if result.get("isLive"):
                live_count += 1
                print(f"   ✅ [{i}] LIVE: {result['normalized']}")
            else:
                print(f"   ❌ [{i}] DEAD: {result.get('normalized', line)}")
            time.sleep(2)  # 2 saniye bekle
        
        return {
            "total": total,
            "live": live_count,
            "dead": total - live_count,
            "results": results
        }
    except Exception as e:
        logger.error(f"[API] Hata: {e}")
        raise HTTPException(status_code=500, detail=str(e))

@app.post("/bin/lookup")
async def bin_lookup_endpoint(request: CardCheckRequest, auth: str = Depends(verify_auth)):
    try:
        pan = digits_only(request.bin or request.pan or request.cardNumber or "")
        if len(pan) < 6:
            raise HTTPException(status_code=400, detail="Card number must be at least 6 digits")
        bin_info = bin_lookup.get_bin_info(pan[:6])
        return {
            "bin": bin_info["bin"],
            "brand": bin_info["brand"],
            "type": bin_info["type"],
            "level": bin_info["level"],
            "level_detail": bin_info["level_detail"],
            "bank": bin_info["bank"],
            "country": bin_info["country"],
            "country_name": bin_info["country_name"],
            "currency": bin_info["currency"],
            "prepaid": bin_info["prepaid"],
            "commercial": bin_info["commercial"],
            "source": bin_info["source"],
            "valid": bin_info["valid"],
            "timestamp": datetime.now().isoformat()
        }
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"[API] BIN lookup hatası: {e}")
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

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=10000)