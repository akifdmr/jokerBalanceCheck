import hashlib
import re
import time
from typing import Dict, List, Optional, Any, Tuple
from datetime import datetime, timedelta
import json

# ================== SERVICE IMPORTS ==================
# Bu servisler gerçek implementasyonlarıyla değiştirilecek
class CloverService:
    def tokenize_card(self, card_data: Dict) -> Dict:
        # Mock implementasyon
        return {"source": f"clv_{hashlib.md5(card_data['pan'].encode()).hexdigest()[:20]}"}
    
    def create_pre_authorization(self, data: Dict) -> Dict:
        return {"transactionId": "clv_auth_123", "status": "approved"}
    
    def verify_card(self, data: Dict) -> Dict:
        return {"transactionId": "clv_verify_123", "status": "verified"}

class AmazonPayService:
    def verify_card(self, data: Dict) -> Dict:
        return {"status": "approved", "chargePermissionId": "amzn_perm_123"}

class AuthorizeNetService:
    def sale_card(self, data: Dict) -> Dict:
        return {"transactionId": "auth_sale_123", "status": "approved"}
    
    def authorize_card(self, data: Dict) -> Dict:
        return {"transactionId": "auth_auth_123", "status": "approved"}
    
    def verify_card(self, data: Dict) -> Dict:
        return {"transactionId": "auth_verify_123", "status": "verified"}

class PayPalService:
    def get_vaulted_payment_method_metadata(self, payload: Dict) -> Dict:
        return {"card": {"brand": "VISA", "last4": "1234", "expiry": "12/2028"}}
    
    def bin_check_card(self, data: Dict) -> Dict:
        return {"status": "passed", "summary": {"brand": "VISA", "scheme": "VISA"}}

class LiveCheckerService:
    def is_live_response(self, result: Dict) -> bool:
        status = str(result.get("status", "")).lower()
        return status in ["approved", "verified", "passed", "succeeded", "authorized"]

# ================== DB ==================
class Database:
    def __init__(self):
        self.collections = {}
    
    def collection(self, name: str):
        if name not in self.collections:
            self.collections[name] = {
                "binLookupCache": {},
                "uncheckedCards": {}
            }
        return self.collections[name]

db = Database()

# ================== SERVICE INSTANCES ==================
clover_service = CloverService()
amazon_pay_service = AmazonPayService()
authorize_net_service = AuthorizeNetService()
paypal_service = PayPalService()
live_checker_service = LiveCheckerService()

# ================== CONSTANTS ==================
BIN_CACHE_MAX_AGE_MS = 30 * 24 * 60 * 60 * 1000  # 30 gün

# ================== HELPER FUNCTIONS ==================

def digits_only(value: Any) -> str:
    """Sadece rakamları döndür"""
    return re.sub(r'\D', '', str(value or ""))

def normalize_expiry(value: Any) -> Optional[Dict]:
    """Expiry değerini normalize et"""
    text = str(value or "").strip()
    compact = digits_only(text)
    
    month = ""
    year = ""
    
    if "/" in text or "-" in text:
        parts = re.split(r'[/-]', text)
        month = digits_only(parts[0]) if len(parts) > 0 else ""
        year = digits_only(parts[1]) if len(parts) > 1 else ""
    elif len(compact) == 4:
        month = compact[:2]
        year = compact[2:]
    elif len(compact) == 6:
        month = compact[:2]
        year = compact[2:]
    
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

def normalize_card_input(payload: Dict = None) -> Dict:
    """Kart girdisini normalize et"""
    if payload is None:
        payload = {}
    
    pan = digits_only(payload.get("pan") or payload.get("cardNumber") or 
                      payload.get("cardnumber") or payload.get("number"))
    
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
        "address": str(payload.get("address") or payload.get("billingAddress") or "").strip(),
        "phone": str(payload.get("phone") or "").strip()
    }

def parse_card_line(line: str) -> Dict:
    """Kart satırını parse et"""
    parts = str(line or "").strip().split("|")
    
    def field_after_cvv(index: int) -> Dict:
        value = str(parts[index] if index < len(parts) else "").strip()
        is_zip = bool(re.match(r'^\d{5}$', value))
        return {
            "zip": value if is_zip else "00000",
            "holderName": parts[index + 1] if is_zip and index + 1 < len(parts) else value if not is_zip else "",
            "address": "|".join(parts[index + 2:]) if is_zip else "|".join(parts[index + 1:])
        }
    
    month = digits_only(parts[1]) if len(parts) > 1 else ""
    year = digits_only(parts[2]) if len(parts) > 2 else ""
    cvv_after_year = str(parts[3] if len(parts) > 3 else "").strip()
    
    if len(parts) >= 4 and re.match(r'^(0?[1-9]|1[0-2])$', month) and re.match(r'^(\d{2}|\d{4})$', year) and cvv_after_year:
        after_cvv = field_after_cvv(4)
        return normalize_card_input({
            "cardNumber": parts[0],
            "exp": f"{month}/{year}",
            "cvv": parts[3],
            "zip": after_cvv["zip"],
            "holderName": after_cvv["holderName"],
            "address": after_cvv["address"]
        })
    
    after_cvv = field_after_cvv(3)
    return normalize_card_input({
        "cardNumber": parts[0],
        "exp": parts[1] if len(parts) > 1 else "",
        "cvv": parts[2] if len(parts) > 2 else "",
        "zip": after_cvv["zip"],
        "holderName": after_cvv["holderName"],
        "address": after_cvv["address"]
    })

def mask_pan(pan: str) -> Optional[str]:
    """PAN'i maskele"""
    digits = digits_only(pan)
    if len(digits) < 10:
        return None
    return f"{digits[:6]}******{digits[-4:]}"

def record_hash(card: Dict) -> str:
    """Kart hash'i oluştur"""
    data = f"{digits_only(card['pan'])}|{card['expMonth']}|{card['expYear']}"
    return hashlib.sha256(data.encode()).hexdigest()[:24]

# ================== MAIN FUNCTIONS ==================

async def enrich_bin_with_paypal_vault(result: Dict, payload: Dict = None) -> Dict:
    """PayPal Vault ile BIN sonucunu zenginleştir"""
    if payload is None:
        payload = {}
    
    provider = str(payload.get("provider", "")).lower()
    if provider != "paypal" or not (payload.get("providerPaymentToken") or payload.get("paymentTokenId") or payload.get("vaultId")):
        return result
    
    try:
        paypal_vault = await paypal_service.get_vaulted_payment_method_metadata(payload)
    except Exception as error:
        is_local_placeholder = hasattr(error, 'code') and error.code == "PAYPAL_VAULT_TOKEN_REQUIRED"
        return {
            **result,
            "paypalVault": {
                "status": "not_vaulted" if is_local_placeholder else "unavailable",
                "source": "paypal_vault",
                "sourceLabel": "PayPal Vault",
                "resultCode": getattr(error, 'code', 'PAYPAL_VAULT_LOOKUP_FAILED'),
                "error": str(error)
            }
        }
    
    return {
        **result,
        "sourceLabel": f"{result.get('sourceLabel', result.get('source', ''))} + PayPal Vault",
        "verificationSources": [
            {
                "source": result.get("source"),
                "label": result.get("sourceLabel") or result.get("source"),
                "role": "issuer_bin_metadata"
            },
            {
                "source": "paypal_vault",
                "label": "PayPal Vault",
                "role": "stored_card_metadata"
            }
        ],
        "paypalVault": paypal_vault,
        "summary": {
            **result.get("summary", {}),
            "brand": paypal_vault.get("card", {}).get("brand") or result.get("summary", {}).get("brand"),
            "countryCode": paypal_vault.get("card", {}).get("countryCode") or result.get("summary", {}).get("countryCode"),
            "paypalCardholder": paypal_vault.get("card", {}).get("name"),
            "paypalLast4": paypal_vault.get("card", {}).get("last4"),
            "paypalExpiry": paypal_vault.get("card", {}).get("expiry")
        }
    }

async def bin_check_card(payload: Dict = None) -> Dict:
    """BIN kontrolü yap"""
    if payload is None:
        payload = {}
    
    card = normalize_card_input(payload) if payload.get("pan") or payload.get("cardNumber") else payload
    normalized_bin = digits_only(payload.get("bin") or card.get("pan") or payload.get("pan", ""))[:6]
    has_ip_lookup = bool(str(payload.get("ip", "")).strip())
    
    database = None
    try:
        db_instance = db
        # MongoDB yerine basit cache kullanımı
        if not has_ip_lookup:
            cached = db_instance.collection("binLookupCache").get(normalized_bin)
            if cached and cached.get("result"):
                cache_age = datetime.now() - datetime.fromisoformat(cached["updatedAt"])
                if cache_age.total_seconds() * 1000 <= BIN_CACHE_MAX_AGE_MS:
                    result = {
                        **cached["result"],
                        "cached": True,
                        "cacheSource": "mongodb"
                    }
                    result = await enrich_bin_with_paypal_vault(result, payload)
                    print(f"[BIN CHECK - CACHE HIT] {result}")
                    return result
    except Exception:
        database = None
    
    result = await paypal_service.bin_check_card({
        "pan": card.get("pan") or payload.get("pan"),
        "bin": normalized_bin,
        "ip": payload.get("ip")
    })
    
    # Fallback history
    if str(result.get("status", "")).lower() in ["limited", "failed"] and database:
        historical = db_instance.collection("uncheckedCards").get(normalized_bin)
        
        if historical:
            network = result.get("summary", {}).get("brand") or result.get("summary", {}).get("scheme")
            
            recovered = {
                **result,
                "status": "fallback",
                "resultCode": "LOCAL_BIN_HISTORY_FALLBACK",
                "source": "local_bin_history",
                "sourceLabel": "Local verified BIN history",
                "confidence": "medium",
                "dataQuality": "partial",
                "summary": {
                    **result.get("summary", {}),
                    "bin": normalized_bin,
                    "countryCode": historical.get("countryCode"),
                    "country": historical.get("countryCode"),
                    "issuer": historical.get("bank"),
                    "type": historical.get("cardType"),
                    "level": historical.get("cardLevel"),
                    "scheme": result.get("summary", {}).get("scheme") or network,
                    "brand": result.get("summary", {}).get("brand") or network,
                    "usefulLabel": " / ".join([x for x in [
                        historical.get("countryCode"),
                        historical.get("bank"),
                        historical.get("cardType"),
                        historical.get("cardLevel"),
                        network
                    ] if x])
                }
            }
            
            if not has_ip_lookup:
                db_instance.collection("binLookupCache")[normalized_bin] = {
                    "bin": normalized_bin,
                    "result": recovered,
                    "updatedAt": datetime.now().isoformat()
                }
            
            result = await enrich_bin_with_paypal_vault(recovered, payload)
            print(f"[BIN CHECK - HISTORICAL FALLBACK] {result}")
            return result
    
    if not has_ip_lookup and str(result.get("status", "")).lower() in ["passed", "fallback"]:
        cache_result = {k: v for k, v in result.items() if k != "raw"}
        db_instance.collection("binLookupCache")[normalized_bin] = {
            "bin": normalized_bin,
            "result": cache_result,
            "updatedAt": datetime.now().isoformat()
        }
    
    result = await enrich_bin_with_paypal_vault(result, payload)
    print(f"[BIN CHECK - LIVE RESULT] {result}")
    return result

async def live_check_card(payload: Dict = None) -> Dict:
    """Canlı kart kontrolü yap"""
    if payload is None:
        payload = {}
    
    provider = str(payload.get("provider", "clover")).lower()
    amount = float(payload.get("amount", 0.1))
    currency = payload.get("currency", "usd")
    
    if provider == "amazonpay":
        charge_permission_id = payload.get("chargePermissionId") or payload.get("providerPaymentToken") or payload.get("source") or payload.get("token")
        if not charge_permission_id:
            raise ValueError("Amazon Pay liveCheck requires chargePermissionId/providerPaymentToken")
        
        result = await amazon_pay_service.verify_card({
            **payload,
            "chargePermissionId": charge_permission_id,
            "providerPaymentToken": charge_permission_id,
            "source": charge_permission_id,
            "token": charge_permission_id,
            "amount": amount,
            "currency": currency
        })
        
        return {
            **result,
            "provider": "amazonpay",
            "operation": "live",
            "isLive": live_checker_service.is_live_response(result)
        }
    
    if provider == "authorizenet":
        operation = str(payload.get("operation", "verification")).lower()
        
        if operation in ["sale", "charge"]:
            result = await authorize_net_service.sale_card({**payload, "amount": amount, "currency": currency})
        elif operation in ["auth", "authorize", "live", "balance"]:
            result = await authorize_net_service.authorize_card({**payload, "amount": amount, "currency": currency})
        else:
            result = await authorize_net_service.verify_card({
                **payload,
                "amount": amount if payload.get("amount") not in [None, "", 0] else amount,
                "currency": currency
            })
        
        return {
            **result,
            "provider": "authorizenet",
            "operation": "auth" if operation == "live" else operation,
            "providerReferenceId": result.get("transactionId") or result.get("providerReferenceId"),
            "isLive": live_checker_service.is_live_response(result)
        }
    
    if provider != "clover":
        raise ValueError("liveCheck service supports clover, amazonpay or authorizenet")
    
    source = payload.get("source") or payload.get("providerPaymentToken") or payload.get("token")
    tokenization = None
    clover_source = source
    normalized_card = None
    
    if not clover_source:
        normalized_card = normalize_card_input(payload)
        tokenization = await clover_service.tokenize_card({
            "pan": normalized_card["pan"],
            "expMonth": normalized_card["expMonth"],
            "expYear": normalized_card["expYear"],
            "cvv2": normalized_card["cvv"],
            "zip": normalized_card["zip"]
        })
        clover_source = tokenization["source"]
    
    if payload.get("liveMode") == "preauth":
        result = await clover_service.create_pre_authorization({
            "source": clover_source,
            "amount": amount,
            "currency": currency
        })
    else:
        result = await clover_service.verify_card({
            "source": clover_source,
            "zip": payload.get("zip") or (normalized_card.get("zip") if normalized_card else None),
            "billingZip": payload.get("billingZip") or (normalized_card.get("zip") if normalized_card else None),
            "postalCode": payload.get("postalCode") or (normalized_card.get("zip") if normalized_card else None)
        })
    
    return {
        **result,
        "provider": "clover",
        "operation": "live",
        "providerReferenceId": result.get("transactionId") or result.get("cloverChargeId"),
        "tokenization": tokenization,
        "isLive": live_checker_service.is_live_response(result)
    }

async def check_card(payload: Dict = None) -> Dict:
    """Kart kontrolü (BIN + Live)"""
    if payload is None:
        payload = {}
    
    # Live check
    try:
        live = await live_check_card(payload)
    except Exception as error:
        live = {
            "status": "failed",
            "resultCode": getattr(error, 'resultCode', getattr(error, 'code', 'LIVE_CHECK_FAILED')),
            "responseMessage": str(error),
            "error": str(error),
            "provider": str(payload.get("provider", "clover")).lower(),
            "operation": "live",
            "isLive": False
        }
    
    live_passed = live_checker_service.is_live_response(live)
    
    # BIN check
    if payload.get("binCheckOnlyIfLive") and not live_passed:
        bin_check = {
            "status": "skipped",
            "source": "live_check_gate",
            "bin": digits_only(payload.get("pan") or payload.get("cardNumber") or payload.get("bin", ""))[:6],
            "providerWarning": "BIN check skipped because live check did not pass"
        }
    else:
        try:
            bin_check = await bin_check_card(payload)
        except Exception as error:
            bin_check = {
                "status": "review",
                "error": str(error),
                "fallbackError": str(error),
                "source": "unavailable",
                "bin": digits_only(payload.get("pan") or payload.get("cardNumber") or payload.get("bin", ""))[:6]
            }
    
    return {
        "status": "passed" if live_passed else "review",
        "live": live,
        "binCheck": bin_check,
        "compact": {
            **live,
            "binCheck": bin_check
        }
    }