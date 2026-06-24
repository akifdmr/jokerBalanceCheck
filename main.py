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

app = FastAPI(title="Live Checker + Balance Sorter API")
security = HTTPBearer()
BASE_DIR = Path(__file__).resolve().parent
API_METHODS_FILE = BASE_DIR / "api_methods.json"

# ================== AUTH (SABIT) ==================
AUTH_TOKEN = os.getenv("AUTH_TOKEN", "change-me-local-token")
CARD_CHECKS_ENABLED = os.getenv("ENABLE_CARD_CHECKS", "false").lower() in {"1", "true", "yes", "on"}

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

# ================== NMI KONFİGÜRASYONU ==================
NMI_CONFIG = {
    "api_username": os.getenv("NMI_API_USERNAME", ""),
    "api_password": os.getenv("NMI_API_PASSWORD", ""),
    "security_key": os.getenv("NMI_SECURITY_KEY", ""),
    "api_url": os.getenv("NMI_API_URL", "https://api.nmi.com/api/v1/transaction")
}

# ================== CLOVER KONFİGÜRASYONU ==================
CLOVER_CONFIG = {
    "merchant_id": os.getenv("CLOVER_MERCHANT_ID", ""),
    "public_token": os.getenv("CLOVER_PUBLIC_TOKEN", ""),
    "private_token": os.getenv("CLOVER_PRIVATE_TOKEN", ""),
    "api_url": os.getenv("CLOVER_API_URL", "https://api.clover.com/v1/charges"),
    "token_url": os.getenv("CLOVER_TOKEN_URL", "https://token.clover.com/v1/tokens")
}

# ================== PROXY ROTASYONU (SABIT) ==================
proxies_list = [proxy.strip() for proxy in os.getenv("PROXIES", "").split(",") if proxy.strip()]
proxy_cycle = itertools.cycle(proxies_list)

def get_proxy_config() -> Tuple[Optional[str], Optional[Dict[str, str]]]:
    if not proxies_list:
        return None, None
    proxy = next(proxy_cycle)
    return proxy, {"https": proxy}

# ================== GATEWAY LISTESI (Legacy) ==================
try:
    GATEWAYS = json.loads(os.getenv("LEGACY_GATEWAYS_JSON", "[]"))
except json.JSONDecodeError:
    GATEWAYS = []

gateway_stats = {g["name"]: {"success": 0, "fail": 0, "total": 0} for g in GATEWAYS}

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

# ================== NMI CARD VERIFY ==================
def nmi_verify_card(card_data: Dict) -> Dict:
    """NMI Account Verification ile kart doğrulama"""
    if not all([NMI_CONFIG["api_username"], NMI_CONFIG["api_password"], NMI_CONFIG["security_key"]]):
        raise HTTPException(status_code=503, detail="NMI env konfigürasyonu eksik")
    proxy, proxy_config = get_proxy_config()
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
            proxies=proxy_config,
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
                "proxy": proxy.split("@")[-1].split(":")[0] if proxy and "@" in proxy else proxy,
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
    if not all([CLOVER_CONFIG["public_token"], CLOVER_CONFIG["private_token"]]):
        raise HTTPException(status_code=503, detail="Clover env konfigürasyonu eksik")
    proxy, proxy_config = get_proxy_config()
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
            proxies=proxy_config,
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
            "amount": 50,  # 0.50 USD in cents
            "currency": "usd",
            "source": token_id,
            "capture": False,  # Pre-auth only
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
            proxies=proxy_config,
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
                "proxy": proxy.split("@")[-1].split(":")[0] if proxy and "@" in proxy else proxy,
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

# ================== LIVE CHECK (NMI + Clover + Legacy) ==================
def live_check_single(card_data: Dict) -> Dict:
    """
    Önce NMI, sonra Clover, başarısız olursa legacy gateway'lere geç
    """
    
    # 1. NMI ile dene
    nmi_result = nmi_verify_card(card_data)
    if nmi_result.get("live", False):
        return nmi_result
    
    # 2. Clover ile dene
    clover_result = clover_verify_card(card_data)
    if clover_result.get("live", False):
        return clover_result
    
    # 3. NMI başarısız olduysa, legacy gateway'leri dene
    sorted_gateways = sorted(
        GATEWAYS,
        key=lambda g: gateway_stats.get(g["name"], {}).get("success", 0) / max(1, gateway_stats.get(g["name"], {}).get("total", 1)),
        reverse=True
    )
    
    for gateway in sorted_gateways:
        try:
            payload = {
                "card_number": card_data["pan"],
                "card_exp_month": card_data["month"],
                "card_exp_year": card_data["year"],
                "card_cvv": card_data["cvv"],
                "amount": "0.50"
            }
            
            headers = gateway["headers"].copy()
            headers.update({
                "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"
            })
            
            r = requests.post(
                gateway["url"],
                json=payload,
                headers=headers,
                timeout=10
            )
            
            gateway_stats[gateway["name"]]["total"] += 1
            
            if r.status_code in [200, 201, 202]:
                gateway_stats[gateway["name"]]["success"] += 1
                response_data = r.json() if r.text else {}
                
                balance = "0.00"
                for key in ["balance", "available_balance", "amount", "remaining"]:
                    if key in response_data:
                        balance = str(response_data[key])
                        break
                
                return {
                    "status": "live",
                    "live": True,
                    "balance": balance,
                    "gateway": gateway["name"],
                    "proxy": "N/A",
                    "bin": nmi_result.get("bin", bin_info),
                    "card": card_data
                }
            else:
                gateway_stats[gateway["name"]]["fail"] += 1
                
        except Exception as e:
            gateway_stats[gateway["name"]]["fail"] += 1
            continue
    
    # Hepsi başarısız olduysa NMI sonucunu döndür (dead)
    return nmi_result

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

# ================== BALANCE SORTER ==================
def balance_sorter(results: List[Dict]) -> Dict:
    live_results = [r for r in results if r.get("live", False)]
    dead_results = [r for r in results if not r.get("live", False)]
    
    return {
        "total_cards": len(results),
        "live_count": len(live_results),
        "dead_count": len(dead_results),
        "success_rate": f"{(len(live_results)/len(results)*100):.1f}%" if results else "0%",
        "live_cards": [
            {
                "pan": r["card"]["pan"][:6] + "****" + r["card"]["pan"][-4:],
                "gateway": r.get("gateway", "unknown"),
                "brand": r.get("bin", {}).get("brand", "UNKNOWN"),
                "country": r.get("bin", {}).get("country_name", "UNKNOWN")
            }
            for r in live_results
        ],
        "dead_cards": [
            {
                "pan": r["card"]["pan"][:6] + "****" + r["card"]["pan"][-4:],
                "brand": r.get("bin", {}).get("brand", "UNKNOWN")
            }
            for r in dead_results
        ]
    }

# ================== SAVE TO MONGODB ==================
def save_to_mongodb(data: Dict):
    if not collection:
        return
    try:
        collection.insert_one({**data, "timestamp": datetime.utcnow()})
    except:
        pass

# ================== POSTMAN API METHOD CATALOG ==================
def load_api_methods() -> Dict:
    try:
        return json.loads(API_METHODS_FILE.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {
            "source": API_METHODS_FILE.name,
            "collection": "unknown",
            "total": 0,
            "methods": [],
            "error": "api_methods.json bulunamadı"
        }
    except json.JSONDecodeError as e:
        return {
            "source": API_METHODS_FILE.name,
            "collection": "unknown",
            "total": 0,
            "methods": [],
            "error": f"api_methods.json okunamadı: {str(e)}"
        }

# ================== API ENDPOINTLER ==================

@app.get("/")
async def home():
    return {
        "status": "API aktif (NMI + Clover + Legacy)",
        "endpoints": [
            "/livecheck",
            "/balancesort",
            "/bulklive",
            "/balancebybin",
            "/gatewaystats",
            "/docs",
            "/nmi/verify",
            "/clover/verify",
            "/api-methods",
            "/api-methods/groups/{group_name:path}"
        ],
        "auth_required": "Bearer token ile",
        "gateways": len(GATEWAYS) + 2,
        "proxies": len(proxies_list),
        "nmi_enabled": True,
        "clover_enabled": True
    }

@app.get("/gatewaystats")
async def get_gateway_stats(auth: str = Depends(verify_auth)):
    stats = {}
    for name, data in gateway_stats.items():
        total = data["total"]
        success = data["success"]
        rate = (success / total * 100) if total > 0 else 0
        stats[name] = {
            "total": total,
            "success": success,
            "fail": data["fail"],
            "success_rate": f"{rate:.1f}%",
            "status": "🟢" if rate > 50 else "🟡" if rate > 20 else "🔴"
        }
    stats["NMI_AccountVerification"] = {"status": "🟢 Active"}
    stats["Clover_Charge"] = {"status": "🟢 Active"}
    return stats

@app.get("/api-methods")
async def api_methods(auth: str = Depends(verify_auth)):
    """Postman collection'dan çıkarılmış API method kataloğu"""
    return load_api_methods()

@app.get("/api-methods/groups/{group_name:path}")
async def api_methods_by_group(group_name: str, auth: str = Depends(verify_auth)):
    """Belirli bir Postman collection grubundaki API methodlarını döndürür"""
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
    save_to_mongodb({"type": "livecheck", "cards": len(cards), "results": results})
    return {"total": len(results), "results": results}

@app.post("/balancesort")
async def balancesort(cards: List[str], auth: str = Depends(verify_auth)):
    require_card_checks_enabled()
    results = bulk_live_check(cards)
    sorted_data = balance_sorter(results)
    save_to_mongodb({"type": "balancesort", "cards": len(cards), "data": sorted_data})
    return sorted_data

@app.post("/bulklive")
async def bulklive(file: UploadFile = File(...), auth: str = Depends(verify_auth)):
    require_card_checks_enabled()
    content = await file.read()
    cards = content.decode("utf-8").splitlines()
    cards = [c.strip() for c in cards if c.strip()]
    if not cards:
        return {"error": "Dosya boş"}
    results = bulk_live_check(cards)
    save_to_mongodb({"type": "bulklive", "cards": len(cards), "results": results})
    return {"total": len(results), "results": results}

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
