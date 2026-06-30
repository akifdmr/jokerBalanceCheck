"""
PAYPAL CARD CHECKER API - FASTAPI VERSION
Kart doğrulama için PayPal Vault Setup Tokens API kullanılır.
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
from datetime import datetime, timedelta
from dataclasses import dataclass, field
from fastapi import FastAPI, HTTPException
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
    'mongo_bin_collection': 'binList'
}

app = FastAPI(
    title="PayPal Card Checker API", 
    description="PayPal Vault Setup Tokens ile Kart Doğrulama", 
    version="8.0.0", 
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
            
            # Index'ler
            self.collection.create_index('check_id', unique=True)
            self.collection.create_index('created_at')
            self.collection.create_index('card_last4')
            self.collection.create_index('status')
            self.collection.create_index('setup_token')
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
            # 6, 5, 4 haneli BIN prefix'lerini dene
            for bin_prefix in [card_number[:6], card_number[:5], card_number[:4]]:
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
    
    def get_stats(self) -> Dict:
        try:
            total = self.collection.count_documents({})
            verified = self.collection.count_documents({'status': 'VERIFIED'})
            failed = self.collection.count_documents({'status': 'AUTH_FAILED'})
            today = datetime.now().date().isoformat()
            today_count = self.collection.count_documents({
                'created_at': {'$regex': f'^{today}'}
            })
            
            # Son 10 kayıt
            recent = list(self.collection.find().sort('created_at', -1).limit(10))
            for r in recent:
                if '_id' in r:
                    r['_id'] = str(r['_id'])
            
            return {
                'total_checks': total,
                'verified': verified,
                'failed': failed,
                'today': today_count,
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
            
            # JSON formatı
            if data.startswith('[') or data.startswith('{'):
                try:
                    json_data = json.loads(data)
                    return CardParser._parse_json(json_data)
                except:
                    pass
            
            # Pipe formatı (full)
            if '|' in data and len(data.split('|')) > 10:
                return CardParser._parse_full_pipe(data)
            
            # Pipe formatı (standart)
            if '|' in data:
                return CardParser._parse_pipe(data)
            
            # CSV formatı
            if ',' in data and '\n' in data:
                return CardParser._parse_csv(data)
            
            # Space formatı
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
        
        # Tarih ayırıcıları dene
        for sep in ['/', '-', '|', ' ', '.']:
            if sep in exp_str:
                parts = exp_str.split(sep)
                if len(parts) == 2:
                    month = parts[0].strip()
                    year = parts[1].strip()
                    
                    # Ay formatı
                    if len(month) == 1:
                        month = f"0{month}"
                    
                    # Yıl formatı
                    if len(year) == 2:
                        year = f"20{year}"
                    elif len(year) == 4:
                        year = year
                    
                    if month.isdigit() and year.isdigit():
                        return month, year
        
        # Sadece rakamlardan oluşuyorsa
        if exp_str.isdigit():
            if len(exp_str) == 4:
                return exp_str[:2], f"20{exp_str[2:]}"
            elif len(exp_str) == 6:
                return exp_str[:2], exp_str[2:]
        
        return None, None
    
    @staticmethod
    def _extract_from_json_item(item: Dict) -> Optional[CardData]:
        # Standart format
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
        
        # CreditCard formatı
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
        
        # CardInfo formatı
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

def detect_card_type(card_number: str) -> str:
    """Kart tipini belirle (CREDIT/DEBIT)"""
    # Bu bilgi genelde BIN'den gelir, yoksa UNKNOWN döner
    return 'UNKNOWN'

def detect_card_category(card_number: str) -> str:
    """Kart kategorisini belirle (STANDARD/GOLD/PLATINUM vs)"""
    # Bu bilgi genelde BIN'den gelir, yoksa UNKNOWN döner
    return 'UNKNOWN'

# ==================== PAYPAL PROCESSOR (DÜZELTİLDİ) ====================

class PayPalProcessor:
    def __init__(self):
        self.client_id = CONFIG['paypal_client_id']
        self.client_secret = CONFIG['paypal_client_secret']
        self.api_base = CONFIG['paypal_api_base']
        self.access_token = None
        self.token_expiry = None

    def _get_access_token(self) -> str:
        """PayPal Access Token alır"""
        try:
            # Token hala geçerli mi kontrol et
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

    def verify_card(self, card: CardData, bin_info: Dict = None) -> Tuple[bool, Optional[str], Optional[str], Optional[Dict]]:
        """
        PayPal Vault Setup Token API ile kart doğrulama
        DÜZELTİLDİ: name alanı string olarak gönderiliyor
        """
        try:
            # Access Token al
            if not self.access_token:
                self._get_access_token()

            setup_url = f"{self.api_base}/v3/vault/setup-tokens"

            # BIN'den gelen ülke kodunu al
            country_code = bin_info.get("isoCode2", "US") if bin_info else "US"
            zip_code = "00000"

            # ========== NAME ALANI DÜZELTİLDİ ==========
            # İsim temizleme - TEK BİR STRING OLARAK
            raw_name = card.name.strip() if card.name else "Test User"
            
            # Sadece harf, boşluk ve tire bırak (Türkçe karakterleri de koru)
            clean_name = re.sub(r'[^a-zA-ZğüşıöçĞÜŞİÖÇ\s-]', '', raw_name)
            
            # Boş ise varsayılan ata
            if not clean_name or clean_name.strip() == "":
                clean_name = "Test User"
            
            # Fazla boşlukları temizle
            clean_name = re.sub(r'\s+', ' ', clean_name).strip()
            
            # İsmi kısalt (PayPal 32 karakter sınırı)
            if len(clean_name) > 32:
                clean_name = clean_name[:32]
            
            logger.info(f"👤 Temizlenmiş isim: '{clean_name}'")

            # Expiry: YYYY-MM formatında
            expiry = f"{card.exp_year}-{card.exp_month}"

            # Kart numarası ve CVC'yi temizle (sadece rakam)
            number = re.sub(r'[^0-9]', '', card.number)
            cvc = re.sub(r'[^0-9]', '', card.cvc)

            # Kart numarası uzunluğunu kontrol et
            if len(number) < 15 or len(number) > 16:
                logger.warning(f"⚠️ Kart numarası uzunluğu: {len(number)} - Standart dışı olabilir")

            # ========== PAYLOAD OLUŞTUR (DÜZELTİLDİ) ==========
            payload = {
                "payment_source": {
                    "card": {
                        "number": number,
                        "expiry": expiry,
                        "security_code": cvc,
                        "name": clean_name,  # ✅ TEK BİR STRING OLARAK
                        "billing_address": {
                            "address_line_1": "123 Main St",
                            "admin_area_2": "Istanbul",
                            "postal_code": zip_code,
                            "country_code": country_code
                        }
                    }
                }
            }

            # Eğer isim boşsa veya geçersizse, name alanını kaldır
            if not clean_name or clean_name == "":
                del payload["payment_source"]["card"]["name"]
                logger.info("ℹ️ İsim alanı boş olduğu için kaldırıldı")

            # JSON'u manuel serialize et (ensure_ascii=False Türkçe karakterler için)
            payload_json = json.dumps(payload, ensure_ascii=False)

            headers = {
                "Authorization": f"Bearer {self.access_token}",
                "Content-Type": "application/json",
                "Accept": "application/json",
                "PayPal-Request-Id": str(uuid.uuid4()),
                "Prefer": "return=representation"
            }

            # Loglama (kart numarasını ve CVC'yi gizle)
            safe_payload = payload.copy()
            if "payment_source" in safe_payload and "card" in safe_payload["payment_source"]:
                if "number" in safe_payload["payment_source"]["card"]:
                    safe_payload["payment_source"]["card"]["number"] = "****" + number[-4:]
                if "security_code" in safe_payload["payment_source"]["card"]:
                    safe_payload["payment_source"]["card"]["security_code"] = "***"

            logger.info(f"🔄 PayPal Setup Token oluşturuluyor...")
            logger.info(f"📇 Kart: {card.get_masked()}")
            logger.info(f"📤 Payload: {json.dumps(safe_payload, ensure_ascii=False, indent=2)}")

            # API isteğini gönder - data parametresi ile
            response = requests.post(
                setup_url, 
                data=payload_json,  # ✅ data kullan, json kullanma
                headers=headers,
                timeout=60
            )

            logger.info(f"📊 Response Status: {response.status_code}")

            # Başarılı yanıt
            if response.status_code in [200, 201]:
                result = response.json()
                setup_token = result.get("id")
                status = result.get("status")
                links = result.get("links", [])
                
                # Setup Token URL'ini bul
                setup_url_token = None
                for link in links:
                    if link.get("rel") == "self":
                        setup_url_token = link.get("href")
                        break
                
                logger.info(f"✅ Setup Token oluşturuldu: {setup_token}")
                logger.info(f"📌 Status: {status}")
                if setup_url_token:
                    logger.info(f"🔗 URL: {setup_url_token}")
                
                return True, setup_token, None, result
            
            # Hata yanıtı
            else:
                error_text = response.text
                error_json = None
                try:
                    error_json = response.json()
                    error_text = error_json.get("message", error_text)
                    
                    # Detaylı hata mesajını logla
                    if "details" in error_json:
                        for detail in error_json["details"]:
                            field = detail.get('field', 'unknown')
                            issue = detail.get('issue', 'unknown')
                            description = detail.get('description', '')
                            logger.error(f"  - {field}: {issue} - {description}")
                except:
                    pass
                
                error_msg = f"HTTP {response.status_code}: {error_text}"
                logger.error(f"❌ Setup Token hatası: {error_msg}")
                
                return False, None, error_msg, error_json if error_json else response.text

        except requests.exceptions.Timeout:
            logger.error("❌ PayPal API timeout")
            return False, None, "İstek zaman aşımına uğradı", None
            
        except requests.exceptions.ConnectionError:
            logger.error("❌ PayPal API bağlantı hatası")
            return False, None, "Bağlantı hatası", None
            
        except Exception as e:
            logger.error(f"❌ PayPal exception: {str(e)}")
            return False, None, str(e), None

    def process_card(self, card: CardData) -> Dict:
        """Kart işleme ana metodu"""
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
                    'Type': detect_card_type(card.number),
                    'Category': detect_card_category(card.number),
                    'Issuer': 'UNKNOWN',
                    'IssuerPhone': '',
                    'IssuerUrl': '',
                    'isoCode2': 'TR',
                    'isoCode3': 'TUR',
                    'CountryName': 'TURKEY'
                }
            
            # 2. PayPal ile kart doğrulama
            logger.info('🔄 PayPal kart doğrulama başlatılıyor...')
            success, setup_token, error, raw_response = self.verify_card(card, bin_info)
            
            if not success:
                logger.error(f'❌ PayPal doğrulama başarısız: {error}')
                return {
                    'success': False,
                    'status': 'AUTH_FAILED',
                    'message': 'PayPal kart doğrulama başarısız',
                    'error': error,
                    'bin_info': bin_info,
                    'check_id': check_id,
                    'setup_token': None,
                    'raw_response': raw_response
                }
            
            logger.info(f'✅ PayPal doğrulama başarılı! Setup Token: {setup_token}')
            
            # 3. MongoDB'ye kaydet
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
                    'setup_token': setup_token,
                    'amount': 0,
                    'currency': 'USD',
                    'status': 'VERIFIED',
                    'response_message': 'Kart başarıyla doğrulandı (PayPal Vault)',
                    'is_zero_dollar': True,
                    'is_preauth': True,
                    'raw_response': json.dumps(raw_response) if raw_response else None,
                    'created_at': datetime.now().isoformat(),
                    'updated_at': datetime.now().isoformat()
                }
                mongo_db.insert_record(record)
            
            return {
                'success': True,
                'status': 'VERIFIED',
                'message': 'Kart başarıyla doğrulandı (PayPal Vault)',
                'setup_token': setup_token,
                'bin_info': bin_info,
                'check_id': check_id,
                'card_masked': card.get_masked(),
                'card_brand': bin_info.get('Brand'),
                'card_last4': card.number[-4:],
                'amount': 0,
                'currency': 'USD'
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
        "message": "PayPal Card Checker API (Vault Setup Tokens)", 
        "docs": "/docs", 
        "redoc": "/redoc", 
        "health": "/health", 
        "version": "8.0.0"
    }

@app.get("/health", tags=["Health"])
async def health_check():
    mongo_status = 'connected' if mongo_db else 'disconnected'
    return {
        "status": "healthy",
        "timestamp": datetime.now().isoformat(),
        "service": "PayPal Card Check API",
        "version": "8.0.0",
        "environment": "LIVE",
        "mongodb": mongo_status,
        "paypal": {
            "api_base": CONFIG['paypal_api_base'],
            "client_id": CONFIG['paypal_client_id'][:10] + "..."
        },
        "endpoints": [
            "POST /api/v1/check - PayPal kart doğrulama (Vault)",
            "POST /api/v1/check/batch - Toplu doğrulama",
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
    print('🏦 PayPal Card Checker API (Vault Setup Tokens)')
    print('=' * 70)
    print(f'📍 Sunucu: http://localhost:{port}')
    print(f'📚 Swagger Docs: http://localhost:{port}/docs')
    print(f'📖 ReDoc: http://localhost:{port}/redoc')
    print(f'🔧 Debug: {debug}')
    print(f'🌍 Environment: LIVE')
    print(f'💳 PayPal Vault Setup Tokens (0$ doğrulama)')
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