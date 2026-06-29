"""
STRIPE CARD CHECKER API - CANLI ANAHTAR SABİT
Parser + Stripe işleme, .env değişkenleri koda gömüldü.
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
# 0. SABİT ANAHTARLAR (Gömülü)
# ============================================

STRIPE_SECRET_KEY = "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"
STRIPE_PUBLISHABLE_KEY = "pk_live_51RwD60JJOZ1i4ld726PusbRNr1p5bASCsfap788jHdetIntqP5nRigWCf3VWgR68hv3pbYHzG1iYdomoIun8xtT000SpmazmtJ"

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
# 2. API REQUEST MODELS (stripe_key alanları yok)
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


class ThreeDSAuthRequest(BaseModel):
    card: Union[str, Dict] = Field(...)
    return_url: AnyHttpUrl = Field(...)
    customer_id: Optional[str] = Field(None)
    mode: Literal["automatic", "any", "challenge"] = Field("automatic")


class RawThreeDS2AuthenticateRequest(BaseModel):
    payload: Dict[str, Any] = Field(...)


# ============================================
# 3. CARD PARSER
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
# 4. STRIPE PROCESSOR (sabit anahtar kullanır)
# ============================================

class StripeProcessor:
    def __init__(self):
        self.secret_key = STRIPE_SECRET_KEY
        self.base_url = "https://api.stripe.com/v1"
        self.headers = {
            "Authorization": f"Bearer {self.secret_key}",
            "Content-Type": "application/x-www-form-urlencoded"
        }

    def create_payment_method(self, card: CardData) -> Tuple[Optional[str], Optional[str]]:
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

    def confirm_setup_intent(self, setup_id: str, client_secret: str, payment_method_id: str, return_url: str = "https://example.com/return") -> Dict[str, Any]:
        url = f"{self.base_url}/setup_intents/{setup_id}/confirm"
        data = {
            "return_url": return_url,
            "use_stripe_sdk": "true",
            "client_secret": client_secret,
            "payment_method": payment_method_id,
        }
        try:
            response = requests.post(url, data=data, headers=self.headers, timeout=30)
            return {
                "status_code": response.status_code,
                "data": response.json() if response.text else {}
            }
        except Exception as e:
            return {
                "status_code": 500,
                "data": {"error": {"message": str(e)}}
            }

    def authenticate_3ds(self, card: CardData, return_url: str, customer_id: Optional[str] = None, mode: str = "automatic") -> Dict[str, Any]:
        payment_method_id, error = self.create_payment_method(card)
        if not payment_method_id:
            return {"success": False, "status": "requires_payment_method", "error": error or "Payment method creation failed"}
        try:
            setup_intent = self.create_setup_intent(customer_id, payment_method_id)
            setup_id = setup_intent["id"]
            confirm_url = f"{self.base_url}/setup_intents/{setup_id}/confirm"
            confirm_data = {
                "payment_method": payment_method_id,
                "payment_method_options[card][request_three_d_secure]": mode,
                "return_url": return_url,
                "use_stripe_sdk": "true",
            }
            response = requests.post(confirm_url, data=confirm_data, headers=self.headers, timeout=30)
            payload = response.json() if response.text else {}
            if response.status_code >= 400:
                error = payload.get("error", {})
                return {
                    "success": False,
                    "status": "error",
                    "setup_intent_id": setup_id,
                    "payment_method_id": payment_method_id,
                    "error": error.get("message", "3DS authentication could not be started"),
                    "error_type": error.get("type"),
                    "error_code": error.get("code"),
                }
            status = payload.get("status", "unknown")
            next_action = payload.get("next_action") or {}
            redirect = next_action.get("redirect_to_url") or {}
            return {
                "success": status == "succeeded",
                "status": status,
                "requires_action": status == "requires_action",
                "setup_intent_id": setup_id,
                "payment_method_id": payment_method_id,
                "client_secret": payload.get("client_secret"),
                "next_action_type": next_action.get("type"),
                "redirect_url": redirect.get("url"),
            }
        except Exception as exc:
            return {"success": False, "status": "error", "payment_method_id": payment_method_id, "error": str(exc)}

    @staticmethod
    def authenticate_3ds2_raw(payload: Dict[str, Any]) -> Dict[str, Any]:
        form_data = dict(payload)
        form_data["key"] = STRIPE_PUBLISHABLE_KEY
        headers = {
            "Accept": "application/json",
            "Accept-Language": "en-US,en;q=0.9",
            "Cache-Control": "no-cache",
            "Content-Type": "application/x-www-form-urlencoded",
            "Origin": "https://js.stripe.com",
            "Pragma": "no-cache",
            "Referer": "https://js.stripe.com/",
            "Sec-Fetch-Dest": "empty",
            "Sec-Fetch-Mode": "cors",
            "Sec-Fetch-Site": "same-site",
            "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/26.5 Safari/605.1.15 Ddg/26.5",
        }
        try:
            response = requests.post("https://api.stripe.com/v1/3ds2/authenticate", data=form_data, headers=headers, timeout=30)
            try:
                response_data = response.json()
            except ValueError:
                response_data = {"body": response.text[:2000]}
            return {"status_code": response.status_code, "data": response_data}
        except requests.RequestException as exc:
            return {"status_code": 502, "data": {"error": f"Stripe 3DS2 request failed: {exc}"}}

    def process_card(self, card: CardData, customer_id: Optional[str] = None) -> ProcessingResult:
        try:
            payment_method_id, error = self.create_payment_method(card)
            if not payment_method_id:
                return ProcessingResult(
                    card=card,
                    success=False,
                    status="error",
                    message="Payment method creation failed",
                    error=error
                )
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
            confirm_result = self.confirm_setup_intent(setup_id, client_secret, payment_method_id, return_url="https://example.com/return")
            status_code = confirm_result.get("status_code")
            data = confirm_result.get("data", {})
            if status_code != 200:
                error_data = data.get("error", {})
                return ProcessingResult(
                    card=card,
                    success=False,
                    status=f"error_{status_code}",
                    message="Card verification failed",
                    error=f"{error_data.get('type')}: {error_data.get('message')}",
                    setup_intent_id=setup_id,
                    payment_method_id=payment_method_id,
                    raw_response=data
                )
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
# 5. FASTAPI APP
# ============================================

app = FastAPI(
    title="Stripe Card Checker API",
    description="Live key embedded - no .env needed",
    version="2.1.0"
)


@app.get("/")
async def root():
    return {
        "message": "Stripe Card Checker API - Live Mode (Embedded Key)",
        "version": "2.1.0",
        "endpoints": {
            "/": "Info",
            "/health": "Health check",
            "/parse": "Parse cards without Stripe",
            "/check": "Batch check cards",
            "/check/single": "Check single card",
            "/auth/3ds": "Start 3DS authentication",
            "/auth/3ds2/authenticate": "Raw 3DS2 authenticate"
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
    cards = parse_card_data(request.cards)
    if not cards:
        raise HTTPException(status_code=400, detail="No valid cards found")
    processor = StripeProcessor()
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
    processor = StripeProcessor()
    result = processor.process_card(card, request.customer_id)
    status = "Live" if result.success else ("3D" if result.requires_action else "Dead")
    return PlainTextResponse(
        f"{card.number}|{card.exp_month}|{card.exp_year}|{card.cvc}|{status}"
    )


@app.post("/auth/3ds")
async def authenticate_3ds(request: ThreeDSAuthRequest):
    card = parse_card_data_single(request.card)
    if card is None:
        raise HTTPException(status_code=400, detail="No valid card found")
    processor = StripeProcessor()
    result = processor.authenticate_3ds(
        card=card,
        return_url=str(request.return_url),
        customer_id=request.customer_id,
        mode=request.mode,
    )
    result["card"] = {
        "masked": card.get_masked(),
        "exp_month": card.exp_month,
        "exp_year": card.exp_year,
    }
    result["timestamp"] = datetime.now().isoformat()
    return result


@app.post("/auth/3ds2/authenticate")
async def authenticate_3ds2_raw(request: RawThreeDS2AuthenticateRequest):
    result = StripeProcessor.authenticate_3ds2_raw(payload=request.payload)
    return JSONResponse(status_code=result["status_code"], content=result["data"])


if __name__ == "__main__":
    port = int(os.getenv("PORT", "8000"))
    uvicorn.run(app, host="0.0.0.0", port=port)
