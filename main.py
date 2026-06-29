"""
STRIPE CARD CHECKER API - TEK DOSYA
Önce parser'dan geçirir, sonra Stripe ile işleme sokar.
Sadece API olarak çalışır.
"""
import json
from typing import List, Dict, Optional, Union, Tuple, Any
from dataclasses import dataclass, field
import os
import requests
from datetime import datetime
from fastapi import FastAPI, HTTPException
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
    dob: Optional[str] = None
    ip: Optional[str] = None
    user_agent: Optional[str] = None
    metadata: Dict[str, Any] = field(default_factory=dict)
    
    def get_masked(self) -> str:
        """Maskeli kart numarası"""
        return f"{self.number[:4]}****{self.number[-4:]}"
    
    def to_stripe_format(self) -> Dict[str, Any]:
        """Stripe API formatına çevir"""
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
    card: CardData
    success: bool
    status: str
    message: str
    error: Optional[str] = None
    setup_intent_id: Optional[str] = None
    payment_method_id: Optional[str] = None
    requires_action: bool = False
    redirect_url: Optional[str] = None
    raw_response: Optional[Dict] = None


# ============================================
# 2. API REQUEST MODELS
# ============================================

class CardCheckRequest(BaseModel):
    """Kart kontrolü için request modeli"""
    cards: Union[str, List[Dict], Dict] = Field(..., description="Kart verileri (string, JSON veya liste)")
    stripe_key: str = Field(..., description="Stripe secret key (sk_test_ ile başlamalı)")
    customer_id: Optional[str] = Field(None, description="Opsiyonel müşteri ID")
    
    @validator('stripe_key')
    def validate_stripe_key(cls, v):
        if not v.startswith('sk_test_'):
            raise ValueError('Only test keys (sk_test_) are allowed for security')
        return v


class ParseRequest(BaseModel):
    """Sadece parse işlemi için request modeli"""
    data: Union[str, List[Dict], Dict] = Field(..., description="Parse edilecek kart verileri")
    return_type: str = Field("json", description="Dönüş tipi: json veya list")


class SingleCardCheckRequest(BaseModel):
    """Tek kart kontrolü için request modeli"""
    card: Union[str, Dict] = Field(..., description="Kart verisi (string pipe format veya JSON)")
    stripe_key: str = Field(..., description="Stripe secret key (sk_test_ ile başlamalı)")
    customer_id: Optional[str] = Field(None, description="Opsiyonel müşteri ID")
    
    @validator('stripe_key')
    def validate_stripe_key(cls, v):
        if not v.startswith('sk_test_'):
            raise ValueError('Only test keys (sk_test_) are allowed for security')
        return v


# ============================================
# 3. CARD PARSER
# ============================================

class CardParser:
    """
    Farklı formatlardaki kart verilerini parse eden sınıf
    """
    
    @staticmethod
    def parse(data: Union[str, List, Dict]) -> List[CardData]:
        """
        Ana parse fonksiyonu - otomatik format tespiti
        
        Args:
            data: Herhangi bir formatta kart verisi
            
        Returns:
            List[CardData]: Parse edilmiş kart listesi
        """
        # JSON ise
        if isinstance(data, (list, dict)):
            return CardParser._parse_json(data)
        
        # String ise
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
                return CardParser._parse_pipe(data)
        
        return []
    
    @staticmethod
    def _parse_pipe(data: str) -> List[CardData]:
        """Pipe (|) ile ayrılmış formatı parse et"""
        cards = []
        lines = data.strip().split('\n')
        
        for line in lines:
            line = line.strip()
            if not line:
                continue
            
            parts = line.split('|')
            
            if len(parts) >= 3:
                number = parts[0].strip()

                # Both number|MM/YY|CVC and number|MM|YY|CVC are accepted.
                if len(parts) >= 4 and parts[1].strip().isdigit() and parts[2].strip().isdigit():
                    exp_part = f"{parts[1].strip()}/{parts[2].strip()}"
                    cvc = parts[3].strip()
                    metadata_offset = 1
                else:
                    exp_part = parts[1].strip()
                    cvc = parts[2].strip()
                    metadata_offset = 0

                exp_month, exp_year = CardParser._parse_expiration(exp_part)
                
                if number and exp_month and exp_year and cvc:
                    cards.append(CardData(
                        number=number,
                        exp_month=exp_month,
                        exp_year=exp_year,
                        cvc=cvc,
                        name=parts[3 + metadata_offset].strip() or "Test User"
                        if len(parts) > 3 + metadata_offset else "Test User",
                        phone=parts[9 + metadata_offset].strip()
                        if len(parts) > 9 + metadata_offset else None,
                        email=parts[10 + metadata_offset].strip()
                        if len(parts) > 10 + metadata_offset else None,
                        dob=parts[11 + metadata_offset].strip()
                        if len(parts) > 11 + metadata_offset else None,
                        ip=parts[12 + metadata_offset].strip()
                        if len(parts) > 12 + metadata_offset else None,
                        user_agent=parts[13 + metadata_offset].strip()
                        if len(parts) > 13 + metadata_offset else None,
                    ))
        
        return cards
    
    @staticmethod
    def _parse_full_pipe(data: str) -> List[CardData]:
        """Tam pipe formatını parse et (kişisel bilgiler içerir)"""
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
                dob = parts[11].strip() if len(parts) > 11 else None
                ip = parts[12].strip() if len(parts) > 12 else None
                user_agent = parts[13].strip() if len(parts) > 13 else None
                
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
                            dob=dob,
                            ip=ip,
                            user_agent=user_agent
                        ))
        
        return cards
    
    @staticmethod
    def _parse_json(data: Union[List, Dict]) -> List[CardData]:
        """JSON formatını parse et"""
        cards = []
        
        # List değilse listeye çevir
        if not isinstance(data, list):
            data = [data]
        
        for item in data:
            card_data = CardParser._extract_from_json_item(item)
            if card_data:
                cards.append(card_data)
        
        return cards
    
    @staticmethod
    def _parse_expiration(exp_str: str) -> Tuple[Optional[str], Optional[str]]:
        """
        Çeşitli tarih formatlarını parse et
        
        Desteklenen formatlar:
        - 09/26
        - 09/2026
        - 9/26
        - 9/2026
        - 09|26
        - 09|2026
        - 02/27
        - 2/2027
        - 10/28
        - 10/2028
        - 3223 (03/23)
        """
        exp_str = exp_str.strip()
        
        # Ay/Yıl ayırıcıları
        separators = ['/', '-', '|', ' ']
        for sep in separators:
            if sep in exp_str:
                parts = exp_str.split(sep)
                if len(parts) == 2:
                    month = parts[0].strip()
                    year = parts[1].strip()
                    
                    # Ay formatını düzenle (2 hane)
                    if len(month) == 1:
                        month = f"0{month}"
                    
                    # Yıl formatını düzenle (4 haneli ise son 2 haneyi al)
                    if len(year) == 4:
                        year = year[-2:]
                    
                    if month.isdigit() and year.isdigit():
                        # Ay kontrolü
                        month_int = int(month)
                        if 1 <= month_int <= 12:
                            return month, year
        
        # Sadece sayı varsa (3223 -> 03/23)
        if exp_str.isdigit() and len(exp_str) == 4:
            month = exp_str[:2]
            year = exp_str[2:]
            month_int = int(month)
            if 1 <= month_int <= 12:
                return month, year
        
        return None, None
    
    @staticmethod
    def _extract_from_json_item(item: Dict) -> Optional[CardData]:
        """JSON objesinden kart verisini çıkar"""
        try:
            # Format 1: Direct fields (number, exp_month, exp_year, cvc)
            if 'number' in item:
                number = item['number']
                exp_month = item.get('exp_month') or item.get('month')
                exp_year = item.get('exp_year') or item.get('year')
                cvc = item.get('cvc') or item.get('cvv') or item.get('CVV')
                
                if number and exp_month and exp_year and cvc:
                    # Eğer exp_month/exp_year string değilse stringe çevir
                    exp_month = str(exp_month).zfill(2)  # 1 -> 01
                    exp_year = str(exp_year)
                    if len(exp_year) == 4:
                        exp_year = exp_year[-2:]
                    
                    return CardData(
                        number=str(number),
                        exp_month=exp_month,
                        exp_year=exp_year,
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
            
            # Format 3: CardInfo wrapper
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
            
            # Format 4: Kart formatındaki string'den parse et
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


# ============================================
# 4. PARSER FONKSİYONLARI
# ============================================

def parse_card_data(data: Union[str, List, Dict]) -> List[CardData]:
    """
    Ana parser fonksiyonu
    
    Args:
        data: Herhangi bir formatta kart verisi
        
    Returns:
        List[CardData]: Parse edilmiş kart listesi
    """
    return CardParser.parse(data)


def parse_card_data_single(data: Union[str, Dict]) -> Optional[CardData]:
    """
    Tek bir kart verisini parse et
    
    Args:
        data: Herhangi bir formatta tek kart verisi
        
    Returns:
        CardData veya None
    """
    results = CardParser.parse(data)
    return results[0] if results else None


def format_card_data(card: CardData) -> Dict[str, Any]:
    """CardData'yı JSON serializable formata çevir"""
    return {
        "number": card.get_masked(),
        "full_number": card.number,
        "exp_month": card.exp_month,
        "exp_year": card.exp_year,
        "cvc": card.cvc,
        "name": card.name,
        "country": card.country,
        "zip": card.zip,
        "email": card.email,
        "phone": card.phone,
        "dob": card.dob,
        "ip": card.ip,
        "user_agent": card.user_agent
    }


# ============================================
# 5. STRIPE PROCESSOR
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
    
    def create_payment_method(self, card: CardData) -> Tuple[Optional[str], Optional[str]]:
        """PaymentMethod oluştur"""
        url = f"{self.base_url}/payment_methods"
        
        data = {
            "type": "card",
            "card[number]": card.number,
            "card[exp_month]": card.exp_month,
            "card[exp_year]": card.exp_year,
            "card[cvc]": card.cvc,
            "billing_details[name]": card.name,
        }
        
        if card.zip:
            data["billing_details[address][postal_code]"] = card.zip
        if card.country:
            data["billing_details[address][country]"] = card.country
        
        try:
            response = requests.post(url, data=data, headers=self.headers)
            result = response.json()
            
            if response.status_code == 200:
                return result.get('id'), None
            else:
                error = result.get('error', {})
                return None, error.get('message', 'Unknown error')
                
        except Exception as e:
            return None, str(e)
    
    def attach_payment_method(self, payment_method_id: str, customer_id: str) -> Tuple[bool, Optional[str]]:
        """PaymentMethod'u müşteriye ata"""
        url = f"{self.base_url}/payment_methods/{payment_method_id}/attach"
        data = {"customer": customer_id}
        
        try:
            response = requests.post(url, data=data, headers=self.headers)
            if response.status_code == 200:
                return True, None
            else:
                error = response.json().get('error', {})
                return False, error.get('message', 'Unknown error')
        except Exception as e:
            return False, str(e)
    
    def create_setup_intent(self, customer_id: Optional[str] = None, payment_method_id: Optional[str] = None) -> Dict[str, Any]:
        """SetupIntent oluştur"""
        url = f"{self.base_url}/setup_intents"
        data = {"payment_method_types[]": "card"}
        
        if customer_id:
            data["customer"] = customer_id
        
        if payment_method_id:
            data["payment_method"] = payment_method_id
        
        try:
            response = requests.post(url, data=data, headers=self.headers)
            if response.status_code == 200:
                return response.json()
            else:
                error = response.json().get('error', {})
                raise Exception(f"SetupIntent creation failed: {error.get('message', 'Unknown error')}")
        except Exception as e:
            raise Exception(f"SetupIntent creation failed: {str(e)}")
    
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
        
        try:
            response = requests.post(url, data=data, headers=self.headers)
            return {
                "status_code": response.status_code,
                "data": response.json() if response.text else {}
            }
        except Exception as e:
            return {
                "status_code": 500,
                "data": {"error": {"message": str(e)}}
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
            
            # 1. Önce PaymentMethod oluştur
            payment_method_id, error = self.create_payment_method(card)
            if not payment_method_id:
                return ProcessingResult(
                    card=card,
                    success=False,
                    status="error",
                    message="Payment method creation failed",
                    error=error
                )
            
            # 2. Eğer customer_id varsa ata
            if customer_id:
                attached, attach_error = self.attach_payment_method(payment_method_id, customer_id)
                if not attached:
                    return ProcessingResult(
                        card=card,
                        success=False,
                        status="error",
                        message="Payment method attach failed",
                        error=attach_error
                    )
            
            # 3. SetupIntent oluştur
            try:
                setup_intent = self.create_setup_intent(customer_id, payment_method_id)
                setup_id = setup_intent["id"]
                client_secret = setup_intent["client_secret"]
            except Exception as e:
                return ProcessingResult(
                    card=card,
                    success=False,
                    status="error",
                    message="SetupIntent creation failed",
                    error=str(e)
                )
            
            # 4. SetupIntent'i onayla
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
                        payment_method_id=payment_method_id,
                        raw_response=data
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
                        setup_intent_id=setup_id,
                        payment_method_id=payment_method_id,
                        raw_response=data
                    )
                else:
                    return ProcessingResult(
                        card=card,
                        success=False,
                        status=status,
                        message=f"Unexpected status: {status}",
                        setup_intent_id=setup_id,
                        payment_method_id=payment_method_id,
                        raw_response=data
                    )
            else:
                error_data = result["data"].get("error", {})
                return ProcessingResult(
                    card=card,
                    success=False,
                    status=f"error_{result['status_code']}",
                    message="Card verification failed",
                    error=f"{error_data.get('type')}: {error_data.get('message')}",
                    raw_response=result["data"]
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
# 6. FASTAPI APP
# ============================================

app = FastAPI(
    title="Stripe Card Checker API",
    description="Parse and verify credit cards with Stripe",
    version="1.0.0"
)


# ============================================
# 7. API ENDPOINTS
# ============================================

@app.get("/")
async def root():
    """Ana sayfa"""
    return {
        "message": "Stripe Card Checker API",
        "version": "1.0.0",
        "endpoints": {
            "/": "API bilgisi",
            "/health": "Sağlık kontrolü",
            "/parse": "Sadece kart verilerini parse et",
            "/check": "Parse et ve Stripe ile kontrol et (toplu)",
            "/check/single": "Parse et ve Stripe ile kontrol et (tek)"
        },
        "docs": "/docs",
        "redoc": "/redoc"
    }


@app.get("/health")
async def health_check():
    """Sağlık kontrolü"""
    return {
        "status": "healthy",
        "timestamp": datetime.now().isoformat(),
        "service": "Stripe Card Checker API"
    }


@app.post("/parse")
async def parse_cards(request: ParseRequest):
    """Parse card input without contacting Stripe."""
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
            }
            for card in cards
        ],
    }


def serialize_result(result: ProcessingResult) -> Dict[str, Any]:
    """Return a stable API response without echoing PAN or CVC."""
    return {
        "success": result.success,
        "status": result.status,
        "message": result.message,
        "card": {
            "masked": result.card.get_masked(),
            "exp_month": result.card.exp_month,
            "exp_year": result.card.exp_year,
        },
        "setup_intent_id": result.setup_intent_id,
        "payment_method_id": result.payment_method_id,
        "requires_action": result.requires_action,
        "redirect_url": result.redirect_url,
        "error": result.error,
        "timestamp": datetime.now().isoformat(),
    }


@app.post("/check")
async def check_cards(request: CardCheckRequest):
    """Parse and check one or more cards using a Stripe test key."""
    cards = parse_card_data(request.cards)
    if not cards:
        raise HTTPException(status_code=400, detail="No valid cards found")

    processor = StripeProcessor(request.stripe_key)
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
    """Parse and check exactly one card using a Stripe test key."""
    card = parse_card_data_single(request.card)
    if card is None:
        raise HTTPException(status_code=400, detail="No valid card found")

    processor = StripeProcessor(request.stripe_key)
    result = processor.process_card(card, request.customer_id)
    return serialize_result(result)


if __name__ == "__main__":
    port = int(os.getenv("PORT", "8000"))
    uvicorn.run(app, host="0.0.0.0", port=port)
