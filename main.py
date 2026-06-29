# stripe_card_checker.py
"""
STRIPE CARD CHECKER API - TEK DOSYA
Önce parser'dan geçirir, sonra Stripe ile işleme sokar.
Sadece API olarak çalışır.
"""

import os
import re
import json
import requests
from typing import Dict, List, Optional, Union, Any, Tuple
from datetime import datetime
from dataclasses import dataclass, field
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field, validator
import uvicorn


# ============================================
# 1. DATA MODELS
# ============================================

@dataclass
class CardData:
    """Standart kart verisi modeli"""
    number: str
    exp_month: str
    exp_year: str
    cvc: str
    name: str = "Test User"
    country: str = "US"
    zip: str = "00000"
    email: Optional[str] = None
    phone: Optional[str] = None
    ip: Optional[str] = None
    
    def get_masked(self) -> str:
        return f"{self.number[:4]}****{self.number[-4:]}"
    
    def to_stripe_format(self) -> Dict[str, Any]:
        return {
            "number": self.number,
            "exp_month": self.exp_month,
            "exp_year": self.exp_year,
            "cvc": self.cvc,
            "billing_details": {
                "name": self.name,
                "address": {
                    "line1": "Test Street 123",
                    "postal_code": self.zip or "00000",
                    "country": self.country or "US"
                }
            }
        }


@dataclass
class ProcessingResult:
    """İşlem sonucu modeli"""
    card: Optional[CardData]
    success: bool
    status: str
    message: str
    setup_intent_id: Optional[str] = None
    payment_method_id: Optional[str] = None
    requires_action: bool = False
    redirect_url: Optional[str] = None
    error: Optional[str] = None
    timestamp: str = field(default_factory=lambda: datetime.now().isoformat())


# ============================================
# 2. API REQUEST MODELS
# ============================================

class CardCheckRequest(BaseModel):
    """API'ye gelen kart kontrol isteği"""
    number: str = Field(..., description="Kart numarası")
    exp_month: str = Field(..., description="Son kullanma ayı")
    exp_year: str = Field(..., description="Son kullanma yılı")
    cvc: str = Field(..., description="CVC kodu")
    name: Optional[str] = Field("Test User", description="Kart sahibi adı")
    country: Optional[str] = Field("US", description="Ülke kodu")
    zip: Optional[str] = Field("00000", description="Posta kodu")
    email: Optional[str] = Field(None, description="Email")
    phone: Optional[str] = Field(None, description="Telefon")
    ip: Optional[str] = Field(None, description="IP")
    customer_id: Optional[str] = Field(None, description="Müşteri ID")
    
    @validator('number')
    def clean_number(cls, v):
        v = re.sub(r'[\s\-]', '', str(v))
        if not v.isdigit() or len(v) < 13 or len(v) > 19:
            raise ValueError("Geçersiz kart numarası")
        return v
    
    @validator('exp_month')
    def format_month(cls, v):
        v = str(v).strip()
        if len(v) == 1:
            v = f"0{v}"
        if not v.isdigit() or int(v) < 1 or int(v) > 12:
            raise ValueError("Geçersiz ay")
        return v
    
    @validator('exp_year')
    def format_year(cls, v):
        v = str(v).strip()
        if len(v) == 4:
            v = v[-2:]
        if not v.isdigit() or int(v) < 0 or int(v) > 99:
            raise ValueError("Geçersiz yıl")
        return v
    
    @validator('cvc')
    def validate_cvc(cls, v):
        v = str(v).strip()
        if not v.isdigit() or len(v) < 3 or len(v) > 4:
            raise ValueError("CVC 3-4 haneli olmalı")
        return v


class BatchCheckRequest(BaseModel):
    """Toplu kart kontrol isteği"""
    cards: List[CardCheckRequest] = Field(..., description="Kart listesi")


class ParseAndCheckRequest(BaseModel):
    """Parse et ve kontrol et isteği"""
    data: Any = Field(..., description="Herhangi bir formatta kart verisi")
    customer_id: Optional[str] = Field(None, description="Müşteri ID")


# ============================================
# 3. CARD PARSER
# ============================================

class CardParser:
    """Farklı formatlardaki kart verilerini parse eden sınıf"""
    
    @staticmethod
    def parse(data: Union[str, List, Dict]) -> List[CardData]:
        """Ana parse fonksiyonu - otomatik format tespiti"""
        if isinstance(data, (list, dict)):
            return CardParser._parse_json(data)
        
        if isinstance(data, str):
            data = data.strip()
            
            # JSON string kontrolü
            if data.startswith('[') or data.startswith('{'):
                try:
                    json_data = json.loads(data)
                    return CardParser._parse_json(json_data)
                except:
                    pass
            
            # Pipe formatı
            if '|' in data:
                if len(data.split('|')) > 10:
                    return CardParser._parse_full_pipe(data)
                else:
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
            if len(parts) >= 3:
                number = parts[0].strip()
                exp_part = parts[1].strip()
                cvc = parts[2].strip()
                exp_month, exp_year = CardParser._parse_expiration(exp_part)
                
                if number and exp_month and exp_year and cvc:
                    cards.append(CardData(
                        number=number,
                        exp_month=exp_month,
                        exp_year=exp_year,
                        cvc=cvc
                    ))
        
        return cards
    
    @staticmethod
    def _parse_full_pipe(data: str) -> List[CardData]:
        cards = []
        lines = data.strip().split('\n')
        
        for line in lines:
            line = line.strip()
            if not line:
                continue
            
            parts = line.split('|')
            if len(parts) >= 3:
                number = parts[0].strip() if parts[0] else None
                exp_part = parts[1].strip() if len(parts) > 1 else None
                cvc = parts[2].strip() if len(parts) > 2 else None
                name = parts[3].strip() if len(parts) > 3 and parts[3] else "Test User"
                email = parts[10].strip() if len(parts) > 10 else None
                phone = parts[9].strip() if len(parts) > 9 else None
                ip = parts[12].strip() if len(parts) > 12 else None
                
                if number and exp_part and cvc:
                    exp_month, exp_year = CardParser._parse_expiration(exp_part)
                    if exp_month and exp_year:
                        cards.append(CardData(
                            number=number,
                            exp_month=exp_month,
                            exp_year=exp_year,
                            cvc=cvc,
                            name=name,
                            email=email,
                            phone=phone,
                            ip=ip
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
                        return month, year
        
        if exp_str.isdigit() and len(exp_str) == 4:
            return exp_str[:2], exp_str[2:]
        
        return None, None
    
    @staticmethod
    def _extract_from_json_item(item: Dict) -> Optional[CardData]:
        # Format 1: Direct fields
        if 'number' in item:
            number = item['number']
            exp_month = item.get('exp_month') or item.get('month')
            exp_year = item.get('exp_year') or item.get('year')
            cvc = item.get('cvc') or item.get('cvv') or item.get('CVV')
            
            if number and exp_month and exp_year and cvc:
                return CardData(
                    number=str(number),
                    exp_month=str(exp_month),
                    exp_year=str(exp_year),
                    cvc=str(cvc)
                )
        
        # Format 2: CreditCard wrapper
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
        
        return None


# ============================================
# 4. STRIPE PROCESSOR
# ============================================

class StripeProcessor:
    """Stripe API işlemleri"""
    
    def __init__(self, secret_key: str):
        self.secret_key = secret_key
        self.base_url = "https://api.stripe.com/v1"
        self.headers = {
            "Authorization": f"Bearer {self.secret_key}",
            "Content-Type": "application/x-www-form-urlencoded"
        }
        self.is_test_mode = secret_key.startswith("sk_test_")
    
    def create_setup_intent(self, customer_id: Optional[str] = None) -> Dict[str, Any]:
        """SetupIntent oluştur"""
        url = f"{self.base_url}/setup_intents"
        data = {"payment_method_types[]": "card"}
        if customer_id:
            data["customer"] = customer_id
        
        response = requests.post(url, data=data, headers=self.headers)
        if response.status_code != 200:
            error = response.json().get('error', {}).get('message', 'Unknown error')
            raise Exception(f"SetupIntent creation failed: {error}")
        return response.json()
    
    def confirm_setup_intent(self, setup_id: str, client_secret: str, card: CardData) -> Dict[str, Any]:
        """SetupIntent'i kart ile onayla"""
        url = f"{self.base_url}/setup_intents/{setup_id}/confirm"
        
        data = {
            "return_url": "https://example.com/return",
            "use_stripe_sdk": "true",
            "client_secret": client_secret,
            "payment_method_data[billing_details][name]": card.name,
            "payment_method_data[billing_details][address][postal_code]": card.zip or "00000",
            "payment_method_data[billing_details][address][country]": card.country,
            "payment_method_data[type]": "card",
            "payment_method_data[card][number]": card.number,
            "payment_method_data[card][cvc]": card.cvc,
            "payment_method_data[card][exp_year]": card.exp_year,
            "payment_method_data[card][exp_month]": card.exp_month,
            "payment_method_data[allow_redisplay]": "unspecified",
            "payment_method_data[pasted_fields]": "number",
            "expected_payment_method_type": "card",
        }
        
        response = requests.post(url, data=data, headers=self.headers)
        return {
            "status_code": response.status_code,
            "data": response.json() if response.text else {}
        }
    
    def process_card(self, card: CardData, customer_id: Optional[str] = None) -> ProcessingResult:
        """Tek bir kartı Stripe ile doğrula"""
        try:
            if not self.is_test_mode:
                return ProcessingResult(
                    card=card,
                    success=False,
                    status="error",
                    message="Live keys cannot be used for testing",
                    error="Please use test keys (sk_test_)"
                )
            
            # 1. SetupIntent oluştur
            setup_intent = self.create_setup_intent(customer_id)
            setup_id = setup_intent["id"]
            client_secret = setup_intent["client_secret"]
            
            # 2. SetupIntent'i onayla
            result = self.confirm_setup_intent(setup_id, client_secret, card)
            
            if result["status_code"] == 200:
                data = result["data"]
                status = data.get("status")
                
                if status == "succeeded":
                    return ProcessingResult(
                        card=card,
                        success=True,
                        status=status,
                        message="Card verified successfully",
                        setup_intent_id=setup_id,
                        payment_method_id=data.get("payment_method")
                    )
                elif status == "requires_action":
                    next_action = data.get("next_action", {})
                    redirect_data = next_action.get("redirect_to_url", {})
                    
                    return ProcessingResult(
                        card=card,
                        success=False,
                        status=status,
                        message="3D Secure authentication required",
                        requires_action=True,
                        redirect_url=redirect_data.get("url"),
                        setup_intent_id=setup_id
                    )
                else:
                    return ProcessingResult(
                        card=card,
                        success=False,
                        status=status,
                        message=f"Unexpected status: {status}",
                        setup_intent_id=setup_id
                    )
            else:
                error_data = result["data"].get("error", {})
                return ProcessingResult(
                    card=card,
                    success=False,
                    status=f"error_{result['status_code']}",
                    message="Card verification failed",
                    error=f"{error_data.get('type')}: {error_data.get('message')}"
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
        """Birden fazla kartı Stripe ile doğrula"""
        results = []
        for card in cards:
            result = self.process_card(card, customer_id)
            results.append(result)
        return results


# ============================================
# 5. FASTAPI APP
# ============================================

app = FastAPI(
    title="Stripe Card Checker API",
    description="Önce parser'dan geçirir, sonra Stripe ile doğrular",
    version="1.0.0"
)

stripe_processor: Optional[StripeProcessor] = None


@app.on_event("startup")
async def startup_event():
    global stripe_processor
    secret_key = os.environ.get("STRIPE_SECRET_KEY", "sk_test_...")
    stripe_processor = StripeProcessor(secret_key)
    
    print("="*60)
    print("🚀 STRIPE CARD CHECKER API")
    print("="*60)
    print(f"🔑 Key: {secret_key[:10]}...")
    print(f"📝 Mode: {'TEST' if secret_key.startswith('sk_test_') else 'LIVE'}")
    print("="*60)


@app.get("/")
async def root():
    return {
        "service": "Stripe Card Checker API",
        "version": "1.0.0",
        "status": "running",
        "endpoints": {
            "/check": "POST - Tek kart kontrolü",
            "/batch-check": "POST - Toplu kart kontrolü",
            "/parse-and-check": "POST - Parse et ve kontrol et",
            "/parse": "POST - Sadece parse et (doğrulama yok)",
            "/health": "GET - Sağlık kontrolü"
        }
    }


@app.get("/health")
async def health():
    if stripe_processor is None:
        return JSONResponse(status_code=503, content={"status": "unhealthy", "error": "Stripe not configured"})
    return {"status": "healthy", "mode": "test" if stripe_processor.is_test_mode else "live"}


# ============================================
# 6. ENDPOINT: /check - Tek kart kontrolü
# ============================================

@app.post("/check")
async def check_card(request: CardCheckRequest):
    """
    Tek bir kartı doğrula
    
    Önce CardCheckRequest'ten CardData'ya çevirir, sonra Stripe ile doğrular.
    """
    if stripe_processor is None:
        raise HTTPException(status_code=503, detail="Stripe not configured")
    
    # Request'ten CardData oluştur
    card = CardData(
        number=request.number,
        exp_month=request.exp_month,
        exp_year=request.exp_year,
        cvc=request.cvc,
        name=request.name or "Test User",
        country=request.country or "US",
        zip=request.zip or "00000",
        email=request.email,
        phone=request.phone,
        ip=request.ip
    )
    
    # Stripe ile doğrula
    result = stripe_processor.process_card(card, request.customer_id)
    
    return {
        "success": result.success,
        "status": result.status,
        "message": result.message,
        "card": {
            "masked": card.get_masked(),
            "exp": f"{card.exp_month}/{card.exp_year}"
        },
        "requires_action": result.requires_action,
        "redirect_url": result.redirect_url,
        "setup_intent_id": result.setup_intent_id,
        "payment_method_id": result.payment_method_id,
        "error": result.error,
        "timestamp": result.timestamp
    }


# ============================================
# 7. ENDPOINT: /batch-check - Toplu kart kontrolü
# ============================================

@app.post("/batch-check")
async def batch_check(request: BatchCheckRequest):
    """
    Birden fazla kartı toplu olarak doğrula
    
    Her kart önce CardData'ya çevrilir, sonra Stripe ile doğrulanır.
    """
    if stripe_processor is None:
        raise HTTPException(status_code=503, detail="Stripe not configured")
    
    cards = []
    for req in request.cards:
        card = CardData(
            number=req.number,
            exp_month=req.exp_month,
            exp_year=req.exp_year,
            cvc=req.cvc,
            name=req.name or "Test User",
            country=req.country or "US",
            zip=req.zip or "00000",
            email=req.email,
            phone=req.phone,
            ip=req.ip
        )
        cards.append(card)
    
    # Stripe ile doğrula
    results = stripe_processor.process_cards(cards)
    
    response = []
    for i, result in enumerate(results):
        response.append({
            "index": i,
            "card": {
                "masked": cards[i].get_masked(),
                "exp": f"{cards[i].exp_month}/{cards[i].exp_year}"
            },
            "success": result.success,
            "status": result.status,
            "message": result.message,
            "requires_action": result.requires_action,
            "redirect_url": result.redirect_url,
            "setup_intent_id": result.setup_intent_id,
            "error": result.error,
            "timestamp": result.timestamp
        })
    
    return {
        "total": len(results),
        "successful": sum(1 for r in results if r.success),
        "failed": sum(1 for r in results if not r.success),
        "results": response
    }


# ============================================
# 8. ENDPOINT: /parse-and-check - Parse et ve kontrol et
# ============================================

@app.post("/parse-and-check")
async def parse_and_check(request: ParseAndCheckRequest):
    """
    Herhangi bir formattaki kart verisini önce parse eder, sonra Stripe ile doğrular
    
    Desteklenen formatlar:
    - Pipe: 4242424242424242|12/28|123
    - JSON: {"number": "4242...", "month": "12", "year": "2028", "cvv": "123"}
    - CreditCard: {"CreditCard": {"CardNumber": "...", "Exp": "12/2028", "CVV": "123"}}
    - Full pipe: card|exp|cvv|name||||||phone|email|ip|useragent
    """
    if stripe_processor is None:
        raise HTTPException(status_code=503, detail="Stripe not configured")
    
    # 1. PARSE ET
    cards = CardParser.parse(request.data)
    
    if not cards:
        raise HTTPException(status_code=400, detail="No valid cards found in the input data")
    
    # 2. STRIPE İLE DOĞRULA
    results = stripe_processor.process_cards(cards, request.customer_id)
    
    # 3. CEVAP OLUŞTUR
    response = []
    for i, (card, result) in enumerate(zip(cards, results)):
        response.append({
            "index": i,
            "original_card": {
                "masked": card.get_masked(),
                "exp_month": card.exp_month,
                "exp_year": card.exp_year,
                "cvc": card.cvc,
                "name": card.name
            },
            "success": result.success,
            "status": result.status,
            "message": result.message,
            "requires_action": result.requires_action,
            "redirect_url": result.redirect_url,
            "setup_intent_id": result.setup_intent_id,
            "payment_method_id": result.payment_method_id,
            "error": result.error,
            "timestamp": result.timestamp
        })
    
    return {
        "total": len(results),
        "successful": sum(1 for r in results if r.success),
        "failed": sum(1 for r in results if not r.success and not r.requires_action),
        "requires_action": sum(1 for r in results if r.requires_action),
        "results": response
    }


# ============================================
# 9. ENDPOINT: /parse - Sadece parse et (doğrulama yok)
# ============================================

@app.post("/parse")
async def parse_only(request: ParseAndCheckRequest):
    """
    Sadece kart verilerini parse et, Stripe ile doğrulama YAPMAZ
    
    Hangi formatta geldiğini görmek için kullanılır.
    """
    cards = CardParser.parse(request.data)
    
    if not cards:
        raise HTTPException(status_code=400, detail="No valid cards found")
    
    response = []
    for i, card in enumerate(cards):
        response.append({
            "index": i,
            "card": {
                "number": card.number,
                "masked": card.get_masked(),
                "exp_month": card.exp_month,
                "exp_year": card.exp_year,
                "cvc": card.cvc,
                "name": card.name,
                "country": card.country,
                "zip": card.zip,
                "email": card.email,
                "phone": card.phone,
                "ip": card.ip
            }
        })
    
    return {
        "total": len(cards),
        "parsed_cards": response
    }


# ============================================
# 10. RUN
# ============================================

if __name__ == "__main__":
    print("="*60)
    print("🚀 STRIPE CARD CHECKER API")
    print("="*60)
    print("⚠️  UYARI: SADECE TEST KEY KULLANIN!")
    print("   Test key: sk_test_...")
    print("   export STRIPE_SECRET_KEY=sk_test_...")
    print("="*60)
    print()
    print("📌 ENDPOINTLER:")
    print("   POST /check              - Tek kart kontrolü")
    print("   POST /batch-check        - Toplu kart kontrolü")
    print("   POST /parse-and-check    - Parse et + kontrol et")
    print("   POST /parse              - Sadece parse et")
    print("   GET  /health             - Sağlık kontrolü")
    print("="*60)
    
    uvicorn.run(app, host="0.0.0.0", port=8000)