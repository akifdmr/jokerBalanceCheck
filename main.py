"""
PAYPAL CARD CHECKER API + AUTHORIZE.NET - FASTAPI v12.2.0
- Live Check (PayPal Vault Setup + Confirm)
- Adaptif Balance Check (PayPal Authorization + Void)
- Authorize.net Auth Only + Capture
- Çoklu format desteği (JSON, pipe, CSV, space, tuple, vb.)
- Her kart işlemi arasında 2-3 saniye bekleme (rate-limit)
- Async HTTP (httpx), Idempotency, Gelişmiş hata yönetimi
- MongoDB ile kart önbellekleme (live_cards)
Swagger: /docs
"""
import os
import re
import json
import uuid
import logging
import asyncio
from typing import Dict, List, Optional, Tuple, Any, Union
from datetime import datetime, timedelta
from dataclasses import dataclass, field
from enum import Enum

from fastapi import FastAPI, HTTPException, Body
from fastapi.responses import PlainTextResponse, JSONResponse
from pydantic import BaseModel, Field, validator

from pymongo import MongoClient
from pymongo.errors import ConnectionFailure, CollectionInvalid

import uvicorn
import base64
import httpx



from authorizenet import apicontractsv1
from authorizenet.apicontrollers import (
    createTransactionController,
    createCustomerProfileController,
    createCustomerPaymentProfileController,
)# ==================== LOGGING ====================
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(name)s - %(message)s',
    datefmt='%Y-%m-%d %H:%M:%S'
)
logger = logging.getLogger(__name__)

# ==================== KONFIGÜRASYON ====================
CONFIG = {
    # PayPal
    'paypal_client_id': 'AexXX36fYkQu_BFsmXISn-6ZRZaU6_Lm-q2BmsCLPLqiz3zt7lhKxc3x13UTWXADXkonA8wbeNKY0ZDW',
    'paypal_client_secret': 'EDdrMKnpxsjdk_MaGcAbTUP_boVPh8jx0w1HNu8c18nbC2j8nL0b1FFYjH0eJSFcPyewmDQv6T0as9n5',
    'paypal_api_base': 'https://api-m.paypal.com',
    # MongoDB
    'mongo_uri': 'mongodb+srv://cardmarketApp:gnbqHdTrlceMZjOS@paymentmanger.gvaavzc.mongodb.net/mydb?retryWrites=true&w=majority',
    'mongo_database': 'mydb',
    'mongo_bin_collection': 'binList',
    'mongo_live_collection': 'live_cards',
    # PayPal Balance
    'balance_start_amount': 1000.0,
    'balance_step': 100.0,
    'balance_max_attempts': 10,
    'request_delay_seconds': 2.5,
    # Authorize.net
    'authorize_api_login_id': '6Px6beH4B4T',
    'authorize_transaction_key': '34677Ck24M5zvuTM',
    'authorize_public_client_key': '4gxGF4UKy6F2hg6t7G3nCZGnq73x4sPKTAFeFrhmVkK9wpb84s8X763xdz84d4Uy',
}

# ==================== PAYPAL HATA KODLARI ====================
class PayPalError(Enum):
    INSTRUMENT_DECLINED = ("INSTRUMENT_DECLINED", "Kart reddedildi (yetersiz bakiye veya geçersiz kart)")
    PAYER_ACTION_REQUIRED = ("PAYER_ACTION_REQUIRED", "Ek doğrulama gerekli (3D Secure)")
    DO_NOT_HONOR = ("DO_NOT_HONOR", "Banka işlemi onaylamadı")
    SOFT_DECLINE = ("SOFT_DECLINE", "Geçici red, tekrar deneyin")
    INVALID_REQUEST = ("INVALID_REQUEST", "Geçersiz istek")
    UNAUTHORIZED = ("UNAUTHORIZED", "Yetkilendirme hatası")
    INTERNAL_SERVER_ERROR = ("INTERNAL_SERVER_ERROR", "PayPal sunucu hatası")
    UNKNOWN = ("UNKNOWN", "Bilinmeyen hata")

    @classmethod
    def from_paypal_code(cls, code: str) -> 'PayPalError':
        for error in cls:
            if error.value[0] == code:
                return error
        return cls.UNKNOWN

    def get_message(self) -> str:
        return self.value[1]

# ==================== PYDANTIC MODELS ====================
class CardRequest(BaseModel):
    number: str = Field(..., example="5549601721207035")
    exp_month: str = Field(..., example="08")
    exp_year: str = Field(..., example="2026")
    cvc: str = Field(..., example="319")
    name: Optional[str] = "Test User"
    country: Optional[str] = "TR"
    zip: Optional[str] = "00000"
    email: Optional[str] = None
    phone: Optional[str] = None
    dob: Optional[str] = None
    ip: Optional[str] = None
    user_agent: Optional[str] = None

    @validator('exp_month')
    def validate_exp_month(cls, v):
        v = str(v).strip()
        if len(v) == 1:
            v = f"0{v}"
        return v

    @validator('exp_year')
    def validate_exp_year(cls, v):
        v = str(v).strip()
        if len(v) == 2:
            v = f"20{v}"
        return v

    @validator('number')
    def validate_number(cls, v):
        v = re.sub(r'[^0-9]', '', str(v))
        if len(v) < 15 or len(v) > 16:
            raise ValueError(f"Geçersiz kart numarası uzunluğu: {len(v)}")
        return v

    @validator('cvc')
    def validate_cvc(cls, v):
        v = re.sub(r'[^0-9]', '', str(v))
        if len(v) < 3 or len(v) > 4:
            raise ValueError(f"Geçersiz CVC uzunluğu: {len(v)}")
        return v

class BinCheckRequest(BaseModel):
    bins: Union[str, List[str]] = Field(..., description="Tek veya çoklu BIN (kart numarası veya ilk 6 hane)")

class AuthOnlyRequest(BaseModel):
    amount: float = Field(..., gt=0, description="Yetkilendirme miktarı")
    card_number: str = Field(..., min_length=15, max_length=16, description="Kredi kartı numarası")
    exp_date: str = Field(..., pattern=r'^\d{4}-\d{2}$', example="2026-08", description="YYYY-MM formatında son kullanma tarihi")
    cvv: str = Field(..., min_length=3, max_length=4, description="Güvenlik kodu")
    first_name: Optional[str] = Field("John", description="Ad")
    last_name: Optional[str] = Field("Doe", description="Soyad")
    address: Optional[str] = Field("123 Main St", description="Adres")
    city: Optional[str] = Field("Anytown", description="Şehir")
    state: Optional[str] = Field("CA", description="Eyalet")
    zip: Optional[str] = Field("12345", description="Posta kodu")
    country: Optional[str] = Field("USA", description="Ülke")
    invoice_number: Optional[str] = Field("INV-001", description="Fatura numarası")
    description: Optional[str] = Field("Test Auth Only", description="Açıklama")

class CaptureRequest(BaseModel):
    transaction_id: str = Field(..., description="Yetkilendirme işleminden alınan transaction ID")
    amount: float = Field(..., gt=0, description="Yakalanacak miktar")

# --- Yeni: Live/Balance için tek bir string alanı ---
class CheckRequest(BaseModel):
    data: str = Field(
        ...,
        description="""
Kart bilgilerini aşağıdaki formatlardan biriyle gönderin (çoklu kartlar için her satıra bir kart):

- PIPE: 5549601721207035|08|2026|319
- CSV: 5549601721207035,08/2026,319
- SPACE: 5549601721207035 08/26 319
- JSON: {"number":"5549601721207035","exp_month":"08","exp_year":"2026","cvc":"319"}
- TUPLE: ('John Doe','555-1234','5549601721207035','08','2026','319','...')
- FULL PIPE: 5549601721207035|08|2026|319|John Doe|...|...|...|...|...|phone|email|dob|ip|user_agent
        """,
        example="5549601721207035|08|2026|319"
    )

# ==================== MONGODB ====================
class MongoDB:
    _instance = None

    def __new__(cls):
        if cls._instance is None:
            cls._instance = super(MongoDB, cls).__new__(cls)
            cls._instance._initialize()
        return cls._instance

    def _initialize(self):
        try:
            self.client = MongoClient(CONFIG['mongo_uri'])
            self.client.admin.command('ping')
            self.db = self.client[CONFIG['mongo_database']]
            self.bin_collection = self.db[CONFIG['mongo_bin_collection']]
            self.live_cards = self.db[CONFIG['mongo_live_collection']]

            try:
                self.live_cards.create_index('card_number', unique=True)
            except CollectionInvalid:
                pass
            self.live_cards.create_index('check_id', unique=True)
            self.live_cards.create_index('payment_token')
            self.bin_collection.create_index('BIN', unique=True)

            logger.info('✅ MongoDB bağlantısı başarılı')
        except ConnectionFailure as e:
            logger.error(f'❌ MongoDB bağlantı hatası: {str(e)}')
            raise
        except Exception as e:
            logger.error(f'❌ MongoDB başlatma hatası: {str(e)}')
            raise

    def get_bin_info(self, card_number: str) -> Optional[Dict]:
        try:
            for bin_prefix in [card_number[:6], card_number[:5], card_number[:4]]:
                result = self.bin_collection.find_one({'BIN': bin_prefix})
                if result:
                    if '_id' in result:
                        result['_id'] = str(result['_id'])
                    return {
                        'bin': result.get('BIN'),
                        'brand': result.get('Brand', 'UNKNOWN'),
                        'type': result.get('Type', 'UNKNOWN'),
                        'level': result.get('Category', 'STANDARD'),
                        'bank': result.get('Issuer', 'Unknown'),
                        'country': result.get('isoCode2', 'XX'),
                        'country_name': result.get('CountryName', 'Unknown'),
                        'issuer_phone': result.get('IssuerPhone', ''),
                        'issuer_url': result.get('IssuerUrl', ''),
                        'raw': result
                    }
            return None
        except Exception as e:
            logger.error(f'❌ BIN sorgulama hatası: {str(e)}')
            return None

    def get_card_by_number(self, card_number: str) -> Optional[Dict]:
        try:
            clean = re.sub(r'[^0-9]', '', card_number)
            result = self.live_cards.find_one({'card_number': clean})
            if result and '_id' in result:
                result['_id'] = str(result['_id'])
            return result
        except Exception as e:
            logger.error(f'get_card_by_number hatası: {e}')
            return None

    def upsert_live_card(self, data: Dict) -> str:
        try:
            now = datetime.now().isoformat()
            data['updated_at'] = now
            if 'created_at' not in data:
                data['created_at'] = now

            result = self.live_cards.update_one(
                {'card_number': data['card_number']},
                {'$set': data},
                upsert=True
            )
            logger.info(f'✅ Kart güncellendi/kaydedildi: {data.get("card_number")}')
            return str(result.upserted_id) if result.upserted_id else None
        except Exception as e:
            logger.error(f'upsert_live_card hatası: {e}')
            raise

    def update_card_balance(self, card_number: str, balance_data: Dict) -> bool:
        try:
            clean = re.sub(r'[^0-9]', '', card_number)
            update = {
                '$set': {
                    'balance_status': balance_data.get('status'),
                    'balance_amount': balance_data.get('amount'),
                    'balance_currency': balance_data.get('currency'),
                    'balance_auth_id': balance_data.get('auth_id'),
                    'last_balance_check': datetime.now().isoformat(),
                    'updated_at': datetime.now().isoformat()
                }
            }
            result = self.live_cards.update_one({'card_number': clean}, update)
            return result.modified_count > 0
        except Exception as e:
            logger.error(f'update_card_balance hatası: {e}')
            return False

    def get_record(self, check_id: str) -> Optional[Dict]:
        result = self.live_cards.find_one({'check_id': check_id})
        if result and '_id' in result:
            result['_id'] = str(result['_id'])
        return result

    def get_bin_by_prefix(self, bin_prefix: str) -> Optional[Dict]:
        try:
            result = self.bin_collection.find_one({'BIN': bin_prefix})
            if result and '_id' in result:
                result['_id'] = str(result['_id'])
            return result
        except Exception as e:
            logger.error(f'get_bin_by_prefix hatası: {e}')
            return None

try:
    mongo_db = MongoDB()
except Exception as e:
    logger.error(f'❌ MongoDB başlatılamadı: {str(e)}')
    mongo_db = None

# ==================== PARSER ====================
@dataclass
class CardData:
    number: str
    exp_month: str
    exp_year: str
    cvc: str
    name: str = "Test User"
    country: str = "TR"
    zip: str = "00000"
    email: Optional[str] = None
    phone: Optional[str] = None
    dob: Optional[str] = None
    ip: Optional[str] = None
    user_agent: Optional[str] = None
    metadata: Dict[str, Any] = field(default_factory=dict)

    def get_masked(self) -> str:
        return f"{self.number[:4]}****{self.number[-4:]}"

    def get_bin(self) -> str:
        return self.number[:6]

class CardParser:
    @staticmethod
    def parse(data: Union[str, List, Dict]) -> List[CardData]:
        if isinstance(data, (list, dict)):
            return CardParser._parse_json(data)
        if isinstance(data, str):
            data = data.strip()
            if not data:
                return []

            if data.startswith('[') or data.startswith('{'):
                try:
                    json_data = json.loads(data)
                    return CardParser._parse_json(json_data)
                except:
                    pass

            lines = data.split('\n')
            if lines and lines[0].strip().startswith('('):
                return CardParser._parse_tuple_format(data)

            if '|' in data and len(data.split('|')) > 10:
                return CardParser._parse_full_pipe(data)

            if '|' in data:
                return CardParser._parse_pipe(data)

            if ',' in data and '\n' in data:
                return CardParser._parse_csv(data)

            if ' ' in data and '\n' in data:
                return CardParser._parse_space(data)

        return []

    @staticmethod
    def _parse_pipe(data: str) -> List[CardData]:
        cards = []
        for line in data.strip().split('\n'):
            line = line.strip()
            if not line:
                continue
            parts = line.split('|')
            if len(parts) >= 3:
                number = parts[0].strip()
                # ✅ DÜZELTİLDİ: 4 parçalı format (pan|ay|yıl|cvc) desteği
                if len(parts) >= 4 and parts[1].strip().isdigit() and len(parts[1].strip()) == 2 and parts[2].strip().isdigit() and (len(parts[2].strip()) == 2 or len(parts[2].strip()) == 4):
                    exp_month = parts[1].strip()
                    exp_year = parts[2].strip()
                    cvc = parts[3].strip()
                    if len(exp_year) == 2:
                        exp_year = f"20{exp_year}"
                else:
                    # Standart format (pan|ay/yıl|cvc)
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
        for line in data.strip().split('\n'):
            line = line.strip()
            if not line:
                continue
            parts = line.split('|')
            if len(parts) < 3:
                continue

            number = parts[0].strip() if parts[0] else None
            exp_part = parts[1].strip() if len(parts) > 1 else None
            cvc = parts[2].strip() if len(parts) > 2 else None
            name = parts[3].strip() if len(parts) > 3 and parts[3] else "Test User"

            phone = None
            if len(parts) > 10 and parts[10].strip():
                phone = parts[10].strip()
            elif len(parts) > 9 and parts[9].strip():
                phone = parts[9].strip()

            email = None
            if len(parts) > 11 and parts[11].strip():
                email = parts[11].strip()
            elif len(parts) > 10 and parts[10].strip() and "@" in parts[10]:
                email = parts[10].strip()

            dob = None
            if len(parts) > 12 and parts[12].strip() and parts[12].strip() != "--":
                dob = parts[12].strip()
            elif len(parts) > 11 and parts[11].strip() and parts[11].strip() != "--":
                dob = parts[11].strip()

            ip = None
            if len(parts) > 13 and parts[13].strip():
                ip = parts[13].strip()
            elif len(parts) > 12 and parts[12].strip():
                ip = parts[12].strip()

            user_agent = None
            if len(parts) > 14 and parts[14].strip():
                user_agent = parts[14].strip()
            elif len(parts) > 13 and parts[13].strip():
                user_agent = parts[13].strip()

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
    def _parse_csv(data: str) -> List[CardData]:
        cards = []
        lines = data.strip().split('\n')
        if not lines:
            return cards
        header = lines[0].lower() if lines else ""
        has_header = 'card' in header or 'number' in header or 'cc' in header
        start_idx = 1 if has_header else 0
        for line in lines[start_idx:]:
            line = line.strip()
            if not line:
                continue
            parts = line.split(',')
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
    def _parse_space(data: str) -> List[CardData]:
        cards = []
        for line in data.strip().split('\n'):
            line = line.strip()
            if not line:
                continue
            if '|' in line:
                return CardParser._parse_pipe(data)
            parts = line.split()
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
    def _parse_tuple_format(data: str) -> List[CardData]:
        cards = []
        for line in data.strip().split('\n'):
            line = line.strip()
            if not line or not line.startswith('('):
                continue
            try:
                line_clean = line.replace('(', '').replace(')', '').strip()
                parts = [p.strip().strip("'\"") for p in line_clean.split(',')]
                if len(parts) >= 7:
                    card_number = parts[3].strip()
                    exp_month = str(parts[4]).strip()
                    exp_year = str(parts[5]).strip()
                    cvc = str(parts[6]).strip()
                    name = parts[1].strip() if len(parts) > 1 else "Test User"
                    phone = parts[2].strip() if len(parts) > 2 else None
                    if card_number and exp_month and exp_year and cvc:
                        if len(exp_year) == 2:
                            exp_year = f"20{exp_year}"
                        if len(exp_month) == 1:
                            exp_month = f"0{exp_month}"
                        cards.append(CardData(
                            number=card_number,
                            exp_month=exp_month,
                            exp_year=exp_year,
                            cvc=cvc,
                            name=name,
                            phone=phone
                        ))
            except Exception as e:
                logger.warning(f"Tuple parse hatası: {e} - line: {line}")
                continue
        return cards

    @staticmethod
    def _parse_expiration(exp_str: str) -> Tuple[Optional[str], Optional[str]]:
        exp_str = exp_str.strip()
        for sep in ['/', '-', '|', ' ', '.']:
            if sep in exp_str:
                parts = exp_str.split(sep)
                if len(parts) == 2:
                    month = parts[0].strip()
                    year = parts[1].strip()
                    if len(month) == 1:
                        month = f"0{month}"
                    if len(year) == 2:
                        year = f"20{year}"
                    elif len(year) == 4:
                        year = year
                    if month.isdigit() and year.isdigit():
                        return month, year
        if exp_str.isdigit():
            if len(exp_str) == 4:
                return exp_str[:2], f"20{exp_str[2:]}"
            elif len(exp_str) == 6:
                return exp_str[:2], exp_str[2:]
        return None, None

    @staticmethod
    def _extract_from_json_item(item: Dict) -> Optional[CardData]:
        if 'number' in item:
            number = item.get('number')
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
        if 'CreditCard' in item:
            cc = item['CreditCard']
            number = cc.get('CardNumber') or cc.get('number')
            exp = cc.get('Exp') or cc.get('exp') or cc.get('expiration') or cc.get('CardExpDate')
            cvc = cc.get('CVV') or cc.get('cvv') or cc.get('cvc') or cc.get('CardCCV2')
            name = cc.get('Name') or cc.get('CardHolderName') or "Test User"
            if number and exp and cvc:
                exp_month, exp_year = CardParser._parse_expiration(str(exp))
                if exp_month and exp_year:
                    return CardData(
                        number=str(number),
                        exp_month=exp_month,
                        exp_year=exp_year,
                        cvc=str(cvc),
                        name=name
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
        return None

# ==================== YARDIMCI FONKSİYONLAR ====================
def detect_card_brand(card_number: str) -> str:
    patterns = {
        'VISA': r'^4',
        'MASTERCARD': r'^(5[1-5]|2[2-7])',
        'AMEX': r'^(34|37)',
        'DISCOVER': r'^(6011|65|64[4-9]|622)',
        'JCB': r'^35',
        'DINERS': r'^3(0[0-5]|[68])'
    }
    for brand, pattern in patterns.items():
        if re.match(pattern, card_number):
            return brand
    return 'UNKNOWN'

def build_pipe_response(result: Dict) -> str:
    pan = result.get('card_number', '')
    exp = result.get('exp_month', '')
    exp_year = result.get('exp_year', '')
    cvc = result.get('cvc', '')
    token = result.get('payment_token', '') or result.get('setup_token', '')
    bin_info = result.get('bin_info', {})
    country = bin_info.get('country', '') or result.get('card_country_code', '')
    issuer = bin_info.get('bank', '') or result.get('card_issuer', '')
    card_type = bin_info.get('type', '') or result.get('card_type', '')
    level = bin_info.get('level', '') or result.get('card_level', '')
    balance_status = result.get('balance_status', '')
    balance_amount = result.get('balance_amount')
    if balance_status == 'SUCCESS' and balance_amount is not None:
        balance = str(balance_amount)
    elif balance_status == 'FAILED':
        balance = 'FAILED'
    else:
        balance = ''
    return f"{pan}|{exp}|{exp_year}|{cvc}|{token}|{country}|{issuer}|{card_type}|{level}|{balance}"

# ==================== PAYPAL PROCESSOR (ASYNC) ====================
class PayPalProcessor:
    def __init__(self):
        self.client_id = CONFIG['paypal_client_id']
        self.client_secret = CONFIG['paypal_client_secret']
        self.api_base = CONFIG['paypal_api_base']
        self.access_token = None
        self.token_expiry = None
        self.client = httpx.AsyncClient(timeout=30.0)

    async def _get_access_token(self) -> str:
        try:
            if self.access_token and self.token_expiry and datetime.now() < self.token_expiry:
                return self.access_token
            auth = base64.b64encode(f"{self.client_id}:{self.client_secret}".encode()).decode()
            headers = {
                "Authorization": f"Basic {auth}",
                "Content-Type": "application/x-www-form-urlencoded"
            }
            data = {"grant_type": "client_credentials"}
            logger.info("RAW JSON")
            logger.info(json.dumps(data))
            response = await self.client.post(
                f"{self.api_base}/v1/oauth2/token",
                headers=headers,
                data=data
            )
            if response.status_code == 200:
                result = response.json()
                self.access_token = result.get("access_token")
                expires_in = result.get("expires_in", 3600)
                self.token_expiry = datetime.now() + timedelta(seconds=expires_in - 60)
                logger.info("✅ PayPal Access Token alındı")
                return self.access_token
            else:
                logger.error(f"❌ Access Token hatası: {response.text}")
                raise Exception(f"PayPal auth failed: {response.text}")
        except Exception as e:
            logger.error(f"❌ Token exception: {str(e)}")
            raise

    async def _confirm_setup_token(
        self,
        setup_token: str,
        check_id: str
    ) -> Tuple[bool, Optional[str], Optional[str], Optional[Dict]]:

        try:

            if not setup_token:
                return False, None, "Setup token boş.", None

            if not self.access_token:
                await self._get_access_token()

            url = f"{self.api_base}/v3/vault/payment-tokens"

            headers = {
                "Authorization": f"Bearer {self.access_token}",
                "Content-Type": "application/json",
                "Accept": "application/json",
                "Prefer": "return=representation",
                "PayPal-Request-Id": str(uuid.uuid4())
            }

            # PayPal'ın resmi dökümantasyonundaki payload
            payload = {
                "payment_source": {
                    "token": {
                        "id": setup_token,
                        "type": "SETUP_TOKEN"
                    }
                }
            }

            logger.info("=" * 120)
            logger.info("PAYMENT TOKEN REQUEST")
            logger.info(f"URL       : {url}")
            logger.info(f"SetupToken: {setup_token}")
            logger.info(f"Check ID  : {check_id}")
            logger.info("Headers:")
            logger.info(json.dumps(
                {k: ("***" if k == "Authorization" else v) for k, v in headers.items()},
                indent=4
            ))
            logger.info("Payload:")
            logger.info(json.dumps(payload, indent=4))
            logger.info("=" * 120)

            response = await self.client.post(
                url,
                headers=headers,
                json=payload
            )

            logger.info("=" * 120)
            logger.info("PAYMENT TOKEN RESPONSE")
            logger.info(f"HTTP Status : {response.status_code}")
            logger.info(response.text)
            logger.info("=" * 120)

            try:
                result = response.json()
            except Exception:
                result = {
                    "raw": response.text
                }

            if response.status_code in (200, 201):

                payment_token = result.get("id")
                status = result.get("status")

                logger.info(f"✅ Payment Token : {payment_token}")
                logger.info(f"✅ Status        : {status}")

                return (
                    True,
                    payment_token,
                    None,
                    result
                )

            logger.error("=" * 120)
            logger.error("PAYPAL ERROR")
            logger.error(json.dumps(result, indent=4))
            logger.error("=" * 120)

            return (
                False,
                None,
                result.get("message", response.text),
                result
            )

        except Exception as ex:
            logger.exception("PAYMENT TOKEN EXCEPTION")
            return (
                False,
                None,
                str(ex),
                None
            )
    async def verify_card(self, card: CardData, bin_info: Dict = None, check_id: str = None) -> Tuple[bool, Optional[str], Optional[str], Optional[str], Optional[Dict]]:
        try:
            if not self.access_token:
                await self._get_access_token()
            if not check_id:
                check_id = str(uuid.uuid4())

            setup_url = f"{self.api_base}/v3/vault/setup-tokens"
            country_code = bin_info.get('country', 'US') if bin_info else 'US'
            zip_code = "00000"

            raw_name = card.name.strip() if card.name else "Test User"
            clean_name = re.sub(r'[^a-zA-ZğüşıöçĞÜŞİÖÇ\s-]', '', raw_name)
            if not clean_name or clean_name.strip() == "":
                clean_name = "Test User"
            clean_name = re.sub(r'\s+', ' ', clean_name).strip()
            if len(clean_name) > 32:
                clean_name = clean_name[:32]

            expiry = f"{card.exp_year}-{card.exp_month}"
            number = re.sub(r'[^0-9]', '', card.number)
            cvc = re.sub(r'[^0-9]', '', card.cvc)

            payload = {
                "payment_source": {
                    "card": {
                        "number": number,
                        "expiry": expiry,
                        "security_code": cvc,
                        "name": clean_name,
                        "billing_address": {
                            "address_line_1": "123 Main St",
                            "admin_area_2": "Istanbul",
                            "postal_code": zip_code,
                            "country_code": country_code
                        }
                    }
                }
            }
            if not clean_name or clean_name == "":
                del payload["payment_source"]["card"]["name"]

            headers = {
                "Authorization": f"Bearer {self.access_token}",
                "Content-Type": "application/json",
                "Accept": "application/json",
                "PayPal-Request-Id": check_id,
                "Prefer": "return=representation"
            }
            logger.info(f"🔄 PayPal Setup Token oluşturuluyor... check_id: {check_id}")

            response = await self.client.post(setup_url, json=payload, headers=headers)
            logger.info("=" * 80)
            logger.info("SETUP TOKEN RESPONSE")
            logger.info(response.text)
            logger.info("=" * 80)

            if response.status_code in (200, 201):

                result = response.json()

                logger.info("=" * 100)
                logger.info("SETUP TOKEN RESPONSE")
                logger.info(json.dumps(result, indent=4))
                logger.info("=" * 100)

                setup_token = result.get("id")
                status = result.get("status")

                if not setup_token:
                    logger.error("❌ Setup token alınamadı!")
                    return False, None, None, "Setup token alınamadı", result

                logger.info(f"✅ Setup Token : {setup_token}")
                logger.info(f"✅ Status      : {status}")

                # Status'u özellikle logla
                if status not in ["APPROVED", "VAULTED", "PAYER_ACTION_REQUIRED", "CREATED"]:
                    logger.warning(f"⚠️ Beklenmeyen Setup Token Status: {status}")

                confirm_success, payment_token, confirm_error, confirm_raw = await self._confirm_setup_token(
                    setup_token=setup_token,
                    check_id=check_id
                )

                if confirm_success:

                    logger.info("=" * 100)
                    logger.info("PAYMENT TOKEN SUCCESS")
                    logger.info(f"Setup Token   : {setup_token}")
                    logger.info(f"Payment Token : {payment_token}")

                    if confirm_raw:
                        logger.info(json.dumps(confirm_raw, indent=4))

                    logger.info("=" * 100)

                    return (
                        True,
                        setup_token,
                        payment_token,
                        None,
                        result
                    )

                else:

                    logger.warning("=" * 100)
                    logger.warning("PAYMENT TOKEN FAILED")
                    logger.warning(f"Setup Token : {setup_token}")
                    logger.warning(f"Error       : {confirm_error}")

                    if confirm_raw:
                        try:
                            logger.warning(json.dumps(confirm_raw, indent=4))
                        except Exception:
                            logger.warning(confirm_raw)

                    logger.warning("=" * 100)

                    # Setup Token oluştuğu için kartı yine LIVE kabul ediyorsun
                    return (
                        True,
                        setup_token,
                        None,
                        confirm_error,
                        result
                    )

            else:
                logger.error("=" * 100)
                logger.error("SETUP TOKEN FAILED")
                logger.error(f"HTTP Status : {response.status_code}")

                try:
                    error_json = response.json()
                    logger.error(json.dumps(error_json, indent=4))
                    error_text = error_json.get("message", response.text)
                except Exception:
                    logger.error(response.text)
                    error_text = response.text

                logger.error("=" * 100)

                return (
                    False,
                    None,
                    None,
                    error_text,
                    None
                )
        except Exception as e:
            logger.error(f"❌ PayPal exception: {str(e)}")
            return False, None, None, str(e), None

    async def _perform_authorization(self, payment_token: str, amount: float, currency: str, check_id: str) -> Tuple[bool, Optional[str], Optional[str], Optional[Dict], Optional[str]]:
        try:
            order_url = f"{self.api_base}/v2/checkout/orders"
            headers = {
                "Authorization": f"Bearer {self.access_token}",
                "Content-Type": "application/json",
                "PayPal-Request-Id": check_id,
            }
            payload = {
                "intent": "AUTHORIZE",
                "purchase_units": [{
                    "amount": {
                        "currency_code": currency,
                        "value": f"{amount:.2f}"
                    }
                }],
                "payment_source": {
                    "token": {
                        "id": payment_token,
                        "type": "PAYMENT_TOKEN"
                    }
                }
            }
            logger.info(f"🔄 Order oluşturuluyor (token: {payment_token}, amount: {amount})")
            response = await self.client.post(order_url, json=payload, headers=headers)
            if response.status_code not in [200, 201]:
                error_data = response.json() if response.text else {}
                error_code = error_data.get('details', [{}])[0].get('issue', 'UNKNOWN')
                error_msg = error_data.get('message', response.text)
                return False, None, error_msg, error_data, error_code

            order = response.json()
            order_id = order.get("id")
            status = order.get("status")

            if status == "CREATED":
                auth_url = f"{self.api_base}/v2/checkout/orders/{order_id}/authorize"
                auth_response = await self.client.post(auth_url, headers=headers)
                if auth_response.status_code in [200, 201]:
                    auth_data = auth_response.json()
                    auth_status = auth_data.get("status")
                    if auth_status == "COMPLETED":
                        authorizations = auth_data.get('purchase_units', [{}])[0].get('payments', {}).get('authorizations', [])
                        if authorizations:
                            auth_id = authorizations[0].get('id')
                            if auth_id:
                                logger.info(f"✅ Yetkilendirme başarılı: {auth_id}")
                                return True, auth_id, None, auth_data, None
                        if 'id' in auth_data:
                            return True, auth_data['id'], None, auth_data, None
                        return False, None, "Authorization ID bulunamadı", auth_data, None
                    else:
                        error_msg = f"Yetkilendirme başarısız: {auth_status}"
                        logger.error(f"❌ {error_msg}")
                        return False, None, error_msg, auth_data, None
                else:
                    error_data = auth_response.json() if auth_response.text else {}
                    error_code = error_data.get('details', [{}])[0].get('issue', 'UNKNOWN')
                    error_msg = error_data.get('message', auth_response.text)
                    return False, None, error_msg, error_data, error_code
            else:
                if status == "APPROVED":
                    return True, order_id, None, order, None
                else:
                    return False, None, f"Beklenmeyen sipariş durumu: {status}", order, None
        except Exception as e:
            logger.error(f"❌ Authorization exception: {str(e)}")
            return False, None, str(e), None, None

    async def perform_balance_check_with_algorithm(self, payment_token: str, check_id: str) -> Dict:
        start_amount = CONFIG['balance_start_amount']
        step = CONFIG['balance_step']
        max_attempts = CONFIG['balance_max_attempts']
        currency = "USD"

        current_amount = start_amount
        last_success_amount = None
        attempts = 0
        last_error = None
        last_error_code = None

        while attempts < max_attempts:
            attempts += 1
            success, auth_id, error, raw_data, error_code = await self._perform_authorization(payment_token, current_amount, currency, check_id)

            if success:
                if auth_id:
                    void_url = f"{self.api_base}/v2/authorizations/{auth_id}/void"
                    void_headers = {
                        "Authorization": f"Bearer {self.access_token}",
                        "Content-Type": "application/json",
                        "PayPal-Request-Id": check_id,
                    }
                    void_resp = await self.client.post(void_url, headers=void_headers)
                    if void_resp.status_code not in [200, 204]:
                        logger.warning(f"⚠️ Void başarısız: {void_resp.text}")
                last_success_amount = current_amount
                new_amount = current_amount * 2
            else:
                last_error = error
                last_error_code = error_code
                new_amount = current_amount / 2

            if abs(new_amount - current_amount) < step:
                break

            current_amount = new_amount

        if last_success_amount is not None:
            return {
                'success': True,
                'amount': last_success_amount,
                'currency': currency,
                'auth_id': auth_id if success else None,
                'status': 'SUCCESS'
            }
        else:
            return {
                'success': False,
                'amount': None,
                'currency': currency,
                'auth_id': None,
                'status': 'FAILED',
                'error': last_error,
                'error_code': last_error_code
            }

    async def process_card(self, card: CardData) -> Dict:
        check_id = str(uuid.uuid4())
        try:
            bin_info = None
            if mongo_db:
                raw_bin = mongo_db.get_bin_info(card.number)
                if raw_bin:
                    bin_info = {
                        'brand': raw_bin.get('brand', 'UNKNOWN'),
                        'type': raw_bin.get('type', 'UNKNOWN'),
                        'level': raw_bin.get('level', 'STANDARD'),
                        'bank': raw_bin.get('bank', 'Unknown'),
                        'country': raw_bin.get('country', 'XX'),
                        'country_name': raw_bin.get('country_name', 'Unknown'),
                        'issuer_phone': raw_bin.get('issuer_phone', ''),
                        'issuer_url': raw_bin.get('issuer_url', ''),
                        'raw': raw_bin.get('raw', {})
                    }
            else:
                bin_info = {
                    'brand': detect_card_brand(card.number),
                    'type': 'UNKNOWN',
                    'level': 'STANDARD',
                    'bank': 'Unknown',
                    'country': 'XX',
                    'country_name': 'Unknown',
                    'issuer_phone': '',
                    'issuer_url': '',
                    'raw': {}
                }

            success, setup_token, payment_token, error, raw_response = await self.verify_card(card, bin_info, check_id)

            if success and setup_token:
                record = {
                    'check_id': check_id,
                    'card_number': card.number,
                    'card_last4': card.number[-4:],
                    'exp_month': card.exp_month,
                    'exp_year': card.exp_year,
                    'cvc': card.cvc,
                    'card_brand': bin_info.get('brand'),
                    'card_type': bin_info.get('type'),
                    'card_level': bin_info.get('level'),
                    'card_issuer': bin_info.get('bank'),
                    'card_issuer_phone': bin_info.get('issuer_phone'),
                    'card_issuer_url': bin_info.get('issuer_url'),
                    'card_country_code': bin_info.get('country'),
                    'card_country_name': bin_info.get('country_name'),
                    'bin_prefix': card.number[:6],
                    'setup_token': setup_token,
                    'payment_token': payment_token,
                    'status': 'VERIFIED',
                    'balance_status': '',
                    'balance_amount': None,
                    'balance_currency': None,
                    'balance_auth_id': None,
                    'last_balance_check': None,
                    'raw_response': json.dumps(raw_response) if raw_response else None
                }
                if mongo_db:
                    mongo_db.upsert_live_card(record)

                return {
                    'success': True,
                    'status': 'VERIFIED',
                    'message': 'Kart doğrulandı',
                    'setup_token': setup_token,
                    'payment_token': payment_token,
                    'bin_info': bin_info,
                    'check_id': check_id,
                    'card_number': card.number,
                    'exp_month': card.exp_month,
                    'exp_year': card.exp_year,
                    'cvc': card.cvc,
                    'card_brand': bin_info.get('brand'),
                    'card_last4': card.number[-4:],
                    'balance_status': '',
                    'balance_amount': None
                }
            else:
                return {
                    'success': False,
                    'status': 'AUTH_FAILED',
                    'message': 'Kart doğrulanamadı',
                    'error': error,
                    'bin_info': bin_info,
                    'check_id': check_id,
                    'setup_token': None,
                    'payment_token': None,
                    'raw_response': raw_response,
                    'card_number': card.number,
                    'exp_month': card.exp_month,
                    'exp_year': card.exp_year,
                    'cvc': card.cvc,
                    'balance_status': '',
                    'balance_amount': None
                }
        except Exception as e:
            logger.error(f'❌ İşlem hatası: {str(e)}')
            return {
                'success': False,
                'status': 'ERROR',
                'message': 'İşlem hatası',
                'error': str(e),
                'check_id': check_id,
                'setup_token': None,
                'payment_token': None,
                'card_number': card.number,
                'exp_month': card.exp_month,
                'exp_year': card.exp_year,
                'cvc': card.cvc,
                'balance_status': '',
                'balance_amount': None
            }

# ==================== AUTHORIZE.NET FONKSİYONLARI ====================
def authorize_only(request_data: AuthOnlyRequest):
    """Authorize.net Auth Only işlemi (sadece yetkilendirme, para çekilmez)"""
    try:
        merchantAuth = apicontractsv1.merchantAuthenticationType()
        merchantAuth.name = CONFIG['authorize_api_login_id']
        merchantAuth.transactionKey = CONFIG['authorize_transaction_key']

        creditCard = apicontractsv1.creditCardType()
        creditCard.cardNumber = request_data.card_number
        creditCard.expirationDate = request_data.exp_date
        creditCard.cardCode = request_data.cvv

        payment = apicontractsv1.paymentType()
        payment.creditCard = creditCard

        billTo = apicontractsv1.customerAddressType()
        billTo.firstName = request_data.first_name
        billTo.lastName = request_data.last_name
        billTo.address = request_data.address
        billTo.city = request_data.city
        billTo.state = request_data.state
        billTo.zip = request_data.zip
        billTo.country = request_data.country

        order = apicontractsv1.orderType()
        order.invoiceNumber = request_data.invoice_number
        order.description = request_data.description

        transactionRequest = apicontractsv1.transactionRequestType()
        transactionRequest.transactionType = "authOnlyTransaction"
        transactionRequest.amount = str(request_data.amount)
        transactionRequest.payment = payment
        transactionRequest.billTo = billTo
        transactionRequest.order = order

        createRequest = apicontractsv1.createTransactionRequest()
        createRequest.merchantAuthentication = merchantAuth
        createRequest.refId = "AuthOnly-" + str(int(datetime.now().timestamp()))
        createRequest.transactionRequest = transactionRequest

        from authorizenet.apicontrollers import createTransactionController
        controller = createTransactionController(createRequest)
        controller.execute()
        return controller.getresponse()
    except Exception as e:
        logger.error(f"Authorize.net Auth Only hatası: {e}")
        raise

def capture_prior_auth(transaction_id: str, amount: float):
    """Authorize.net Prior Auth Capture işlemi (yetkilendirmeyi yakala)"""
    try:
        merchantAuth = apicontractsv1.merchantAuthenticationType()
        merchantAuth.name = CONFIG['authorize_api_login_id']
        merchantAuth.transactionKey = CONFIG['authorize_transaction_key']

        transactionRequest = apicontractsv1.transactionRequestType()
        transactionRequest.transactionType = "priorAuthCaptureTransaction"
        transactionRequest.amount = str(amount)
        transactionRequest.refTransId = transaction_id

        createRequest = apicontractsv1.createTransactionRequest()
        createRequest.merchantAuthentication = merchantAuth
        createRequest.refId = "Capture-" + str(int(datetime.now().timestamp()))
        createRequest.transactionRequest = transactionRequest

        controller = createTransactionController(createRequest)
        controller.execute()
        return controller.getresponse()
    except Exception as e:
        logger.error(f"Authorize.net Capture hatası: {e}")
        raise

# ==================== YARDIMCI: KART İŞLEME (GECİKMELİ) ====================
async def process_cards_with_delay(cards: List[CardData], processor: PayPalProcessor, mode: str = 'live', delay: float = None) -> List[Dict]:
    """Kartları sırayla işler, her biri arasında delay (varsayılan 2.5 sn) bekler."""
    if delay is None:
        delay = CONFIG['request_delay_seconds']
    results = []
    for idx, card in enumerate(cards):
        if idx > 0:
            await asyncio.sleep(delay)
        if mode == 'live':
            result = await processor.process_card(card)
        elif mode == 'balance':
            existing = mongo_db.get_card_by_number(card.number) if mongo_db else None
            payment_token = existing.get('payment_token') if existing else None
            if payment_token:
                check_id = str(uuid.uuid4())
                balance_result = await processor.perform_balance_check_with_algorithm(payment_token, check_id)
                if mongo_db:
                    mongo_db.update_card_balance(card.number, {
                        'status': balance_result.get('status'),
                        'amount': balance_result.get('amount'),
                        'currency': balance_result.get('currency'),
                        'auth_id': balance_result.get('auth_id')
                    })
                updated = mongo_db.get_card_by_number(card.number) if mongo_db else {}
                result = {
                    'card_number': updated.get('card_number', card.number),
                    'exp_month': updated.get('exp_month', card.exp_month),
                    'exp_year': updated.get('exp_year', card.exp_year),
                    'cvc': updated.get('cvc', card.cvc),
                    'payment_token': updated.get('payment_token', payment_token),
                    'card_country_code': updated.get('card_country_code'),
                    'card_issuer': updated.get('card_issuer'),
                    'card_type': updated.get('card_type'),
                    'card_level': updated.get('card_level'),
                    'balance_status': updated.get('balance_status'),
                    'balance_amount': updated.get('balance_amount')
                }
            else:
                live_result = await processor.process_card(card)
                if not live_result.get('success'):
                    live_result['balance_status'] = 'FAILED'
                    live_result['balance_amount'] = None
                    result = live_result
                else:
                    payment_token = live_result.get('payment_token')
                    if not payment_token:
                        live_result['balance_status'] = 'FAILED'
                        live_result['balance_amount'] = None
                        result = live_result
                    else:
                        check_id = live_result.get('check_id')
                        balance_result = await processor.perform_balance_check_with_algorithm(payment_token, check_id)
                        if mongo_db:
                            mongo_db.update_card_balance(card.number, {
                                'status': balance_result.get('status'),
                                'amount': balance_result.get('amount'),
                                'currency': balance_result.get('currency'),
                                'auth_id': balance_result.get('auth_id')
                            })
                        updated = mongo_db.get_card_by_number(card.number) if mongo_db else {}
                        result = {
                            'card_number': updated.get('card_number', card.number),
                            'exp_month': updated.get('exp_month', card.exp_month),
                            'exp_year': updated.get('exp_year', card.exp_year),
                            'cvc': updated.get('cvc', card.cvc),
                            'payment_token': updated.get('payment_token', payment_token),
                            'card_country_code': updated.get('card_country_code'),
                            'card_issuer': updated.get('card_issuer'),
                            'card_type': updated.get('card_type'),
                            'card_level': updated.get('card_level'),
                            'balance_status': updated.get('balance_status'),
                            'balance_amount': updated.get('balance_amount')
                        }
        else:
            result = {'error': 'Bilinmeyen mod'}
        results.append(result)
    return results

# ==================== FASTAPI APP ====================
app = FastAPI(
    title="PayPal + Authorize.net Card Checker API",
    description="Live Check (PayPal), Adaptif Balance Check, Authorize.net Auth/Capture, BIN Sorgulama",
    version="12.2.0",
    docs_url="/docs",
    redoc_url="/redoc"
)

# ==================== ENDPOINT'LER ====================

@app.get("/", tags=["Root"])
async def root():
    return {
        "message": "PayPal + Authorize.net Card API v12.2.0",
        "docs": "/docs",
        "version": "12.2.0"
    }

@app.get("/health", tags=["Health"])
async def health_check():
    mongo_status = 'connected' if mongo_db else 'disconnected'
    return {
        "status": "healthy",
        "timestamp": datetime.now().isoformat(),
        "mongodb": mongo_status,
        "paypal": CONFIG['paypal_api_base'],
        "authorize": "configured"
    }

# ---------- PayPal Endpoint'leri ----------
@app.post("/api/v1/check/live", response_class=PlainTextResponse, tags=["Live Check"])
async def check_live(req: CheckRequest):
    """Live Check (PayPal Vault Setup + Confirm) – çoklu format desteği"""
    try:
        cards = CardParser.parse(req.data)
        if not cards:
            return PlainTextResponse("HATA: Geçerli kart bulunamadı", status_code=400)

        processor = PayPalProcessor()
        results = await process_cards_with_delay(cards, processor, mode='live')
        lines = [build_pipe_response(r) for r in results]
        return PlainTextResponse("\n".join(lines), media_type="text/plain")
    except Exception as e:
        logger.error(f"Live check hatası: {e}")
        raise HTTPException(500, str(e))

@app.post("/api/v1/check/balance", response_class=PlainTextResponse, tags=["Balance Check"])
async def check_balance(req: CheckRequest):
    """Balance Check (PayPal Authorization + Void) – çoklu format desteği"""
    try:
        if not mongo_db:
            raise HTTPException(500, "MongoDB bağlantısı yok")

        cards = CardParser.parse(req.data)
        if not cards:
            return PlainTextResponse("HATA: Geçerli kart bulunamadı", status_code=400)

        processor = PayPalProcessor()
        results = await process_cards_with_delay(cards, processor, mode='balance')
        lines = [build_pipe_response(r) for r in results]
        return PlainTextResponse("\n".join(lines), media_type="text/plain")
    except Exception as e:
        logger.error(f"Balance check hatası: {e}")
        raise HTTPException(500, str(e))

@app.post("/api/v1/bin/check", tags=["BIN"])
async def bin_check(request: BinCheckRequest):
    if not mongo_db:
        raise HTTPException(500, "MongoDB bağlantısı yok")

    bins = request.bins
    if isinstance(bins, str):
        bins = [bins]

    results = []
    for b in bins:
        clean = re.sub(r'[^0-9]', '', b)
        if len(clean) < 4:
            results.append({"bin": clean, "error": "Geçersiz BIN (en az 4 hane)"})
            continue
        bin_info = None
        for length in [6, 5, 4]:
            prefix = clean[:length]
            info = mongo_db.get_bin_by_prefix(prefix)
            if info:
                bin_info = info
                break
        if bin_info:
            results.append({
                "bin": clean,
                "brand": bin_info.get('Brand'),
                "type": bin_info.get('Type'),
                "level": bin_info.get('Category'),
                "bank": bin_info.get('Issuer'),
                "country_code": bin_info.get('isoCode2'),
                "country_name": bin_info.get('CountryName')
            })
        else:
            results.append({"bin": clean, "error": "BIN bulunamadı"})
    return {"success": True, "results": results}

@app.get("/api/v1/card/{card_number}", tags=["Card"])
async def find_card(card_number: str):
    if not mongo_db:
        raise HTTPException(500, "MongoDB bağlantısı yok")
    clean = re.sub(r'[^0-9]', '', card_number)
    card = mongo_db.get_card_by_number(clean)
    if not card:
        raise HTTPException(404, "Kart bulunamadı")
    if 'cvc' in card:
        card['cvc'] = '***'
    return {"success": True, "card": card}

# ---------- Authorize.net Endpoint'leri ----------
@app.post("/api/v1/authorize/auth-only", tags=["Authorize.net"])
async def auth_only_endpoint(request: AuthOnlyRequest):
    """Authorize.net Auth Only - sadece yetkilendirme (para çekilmez)"""
    try:
        response = authorize_only(request)
        if response is not None:
            if response.messages.resultCode == "Ok":
                if hasattr(response.transactionResponse, 'transId'):
                    return {
                        "success": True,
                        "transaction_id": response.transactionResponse.transId,
                        "response_code": response.transactionResponse.responseCode,
                        "message": "Yetkilendirme başarılı"
                    }
                else:
                    raise HTTPException(500, "Transaction ID alınamadı")
            else:
                error_msg = response.messages.message[0].text if hasattr(response.messages, 'message') else "Bilinmeyen hata"
                raise HTTPException(400, f"Yetkilendirme başarısız: {error_msg}")
        else:
            raise HTTPException(500, "Boş yanıt")
    except Exception as e:
        logger.error(f"Auth-only hatası: {e}")
        raise HTTPException(500, str(e))

@app.post("/api/v1/authorize/capture", tags=["Authorize.net"])
async def capture_endpoint(request: CaptureRequest):
    """Authorize.net Capture - daha önce yapılmış bir yetkilendirmeyi yakala (para çek)"""
    try:
        response = capture_prior_auth(request.transaction_id, request.amount)
        if response is not None:
            if response.messages.resultCode == "Ok":
                if hasattr(response.transactionResponse, 'transId'):
                    return {
                        "success": True,
                        "transaction_id": response.transactionResponse.transId,
                        "response_code": response.transactionResponse.responseCode,
                        "message": "Yakalama başarılı"
                    }
                else:
                    raise HTTPException(500, "Transaction ID alınamadı")
            else:
                error_msg = response.messages.message[0].text if hasattr(response.messages, 'message') else "Bilinmeyen hata"
                raise HTTPException(400, f"Yakalama başarısız: {error_msg}")
        else:
            raise HTTPException(500, "Boş yanıt")
    except Exception as e:
        logger.error(f"Capture hatası: {e}")
        raise HTTPException(500, str(e))

# ---------- Legacy ----------
@app.post("/api/v1/check", tags=["Legacy"])
async def check_card_json(card_request: CardRequest):
    """Eski JSON formatında Live Check (sadece tek kart)"""
    try:
        card = CardData(
            number=card_request.number,
            exp_month=card_request.exp_month,
            exp_year=card_request.exp_year,
            cvc=card_request.cvc,
            name=card_request.name or "Test User",
            country=card_request.country or "TR",
            zip=card_request.zip or "00000",
            email=card_request.email,
            phone=card_request.phone,
            dob=card_request.dob,
            ip=card_request.ip,
            user_agent=card_request.user_agent
        )
        processor = PayPalProcessor()
        result = await processor.process_card(card)
        return JSONResponse(content=result)
    except Exception as e:
        raise HTTPException(500, str(e))

# ==================== SUNUCUYU BAŞLAT ====================
if __name__ == '__main__':
    port = int(os.getenv('PORT', 3000))
    debug = os.getenv('DEBUG', 'False').lower() == 'true'
    print('=' * 70)
    print('🏦 PayPal + Authorize.net Card API v12.2.0')
    print('=' * 70)
    print(f'📍 Sunucu: http://localhost:{port}')
    print(f'📚 Swagger: http://localhost:{port}/docs')
    print('💳 PayPal Live check  → /api/v1/check/live')
    print('⚖️ PayPal Balance check → /api/v1/check/balance')
    print('🔍 BIN check          → /api/v1/bin/check')
    print('🔎 Find card          → /api/v1/card/{number}')
    print('🔐 Authorize.net Auth → /api/v1/authorize/auth-only')
    print('💲 Authorize.net Capture → /api/v1/authorize/capture')
    print('⏱️  Gecikme: {} saniye/kart'.format(CONFIG['request_delay_seconds']))
    print('=' * 70)
    uvicorn.run(
        "main:app",
        host="0.0.0.0",
        port=port,
        reload=debug,
        log_level="info"
    )