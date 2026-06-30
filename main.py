"""
CLOVER CARD CHECKER API - FASTAPI VERSION
0$ CHARGE ile kart doğrulama
Swagger Docs: /docs
ReDoc: /redoc
"""
import os
import re
import json
import uuid
import logging
import requests
from typing import Dict, List, Optional, Union, Any, Tuple
from datetime import datetime
from dataclasses import dataclass, field
from fastapi import FastAPI, HTTPException
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field, validator
from pymongo import MongoClient
from pymongo.errors import ConnectionFailure
import uvicorn

# Logging ayarları
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)

# ==================== PYDANTIC MODELS ====================

class CardRequest(BaseModel):
    number: str = Field(..., example="5549601721207035")
    exp_month: str = Field(..., example="08")
    exp_year: str = Field(..., example="2026")
    cvc: str = Field(..., example="319")
    name: Optional[str] = "Test User"
    country: Optional[str] = "US"
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

class BatchCardRequest(BaseModel):
    cards: List[CardRequest] = Field(..., min_items=1, max_items=50)

class ParseRequest(BaseModel):
    data: Union[str, List, Dict] = Field(...)

# ==================== KONFIGÜRASYON (YENİ TOKEN'LAR) ====================
CONFIG = {
    'merchant_id': '518993421163932',
    'public_token': 'ede19e1b042d053ddfea06f8f206fb22',  # YENİ
    'private_token': 'cc43c8d1-7813-fad4-4d3a-7bd733ba1fd6',  # YENİ - charge yetkili
    'api_token': '1271ec57-b9a5-481d-9ac4-60d8cfa02e0e',  # Yedek
    'api_base': 'https://api.clover.com',
    'token_api': 'https://token.clover.com',
    'charge_endpoint': 'https://www.clover.com/scl/v1/merchant/YHQFFZ1ZDDT61/charge',
    'company_id': 'YHQFFZ1ZDDT61',
    'mongo_uri': 'mongodb+srv://cardmarketApp:gnbqHdTrlceMZjOS@paymentmanger.gvaavzc.mongodb.net/mydb?retryWrites=true&w=majority',
    'mongo_database': 'mydb',
    'mongo_collection': 'card_checks',
    'mongo_bin_collection': 'binList'
}

app = FastAPI(
    title="Clover Card Checker API",
    description="Clover 0$ Charge ile Kart Doğrulama API'si",
    version="7.0.0",
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
            
            self.collection.create_index('check_id', unique=True)
            self.collection.create_index('created_at')
            self.collection.create_index('card_last4')
            self.collection.create_index('status')
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
            bin_prefixes = [card_number[:6], card_number[:5], card_number[:4]]
            for bin_prefix in bin_prefixes:
                result = self.bin_collection.find_one({'BIN': bin_prefix})
                if result:
                    if '_id' in result:
                        result['_id'] = str(result['_id'])
                    return result
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
    
    def get_record(self, check_id: str) -> Optional[Dict]:
        try:
            result = self.collection.find_one({'check_id': check_id})
            if result and '_id' in result:
                result['_id'] = str(result['_id'])
            return result
        except Exception as e:
            logger.error(f'❌ Kayıt getirme hatası: {str(e)}')
            raise


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
                if len(data.split('|')) > 10:
                    return CardParser._parse_full_pipe(data)
                else:
                    return CardParser._parse_pipe(data)
            if ',' in data and '\n' in data:
                return CardParser._parse_csv(data)
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
    def _parse_csv(data: str) -> List[CardData]:
        cards = []
        lines = data.strip().split('\n')
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
                    if len(year) == 2:
                        year = f"20{year}"
                    elif len(year) == 4:
                        year = year
                    if month.isdigit() and year.isdigit():
                        return month, year
        if exp_str.isdigit() and len(exp_str) == 4:
            return exp_str[:2], f"20{exp_str[2:]}"
        elif exp_str.isdigit() and len(exp_str) == 6:
            return exp_str[:2], exp_str[2:]
        return None, None
    
    @staticmethod
    def _extract_from_json_item(item: Dict) -> Optional[CardData]:
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


# ==================== CLOVER PROCESSOR ====================

def detect_card_brand(card_number: str) -> str:
    patterns = {
        'VISA': r'^4',
        'MASTERCARD': r'^(5[1-5]|2[2-7])',
        'AMEX': r'^(34|37)',
        'DISCOVER': r'^(6011|65|64[4-9]|622)',
        'JCB': r'^35'
    }
    for brand, pattern in patterns.items():
        if re.match(pattern, card_number):
            return brand
    return 'UNKNOWN'


class CloverProcessor:
    def __init__(self):
        self.merchant_id = CONFIG['merchant_id']
        self.public_token = CONFIG['public_token']
        self.private_token = CONFIG['private_token']
        self.api_token = CONFIG['api_token']
        self.token_api = CONFIG['token_api']
        self.charge_endpoint = CONFIG['charge_endpoint']
        self.company_id = CONFIG['company_id']
    
    def create_token(self, card: CardData) -> Tuple[bool, Optional[str], Optional[str]]:
        try:
            token_url = f"{self.token_api}/v1/tokens"
            
            brand = detect_card_brand(card.number)
            if brand == "MASTERCARD":
                brand = "MC"
            
            payload = {
                "card": {
                    "number": card.number,
                    "exp_month": card.exp_month,
                    "exp_year": card.exp_year,
                    "cvv": card.cvc,
                    "brand": brand,
                    "address_zip": "00000"
                },
                "multipay": False
            }
            
            logger.info(f'📤 Token payload: {json.dumps(payload)}')
            
            headers = {
                'apikey': self.public_token,
                'content-type': 'application/json'
            }
            
            response = requests.post(token_url, json=payload, headers=headers)
            
            logger.info(f'📊 Token Response Status: {response.status_code}')
            
            if response.status_code == 200:
                result = response.json()
                logger.info(f'✅ Token başarılı: {result.get("id")}')
                return True, result.get('id'), None
            else:
                error_text = response.text
                try:
                    error_json = response.json()
                    error_text = error_json.get('message', error_text)
                except:
                    pass
                logger.error(f'❌ Token hatası: {error_text}')
                return False, None, error_text
                
        except Exception as e:
            logger.error(f'❌ Token exception: {str(e)}')
            return False, None, str(e)
    
    def charge_zero_dollar(self, token: str, card: CardData, bin_info: Dict = None) -> Tuple[bool, Optional[str], Optional[str], Optional[Dict]]:
        try:
            charge_url = f"{self.charge_endpoint}?companyId={self.company_id}&companyType=merchant"
            
            payload = {
                'amount': 0,
                'capture': True,
                'tax_rate_uuid': 'FY6ZPX2PMQZM8',
                'description': '',
                'currency': 'USD',
                'metadata': {
                    'vt_payment_type': 'vt_checkout',
                    'source_app': 'com.clover.virtualterminal',
                    'existingDebtIndicator': 'false'
                },
                'ecomind': 'moto',
                'source': token,
                'custom_attributes': {}
            }
            
            # Önce private token ile dene
            headers = {
                'Authorization': f'Bearer {self.private_token}',
                'Content-Type': 'application/json',
                'Accept': 'application/json, text/plain, */*',
                'Origin': 'https://www.clover.com',
                'Referer': 'https://www.clover.com/',
                'User-Agent': 'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36'
            }
            
            logger.info(f'🔄 0$ Charge işlemi başlatılıyor...')
            logger.info(f'📇 Kart: {card.get_masked()}')
            logger.info(f'🔑 Token: {token}')
            logger.info(f'🔐 Private Token: {self.private_token[:10]}...')
            logger.info(f'📤 Payload: {json.dumps(payload)}')
            
            response = requests.post(charge_url, json=payload, headers=headers)
            
            logger.info(f'📊 Response Status: {response.status_code}')
            
            if response.status_code == 200:
                result = response.json()
                logger.info(f'✅ 0$ Charge başarılı!')
                logger.info(f'📋 Charge ID: {result.get("id")}')
                return True, result.get('id'), None, result
            elif response.status_code == 401 and self.api_token:
                # Private token çalışmadı, api_token ile dene
                logger.warning(f'⚠️ Private token 401 verdi, API token deneniyor...')
                headers['Authorization'] = f'Bearer {self.api_token}'
                response = requests.post(charge_url, json=payload, headers=headers)
                logger.info(f'📊 API Token Response Status: {response.status_code}')
                
                if response.status_code == 200:
                    result = response.json()
                    logger.info(f'✅ API Token ile 0$ Charge başarılı!')
                    logger.info(f'📋 Charge ID: {result.get("id")}')
                    return True, result.get('id'), None, result
                else:
                    error_text = response.text
                    try:
                        error_json = response.json()
                        error_text = error_json.get('message', error_text)
                    except:
                        pass
                    error_msg = f"HTTP {response.status_code}: {error_text}"
                    logger.error(f'❌ API Token Charge hatası: {error_msg}')
                    return False, None, error_msg, response.json()
            else:
                error_text = response.text
                try:
                    error_json = response.json()
                    error_text = error_json.get('message', error_text)
                except:
                    pass
                error_msg = f"HTTP {response.status_code}: {error_text}"
                logger.error(f'❌ Charge hatası: {error_msg}')
                return False, None, error_msg, response.json()
                
        except Exception as e:
            logger.error(f'❌ Charge exception: {str(e)}')
            return False, None, str(e), None
    
    def process_card(self, card: CardData) -> Dict:
        check_id = str(uuid.uuid4())
        
        try:
            # 1. BIN Check
            bin_info = None
            if mongo_db:
                bin_info = mongo_db.get_bin_info(card.number)
            
            if not bin_info:
                bin_info = {
                    'BIN': card.number[:6],
                    'Brand': detect_card_brand(card.number),
                    'Type': 'UNKNOWN',
                    'Category': 'UNKNOWN',
                    'Issuer': 'UNKNOWN',
                    'IssuerPhone': '',
                    'IssuerUrl': '',
                    'isoCode2': '',
                    'isoCode3': '',
                    'CountryName': ''
                }
            
            # 2. Token Oluştur
            logger.info('🔄 Adım 1/2: Token oluşturuluyor...')
            success, token, error = self.create_token(card)
            
            if not success:
                logger.error(f'❌ Token oluşturulamadı: {error}')
                return {
                    'success': False,
                    'status': 'TOKEN_FAILED',
                    'message': 'Token oluşturulamadı',
                    'error': error,
                    'bin_info': bin_info,
                    'check_id': check_id,
                    'token': None
                }
            
            logger.info(f'✅ Token oluşturuldu: {token}')
            
            # 3. 0$ Charge
            logger.info('🔄 Adım 2/2: 0$ Charge işlemi yapılıyor...')
            success, charge_id, error, raw_response = self.charge_zero_dollar(token, card, bin_info)
            
            if not success:
                logger.error(f'❌ 0$ Charge başarısız: {error}')
                return {
                    'success': False,
                    'status': 'CHARGE_FAILED',
                    'message': '0$ Charge başarısız',
                    'error': error,
                    'token': token,
                    'bin_info': bin_info,
                    'check_id': check_id,
                    'charge_id': None,
                    'raw_response': raw_response
                }
            
            logger.info(f'✅ 0$ Charge başarılı! Charge ID: {charge_id}')
            
            # 4. MongoDB'ye kaydet
            if mongo_db:
                record = {
                    'check_id': check_id,
                    'card_number_masked': card.get_masked(),
                    'card_last4': card.number[-4:],
                    'card_brand': bin_info.get('Brand'),
                    'card_type': bin_info.get('Type'),
                    'card_category': bin_info.get('Category'),
                    'card_issuer': bin_info.get('Issuer'),
                    'card_issuer_phone': bin_info.get('IssuerPhone'),
                    'card_issuer_url': bin_info.get('IssuerUrl'),
                    'card_country_code': bin_info.get('isoCode2'),
                    'card_country_name': bin_info.get('CountryName'),
                    'bin_prefix': bin_info.get('BIN'),
                    'token': token,
                    'charge_id': charge_id,
                    'amount': 0,
                    'currency': 'USD',
                    'status': 'CAPTURED',
                    'response_message': 'Kart başarıyla doğrulandı (0$ charge)',
                    'is_zero_dollar': True,
                    'raw_response': json.dumps(raw_response) if raw_response else None
                }
                mongo_db.insert_record(record)
            
            return {
                'success': True,
                'status': 'CAPTURED',
                'message': 'Kart başarıyla doğrulandı (0$ charge)',
                'token': token,
                'charge_id': charge_id,
                'bin_info': bin_info,
                'check_id': check_id,
                'amount': 0,
                'currency': 'USD',
                'card_masked': card.get_masked(),
                'card_brand': bin_info.get('Brand'),
                'card_last4': card.number[-4:]
            }
            
        except Exception as e:
            logger.error(f'❌ İşlem hatası: {str(e)}')
            return {
                'success': False,
                'status': 'ERROR',
                'message': 'İşlem hatası',
                'error': str(e),
                'check_id': check_id
            }


# ==================== API ENDPOINT'LER ====================

@app.get("/", tags=["Root"])
async def root():
    return {
        "message": "Clover Card Checker API",
        "docs": "/docs",
        "redoc": "/redoc",
        "health": "/health",
        "version": "7.0.0"
    }


@app.get("/health", tags=["Health"])
async def health_check():
    mongo_status = 'connected' if mongo_db else 'disconnected'
    return {
        "status": "healthy",
        "timestamp": datetime.now().isoformat(),
        "service": "Clover Card Check API (0$ Charge)",
        "version": "7.0.0",
        "environment": "LIVE",
        "mongodb": mongo_status,
        "clover": {
            "merchant_id": CONFIG['merchant_id'][:4] + '***',
            "charge_endpoint": CONFIG['charge_endpoint']
        },
        "endpoints": [
            "POST /api/v1/check - Kart kontrolü (0$ charge)",
            "POST /api/v1/check/batch - Toplu kart kontrolü",
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
            country=card_request.country or "US",
            zip=card_request.zip or "00000",
            email=card_request.email,
            phone=card_request.phone,
            dob=card_request.dob,
            ip=card_request.ip,
            user_agent=card_request.user_agent
        )
        
        processor = CloverProcessor()
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
        processor = CloverProcessor()
        results = []
        
        for card_request in batch_request.cards:
            card = CardData(
                number=card_request.number,
                exp_month=card_request.exp_month,
                exp_year=card_request.exp_year,
                cvc=card_request.cvc,
                name=card_request.name or "Test User",
                country=card_request.country or "US",
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


@app.post("/api/v1/parse", tags=["Parser"])
async def parse_only(parse_request: ParseRequest):
    try:
        cards = CardParser.parse(parse_request.data)
        
        return {
            "success": True,
            "count": len(cards),
            "cards": [
                {
                    "number_masked": card.get_masked(),
                    "exp_month": card.exp_month,
                    "exp_year": card.exp_year,
                    "cvc": card.cvc,
                    "name": card.name,
                    "email": card.email,
                    "phone": card.phone,
                    "country": card.country,
                    "zip": card.zip
                }
                for card in cards
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
        
        bin_info = mongo_db.get_bin_info(card_number.replace(' ', ''))
        
        if not bin_info:
            raise HTTPException(
                status_code=404,
                detail={
                    "error": "BIN bulunamadı",
                    "bin_prefix": card_number[:6]
                }
            )
        
        return {
            "success": True,
            "bin_info": bin_info
        }
        
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
        
        return {
            "success": True,
            "record": record
        }
        
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
        
        return {
            "success": True,
            "stats": stats
        }
        
    except Exception as e:
        logger.error(f'❌ İstatistik hatası: {str(e)}')
        raise HTTPException(status_code=500, detail=str(e))


# ==================== SUNUCUYU BAŞLAT ====================

if __name__ == '__main__':
    port = int(os.getenv('PORT', 3000))
    debug = os.getenv('DEBUG', 'False').lower() == 'true'
    
    print('=' * 60)
    print('🏦 Clover Card Checker API (FastAPI)')
    print('=' * 60)
    print(f'📍 Sunucu: http://localhost:{port}')
    print(f'📚 Swagger Docs: http://localhost:{port}/docs')
    print(f'📖 ReDoc: http://localhost:{port}/redoc')
    print(f'🔧 Debug: {debug}')
    print(f'🌍 Environment: LIVE')
    print(f'💰 Charge Miktarı: 0$ (Doğrulama)')
    print(f'🔐 Public Token: {CONFIG["public_token"][:10]}...')
    print(f'🔐 Private Token: {CONFIG["private_token"][:10]}...')
    print('=' * 60)
    
    uvicorn.run(
        "main:app",
        host="0.0.0.0",
        port=port,
        reload=debug,
        log_level="info"
    )