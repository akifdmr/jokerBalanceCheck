"""
CLOVER CARD CHECKER API - 0$ CAPTURE (CHARGE ENDPOINT)
Önce parser'dan geçirir, BIN check yapar, Clover ile tokenize eder, 0$ capture=true yapar.
Sadece API olarak çalışır.
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
from flask import Flask, request, jsonify
from flask_cors import CORS
from dotenv import load_dotenv
from pymongo import MongoClient
from pymongo.errors import ConnectionFailure

# .env dosyasını yükle
load_dotenv()

# Logging ayarları
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)

app = Flask(__name__)
CORS(app)

# ==================== KONFIGÜRASYON ====================
CONFIG = {
    'merchant_id': os.getenv('CLOVER_MERCHANT_ID'),
    'public_token': os.getenv('CLOVER_ECOMM_PUBLIC_TOKEN'),
    'private_token': os.getenv('CLOVER_ECOMM_PRIVATE_TOKEN'),
    'api_base': os.getenv('CLOVER_API_BASE', 'https://api.clover.com'),
    'token_api': os.getenv('CLOVER_TOKEN_API', 'https://token.clover.com'),
    'charge_endpoint': os.getenv('CLOVER_CHARGE_ENDPOINT', 'https://www.clover.com/scl/v1/merchant/YHQFFZ1ZDDT61/charge'),
    'company_id': os.getenv('CLOVER_COMPANY_ID', 'YHQFFZ1ZDDT61'),
    'mongo_uri': os.getenv('MONGO_URI'),
    'mongo_database': os.getenv('MONGO_DATABASE', 'mydb'),
    'mongo_collection': os.getenv('MONGO_COLLECTION', 'card_checks'),
    'mongo_bin_collection': os.getenv('MONGO_BIN_COLLECTION', 'binList')
}

# ==================== DATA CLASSES ====================

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
        return f"{self.number[:4]}****{self.number[-4:]}"
    
    def get_bin(self) -> str:
        return self.number[:6]


@dataclass
class ProcessingResult:
    """İşlem sonucu"""
    card: CardData
    success: bool
    status: str
    message: str
    error: Optional[str] = None
    token: Optional[str] = None
    charge_id: Optional[str] = None
    bin_info: Optional[Dict] = None
    check_id: Optional[str] = None
    amount: float = 0
    currency: str = "USD"
    raw_response: Optional[Dict] = None
    
    def to_dict(self) -> Dict:
        return {
            'success': self.success,
            'status': self.status,
            'message': self.message,
            'error': self.error,
            'token': self.token,
            'charge_id': self.charge_id,
            'bin_info': self.bin_info,
            'check_id': self.check_id,
            'amount': self.amount,
            'currency': self.currency,
            'card_masked': self.card.get_masked() if self.card else None,
            'card_brand': self.bin_info.get('Brand') if self.bin_info else None,
            'card_last4': self.card.number[-4:] if self.card else None
        }


# ==================== PARSER ====================

class CardParser:
    """Farklı formatlardaki kart verilerini parse eden sınıf"""
    
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
                    
                    if len(year) == 4:
                        year = year[-2:]
                    
                    if month.isdigit() and year.isdigit():
                        return month, year
        
        if exp_str.isdigit() and len(exp_str) == 4:
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


# MongoDB instance
try:
    mongo_db = MongoDB()
except Exception as e:
    logger.error(f'❌ MongoDB başlatılamadı: {str(e)}')
    mongo_db = None


# ==================== CLOVER PROCESSOR ====================

class CloverProcessor:
    """Clover API işlemleri - 0$ Capture=true ile"""
    
    def __init__(self):
        self.merchant_id = CONFIG['merchant_id']
        self.public_token = CONFIG['public_token']
        self.private_token = CONFIG['private_token']
        self.token_api = CONFIG['token_api']
        self.charge_endpoint = CONFIG['charge_endpoint']
        self.company_id = CONFIG['company_id']
        
    def create_token(self, card: CardData) -> Tuple[bool, Optional[str], Optional[str]]:
        """Clover token oluştur"""
        try:
            token_url = f"{self.token_api}/v1/tokens"
            
            payload = {
                'card': {
                    'number': card.number,
                    'exp_month': card.exp_month,
                    'exp_year': card.exp_year,
                    'cvv': card.cvc,
                    'brand': detect_card_brand(card.number)
                }
            }
            
            headers = {
                'apikey': self.public_token,
                'content-type': 'application/json'
            }
            
            response = requests.post(token_url, json=payload, headers=headers)
            
            if response.status_code == 200:
                result = response.json()
                return True, result.get('id'), None
            else:
                error = response.json().get('message', 'Unknown error')
                return False, None, error
                
        except Exception as e:
            return False, None, str(e)
    
    def charge_zero_dollar(self, token: str, card: CardData, bin_info: Dict = None) -> Tuple[bool, Optional[str], Optional[str], Optional[Dict]]:
        """
        0$ Capture=true ile charge işlemi yap
        
        Request Body:
        {
            amount: 0,
            capture: true,
            tax_rate_uuid: "FY6ZPX2PMQZM8",
            currency: "USD",
            ecomind: "moto",
            source: "clv_xxxxxxxx",
            metadata: {
                vt_payment_type: "vt_checkout",
                source_app: "com.clover.virtualterminal"
            }
        }
        """
        try:
            # Charge URL
            charge_url = f"{self.charge_endpoint}?companyId={self.company_id}&companyType=merchant"
            
            # Payload oluştur
            payload = {
                'amount': 0,
                'capture': True,
                'tax_rate_uuid': 'FY6ZPX2PMQZM8',
                'currency': 'USD',
                'ecomind': 'moto',
                'source': token,
                'custom_attributes': {},
                'metadata': {
                    'vt_payment_type': 'vt_checkout',
                    'source_app': 'com.clover.virtualterminal',
                    'card_last4': card.number[-4:],
                    'card_brand': bin_info.get('Brand') if bin_info else detect_card_brand(card.number),
                    'bin_prefix': card.number[:6],
                    'check_type': 'zero_dollar_verification'
                }
            }
            
            # Headers
            headers = {
                'Authorization': f'Bearer {self.private_token}',
                'Content-Type': 'application/json'
            }
            
            logger.info(f'🔄 0$ Charge işlemi başlatılıyor...')
            logger.info(f'📇 Kart: {card.get_masked()}')
            logger.info(f'🔑 Token: {token}')
            
            response = requests.post(charge_url, json=payload, headers=headers)
            
            logger.info(f'📊 Response Status: {response.status_code}')
            
            if response.status_code == 200:
                result = response.json()
                logger.info(f'✅ 0$ Charge başarılı!')
                logger.info(f'📋 Charge ID: {result.get("id")}')
                return True, result.get('id'), None, result
            else:
                error = response.json().get('message', 'Unknown error')
                logger.error(f'❌ Charge hatası: {error}')
                return False, None, error, response.json()
                
        except Exception as e:
            logger.error(f'❌ Charge exception: {str(e)}')
            return False, None, str(e), None
    
    def process_card(self, card: CardData) -> ProcessingResult:
        """Kartı Clover ile işle - 0$ Capture=true"""
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
                return ProcessingResult(
                    card=card,
                    success=False,
                    status='TOKEN_FAILED',
                    message='Token oluşturulamadı',
                    error=error,
                    bin_info=bin_info,
                    check_id=check_id
                )
            
            logger.info(f'✅ Token oluşturuldu: {token}')
            
            # 3. 0$ Charge (capture=true)
            logger.info('🔄 Adım 2/2: 0$ Capture işlemi yapılıyor...')
            success, charge_id, error, raw_response = self.charge_zero_dollar(token, card, bin_info)
            
            if not success:
                logger.error(f'❌ 0$ Capture başarısız: {error}')
                return ProcessingResult(
                    card=card,
                    success=False,
                    status='CHARGE_FAILED',
                    message='0$ Capture başarısız',
                    error=error,
                    token=token,
                    bin_info=bin_info,
                    check_id=check_id,
                    raw_response=raw_response
                )
            
            logger.info(f'✅ 0$ Capture başarılı! Charge ID: {charge_id}')
            
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
                    'response_message': 'Kart başarıyla doğrulandı (0$ capture)',
                    'is_zero_dollar': True,
                    'raw_response': json.dumps(raw_response) if raw_response else None
                }
                mongo_db.insert_record(record)
            
            return ProcessingResult(
                card=card,
                success=True,
                status='CAPTURED',
                message='Kart başarıyla doğrulandı (0$ capture)',
                token=token,
                charge_id=charge_id,
                bin_info=bin_info,
                check_id=check_id,
                amount=0,
                currency='USD',
                raw_response=raw_response
            )
            
        except Exception as e:
            logger.error(f'❌ İşlem hatası: {str(e)}')
            return ProcessingResult(
                card=card,
                success=False,
                status='ERROR',
                message='İşlem hatası',
                error=str(e),
                check_id=check_id
            )


# ==================== YARDIMCI FONKSİYONLAR ====================

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


# ==================== API ENDPOINT'LER ====================

@app.route('/api/v1/check', methods=['POST'])
@app.route('/api/v1/single', methods=['POST'])
def check_card():
    """
    Kart kontrolü - Parser + BIN Check + Clover 0$ Capture=true
    
    Request Body (desteklenen formatlar):
    1. JSON:
    {
        "card_number": "6011361000006668",
        "exp_month": "12",
        "exp_year": "2030",
        "cvv": "123"
    }
    
    2. Pipe formatı:
    "6011361000006668|12|2030|123"
    
    3. JSON array:
    [
        {"number": "6011361000006668", "exp_month": "12", "exp_year": "2030", "cvc": "123"}
    ]
    """
    try:
        data = request.get_json()
        
        if not data:
            return jsonify({
                'success': False,
                'status': 'ERROR',
                'error': 'Veri gönderilmedi'
            }), 400
        
        # Parse et
        cards = parse_card_data(data)
        
        if not cards:
            return jsonify({
                'success': False,
                'status': 'ERROR',
                'error': 'Kart verisi parse edilemedi',
                'supported_formats': [
                    'JSON: {"number": "...", "exp_month": "...", "exp_year": "...", "cvc": "..."}',
                    'Pipe: "number|month|year|cvc"',
                    'JSON Array: [{"number": "...", ...}]'
                ]
            }), 400
        
        # İlk kartı işle
        card = cards[0]
        processor = CloverProcessor()
        result = processor.process_card(card)
        
        return jsonify({
            'success': result.success,
            'status': result.status,
            'message': result.message,
            'error': result.error,
            'data': result.to_dict()
        })
        
    except Exception as e:
        logger.error(f'❌ API hatası: {str(e)}')
        return jsonify({
            'success': False,
            'status': 'ERROR',
            'error': str(e)
        }), 500


@app.route('/api/v1/check/batch', methods=['POST'])
def check_cards_batch():
    """Birden fazla kartı kontrol et"""
    try:
        data = request.get_json()
        
        if not data:
            return jsonify({
                'success': False,
                'status': 'ERROR',
                'error': 'Veri gönderilmedi'
            }), 400
        
        cards = parse_card_data(data)
        
        if not cards:
            return jsonify({
                'success': False,
                'status': 'ERROR',
                'error': 'Kart verisi parse edilemedi'
            }), 400
        
        processor = CloverProcessor()
        results = []
        
        for card in cards:
            result = processor.process_card(card)
            results.append(result.to_dict())
        
        return jsonify({
            'success': True,
            'total': len(results),
            'results': results
        })
        
    except Exception as e:
        logger.error(f'❌ Batch API hatası: {str(e)}')
        return jsonify({
            'success': False,
            'status': 'ERROR',
            'error': str(e)
        }), 500


@app.route('/api/v1/parse', methods=['POST'])
def parse_only():
    """Sadece parser test et"""
    try:
        data = request.get_json()
        
        if not data:
            return jsonify({
                'success': False,
                'error': 'Veri gönderilmedi'
            }), 400
        
        cards = parse_card_data(data)
        
        return jsonify({
            'success': True,
            'count': len(cards),
            'cards': [
                {
                    'number_masked': card.get_masked(),
                    'exp_month': card.exp_month,
                    'exp_year': card.exp_year,
                    'cvc': card.cvc,
                    'name': card.name,
                    'email': card.email
                }
                for card in cards
            ]
        })
        
    except Exception as e:
        logger.error(f'❌ Parse hatası: {str(e)}')
        return jsonify({
            'success': False,
            'error': str(e)
        }), 500


@app.route('/api/v1/bin/<card_number>', methods=['GET'])
def bin_check(card_number: str):
    """BIN kontrolü"""
    try:
        if not mongo_db:
            return jsonify({
                'success': False,
                'error': 'MongoDB bağlantısı yok'
            }), 500
        
        bin_info = mongo_db.get_bin_info(card_number.replace(' ', ''))
        
        if not bin_info:
            return jsonify({
                'success': False,
                'error': 'BIN bulunamadı',
                'bin_prefix': card_number[:6]
            }), 404
        
        return jsonify({
            'success': True,
            'bin_info': bin_info
        })
        
    except Exception as e:
        logger.error(f'❌ BIN API hatası: {str(e)}')
        return jsonify({
            'success': False,
            'error': str(e)
        }), 500


@app.route('/api/v1/check/<check_id>', methods=['GET'])
def get_check_record(check_id: str):
    """Kayıt sorgula"""
    try:
        if not mongo_db:
            return jsonify({
                'success': False,
                'error': 'MongoDB bağlantısı yok'
            }), 500
        
        record = mongo_db.collection.find_one({'check_id': check_id})
        
        if not record:
            return jsonify({
                'success': False,
                'error': 'Kayıt bulunamadı'
            }), 404
        
        if '_id' in record:
            record['_id'] = str(record['_id'])
        
        return jsonify({
            'success': True,
            'record': record
        })
        
    except Exception as e:
        logger.error(f'❌ Sorgulama hatası: {str(e)}')
        return jsonify({
            'success': False,
            'error': str(e)
        }), 500


@app.route('/health', methods=['GET'])
def health_check():
    """Sağlık kontrolü"""
    mongo_status = 'connected' if mongo_db else 'disconnected'
    
    return jsonify({
        'status': 'healthy',
        'timestamp': datetime.now().isoformat(),
        'service': 'Clover Card Check API (0$ Capture)',
        'version': '5.0.0',
        'environment': 'LIVE',
        'mongodb': mongo_status,
        'clover': {
            'merchant_id': CONFIG['merchant_id'][:4] + '***' if CONFIG['merchant_id'] else None,
            'charge_endpoint': CONFIG['charge_endpoint']
        }
    })


# ==================== SUNUCUYU BAŞLAT ====================

if __name__ == '__main__':
    port = int(os.getenv('PORT', 3000))
    debug = os.getenv('DEBUG', 'False').lower() == 'true'
    
    print('=' * 60)
    print('🏦 Clover Card Checker API (0$ Capture)')
    print('=' * 60)
    print(f'📍 Sunucu: http://localhost:{port}')
    print(f'🔧 Debug: {debug}')
    print(f'🌍 Environment: LIVE')
    
    print('\n📋 API Endpoint\'leri:')
    print('  POST /api/v1/check        - Kart kontrolü (0$ capture)')
    print('  POST /api/v1/check/batch  - Toplu kart kontrolü')
    print('  POST /api/v1/parse        - Sadece parse test')
    print('  GET  /api/v1/bin/<card>   - BIN kontrolü')
    print('  GET  /api/v1/check/<id>   - Kayıt sorgula')
    print('  GET  /health              - Sağlık kontrolü')
    
    print('\n🔐 Clover Konfigürasyonu:')
    print(f'  Merchant ID: {CONFIG["merchant_id"]}')
    print(f'  Company ID: {CONFIG["company_id"]}')
    print(f'  Charge Endpoint: {CONFIG["charge_endpoint"]}')
    
    print('\n💳 0$ Capture Akışı:')
    print('  1. Kart verisi parse edilir')
    print('  2. BIN check yapılır (MongoDB)')
    print('  3. Token oluşturulur (Clover)')
    print('  4. 0$ Capture=true charge yapılır')
    print('  5. Sonuç MongoDB\'ye kaydedilir')
    print('=' * 60)
    
    app.run(host='0.0.0.0', port=port, debug=debug)            if re.match(pattern, self.number):
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
