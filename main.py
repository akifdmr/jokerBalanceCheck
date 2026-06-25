from fastapi import FastAPI, UploadFile, File, Depends, HTTPException, Security
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
import requests
import re
import time
import random
import itertools
from typing import List, Dict, Optional
from datetime import datetime
from pymongo import MongoClient
import json
import os
import xml.etree.ElementTree as ET
from pathlib import Path
import logging

# ================== LOGGING ==================
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

app = FastAPI(title="Live Checker + Balance Sorter API")
security = HTTPBearer()

# ================== AUTH ==================
AUTH_TOKEN = os.getenv("AUTH_TOKEN", "b9f3k7m2v8t3w5z1q6p9c4b7n2v8m2025")
MOCK_MODE = os.getenv("MOCK_MODE", "true").lower() in {"1", "true", "yes", "on"}

def verify_auth(credentials: HTTPAuthorizationCredentials = Security(security)):
    if credentials.credentials != AUTH_TOKEN:
        raise HTTPException(status_code=401, detail="Geçersiz token")
    return credentials.credentials

# ================== BIN LOOKUP ==================
def get_bin_info(bin_number: str) -> Dict:
    try:
        logger.info(f"[BIN] Sorgulanıyor: {bin_number[:6]}")
        r = requests.get(f"https://lookup.binlist.net/{bin_number[:6]}", timeout=8)
        if r.status_code == 200:
            data = r.json()
            result = {
                "bin": bin_number[:6],
                "brand": data.get("scheme", "").upper(),
                "type": data.get("type", "").upper(),
                "level": data.get("brand", "").upper(),
                "bank": data.get("bank", {}).get("name", "Unknown"),
                "country": data.get("country", {}).get("alpha2", "XX"),
                "country_name": data.get("country", {}).get("name", "Unknown")
            }
            logger.info(f"[BIN] Başarılı: {result}")
            return result
    except Exception as e:
        logger.error(f"[BIN] Hata: {e}")
    
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
    try:
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
    except Exception as e:
        logger.error(f"[PARSE] Hata: {e}")
        return None

# ================== LIVE CHECK (MOCK + HATA YAKALAMA) ==================
def live_check_single(card_data: Dict) -> Dict:
    """
    MOCK MOD - Gerçek gateway'ler çalışmazsa kullanılır
    """
    try:
        logger.info(f"[LIVE] Kontrol ediliyor: {card_data['pan'][:6]}****{card_data['pan'][-4:]}")
        
        # MOCK MOD
        if MOCK_MODE:
            is_live = random.random() < 0.15  # %15 live
            balance = f"{random.randint(100, 5000)}.00" if is_live else "0.00"
            gateways = ["NMI_AccountVerification", "Clover_Charge", "Stripe_Live"]
            bin_info = get_bin_info(card_data["pan"])
            
            result = {
                "status": "live" if is_live else "dead",
                "live": is_live,
                "balance": balance,
                "gateway": random.choice(gateways) if is_live else "none",
                "proxy": "mock",
                "bin": bin_info,
                "card": card_data,
                "mock": True
            }
            logger.info(f"[LIVE] Sonuç: {result['status']} - {result['gateway']}")
            return result
        
        # Gerçek gateway'ler (eğer MOCK_MODE false ise)
        logger.warning("[LIVE] Gerçek gateway'ler devre dışı (MOCK_MODE=false)")
        return {
            "status": "dead",
            "live": False,
            "balance": "0.00",
            "gateway": "none",
            "error": "Gerçek gateway'ler devre dışı",
            "bin": get_bin_info(card_data["pan"]),
            "card": card_data
        }
        
    except Exception as e:
        logger.error(f"[LIVE] Kritik hata: {e}", exc_info=True)
        return {
            "status": "dead",
            "live": False,
            "balance": "0.00",
            "gateway": "error",
            "error": str(e),
            "bin": get_bin_info(card_data["pan"]),
            "card": card_data
        }

# ================== TOPLU LIVE CHECK ==================
def bulk_live_check(cards: List[str]) -> List[Dict]:
    results = []
    parsed_cards = []
    
    try:
        logger.info(f"[BULK] {len(cards)} kart işlenecek")
        
        for card_str in cards:
            parsed = parse_card(card_str)
            if parsed:
                parsed_cards.append(parsed)
            else:
                logger.warning(f"[BULK] Geçersiz kart: {card_str[:20]}...")
                results.append({"error": "Geçersiz kart formatı", "card": card_str})
        
        if not parsed_cards:
            return [{"error": "Geçerli kart bulunamadı"}]
        
        for i, card in enumerate(parsed_cards):
            result = live_check_single(card)
            results.append(result)
            if i < len(parsed_cards) - 1:
                delay = random.uniform(0.5, 1.0)
                time.sleep(delay)
        
        logger.info(f"[BULK] {len(results)} sonuç döndü")
        return results
        
    except Exception as e:
        logger.error(f"[BULK] Kritik hata: {e}", exc_info=True)
        return [{"error": f"Toplu işlem hatası: {str(e)}"}]

# ================== API ENDPOINTLER ==================

@app.get("/")
async def home():
    return {
        "status": "API aktif",
        "mock_mode": MOCK_MODE,
        "endpoints": ["/livecheck", "/health", "/docs"],
        "auth_required": "Bearer token ile"
    }

@app.get("/health")
async def health():
    """Sağlık kontrolü"""
    return {"status": "healthy", "mock_mode": MOCK_MODE}

@app.post("/livecheck")
async def livecheck(cards: List[str], auth: str = Depends(verify_auth)):
    try:
        logger.info(f"[API] /livecheck çağrıldı, {len(cards)} kart")
        results = bulk_live_check(cards)
        return {"total": len(results), "results": results}
    except Exception as e:
        logger.error(f"[API] Hata: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=str(e))

@app.post("/nmi/verify")
async def nmi_verify(card: str, auth: str = Depends(verify_auth)):
    try:
        logger.info(f"[API] /nmi/verify çağrıldı")
        card_data = parse_card(card)
        if not card_data:
            raise HTTPException(status_code=400, detail="Geçersiz kart formatı")
        result = live_check_single(card_data)
        return result
    except Exception as e:
        logger.error(f"[API] Hata: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=str(e))

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=10000)