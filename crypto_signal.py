import sys
import asyncio
if sys.platform.startswith("win"):
    asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())
import pandas as pd
import ta
from datetime import datetime, timedelta
from telegram.ext import Application, CommandHandler, MessageHandler, filters
import json
import aiohttp
from dotenv import load_dotenv
import os
from pymongo import MongoClient
from pymongo.errors import ConnectionFailure, ServerSelectionTimeoutError
from decimal import Decimal, ROUND_DOWN, getcontext
from binance.client import Client

load_dotenv()

TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")

# MongoDB bağlantı bilgileri
MONGODB_URI = os.getenv("MONGODB_URI", "mongodb://localhost:27017/")
MONGODB_DB = os.getenv("MONGODB_DB", "crypto_signal_bot")
MONGODB_COLLECTION = os.getenv("MONGODB_COLLECTION", "allowed_users")

# Bot sahibinin ID'si (bu değeri .env dosyasından alabilirsiniz)
BOT_OWNER_ID = int(os.getenv("BOT_OWNER_ID", "0"))

# Admin kullanıcıları listesi (bot sahibi tarafından yönetilir)
ADMIN_USERS = set()

# MongoDB bağlantısı
mongo_client = None
mongo_db = None
mongo_collection = None

# Binance client
client = Client()

# === Genel Telegram Komut Yardımcı Fonksiyonları ===
def validate_user_command(update, require_admin=False, require_owner=False):
    """Kullanıcı komut yetkisini kontrol eder"""
    if not update.effective_user:
        return None, False
    
    user_id = update.effective_user.id
    
    if require_owner and user_id != BOT_OWNER_ID:
        return user_id, False
    
    if require_admin and not is_admin(user_id):
        return user_id, False
    
    return user_id, True

def validate_command_args(update, context, expected_args=1):
    """Komut argümanlarını kontrol eder"""
    if not context.args or len(context.args) < expected_args:
        # Komut adını update.message.text'ten al
        command_name = "komut"
        if update and update.message and update.message.text:
            text = update.message.text
            if text.startswith('/'):
                command_name = text.split()[0][1:]  # /adduser -> adduser
        return False, f"❌ Kullanım: /{command_name} {' '.join(['<arg>'] * expected_args)}"
    return True, None

def validate_user_id(user_id_str):
    """User ID'yi doğrular ve döndürür"""
    try:
        user_id = int(user_id_str)
        return True, user_id
    except ValueError:
        return False, "❌ Geçersiz user_id. Lütfen sayısal bir değer girin."

async def send_command_response(update, message, parse_mode='Markdown'):
    """Komut yanıtını gönderir"""
    await update.message.reply_text(message, parse_mode=parse_mode)

# === Genel DB Yardımcı Fonksiyonları ===
def save_data_to_db(doc_id, data, collection_name="data"):
    """Genel veri kaydetme fonksiyonu (upsert)."""
    global mongo_collection
    if mongo_collection is None:
        return False
    try:
        mongo_collection.update_one(
            {"_id": doc_id},
            {"$set": {"data": data, "updated_at": str(datetime.now())}},
            upsert=True
        )
        return True
    except Exception as e:
        print(f"❌ {collection_name} DB kaydı hatası: {e}")
        return False

def load_data_from_db(doc_id, default_value=None):
    """Genel veri okuma fonksiyonu."""
    global mongo_collection
    if mongo_collection is None:
        return default_value
    try:
        doc = mongo_collection.find_one({"_id": doc_id})
        if doc:
            # Eğer "data" alanı varsa onu döndür, yoksa tüm dokümanı döndür (geriye uyumluluk için)
            if "data" in doc:
                return doc["data"]
            else:
                # "data" alanı yoksa, tüm dokümanı döndür (geriye uyumluluk için)
                return doc
    except Exception as e:
        print(f"❌ {doc_id} DB okuma hatası: {e}")
    return default_value

# === Özel DB Fonksiyonları (Genel fonksiyonları kullanır) ===
def save_stats_to_db(stats):
    """İstatistik sözlüğünü MongoDB'ye kaydeder."""
    return save_data_to_db("bot_stats", stats, "Stats")

def load_stats_from_db():
    """MongoDB'den son istatistik sözlüğünü döndürür."""
    return load_data_from_db("bot_stats", {})

def save_active_signals_to_db(active_signals):
    """Aktif sinyalleri MongoDB'ye kaydeder."""
    try:
        if mongo_collection is None:
            if not connect_mongodb():
                print("❌ MongoDB bağlantısı kurulamadı, aktif sinyaller kaydedilemedi")
                return False
        
        # Her aktif sinyali ayrı doküman olarak kaydet
        for symbol, signal in active_signals.items():
            signal_doc = {
                "_id": f"active_signal_{symbol}",
                "symbol": signal["symbol"],
                "type": signal["type"],
                "entry_price": signal["entry_price"],
                "entry_price_float": signal["entry_price_float"],
                "target_price": signal["target_price"],
                "stop_loss": signal["stop_loss"],
                "signals": signal["signals"],
                "leverage": signal["leverage"],
                "signal_time": signal["signal_time"],
                "current_price": signal["current_price"],
                "current_price_float": signal["current_price_float"],
                "last_update": signal["last_update"],
                "saved_at": str(datetime.now())
            }
            
            # Genel DB fonksiyonunu kullan
            if not save_data_to_db(f"active_signal_{symbol}", signal_doc, "Aktif Sinyal"):
                return False
        
        print(f"✅ MongoDB'ye {len(active_signals)} aktif sinyal kaydedildi")
        return True
    except Exception as e:
        print(f"❌ MongoDB'ye aktif sinyaller kaydedilirken hata: {e}")
        return False

def load_active_signals_from_db():
    """MongoDB'den aktif sinyalleri döndürür."""
    try:
        if mongo_collection is None:
            if not connect_mongodb():
                print("❌ MongoDB bağlantısı kurulamadı, aktif sinyaller yüklenemedi")
                return {}
        
        result = {}
        docs = mongo_collection.find({"_id": {"$regex": "^active_signal_"}})
        
        for doc in docs:
            # Symbol alanı yoksa bu dokümanı atla
            if "symbol" not in doc:
                continue
                
            symbol = doc["symbol"]
            result[symbol] = {
                "symbol": doc["symbol"],
                "type": doc["type"],
                "entry_price": doc["entry_price"],
                "entry_price_float": doc["entry_price_float"],
                "target_price": doc["target_price"],
                "stop_loss": doc["stop_loss"],
                "signals": doc["signals"],
                "leverage": doc["leverage"],
                "signal_time": doc["signal_time"],
                "current_price": doc["current_price"],
                "current_price_float": doc["current_price_float"],
                "last_update": doc["last_update"]
            }
        
        print(f"✅ MongoDB'den {len(result)} aktif sinyal yüklendi")
        return result
    except Exception as e:
        print(f"❌ MongoDB'den aktif sinyaller yüklenirken hata: {e}")
        return {}

# İzin verilen kullanıcılar listesi (bot sahibi tarafından yönetilir)
ALLOWED_USERS = set()

def connect_mongodb():
    """MongoDB bağlantısını kur"""
    global mongo_client, mongo_db, mongo_collection
    try:
        mongo_client = MongoClient(MONGODB_URI, serverSelectionTimeoutMS=5000)
        # Bağlantıyı test et
        mongo_client.admin.command('ping')
        mongo_db = mongo_client[MONGODB_DB]
        mongo_collection = mongo_db[MONGODB_COLLECTION]
        print("✅ MongoDB bağlantısı başarılı")
        return True
    except (ConnectionFailure, ServerSelectionTimeoutError) as e:
        print(f"❌ MongoDB bağlantı hatası: {e}")
        return False
    except Exception as e:
        print(f"❌ MongoDB bağlantı hatası: {e}")
        return False

def load_allowed_users():
    """İzin verilen kullanıcıları ve admin bilgilerini MongoDB'den yükle"""
    global ALLOWED_USERS, BOT_OWNER_GROUPS, ADMIN_USERS
    try:
        if not connect_mongodb():
            print("⚠️ MongoDB bağlantısı kurulamadı, boş liste ile başlatılıyor")
            ALLOWED_USERS = set()
            BOT_OWNER_GROUPS = set()
            ADMIN_USERS = set()
            return
        
        # MongoDB'den kullanıcıları çek - genel fonksiyonu kullan
        users_data = load_data_from_db("allowed_users")
        if users_data and 'user_ids' in users_data:
            ALLOWED_USERS = set(users_data['user_ids'])
            print(f"✅ MongoDB'den {len(ALLOWED_USERS)} izin verilen kullanıcı yüklendi")
        else:
            print("ℹ️ MongoDB'de izin verilen kullanıcı bulunamadı, boş liste ile başlatılıyor")
            ALLOWED_USERS = set()
        
        # Admin gruplarını çek - genel fonksiyonu kullan
        admin_groups_data = load_data_from_db("admin_groups")
        if admin_groups_data and 'group_ids' in admin_groups_data:
            BOT_OWNER_GROUPS = set(admin_groups_data['group_ids'])
            print(f"✅ MongoDB'den {len(BOT_OWNER_GROUPS)} admin grubu yüklendi")
        else:
            print("ℹ️ MongoDB'de admin grubu bulunamadı, boş liste ile başlatılıyor")
            BOT_OWNER_GROUPS = set()
        
        # Admin kullanıcılarını çek - genel fonksiyonu kullan
        admin_users_data = load_data_from_db("admin_users")
        if admin_users_data and 'admin_ids' in admin_users_data:
            ADMIN_USERS = set(admin_users_data['admin_ids'])
            print(f"✅ MongoDB'den {len(ADMIN_USERS)} admin kullanıcı yüklendi")
        else:
            print("ℹ️ MongoDB'de admin kullanıcı bulunamadı, boş liste ile başlatılıyor")
            ADMIN_USERS = set()
    except Exception as e:
        print(f"❌ MongoDB'den veriler yüklenirken hata: {e}")
        ALLOWED_USERS = set()
        BOT_OWNER_GROUPS = set()
        ADMIN_USERS = set()

def save_allowed_users():
    """İzin verilen kullanıcıları MongoDB'ye kaydet"""
    try:
        if mongo_collection is None:
            if not connect_mongodb():
                print("❌ MongoDB bağlantısı kurulamadı, kullanıcılar kaydedilemedi")
                return False
        
        user_data = {
            "user_ids": list(ALLOWED_USERS),
            "last_updated": str(datetime.now()),
            "count": len(ALLOWED_USERS)
        }
        
        if save_data_to_db("allowed_users", user_data, "İzin Verilen Kullanıcılar"):
            print(f"✅ MongoDB'ye {len(ALLOWED_USERS)} izin verilen kullanıcı kaydedildi")
            return True
        return False
    except Exception as e:
        print(f"❌ MongoDB'ye kullanıcılar kaydedilirken hata: {e}")
        return False

def save_admin_groups():
    """Admin gruplarını MongoDB'ye kaydet"""
    try:
        if mongo_collection is None:
            if not connect_mongodb():
                print("❌ MongoDB bağlantısı kurulamadı, admin grupları kaydedilemedi")
                return False
        
        group_data = {
            "group_ids": list(BOT_OWNER_GROUPS),
            "last_updated": str(datetime.now()),
            "count": len(BOT_OWNER_GROUPS)
        }
        
        if save_data_to_db("admin_groups", group_data, "Admin Grupları"):
            print(f"✅ MongoDB'ye {len(BOT_OWNER_GROUPS)} admin grubu kaydedildi")
            return True
        return False
    except Exception as e:
        print(f"❌ MongoDB'ye admin grupları kaydedilirken hata: {e}")
        return False

def save_admin_users():
    """Admin kullanıcılarını MongoDB'ye kaydet"""
    try:
        if mongo_collection is None:
            if not connect_mongodb():
                print("❌ MongoDB bağlantısı kurulamadı, admin kullanıcıları kaydedilemedi")
                return False
        
        admin_data = {
            "admin_ids": list(ADMIN_USERS),
            "last_updated": str(datetime.now()),
            "count": len(ADMIN_USERS)
        }
        
        if save_data_to_db("admin_users", admin_data, "Admin Kullanıcıları"):
            print(f"✅ MongoDB'ye {len(ADMIN_USERS)} admin kullanıcı kaydedildi")
            return True
        return False
    except Exception as e:
        print(f"❌ MongoDB'ye admin kullanıcıları kaydedilirken hata: {e}")
        return False

def close_mongodb():
    """MongoDB bağlantısını kapat"""
    global mongo_client
    if mongo_client:
        try:
            mongo_client.close()
            print("✅ MongoDB bağlantısı kapatıldı")
        except Exception as e:
            print(f"⚠️ MongoDB bağlantısı kapatılırken hata: {e}")

def save_positions_to_db(positions):
    """Pozisyonları MongoDB'ye kaydet"""
    try:
        if mongo_collection is None:
            if not connect_mongodb():
                print("❌ MongoDB bağlantısı kurulamadı, pozisyonlar kaydedilemedi")
                return False
        
        # Tüm pozisyonları tek seferde kaydet
        for symbol, position in positions.items():
            position_doc = {
                "_id": f"position_{symbol}",
                "symbol": symbol,
                "type": position["type"],
                "target": position["target"],
                "stop": position["stop"],
                "open_price": position["open_price"],
                "stop_str": position["stop_str"],
                "signals": position["signals"],
                "leverage": position.get("leverage", 10),
                "entry_time": position["entry_time"],
                "entry_timestamp": position["entry_timestamp"].isoformat() if isinstance(position["entry_timestamp"], datetime) else position["entry_timestamp"],
                "last_updated": str(datetime.now())
            }
            
            # Genel DB fonksiyonunu kullan
            if not save_data_to_db(f"position_{symbol}", position_doc, "Pozisyon"):
                return False
        
        print(f"✅ MongoDB'ye {len(positions)} pozisyon kaydedildi")
        return True
    except Exception as e:
        print(f"❌ MongoDB'ye pozisyonlar kaydedilirken hata: {e}")
        return False

def load_positions_from_db():
    """Pozisyonları MongoDB'den yükle"""
    try:
        def transform_position(doc):
            # _id'den symbol'ü çıkar (position_BTCUSDT -> BTCUSDT)
            symbol = doc["_id"].replace("position_", "")
            return {symbol: {
                "type": doc.get("type", "ALIS"),  # Varsayılan ALIS
                "target": doc.get("target", 0.0),
                "stop": doc.get("stop", 0.0),
                "open_price": doc.get("open_price", 0.0),
                "stop_str": doc.get("stop_str", ""),
                "signals": doc.get("signals", {}),
                "leverage": doc.get("leverage", 10),
                "entry_time": doc.get("entry_time", ""),
                "entry_timestamp": datetime.fromisoformat(doc["entry_timestamp"]) if isinstance(doc.get("entry_timestamp"), str) else doc.get("entry_timestamp", datetime.now()),

            }}
        
        # Pozisyon dokümanlarında "data" alanı yok, direkt dokümanı kullan
        return load_data_by_pattern("^position_", None, "pozisyon", transform_position)
    except Exception as e:
        print(f"❌ MongoDB'den pozisyonlar yüklenirken hata: {e}")
        return {}

def save_previous_signals_to_db(previous_signals):
    """Önceki sinyalleri MongoDB'ye kaydet (sadece ilk çalıştırmada)"""
    try:
        if mongo_collection is None:
            if not connect_mongodb():
                print("❌ MongoDB bağlantısı kurulamadı, önceki sinyaller kaydedilemedi")
                return False
        
        # Önceki sinyallerin kaydedilip kaydedilmediğini kontrol et
        existing_doc = mongo_collection.find_one({"_id": "previous_signals_initialized"})
        if existing_doc:
            print("ℹ️ Önceki sinyaller zaten kaydedilmiş, tekrar kaydedilmiyor")
            return True
        
        # Tüm önceki sinyalleri kaydet
        for symbol, signals in previous_signals.items():
            signal_doc = {
                "_id": f"previous_signal_{symbol}",
                "symbol": symbol,
                "signals": signals,
                "saved_time": str(datetime.now())
            }
            
            # Genel DB fonksiyonunu kullan
            if not save_data_to_db(f"previous_signal_{symbol}", signal_doc, "Önceki Sinyal"):
                return False
        
        # İlk kayıt işaretini koy
        if not save_data_to_db("previous_signals_initialized", {"initialized": True, "initialized_time": str(datetime.now())}, "İlk Kayıt"):
            return False
        
        print(f"✅ MongoDB'ye {len(previous_signals)} önceki sinyal kaydedildi (ilk çalıştırma)")
        return True
    except Exception as e:
        print(f"❌ MongoDB'ye önceki sinyaller kaydedilirken hata: {e}")
        return False

def load_previous_signals_from_db():
    """Önceki sinyalleri MongoDB'den yükle"""
    try:
        def transform_signal(doc):
            # _id'den symbol'ü çıkar (previous_signal_BTCUSDT -> BTCUSDT)
            symbol = doc["_id"].replace("previous_signal_", "")
            if "signals" in doc:
                return {symbol: doc["signals"]}
            else:
                # Eski veri yapısı için geriye uyumluluk
                return {symbol: doc}
        
        # Genel DB fonksiyonunu kullan
        return load_data_by_pattern("^previous_signal_", "signals", "önceki sinyal", transform_signal)
    except Exception as e:
        print(f"❌ MongoDB'den önceki sinyaller yüklenirken hata: {e}")
        return {}

def is_first_run():
    """İlk çalıştırma mı kontrol et"""
    def check_first_run():
        # Önceki sinyallerin kaydedilip kaydedilmediğini kontrol et
        existing_doc = mongo_collection.find_one({"_id": "previous_signals_initialized"})
        if existing_doc is None:
            return True  # İlk çalıştırma
        
        # Pozisyonların varlığını da kontrol et
        position_count = mongo_collection.count_documents({"_id": {"$regex": "^position_"}})
        if position_count > 0:
            print(f"📊 MongoDB'de {position_count} aktif pozisyon bulundu, yeniden başlatma olarak algılanıyor")
            return False  # Yeniden başlatma
        
        # Önceki sinyallerin varlığını kontrol et
        signal_count = mongo_collection.count_documents({"_id": {"$regex": "^previous_signal_"}})
        if signal_count > 0:
            print(f"📊 MongoDB'de {signal_count} önceki sinyal bulundu, yeniden başlatma olarak algılanıyor")
            return False  # Yeniden başlatma
        
        return True  # İlk çalıştırma
    
    return safe_mongodb_operation(check_first_run, "İlk çalıştırma kontrolü", True)

def update_previous_signal_in_db(symbol, signals):
    """Belirli bir coin'in önceki sinyallerini güncelle"""
    try:
        if mongo_collection is None:
            if not connect_mongodb():
                return False
        
        signal_doc = {
            "_id": f"previous_signal_{symbol}",
            "symbol": symbol,
            "signals": signals,
            "updated_time": str(datetime.now())
        }
        
        # Genel DB fonksiyonunu kullan
        if not save_data_to_db(f"previous_signal_{symbol}", signal_doc, "Önceki Sinyal"):
            return False
        
        return True
    except Exception as e:
        print(f"❌ Önceki sinyal güncellenirken hata: {e}")
        return False

def remove_position_from_db(symbol):
    """Pozisyonu MongoDB'den kaldır"""
    def delete_position():
        # Pozisyonu sil
        mongo_collection.delete_one({"_id": f"position_{symbol}"})
        print(f"✅ {symbol} pozisyonu MongoDB'den kaldırıldı")
        return True
    
    return safe_mongodb_operation(delete_position, f"{symbol} pozisyonu kaldırma", False)

# Bot handler'ları için global değişkenler
app = None

# MongoDB kullanıldığı için dosya referansını kaldırıyoruz
# Global değişkenler (main fonksiyonundan erişim için)
global_stats = {
    "total_signals": 0,
    "successful_signals": 0,
    "failed_signals": 0,
    "total_profit_loss": 0.0,
    "active_signals_count": 0,
    "tracked_coins_count": 0
}
global_active_signals = {}

# Bekleme listesi sistemi için global değişkenler
global_waiting_signals = {}  # {symbol: {"signals": {...}, "volume": float, "type": str, "timestamp": datetime}}
global_waiting_previous_signals = {}  # Bekleme listesindeki sinyallerin önceki durumları

# 7/7 özel sinyal sistemi için global değişkenler
global_successful_signals = {}
global_failed_signals = {}
# global_changed_symbols artık kullanılmıyor
global_allowed_users = set()  # İzin verilen kullanıcılar
global_admin_users = set()  # Admin kullanıcılar
# Saatlik yeni sinyal taraması için zaman damgası
global_last_signal_scan_time = None

# 15m mum kapanış onayı bekleyen genel sinyaller (ALIŞ/SATIŞ)
global_pending_signals = {}

def is_authorized_chat(update):
    """Kullanıcının yetkili olduğu sohbet mi kontrol et"""
    chat = update.effective_chat
    if not chat or not update.effective_user:
        return False
    
    user_id = update.effective_user.id
    
    # Özel sohbet ve yetkili kullanıcı
    if chat.type == "private":
        return user_id == BOT_OWNER_ID or user_id in ALLOWED_USERS or user_id in ADMIN_USERS
    
    # Bot sahibinin eklediği grup/kanal ve bot sahibi
    if chat.type in ["group", "supergroup", "channel"] and chat.id in BOT_OWNER_GROUPS:
        return user_id == BOT_OWNER_ID or user_id in ADMIN_USERS
    
    return False

def should_respond_to_message(update):
    """Mesaja yanıt verilmeli mi kontrol et (gruplarda ve kanallarda sadece bot sahibi ve adminler)"""
    chat = update.effective_chat
    if not chat or not update.effective_user:
        return False
    
    user_id = update.effective_user.id
    
    # Özel sohbet ve yetkili kullanıcı
    if chat.type == "private":
        return user_id == BOT_OWNER_ID or user_id in ALLOWED_USERS or user_id in ADMIN_USERS
    
    # Bot sahibinin eklediği grup/kanal ve sadece bot sahibi ve adminler
    if chat.type in ["group", "supergroup", "channel"] and chat.id in BOT_OWNER_GROUPS:
        return user_id == BOT_OWNER_ID or user_id in ADMIN_USERS
    
    return False

def is_admin(user_id):
    """Kullanıcının admin olup olmadığını kontrol et"""
    return user_id == BOT_OWNER_ID or user_id in ADMIN_USERS

async def send_telegram_message(message, chat_id=None):
    """Telegram mesajı gönder"""
    try:
        if not chat_id:
            chat_id = TELEGRAM_CHAT_ID
        
        if not chat_id:
            print("❌ Telegram chat ID bulunamadı!")
            return False
        
        # Connection pool ayarlarını güncelle
        connector = aiohttp.TCPConnector(
            limit=100,  # Bağlantı limitini artır
            limit_per_host=30,  # Host başına limit
            ttl_dns_cache=300,  # DNS cache süresi
            use_dns_cache=True,
            keepalive_timeout=30,
            enable_cleanup_closed=True
        )
        
        timeout = aiohttp.ClientTimeout(total=30, connect=10)
        
        async with aiohttp.ClientSession(
            connector=connector, 
            timeout=timeout,
            headers={'User-Agent': 'Mozilla/5.0'}
        ) as session:
            url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
            data = {
                'chat_id': chat_id,
                'text': message,
                'parse_mode': 'HTML',
                'disable_web_page_preview': True
            }
            
            async with session.post(url, json=data, ssl=False) as response:
                if response.status == 200:
                    return True
                else:
                    response_text = await response.text()
                    print(f"❌ Telegram API hatası: {response.status} - {response_text}")
                    return False
                    
    except asyncio.TimeoutError:
        print(f"❌ Telegram mesaj gönderme timeout: {chat_id}")
        return False
    except Exception as e:
        print(f"❌ Mesaj gönderme hatası (chat_id: {chat_id}): {e}")
        return False

async def send_signal_to_all_users(message):
    """Sinyali sadece izin verilen kullanıcılara, gruplara ve kanallara gönder"""
    sent_chats = set()  # Gönderilen chat'leri takip et
    
    # Bot sahibine özel mesaj gönderme kaldırıldı (grup/kanal üzerinden alacak)
    # İzin verilen kullanıcılara gönder
    for user_id in ALLOWED_USERS:
        if str(user_id) not in sent_chats:
            try:
                await send_telegram_message(message, user_id)
                print(f"✅ Kullanıcıya sinyal gönderildi: {user_id}")
                sent_chats.add(str(user_id))
            except Exception as e:
                print(f"❌ Kullanıcıya sinyal gönderilemedi ({user_id}): {e}")
    
    # İzin verilen gruplara ve kanallara gönder
    for group_id in BOT_OWNER_GROUPS:
        if str(group_id) not in sent_chats:
            try:
                await send_telegram_message(message, group_id)
                print(f"✅ Gruba/Kanala sinyal gönderildi: {group_id}")
                sent_chats.add(str(group_id))
            except Exception as e:
                print(f"❌ Gruba/Kanala sinyal gönderilemedi ({group_id}): {e}")

# start_command fonksiyonunu kaldır

async def help_command(update, context):
    """Yardım komutu"""
    if not update.effective_user:
        return
    
    user_id = update.effective_user.id
    
    # Sadece bot sahibi, adminler ve izin verilen kullanıcılar kullanabilir
    if user_id != BOT_OWNER_ID and user_id not in ALLOWED_USERS and user_id not in ADMIN_USERS:
        return  # İzin verilmeyen kullanıcılar için hiçbir yanıt verme
    
    # Bot sahibi için tüm komutları göster
    if user_id == BOT_OWNER_ID:
        help_text = """
👑 **Kripto Sinyal Botu Komutları (Bot Sahibi):**

📊 **Temel Komutlar:**
/help - Bu yardım mesajını göster
/stats - İstatistikleri göster
/active - Aktif sinyalleri göster
/test - Test sinyali gönder

👥 **Kullanıcı Yönetimi:**
/adduser <user_id> - Kullanıcı ekle
/removeuser <user_id> - Kullanıcı çıkar
/listusers - İzin verilen kullanıcıları listele

👑 **Admin Yönetimi:**
/adminekle <user_id> - Admin ekle
/adminsil <user_id> - Admin sil
/listadmins - Admin listesini göster

🧹 **Temizleme Komutları:**
/clearall - Tüm verileri temizle (pozisyonlar, önceki sinyaller, bekleyen kuyruklar, istatistikler)

🔧 **Özel Yetkiler:**
• Tüm komutlara erişim
• Admin ekleme/silme
• Veri temizleme
• Bot tam kontrolü
        """
    elif user_id in ADMIN_USERS:
        # Adminler için admin komutlarını da göster
        help_text = """
🛡️ **Kripto Sinyal Botu Komutları (Admin):**

📊 **Temel Komutlar:**
/help - Bu yardım mesajını göster
/stats - İstatistikleri göster
/active - Aktif sinyalleri göster
/test - Test sinyali gönder

👥 **Kullanıcı Yönetimi:**
/adduser <user_id> - Kullanıcı ekle
/removeuser <user_id> - Kullanıcı çıkar
/listusers - İzin verilen kullanıcıları listele

👑 **Admin Yönetimi:**
/listadmins - Admin listesini göster

🔧 **Yetkiler:**
• Kullanıcı ekleme/silme
• Test sinyali gönderme
• İstatistik görüntüleme
• Admin listesi görüntüleme
        """
    else:
        # İzin verilen kullanıcılar için sadece temel komutları göster
        help_text = """
📱 **Kripto Sinyal Botu Komutları (Kullanıcı):**

📊 **Temel Komutlar:**
/help - Bu yardım mesajını göster
/active - Aktif sinyalleri göster

🔧 **Yetkiler:**
• Aktif sinyalleri görüntüleme
• Sinyal mesajlarını alma
        """
    
    # Mesajı özel mesaj olarak gönder (grup yerine)
    try:
        await context.bot.send_message(
            chat_id=user_id,
            text=help_text,
            parse_mode='Markdown'
        )
        # Grup mesajını sil (isteğe bağlı)
        if update.message.chat.type != 'private':
            await update.message.delete()
    except Exception as e:
        print(f"❌ Özel mesaj gönderilemedi ({user_id}): {e}")
        # Eğer özel mesaj gönderilemezse, grup mesajı olarak gönder
        await update.message.reply_text(help_text, parse_mode='Markdown')

async def test_command(update, context):
    """Test sinyali gönderme komutu (sadece bot sahibi ve adminler)"""
    if not update.effective_user:
        return
    
    user_id = update.effective_user.id
    
    # Sadece bot sahibi ve adminler kullanabilir
    if not is_admin(user_id):
        return  # İzin verilmeyen kullanıcılar için hiçbir yanıt verme
    
    # Test sinyali oluştur - gerçek format
    test_message = """🚨 AL SİNYALİ 🚨

🔹 Kripto Çifti: BTCUSDT  
💵 Giriş Fiyatı: $45,000.00
📈 Hedef Fiyat: $46,350.00  
📉 Stop Loss: $43,875.00
⚡ Kaldıraç: 10x
📊 24h Hacim: $2.5B

⚠️ <b>ÖNEMLİ UYARILAR:</b>
• Bu bir yatırım tavsiyesi değildir
• Stopunuzu en fazla %25 ayarlayın

📺 <b>Kanallar:</b>
🔗 <a href="https://www.youtube.com/@kriptotek">YouTube</a> | <a href="https://t.me/kriptotek8907">Telegram</a> | <a href="https://x.com/kriptotek8907">X</a> | <a href="https://www.instagram.com/kriptotek/">Instagram</a>

⚠️ <b>Bu bir test sinyalidir!</b> ⚠️"""
    
    await update.message.reply_text("🧪 Test sinyali gönderiliyor...")
    
    # Test sinyalini gönder
    await send_signal_to_all_users(test_message)
    
    await update.message.reply_text("✅ Test sinyali başarıyla gönderildi!")

async def stats_command(update, context):
    """İstatistik komutu (sadece bot sahibi ve adminler)"""
    if not update.effective_user:
        return
    
    user_id = update.effective_user.id
    
    # Sadece bot sahibi ve adminler kullanabilir
    if not is_admin(user_id):
        return  # İzin verilmeyen kullanıcılar için hiçbir yanıt verme
    
    # DB'den istatistikleri al; yoksa global yedeğe düş
    stats = load_stats_from_db() or global_stats
    if not stats:
        stats_text = "📊 **Bot İstatistikleri:**\n\nHenüz istatistik verisi yok."
    else:
        closed_count = stats.get('successful_signals', 0) + stats.get('failed_signals', 0)
        success_rate = 0
        if closed_count > 0:
            success_rate = (stats.get('successful_signals', 0) / closed_count) * 100
        
        # Görüntülenecek toplam: başarılı + başarısız + aktif (bekleyenler hariç)
        computed_total = (
            stats.get('successful_signals', 0)
            + stats.get('failed_signals', 0)
            + stats.get('active_signals_count', 0)
        )
        
        # Bot durumu
        status_emoji = "🟢"
        status_text = "Aktif (Sinyal Arama Çalışıyor)"
        
        stats_text = f"""📊 **Bot İstatistikleri:**

📈 **Genel Durum:**
• Toplam Sinyal: {computed_total}
• Başarılı: {stats.get('successful_signals', 0)}
• Başarısız: {stats.get('failed_signals', 0)}
• Aktif Sinyal: {stats.get('active_signals_count', 0)}
• Takip Edilen Coin: {stats.get('tracked_coins_count', 0)}

💰 **Kar/Zarar (100$ yatırım):**
• Toplam: ${stats.get('total_profit_loss', 0):.2f}
• Başarı Oranı: %{success_rate:.1f}

🕒 **Son Güncelleme:** {datetime.now().strftime('%H:%M:%S')}
{status_emoji} **Bot Durumu:** {status_text}"""
    
    await update.message.reply_text(stats_text, parse_mode='Markdown')

async def active_command(update, context):
    """Aktif sinyaller komutu"""
    if not update.effective_user:
        return
    
    user_id = update.effective_user.id
    
    # Sadece bot sahibi, adminler ve izin verilen kullanıcılar kullanabilir
    if user_id != BOT_OWNER_ID and user_id not in ALLOWED_USERS and user_id not in ADMIN_USERS:
        return  # İzin verilmeyen kullanıcılar için hiçbir yanıt verme
    
    # Aktif sinyalleri DB'den oku; yoksa global yedeğe düş
    active_signals = load_active_signals_from_db() or global_active_signals
    if not active_signals:
        active_text = "📈 **Aktif Sinyaller:**\n\nHenüz aktif sinyal yok."
    else:
        active_text = "📈 **Aktif Sinyaller:**\n\n"
        for symbol, signal in active_signals.items():
            active_text += f"""🔹 **{symbol}** ({signal['type']})
• Giriş: {signal['entry_price']}
• Hedef: {signal['target_price']}
• Stop: {signal['stop_loss']}
• Şu anki: {signal['current_price']}
• Kaldıraç: {signal['leverage']}x
• Sinyal: {signal['signal_time']}

"""
    
    await update.message.reply_text(active_text, parse_mode='Markdown')

async def adduser_command(update, context):
    """Kullanıcı ekleme komutu (sadece bot sahibi ve adminler)"""
    user_id, is_authorized = validate_user_command(update, require_admin=True)
    if not is_authorized:
        return
    
    is_valid, error_msg = validate_command_args(update, context, 1)
    if not is_valid:
        await send_command_response(update, error_msg)
        return
    
    is_valid, new_user_id = validate_user_id(context.args[0])
    if not is_valid:
        await send_command_response(update, new_user_id)
        return
    
    if new_user_id == BOT_OWNER_ID:
        await send_command_response(update, "❌ Bot sahibi zaten her zaman erişime sahiptir.")
        return
    
    if new_user_id in ALLOWED_USERS:
        await send_command_response(update, "❌ Bu kullanıcı zaten izin verilen kullanıcılar listesinde.")
        return
    
    if new_user_id in ADMIN_USERS:
        await send_command_response(update, "❌ Bu kullanıcı zaten admin listesinde.")
        return
    
    ALLOWED_USERS.add(new_user_id)
    save_allowed_users()  # MongoDB'ye kaydet
    await send_command_response(update, f"✅ Kullanıcı {new_user_id} başarıyla eklendi ve kalıcı olarak kaydedildi.")

async def removeuser_command(update, context):
    """Kullanıcı çıkarma komutu (sadece bot sahibi ve adminler)"""
    user_id, is_authorized = validate_user_command(update, require_admin=True)
    if not is_authorized:
        return
    
    is_valid, error_msg = validate_command_args(update, context, 1)
    if not is_valid:
        await send_command_response(update, error_msg)
        return
    
    is_valid, remove_user_id = validate_user_id(context.args[0])
    if not is_valid:
        await send_command_response(update, remove_user_id)
        return
    
    if remove_user_id in ALLOWED_USERS:
        ALLOWED_USERS.remove(remove_user_id)
        save_allowed_users()  # MongoDB'ye kaydet
        await send_command_response(update, f"✅ Kullanıcı {remove_user_id} başarıyla çıkarıldı ve kalıcı olarak kaydedildi.")
    else:
        await send_command_response(update, f"❌ Kullanıcı {remove_user_id} zaten izin verilen kullanıcılar listesinde yok.")

async def listusers_command(update, context):
    """İzin verilen kullanıcıları listeleme komutu (sadece bot sahibi ve adminler)"""
    user_id, is_authorized = validate_user_command(update, require_admin=True)
    if not is_authorized:
        return
    
    if not ALLOWED_USERS:
        users_text = "📋 **İzin Verilen Kullanıcılar:**\n\nHenüz izin verilen kullanıcı yok."
    else:
        users_list = "\n".join([f"• {user_id}" for user_id in ALLOWED_USERS])
        users_text = f"📋 **İzin Verilen Kullanıcılar:**\n\n{users_list}"
    
    await send_command_response(update, users_text)

async def adminekle_command(update, context):
    """Admin ekleme komutu (sadece bot sahibi)"""
    user_id, is_authorized = validate_user_command(update, require_owner=True)
    if not is_authorized:
        return
    
    is_valid, error_msg = validate_command_args(update, context, 1)
    if not is_valid:
        await send_command_response(update, error_msg)
        return
    
    is_valid, new_admin_id = validate_user_id(context.args[0])
    if not is_valid:
        await send_command_response(update, new_admin_id)
        return
    
    if new_admin_id == BOT_OWNER_ID:
        await send_command_response(update, "❌ Bot sahibi zaten admin yetkilerine sahiptir.")
        return
    
    if new_admin_id in ADMIN_USERS:
        await send_command_response(update, "❌ Bu kullanıcı zaten admin listesinde.")
        return
    
    ADMIN_USERS.add(new_admin_id)
    save_admin_users()  # MongoDB'ye kaydet
    await send_command_response(update, f"✅ Admin {new_admin_id} başarıyla eklendi ve kalıcı olarak kaydedildi.")

async def adminsil_command(update, context):
    """Admin silme komutu (sadece bot sahibi)"""
    user_id, is_authorized = validate_user_command(update, require_owner=True)
    if not is_authorized:
        return
    
    is_valid, error_msg = validate_command_args(update, context, 1)
    if not is_valid:
        await send_command_response(update, error_msg)
        return
    
    is_valid, remove_admin_id = validate_user_id(context.args[0])
    if not is_valid:
        await send_command_response(update, remove_admin_id)
        return
    
    if remove_admin_id in ADMIN_USERS:
        ADMIN_USERS.remove(remove_admin_id)
        save_admin_users()  # MongoDB'ye kaydet
        await send_command_response(update, f"✅ Admin {remove_admin_id} başarıyla silindi ve kalıcı olarak kaydedildi.")
    else:
        await send_command_response(update, f"❌ Admin {remove_admin_id} zaten admin listesinde yok.")

async def listadmins_command(update, context):
    """Admin listesini gösterme komutu (sadece bot sahibi ve adminler)"""
    user_id, is_authorized = validate_user_command(update, require_admin=True)
    if not is_authorized:
        return
    
    if not ADMIN_USERS:
        admins_text = f"👑 **Admin Kullanıcıları:**\n\nHenüz admin kullanıcı yok.\n\nBot Sahibi: {BOT_OWNER_ID}"
    else:
        admins_list = "\n".join([f"• {admin_id}" for admin_id in ADMIN_USERS])
        admins_text = f"👑 **Admin Kullanıcıları:**\n\n{admins_list}\n\nBot Sahibi: {BOT_OWNER_ID}"
    
    await send_command_response(update, admins_text)

async def handle_message(update, context):
    """Genel mesaj handler'ı"""
    user_id, is_authorized = validate_user_command(update, require_admin=False)
    if not is_authorized:
        return
    
    # Sadece yetkili kullanıcılar için yardım mesajı göster
    if user_id == BOT_OWNER_ID or user_id in ALLOWED_USERS or user_id in ADMIN_USERS:
        await send_command_response(update, "🤖 Bu bot sadece komutları destekler. /help yazarak mevcut komutları görebilirsiniz.")
    # İzin verilmeyen kullanıcılar için hiçbir yanıt verme

async def error_handler(update, context):
    """Hata handler'ı"""
    error = context.error
    
    # CancelledError'ları görmezden gel (bot kapatılırken normal)
    if isinstance(error, asyncio.CancelledError):
        print("ℹ️ Bot kapatılırken task iptal edildi (normal durum)")
        return
    
    # Conflict hatası için özel işlem
    if "Conflict" in str(error) and "getUpdates" in str(error):
        print("⚠️ Conflict hatası tespit edildi. Bot yeniden başlatılıyor...")
        try:
            # Webhook'ları temizle
            await app.bot.delete_webhook(drop_pending_updates=True)
            print("✅ Webhook'lar temizlendi")
            
            # 5 saniye bekle
            await asyncio.sleep(5)
            
            # Polling'i yeniden başlat
            await app.updater.stop()
            await asyncio.sleep(2)
            await app.updater.start_polling(drop_pending_updates=True, allowed_updates=["message", "callback_query", "chat_member", "my_chat_member", "channel_post"])
            print("✅ Bot yeniden başlatıldı")
            
        except Exception as e:
            print(f"❌ Bot yeniden başlatma hatası: {e}")
        return
    
    # Diğer hataları logla
    print(f"Bot hatası: {error}")
    
    if update and update.effective_chat and update.effective_user:
        if update.effective_chat.type == "private":
            user_id, is_authorized = validate_user_command(update, require_admin=False)
            if is_authorized:
                await send_command_response(update, "❌ Bir hata oluştu. Lütfen daha sonra tekrar deneyin.")
            # İzin verilmeyen kullanıcılar için hata mesajı bile gösterme

async def handle_all_messages(update, context):
    """Tüm mesajları dinler ve kanal olaylarını yakalar"""
    try:
        chat = update.effective_chat
        if not chat:
            return
        
        # Debug bilgisi
        print(f"🔍 Mesaj alındı: chat_type={chat.type}, chat_id={chat.id}, title={getattr(chat, 'title', 'N/A')}")
        
        # Eğer bu bir kanal mesajıysa
        if chat.type == "channel":
            # Kanal henüz izin verilen gruplarda değilse ekle
            if chat.id not in BOT_OWNER_GROUPS:
                print(f"📢 Kanal mesajı alındı: {chat.title} ({chat.id})")
                
                # Kanalı izin verilen gruplara ekle
                BOT_OWNER_GROUPS.add(chat.id)
                print(f"✅ Kanal eklendi: {chat.title} ({chat.id})")
                
                # MongoDB'ye kaydet
                save_admin_groups()
                
                # Bot sahibine bildirim gönderilmiyor (zaten sinyalleri alıyor)
            # Kanal zaten izin verilen gruplarda ise hiçbir şey yapma
            return
        
        # Eğer bu bir grup mesajıysa ve bot ekleme olayıysa
        elif chat.type in ["group", "supergroup"] and update.message and update.message.new_chat_members:
            for new_member in update.message.new_chat_members:
                if new_member.id == context.bot.id:
                    print(f"🔍 Bot grup ekleme olayı: {chat.title} ({chat.id})")
        
        # Eğer bu bir özel mesajsa ve bot sahibinden geliyorsa
        elif chat.type == "private" and update.effective_user:
            user_id = update.effective_user.id
            if user_id == BOT_OWNER_ID:
                print(f"🔍 Bot sahibi mesajı: {update.message.text if update.message else 'N/A'}")
    
    except Exception as e:
        print(f"🔍 handle_all_messages Hatası: {e}")
    
    return

async def handle_debug_messages(update, context):
    """Tüm mesajları debug için dinler"""
    try:
        chat = update.effective_chat
        if not chat:
            return
        
        # Debug bilgisi - sadece önemli olayları logla
        if chat.type == "channel":
            print(f"🔍 DEBUG: Kanal mesajı - {chat.title} ({chat.id}) - İzin verilen gruplarda: {chat.id in BOT_OWNER_GROUPS}")
        elif chat.type in ["group", "supergroup"] and update.message and update.message.new_chat_members:
            print(f"🔍 DEBUG: Grup üye ekleme - {chat.title} ({chat.id})")
        elif chat.type == "private" and update.effective_user:
            user_id = update.effective_user.id
            if user_id == BOT_OWNER_ID:
                print(f"🔍 DEBUG: Bot sahibi mesajı - {update.message.text if update.message else 'N/A'}")
    except Exception as e:
        print(f"🔍 DEBUG Hatası: {e}")
    
    return

async def handle_chat_member_update(update, context):
    """Grup ve kanal ekleme/çıkarma olaylarını dinler"""
    chat = update.effective_chat
    
    # Yeni üye eklenme durumu
    if update.message and update.message.new_chat_members:
        for new_member in update.message.new_chat_members:
            # Bot'un kendisi eklenmiş mi?
            if new_member.id == context.bot.id:
                # Bot sahibi tarafından mı eklendi?
                if not update.effective_user:
                    return
                
                user_id = update.effective_user.id
                
                print(f"🔍 Bot ekleme: chat_type={chat.type}, user_id={user_id}, BOT_OWNER_ID={BOT_OWNER_ID}")
                
                if user_id != BOT_OWNER_ID:
                    # Bot sahibi olmayan biri ekledi, gruptan/kanaldan çık
                    try:
                        await context.bot.leave_chat(chat.id)
                        chat_type = "kanalından" if chat.type == "channel" else "grubundan"
                        print(f"❌ Bot sahibi olmayan {user_id} tarafından {chat.title} {chat_type.replace('ndan', 'na')} eklenmeye çalışıldı. Bot {chat_type} çıktı.")
                        
                        # Bot sahibine bildirim gönderilmiyor (zaten sinyalleri alıyor)
                        
                    except Exception as e:
                        print(f"Gruptan/kanaldan çıkma hatası: {e}")
                else:
                    # Bot sahibi tarafından eklendi, grubu/kanalı izin verilen gruplara ekle
                    BOT_OWNER_GROUPS.add(chat.id)
                    chat_type = "kanalına" if chat.type == "channel" else "grubuna"
                    print(f"✅ Bot sahibi tarafından {chat.title} {chat_type} eklendi. Chat ID: {chat.id}")
                    print(f"🔍 BOT_OWNER_GROUPS güncellendi: {BOT_OWNER_GROUPS}")
                    
                    # MongoDB'ye kaydet
                    save_admin_groups()
                    
                    # Bot sahibine bildirim gönderilmiyor (zaten sinyalleri alıyor)
    
    # Üye çıkma durumu
    elif update.message and update.message.left_chat_member:
        left_member = update.message.left_chat_member
        # Bot'un kendisi çıkarılmış mı?
        if left_member.id == context.bot.id:
            # Gruptan/kanaldan çıkarıldıysa, izin verilen gruplardan da çıkar
            if chat.id in BOT_OWNER_GROUPS:
                BOT_OWNER_GROUPS.remove(chat.id)
                chat_type = "kanalından" if chat.type == "channel" else "grubundan"
                print(f"Bot {chat.title} {chat_type} çıkarıldı. Chat ID: {chat.id} izin verilen gruplardan kaldırıldı.")
                
                # MongoDB'ye kaydet
                save_admin_groups()
                
                # Bot sahibine bildirim gönderilmiyor (zaten sinyalleri alıyor)
            else:
                chat_type = "kanalından" if chat.type == "channel" else "grubundan"
                print(f"Bot {chat.title} {chat_type} çıkarıldı.")

async def setup_bot():
    """Bot handler'larını kur"""
    global app
    app = Application.builder().token(TELEGRAM_TOKEN).build()
    
    # Mevcut webhook'ları temizle ve offset'i sıfırla
    try:
        # Webhook'ı sil
        await app.bot.delete_webhook(drop_pending_updates=True)
        print("✅ Webhook silindi")
        
        # Pending updates'leri temizle
        await app.bot.delete_webhook(drop_pending_updates=True)
        print("✅ Pending updates temizlendi")
        
        # Offset'i sıfırla
        await app.bot.get_updates(offset=-1, limit=1)
        print("✅ Update offset sıfırlandı")
        
        print("✅ Mevcut webhook'lar temizlendi ve pending updates silindi")
    except Exception as e:
        print(f"⚠️ Webhook temizleme hatası: {e}")
        # Hata durumunda polling moduna geç
        print("🔄 Polling moduna geçiliyor...")
    
    # Komut handler'ları
    app.add_handler(CommandHandler("help", help_command))
    app.add_handler(CommandHandler("stats", stats_command))
    app.add_handler(CommandHandler("active", active_command))
    app.add_handler(CommandHandler("test", test_command))
    app.add_handler(CommandHandler("adduser", adduser_command))
    app.add_handler(CommandHandler("removeuser", removeuser_command))
    app.add_handler(CommandHandler("listusers", listusers_command))
    app.add_handler(CommandHandler("adminekle", adminekle_command))
    app.add_handler(CommandHandler("adminsil", adminsil_command))
    app.add_handler(CommandHandler("listadmins", listadmins_command))
    app.add_handler(CommandHandler("clearall", clear_all_command))
    
    # Genel mesaj handler'ı
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))
    
    # Grup ekleme/çıkarma handler'ı - ChatMemberUpdated event'ini dinle
    app.add_handler(MessageHandler(filters.StatusUpdate.NEW_CHAT_MEMBERS, handle_chat_member_update))
    app.add_handler(MessageHandler(filters.StatusUpdate.LEFT_CHAT_MEMBER, handle_chat_member_update))
    
    # Kanal mesajlarını dinle (kanal ekleme olayları için)
    app.add_handler(MessageHandler(filters.ChatType.CHANNEL, handle_all_messages))
    
    # Tüm mesajları dinle (debug için)
    app.add_handler(MessageHandler(filters.ALL, handle_debug_messages))
    
    # Hata handler'ı
    app.add_error_handler(error_handler)
    
    print("Bot handler'ları kuruldu!")

def format_price(price, ref_price=None):
    """
    Fiyatı, referans fiyatın ondalık basamak sayısı kadar string olarak döndürür.
    float hassasiyeti olmadan, gereksiz yuvarlama veya fazla basamak olmadan gösterir.
    """
    if ref_price is not None:
        s = str(ref_price)
        if 'e' in s or 'E' in s:
            # Bilimsel gösterim varsa düzelt
            s = f"{ref_price:.20f}".rstrip('0').rstrip('.')
        if '.' in s:
            dec = len(s.split('.')[-1])
            # Decimal ile hassasiyetli kısaltma
            getcontext().prec = dec + 8
            d_price = Decimal(str(price)).quantize(Decimal('1.' + '0'*dec), rounding=ROUND_DOWN)
            return format(d_price, f'.{dec}f').rstrip('0').rstrip('.') if dec > 0 else str(int(d_price))
        else:
            return str(int(round(price)))
    else:
        # ref_price yoksa, eski davranış
        if price >= 1:
            return f"{price:.4f}".rstrip('0').rstrip('.')
        elif price >= 0.01:
            return f"{price:.6f}".rstrip('0').rstrip('.')
        elif price >= 0.0001:
            return f"{price:.8f}".rstrip('0').rstrip('.')
        else:
            return f"{price:.10f}".rstrip('0').rstrip('.')

def format_volume(volume):
    """Hacmi bin, milyon, milyar formatında formatla"""
    if volume >= 1_000_000_000:
        return f"${volume/1_000_000_000:.1f}B"
    elif volume >= 1_000_000:
        return f"${volume/1_000_000:.1f}M"
    elif volume >= 1_000:
        return f"${volume/1_000:.1f}K"
    else:
        return f"${volume:,.0f}"

def create_signal_message_new_55(symbol, price, all_timeframes_signals, volume, profit_percent=2.0, stop_percent=1.5):
    """7/7 sinyal sistemi - 15m,30m,1h,2h,4h,8h,1d zaman dilimlerini kontrol et"""
    price_str = format_price(price, price)
    
    # Tüm zaman dilimlerindeki sinyalleri kontrol et
    timeframes = ['15m', '30m', '1h', '2h', '4h', '8h', '1d']
    signal_values = []
    
    for tf in timeframes:
        signal_values.append(all_timeframes_signals.get(tf, 0))
    
    # Sinyal sayılarını hesapla
    buy_signals = sum(1 for s in signal_values if s == 1)
    sell_signals = sum(1 for s in signal_values if s == -1)
    
    # 7/7 kuralı: Tüm 7 sinyal aynı yönde olmalı
    if buy_signals != 7 and sell_signals != 7:
        return None, None, None, None, None, None, None
    
    # Dominant sinyali belirle
    if buy_signals >= sell_signals:
        sinyal_tipi = "ALIŞ SİNYALİ"
        target_price = price * (1 + profit_percent / 100)
        stop_loss = price * (1 - stop_percent / 100)
        dominant_signal = "ALIŞ"
    else:
        sinyal_tipi = "SATIŞ SİNYALİ"
        target_price = price * (1 - profit_percent / 100)
        stop_loss = price * (1 + stop_percent / 100)
        dominant_signal = "SATIŞ"
    
    # Kaldıraç seviyesini belirle - sabit 10x
    leverage = 10  # Sabit 10x kaldıraç

    leverage_reason = ""
    
    # 7/7 kuralı: Tüm 7 zaman dilimi aynıysa 10x kaldıraçlı
    if max(buy_signals, sell_signals) == 7:
        print(f"{symbol} - 7/7 sinyal")
    
    target_price_str = format_price(target_price, price)
    stop_loss_str = format_price(stop_loss, price)
    volume_formatted = format_volume(volume)
    
    message = f"""
🚨 {sinyal_tipi} 🚨

🔹 Kripto Çifti: {symbol}  
💵 Giriş Fiyatı: {price_str}
📈 Hedef Fiyat: {target_price_str}  
📉 Stop Loss: {stop_loss_str}
⚡ Kaldıraç: {leverage}x
📊 24h Hacim: {volume_formatted}

⚠️ <b>ÖNEMLİ UYARILAR:</b>
• Bu bir yatırım tavsiyesi değildir
• Stopunuzu en fazla %25 ayarlayın

📺 <b>Kanallar:</b>
🔗 <a href="https://www.youtube.com/@kriptotek">YouTube</a> | <a href="https://t.me/kriptotek8907">Telegram</a> | <a href="https://x.com/kriptotek8907">X</a> | <a href="https://www.instagram.com/kriptotek/">Instagram</a>"""

    return message, dominant_signal, target_price, stop_loss, stop_loss_str, leverage, None

async def async_get_historical_data(symbol, interval, lookback):
    """Binance Futures'den geçmiş verileri asenkron çek"""
    # Futures sembolü için USDT ekle (eğer yoksa)
    if not symbol.endswith('USDT'):
        symbol = symbol + 'USDT'
    
    url = f"https://fapi.binance.com/fapi/v1/klines?symbol={symbol}&interval={interval}&limit={lookback}"
    try:
        async with aiohttp.ClientSession(connector=aiohttp.TCPConnector(limit=10)) as session:
            async with session.get(url, ssl=False) as resp:
                if resp.status != 200:
                    raise Exception(f"Futures API hatası: {resp.status} - {await resp.text()}")
                klines = await resp.json()
                if not klines or len(klines) == 0:
                    raise Exception(f"{symbol} için futures veri yok")
    except Exception as e:
        raise Exception(f"Futures veri çekme hatası: {symbol} - {interval} - {str(e)}")
    
    df = pd.DataFrame(klines, columns=[
        'timestamp', 'open', 'high', 'low', 'close', 'volume',
        'close_time', 'quote_volume', 'trades', 'taker_buy_base',
        'taker_buy_quote', 'ignored'
    ])
    df['timestamp'] = pd.to_datetime(df['timestamp'], unit='ms')
    df['close'] = df['close'].astype(float)
    df['high'] = df['high'].astype(float)
    df['low'] = df['low'].astype(float)
    df['volume'] = df['volume'].astype(float)
    df['open'] = df['open'].astype(float)
    return df

def calculate_full_pine_signals(df, timeframe):
    """
    Kriptotek Nokta Atışı Pivot - Pine Script'e birebir uyumlu AL/SAT sinyal hesaplaması.
    Zaman dilimine göre dinamik parametreler içerir.
    """
    # --- Zaman dilimine göre dinamik parametreler ---
    is_higher_tf = timeframe in ['1d', '4h', '1w']
    is_weekly = timeframe == '1w'
    is_daily = timeframe == '1d' 
    is_4h = timeframe == '4h'
    
    # Dinamik parametreler (Pine Script'teki gibi)
    if is_weekly:
        rsi_length = 28
        macd_fast = 18
        macd_slow = 36
        macd_signal = 12
        short_ma_period = 30
        long_ma_period = 150
        mfi_length = 25
        fib_lookback = 150
        atr_period = 7
        volume_multiplier = 0.15
        rsi_overbought = 60
        rsi_oversold = 40
    elif is_daily:
        rsi_length = 21
        macd_fast = 13
        macd_slow = 26
        macd_signal = 10
        short_ma_period = 20
        long_ma_period = 100
        mfi_length = 20
        fib_lookback = 100
        atr_period = 7
        volume_multiplier = 0.15
        rsi_overbought = 60
        rsi_oversold = 40
    elif is_4h:
        rsi_length = 18
        macd_fast = 11
        macd_slow = 22
        macd_signal = 8
        short_ma_period = 12
        long_ma_period = 60
        mfi_length = 16
        fib_lookback = 70
        atr_period = 7
        volume_multiplier = 0.15
        rsi_overbought = 60
        rsi_oversold = 40
    else:
        # Küçük zaman dilimleri (15m, 30m, 1h, 2h, 8h, 12h)
        rsi_length = 14
        macd_fast = 10
        macd_slow = 20
        macd_signal = 9
        short_ma_period = 9
        long_ma_period = 50
        mfi_length = 14
        fib_lookback = 50
        atr_period = 10
        volume_multiplier = 0.4
        rsi_overbought = 60
        rsi_oversold = 40

    # EMA 200 ve trend
    df['ema200'] = ta.trend.EMAIndicator(df['close'], window=200).ema_indicator()
    df['trend_bullish'] = df['close'] > df['ema200']
    df['trend_bearish'] = df['close'] < df['ema200']

    # RSI
    df['rsi'] = ta.momentum.RSIIndicator(df['close'], window=rsi_length).rsi()

    # MACD
    macd = ta.trend.MACD(df['close'], window_slow=macd_slow, window_fast=macd_fast, window_sign=macd_signal)
    df['macd'] = macd.macd()
    df['macd_signal'] = macd.macd_signal()

    # Dinamik Supertrend (Pine Script'teki gibi)
    def supertrend_dynamic(df, atr_period, timeframe):
        hl2 = (df['high'] + df['low']) / 2
        atr = ta.volatility.AverageTrueRange(df['high'], df['low'], df['close'], window=atr_period).average_true_range()
        atr_dynamic = atr.rolling(window=5).mean()  # SMA(ATR, 5)
        
        # Dinamik multiplier (Pine Script'teki gibi)
        if is_weekly:
            multiplier = atr_dynamic / 2
        elif is_daily:
            multiplier = atr_dynamic / 1.2
        elif is_4h:
            multiplier = atr_dynamic / 1.3
        else:
            multiplier = atr_dynamic / 1.5
            
        upperband = hl2 + multiplier
        lowerband = hl2 - multiplier
        direction = [1]
        supertrend_values = [lowerband.iloc[0]]
        
        for i in range(1, len(df)):
            if df['close'].iloc[i] > upperband.iloc[i-1]:
                direction.append(1)
                supertrend_values.append(lowerband.iloc[i])
            elif df['close'].iloc[i] < lowerband.iloc[i-1]:
                direction.append(-1)
                supertrend_values.append(upperband.iloc[i])
            else:
                direction.append(direction[-1])
                if direction[-1] == 1:
                    supertrend_values.append(lowerband.iloc[i])
                else:
                    supertrend_values.append(upperband.iloc[i])

        return pd.Series(direction, index=df.index), pd.Series(supertrend_values, index=df.index)

    df['supertrend_dir'], df['supertrend'] = supertrend_dynamic(df, atr_period, timeframe)

    # MA'lar
    df['short_ma'] = ta.trend.EMAIndicator(df['close'], window=short_ma_period).ema_indicator()
    df['long_ma'] = ta.trend.EMAIndicator(df['close'], window=long_ma_period).ema_indicator()
    df['ma_bullish'] = df['short_ma'] > df['long_ma']
    df['ma_bearish'] = df['short_ma'] < df['long_ma']

    # Hacim Analizi (Pine Script'teki gibi)
    volume_ma_period = 20
    df['volume_ma'] = df['volume'].rolling(window=volume_ma_period).mean()
    df['enough_volume'] = df['volume'] > df['volume_ma'] * volume_multiplier

    # Para Akışı Endeksi (MFI) - Pine Script'teki mantıkla
    typical_price = (df['high'] + df['low'] + df['close']) / 3
    money_flow = typical_price * df['volume']
    
    positive_flow = []
    negative_flow = []
    
    for i in range(len(df)):
        if i == 0:
            positive_flow.append(0)
            negative_flow.append(0)
        else:
            if typical_price.iloc[i] > typical_price.iloc[i-1]:
                positive_flow.append(money_flow.iloc[i])
                negative_flow.append(0)
            elif typical_price.iloc[i] < typical_price.iloc[i-1]:
                positive_flow.append(0)
                negative_flow.append(money_flow.iloc[i])
            else:
                positive_flow.append(0)
                negative_flow.append(0)
    
    positive_flow_sum = pd.Series(positive_flow).rolling(window=mfi_length).sum()
    negative_flow_sum = pd.Series(negative_flow).rolling(window=mfi_length).sum()
    
    # MFI hesaplama
    money_ratio = positive_flow_sum / (negative_flow_sum + 1e-10)  # Sıfıra bölmeyi önle
    df['mfi'] = 100 - (100 / (1 + money_ratio))
    df['mfi_bullish'] = df['mfi'] < 65
    df['mfi_bearish'] = df['mfi'] > 35

    # Fibo seviyesi
    highest_high = df['high'].rolling(window=fib_lookback).max()
    lowest_low = df['low'].rolling(window=fib_lookback).min()
    fib_level1 = highest_high * 0.618
    fib_level2 = lowest_low * 1.382
    df['fib_in_range'] = (df['close'] > fib_level1) & (df['close'] < fib_level2)

    # Genel teknik analiz fonksiyonlarını kullan
    crossover = lambda s1, s2, shift=1: (s1.shift(shift) < s2.shift(shift)) & (s1 > s2)
    crossunder = lambda s1, s2, shift=1: (s1.shift(shift) > s2.shift(shift)) & (s1 < s2)

    # Sinyaller
    buy_signal = (
        crossover(df['macd'], df['macd_signal']) |
        (
            (df['rsi'] < rsi_oversold) &
            (df['supertrend_dir'] == 1) &
            df['ma_bullish'] &
            df['enough_volume'] &
            df['mfi_bullish'] &
            df['trend_bullish']
        )
    ) & df['fib_in_range']

    sell_signal = (
        crossunder(df['macd'], df['macd_signal']) |
        (
            (df['rsi'] > rsi_overbought) &
            (df['supertrend_dir'] == -1) &
            df['ma_bearish'] &
            df['enough_volume'] &
            df['mfi_bearish'] &
            df['trend_bearish']
        )
    ) & df['fib_in_range']

    df['signal'] = 0
    df.loc[buy_signal, 'signal'] = 1
    df.loc[sell_signal, 'signal'] = -1

    # Sinyal devam ettirme ve MACD fallback
    for i in range(len(df)):
        if df['signal'].iloc[i] == 0:
            if i > 0:
                # Önceki sinyali devam ettir
                df.at[df.index[i], 'signal'] = df['signal'].iloc[i-1]
            else:
                # İlk mum için MACD fallback
                if df['macd'].iloc[i] > df['macd_signal'].iloc[i]:
                    df.at[df.index[i], 'signal'] = 1
                else:
                    df.at[df.index[i], 'signal'] = -1

    return df

async def get_active_high_volume_usdt_pairs(top_n=50):
    """
    Sadece Futures'da aktif, USDT bazlı coinlerden hacme göre sıralanmış ilk top_n kadar uygun coin döndürür.
    1 günlük verisi 30 mumdan az olan coin'ler elenir.
    """
    # Futures exchange info al
    futures_exchange_info = client.futures_exchange_info()
    
    # Sadece USDT çiftlerini filtrele
    futures_usdt_pairs = set()
    for symbol in futures_exchange_info['symbols']:
        if (
            symbol['quoteAsset'] == 'USDT' and
            symbol['status'] == 'TRADING' and
            symbol['contractType'] == 'PERPETUAL'
        ):
            futures_usdt_pairs.add(symbol['symbol'])

    # Sadece USDT çiftlerinin ticker'larını al
    futures_tickers = client.futures_ticker()
    
    high_volume_pairs = []
    for ticker in futures_tickers:
        symbol = ticker['symbol']
        if symbol in ['USDCUSDT', 'FDUSDUSDT', 'TUSDUSDT', 'BUSDUSDT', 'USDPUSDT', 'USDTUSDT']:
            continue
        if symbol in futures_usdt_pairs:
            try:
                quote_volume = ticker.get('quoteVolume', 0)
                if quote_volume is None:
                    continue
                quote_volume = float(quote_volume)
                high_volume_pairs.append((symbol, quote_volume))
            except Exception:
                continue

    high_volume_pairs.sort(key=lambda x: x[1], reverse=True)

    # Sadece ilk 150 sembolü al (hacme göre sıralanmış) - 100'e ulaşmak için
    high_volume_pairs = high_volume_pairs[:150]

    uygun_pairs = []
    idx = 0
    while len(uygun_pairs) < top_n and idx < len(high_volume_pairs):
        symbol, volume = high_volume_pairs[idx]
        try:
            # En az 30 mum 1 günlük veri kontrolü
            df_1d = await async_get_historical_data(symbol, '1d', 30)
            if len(df_1d) < 30:
                # Sessizce atla, mesaj yazdırma
                idx += 1
                continue
            uygun_pairs.append(symbol)
            # print(f"✅ {symbol} eklendi (hacim: {volume:,.0f})")  # Debug mesajını kaldır
        except Exception as e:
            # Sessizce atla, mesaj yazdırma
            idx += 1
            continue
        idx += 1

    # Debug: Kaç kripto bulunduğunu göster
    print(f"📊 Binance'den toplam {len(high_volume_pairs)} USDT çifti bulundu")
    print(f"📊 Veri kontrollerinden {len(uygun_pairs)} kripto geçti (hedef: {top_n})")
    
    # Sembolleri sadece ilk çalıştığında yazdır
    if uygun_pairs and not hasattr(get_active_high_volume_usdt_pairs, '_first_run'):
        print("📋 İşlenecek semboller:")
        # 10'arlı gruplar halinde yazdır
        for i in range(0, len(uygun_pairs), 10):
            group = uygun_pairs[i:i+10]
            group_str = ", ".join(group)
            print(f"   {group_str}")
        # İlk çalıştırma işaretini koy
        get_active_high_volume_usdt_pairs._first_run = True
    
    return uygun_pairs

# check_special_signal fonksiyonu artık gerekli değil - 7/7 sistemi kullanılıyor

async def check_signal_potential(symbol, positions, stop_cooldown, successful_signals, failed_signals, timeframes, tf_names, previous_signals):
    """Bir sembolün sinyal potansiyelini kontrol eder, sinyal varsa detayları döndürür."""
    # Stop cooldown kontrolü (1 saat)
    if check_cooldown(symbol, stop_cooldown, 4):
        return None

    try:
        # 1 günlük veri al - 1d timeframe için gerekli
        df_1d = await async_get_historical_data(symbol, timeframes['1d'], 30)
        if df_1d is None or df_1d.empty:
            return None

        # Her timeframe için sinyalleri hesapla - genel fonksiyonu kullan
        current_signals = await calculate_signals_for_symbol(symbol, timeframes, tf_names)
        if current_signals is None:
            return None
        
        # Sinyal sayılarını hesapla - genel fonksiyonu kullan
        buy_count, sell_count = calculate_signal_counts(current_signals, tf_names)
        
        # Önceki sinyalleri al
        prev_signals = previous_signals.get(symbol, {tf: 0 for tf in tf_names})
        
        # İlk çalıştırmada kaydedilen sinyalleri kontrol et
        prev_buy_count, prev_sell_count = calculate_signal_counts(prev_signals, tf_names)
        
        # 7/7 kuralı kontrol - sadece bu kural geçerli
        if not check_7_7_rule(buy_count, sell_count):
            if buy_count > 0 or sell_count > 0:
                print(f"ℹ️ {symbol} → 7/7 kuralı sağlanmadı: ALIŞ={buy_count}, SATIŞ={sell_count}")
            previous_signals[symbol] = current_signals.copy()
            return None
        
        print(f"🎯 {symbol} → 7/7 kuralı sağlandı! ALIŞ={buy_count}, SATIŞ={sell_count}")
        
        # Sinyal türünü belirle
        if buy_count >= sell_count:
            sinyal_tipi = 'ALIS'
            dominant_signal = "ALIŞ"
        else:
            sinyal_tipi = 'SATIS'
            dominant_signal = "SATIŞ"
        
        # Fiyat ve hacim bilgilerini al
        try:
            # Binance API'den ticker verisi çek - tek sembol için
            ticker_data = client.futures_ticker(symbol=symbol)
            
            # API bazen liste döndürüyor, bazen dict
            if isinstance(ticker_data, list):
                if len(ticker_data) == 0:
                    print(f"❌ {symbol} → Ticker verisi boş liste, sinyal iptal edildi")
                    return None
                ticker = ticker_data[0]  # İlk elementi al
            else:
                ticker = ticker_data
            
            # Ticker kontrolü - Binance'de 'price' yerine 'lastPrice' kullanılıyor
            if not ticker or not isinstance(ticker, dict):
                print(f"❌ {symbol} → Ticker verisi eksik veya hatalı format, sinyal iptal edildi")
                print(f"   Ticker: {ticker}")
                return None  # Sinyal iptal edildi
            
            # Binance'de 'price' yerine 'lastPrice' kullanılıyor
            if 'lastPrice' in ticker:
                price = float(ticker['lastPrice'])
            elif 'price' in ticker:
                price = float(ticker['price'])
            else:
                print(f"❌ {symbol} → Fiyat alanı bulunamadı (lastPrice/price), sinyal iptal edildi")
                print(f"   Ticker: {ticker}")
                return None
            
            volume_usd = float(ticker.get('quoteVolume', 0))
            
            if price <= 0 or volume_usd <= 0:
                print(f"❌ {symbol} → Fiyat ({price}) veya hacim ({volume_usd}) geçersiz, sinyal iptal edildi")
                return None  # Sinyal iptal edildi
                
        except Exception as e:
            print(f"❌ {symbol} → Fiyat/hacim bilgisi alınamadı: {e}, sinyal iptal edildi")
            return None  # Sinyal iptal edildi
        
        return {
            'symbol': symbol,
            'signals': current_signals,
            'price': price,
            'volume_usd': volume_usd,
            'signal_type': sinyal_tipi,
            'dominant_signal': dominant_signal,
            'buy_count': buy_count,
            'sell_count': sell_count
        }
        
    except Exception as e:
        print(f"❌ {symbol} sinyal potansiyeli kontrol hatası: {e}")
        return None

async def process_selected_signal(signal_data, positions, active_signals, stats):
    """Seçilen sinyali işler ve gönderir."""
    symbol = signal_data['symbol']
    current_signals = signal_data['signals']
    price = signal_data['price']
    volume_usd = signal_data['volume_usd']
    sinyal_tipi = signal_data['signal_type']
    
    try:
        # Mesaj oluştur ve gönder
        message, _, target_price, stop_loss, stop_loss_str, leverage, _ = create_signal_message_new_55(symbol, price, current_signals, volume_usd, 2.0, 1.5)
        
        if message:
            # Pozisyonu kaydet
            position = {
                "type": sinyal_tipi,
                "target": target_price,
                "stop": stop_loss,
                "open_price": price,
                "stop_str": stop_loss_str,
                "signals": current_signals,
                "leverage": leverage,
                "entry_time": str(datetime.now()),
                "entry_timestamp": datetime.now(),
            }
            
            positions[symbol] = position
            
            # ✅ POZİSYONU MONGODB'YE KAYDET
            save_positions_to_db(positions)
            
            # Aktif sinyale ekle
            active_signals[symbol] = {
                "symbol": symbol,
                "type": sinyal_tipi,
                "entry_price": format_price(price, price),
                "entry_price_float": price,
                "target_price": format_price(target_price, price),
                "stop_loss": format_price(stop_loss, price),
                "signals": current_signals,
                "leverage": leverage,
                "signal_time": datetime.now().strftime('%Y-%m-%d %H:%M'),
                "current_price": format_price(price, price),
                "current_price_float": price,
                "last_update": str(datetime.now()),
        }
            
            # ✅ AKTİF SİNYALİ MONGODB'YE KAYDET
            save_active_signals_to_db(active_signals)
            
            # İstatistikleri güncelle
            stats["total_signals"] += 1
            stats["active_signals_count"] = len(active_signals)
            
            # ✅ İSTATİSTİKLERİ MONGODB'YE KAYDET
            save_stats_to_db(stats)
            
            # Sinyali gönder
            await send_signal_to_all_users(message)
            
            # Kaldıraç bilgisini göster
            leverage_text = "10x"  # Sabit 10x kaldıraç
            print(f"✅ {symbol} {sinyal_tipi} sinyali gönderildi! Kaldıraç: {leverage_text}")
            
    except Exception as e:
        print(f"❌ {symbol} sinyal gönderme hatası: {e}")

def update_waiting_list(waiting_signals):
    """Bekleme listesini günceller."""
    global global_waiting_signals, global_waiting_previous_signals
    
    # Sadece beklemeye alınan sinyalleri ekle (mevcut listeyi temizleme)
    for signal_data in waiting_signals:
        symbol = signal_data['symbol']
        global_waiting_signals[symbol] = {
            'signals': signal_data['signals'],
            'volume': signal_data['volume_usd'],
            'type': signal_data['signal_type'],
            'timestamp': datetime.now(),
            'wait_until': datetime.now() + timedelta(minutes=30),  # 30dk sonra kontrol
            'original_signals': signal_data['signals'].copy()  # Orijinal sinyalleri sakla
        }
        
        # Önceki durumu da kaydet
        global_waiting_previous_signals[symbol] = signal_data['signals'].copy()

async def check_waiting_list_changes(positions, stop_cooldown, successful_signals, failed_signals, timeframes, tf_names, previous_signals):
    """Bekleme listesindeki sinyallerin değişip değişmediğini kontrol eder."""
    global global_waiting_signals, global_waiting_previous_signals
    
    if not global_waiting_signals:
        return []
    
    changed_signals = []
    now = datetime.now()
    
    for symbol in list(global_waiting_signals.keys()):
        try:
            waiting_data = global_waiting_signals[symbol]
            wait_until = waiting_data.get('wait_until')
            original_signals = waiting_data.get('original_signals', {})
            
            # 30dk bekleme süresi doldu mu kontrol et
            if wait_until and now >= wait_until:
                print(f"⏰ {symbol} 30dk bekleme süresi doldu, sinyal değişikliği kontrol ediliyor...")
                
                # Mevcut sinyalleri kontrol et
                current_check = await check_signal_potential(symbol, positions, stop_cooldown, successful_signals, failed_signals, timeframes, tf_names, previous_signals)
                
                if current_check is None:
                    # Artık sinyal yok, bekleme listesinden çıkar
                    print(f"❌ {symbol} artık sinyal vermiyor, bekleme listesinden çıkarılıyor")
                    del global_waiting_signals[symbol]
                    if symbol in global_waiting_previous_signals:
                        del global_waiting_previous_signals[symbol]
                    continue
                
                # Sinyal değişikliği var mı kontrol et
                current_signals = current_check['signals']
                if current_signals != original_signals:
                    print(f"🔄 {symbol} bekleme listesinde sinyal değişikliği tespit edildi!")
                    changed_signals.append(current_check)
                    
                    # Bekleme listesinden çıkar (tekrar öncelik sıralamasına girecek)
                    del global_waiting_signals[symbol]
                    if symbol in global_waiting_previous_signals:
                        del global_waiting_previous_signals[symbol]
                else:
                    print(f"⏰ {symbol} sinyalleri değişmedi, 30dk daha beklemeye alınıyor")
                    # 30dk daha bekle
                    global_waiting_signals[symbol]['wait_until'] = now + timedelta(minutes=30)
                    global_waiting_signals[symbol]['original_signals'] = current_signals.copy()
            
        except Exception as e:
            print(f"❌ {symbol} bekleme listesi kontrol hatası: {e}")
            continue
    
    return changed_signals

async def signal_processing_loop():
    """Sinyal arama ve işleme döngüsü"""
    # Profit/Stop parametreleri
    profit_percent = 2.0
    stop_percent = 1.5

    positions = dict()  # {symbol: position_info}

    stop_cooldown = dict()  # {symbol: datetime}
    previous_signals = dict()  # {symbol: {tf: signal}} - İlk çalıştığında kaydedilen sinyaller
    # changed_symbols artık kullanılmıyor (sinyal aramaya başlayabilir)

    active_signals = dict()  # {symbol: {...}} - Aktif sinyaller
    successful_signals = dict()  # {symbol: {...}} - Başarılı sinyaller (hedefe ulaşan)
    failed_signals = dict()  # {symbol: {...}} - Başarısız sinyaller (stop olan)
    tracked_coins = set()  # Takip edilen tüm coinlerin listesi
    
    # Genel istatistikler
    stats = {
        "total_signals": 0,
        "successful_signals": 0,
        "failed_signals": 0,
        "total_profit_loss": 0.0,  # 100$ yatırım için
        "active_signals_count": 0,
        "tracked_coins_count": 0
    }
    
    # DB'de kayıtlı stats varsa yükle
    db_stats = load_stats_from_db()
    if db_stats:
        stats.update(db_stats)
    
    # 7/7 sinyal sistemi için timeframe'ler - 7 zaman dilimi
    timeframes = {
        '15m': '15m',
        '30m': '30m',
        '1h': '1h',
        '2h': '2h',
        '4h': '4h',
        '8h': '8h',
        '1d': '1d'
    }
    tf_names = ['15m', '30m', '1h', '2h', '4h', '8h', '1d']  # 7/7 sistemi

    print("🚀 Bot başlatıldı!")
    
    # İlk çalıştırma kontrolü
    is_first = is_first_run()
    if is_first:
        print("⏰ İlk çalıştırma: Mevcut sinyaller kaydediliyor, değişiklik bekleniyor...")
    else:
        print("🔄 Yeniden başlatma: Veritabanından pozisyonlar ve sinyaller yükleniyor...")
        # Pozisyonları yükle
        positions = load_positions_from_db()
        # Önceki sinyalleri yükle
        previous_signals = load_previous_signals_from_db()
        
        # Aktif sinyalleri DB'den yükle
        active_signals = load_active_signals_from_db()
        
        # Eğer DB'de aktif sinyal yoksa, pozisyonlardan oluştur
        if not active_signals:
            print("ℹ️ DB'de aktif sinyal bulunamadı, pozisyonlardan oluşturuluyor...")
            for symbol, pos in positions.items():
                active_signals[symbol] = {
                    "symbol": symbol,
                    "type": pos["type"],
                    "entry_price": format_price(pos["open_price"], pos["open_price"]),
                    "entry_price_float": pos["open_price"],
                    "target_price": format_price(pos["target"], pos["open_price"]),
                    "stop_loss": format_price(pos["stop"], pos["open_price"]),
                    "signals": pos["signals"],
                    "leverage": pos.get("leverage", 10),
                    "signal_time": pos.get("entry_time", datetime.now().strftime('%Y-%m-%d %H:%M')),
                    "current_price": format_price(pos["open_price"], pos["open_price"]),
                    "current_price_float": pos["open_price"],
                    "last_update": datetime.now().strftime('%Y-%m-%d %H:%M')
                }
            
            # Yeni oluşturulan aktif sinyalleri DB'ye kaydet
            save_active_signals_to_db(active_signals)
        
        # İstatistikleri güncelle
        stats["active_signals_count"] = len(active_signals)
        save_stats_to_db(stats)
        
        print(f"📊 {len(positions)} aktif pozisyon ve {len(previous_signals)} önceki sinyal yüklendi")
        print(f"📈 {len(active_signals)} aktif sinyal oluşturuldu")
    
    while True:
        try:
            # Aktif pozisyonları kontrol etmeye devam et
            for symbol, pos in list(positions.items()):
                try:
                    df = await async_get_historical_data(symbol, '1h', 1)  # Son 1 mumu al
                    if df is None or df.empty:
                        print(f"❌ {symbol} pozisyon kontrolü için fiyat verisi alınamadı")
                        continue
                    
                    last_price = float(df['close'].iloc[-1])
                    current_high = float(df['high'].iloc[-1])
                    current_low = float(df['low'].iloc[-1])
                    
                    if last_price is None or last_price <= 0:
                        print(f"❌ {symbol} pozisyon kontrolü için geçersiz fiyat: {last_price}")
                        continue
                    
                    # Aktif sinyal bilgilerini güncelle
                    if symbol in active_signals:
                        active_signals[symbol]["current_price"] = format_price(last_price, pos["open_price"])
                        active_signals[symbol]["current_price_float"] = last_price
                        active_signals[symbol]["last_update"] = str(datetime.now())
                    
                    # 1h mumunun high/low değerlerine bak
                    if pos["type"] == "ALIŞ":
                        # Hedef kontrolü: High fiyatı hedefe ulaştı mı?
                        if current_high >= pos["target"]:
                            print(f"🎯 {symbol} HEDEF BAŞARIYLA GERÇEKLEŞTİ! Çıkış: {format_price(last_price)}")
                            msg = f"🎯 <b>HEDEF BAŞARIYLA GERÇEKLEŞTİ!</b> 🎯\n\n<b>{symbol}</b> işlemi için hedef fiyatına ulaşıldı!\nÇıkış Fiyatı: <b>{format_price(last_price)}</b>\n"
                            await send_signal_to_all_users(msg)
                            # 4 saat cooldown başlat (hedef sonrası)
                            
                            # Başarılı sinyal olarak kaydet
                            leverage = pos.get("leverage", 10)  # Pozisyondan kaldıracı al
                            profit_usd = 100 * (profit_percent / 100) * leverage
                            successful_signals[symbol] = {
                                "symbol": symbol,
                                "type": pos["type"],
                                "entry_price": format_price(pos["open_price"], pos["open_price"]),
                                "exit_price": format_price(last_price, pos["open_price"]),
                                "target_price": format_price(pos["target"], pos["open_price"]),
                                "stop_loss": format_price(pos["stop"], pos["open_price"]),
                                "signals": pos["signals"],
                                "completion_time": str(datetime.now()),
                                "status": "SUCCESS",
                                "profit_percent": round(profit_percent, 2),
                                "profit_usd": round(profit_usd, 2),
                                "leverage": leverage,
                                "entry_time": pos.get("entry_time", str(datetime.now())),
                                "duration_hours": round((datetime.now() - datetime.fromisoformat(pos.get("entry_time", str(datetime.now())))).total_seconds() / 3600, 2)
                            }
                            
                            # İstatistikleri güncelle
                            stats["successful_signals"] += 1
                            stats["total_profit_loss"] += profit_usd
                            stats["active_signals_count"] = len(active_signals)
                            
                            if symbol in active_signals:
                                del active_signals[symbol]
                            
                            # ✅ İSTATİSTİKLERİ MONGODB'YE KAYDET
                            save_stats_to_db(stats)
                            
                            # ✅ AKTİF SİNYALLERİ MONGODB'YE KAYDET
                            save_active_signals_to_db(active_signals)
                            
                            # Pozisyonu veritabanından kaldır
                            remove_position_from_db(symbol)
                            del positions[symbol]
                        # Stop kontrolü: Low fiyatı stop'a ulaştı mı?
                        elif current_low <= pos["stop"]:
                            print(f"🛑 {symbol} STOP HİT! Çıkış: {format_price(last_price)}")
                            # Stop mesajı gönderilmiyor - sadece hedef mesajları gönderiliyor
                            # Yalnızca bot sahibine STOP bilgisi gönder
                            try:
                                stop_msg = (
                                    f"🛑 STOP\n"
                                    f"<b>{symbol}</b> işlemi stop oldu.\n"
                                    f"Çıkış Fiyatı: <b>{format_price(last_price)}</b>\n"
                                    f"Stop: <b>{format_price(pos['stop'], pos['open_price'])}</b>"
                                )
                                await send_telegram_message(stop_msg, BOT_OWNER_ID)
                            except Exception as _e:
                                pass
                             
                            # 4 saat cooldown başlat (stop sonrası)
                            stop_cooldown[symbol] = datetime.now()

                            # Başarısız sinyal olarak kaydet
                            loss_percent = -stop_percent
                            leverage = pos.get("leverage", 10)  # Pozisyondan kaldıracı al
                            loss_usd = -100 * (stop_percent / 100) * leverage
                            failed_signals[symbol] = {
                                "symbol": symbol,
                                "type": pos["type"],
                                "entry_price": format_price(pos["open_price"], pos["open_price"]),
                                "exit_price": format_price(last_price, pos["open_price"]),
                                "target_price": format_price(pos["target"], pos["open_price"]),
                                "stop_loss": format_price(pos["stop"], pos["open_price"]),
                                "signals": pos["signals"],
                                "completion_time": str(datetime.now()),
                                "status": "FAILED",
                                "loss_percent": round(loss_percent, 2),
                                "loss_usd": round(loss_usd, 2),
                                "leverage": leverage,
                                "entry_time": pos.get("entry_time", str(datetime.now())),
                                "duration_hours": round((datetime.now() - datetime.fromisoformat(pos.get("entry_time", str(datetime.now())))).total_seconds() / 3600, 2)
                            }
                            
                            # İstatistikleri güncelle
                            stats["failed_signals"] += 1
                            stats["total_profit_loss"] += loss_usd
                            stats["active_signals_count"] = len(active_signals)
                            
                            if symbol in active_signals:
                                del active_signals[symbol]
                            
                            # ✅ İSTATİSTİKLERİ MONGODB'YE KAYDET
                            save_stats_to_db(stats)
                            
                            # ✅ AKTİF SİNYALLERİ MONGODB'YE KAYDET
                            save_active_signals_to_db(active_signals)
                            
                            # Pozisyonu veritabanından kaldır
                            remove_position_from_db(symbol)
                            del positions[symbol]
                        elif pos["type"] == "SATIŞ":
                            # Hedef kontrolü: Low fiyatı hedefe ulaştı mı?
                            if current_low <= pos["target"]:
                                print(f"🎯 {symbol} HEDEF BAŞARIYLA GERÇEKLEŞTİ! Çıkış: {format_price(last_price)}")
                                msg = f"🎯 <b>HEDEF BAŞARIYLA GERÇEKLEŞTİ!</b> 🎯\n\n<b>{symbol}</b> işlemi için hedef fiyatına ulaşıldı!\nÇıkış Fiyatı: <b>{format_price(last_price)}</b>\n"
                                await send_signal_to_all_users(msg)
                                # 4 saat cooldown başlat (hedef sonrası)
                                
                                # Başarılı sinyal olarak kaydet
                                leverage = pos.get("leverage", 10)  # Pozisyondan kaldıracı al
                                profit_usd = 100 * (profit_percent / 100) * leverage
                                successful_signals[symbol] = {
                                    "symbol": symbol,
                                    "type": pos["type"],
                                    "entry_price": format_price(pos["open_price"], pos["open_price"]),
                                    "exit_price": format_price(last_price, pos["open_price"]),
                                    "target_price": format_price(pos["target"], pos["open_price"]),
                                    "stop_loss": format_price(pos["stop"], pos["open_price"]),
                                    "signals": pos["signals"],
                                    "completion_time": str(datetime.now()),
                                    "status": "SUCCESS",
                                    "profit_percent": round(profit_percent, 2),
                                    "profit_usd": round(profit_usd, 2),
                                    "leverage": leverage,
                                    "entry_time": pos.get("entry_time", str(datetime.now())),
                                    "duration_hours": round((datetime.now() - datetime.fromisoformat(pos.get("entry_time", str(datetime.now())))).total_seconds() / 3600, 2)
                                }
                                
                                # İstatistikleri güncelle
                                stats["successful_signals"] += 1
                                stats["total_profit_loss"] += profit_usd
                                
                                if symbol in active_signals:
                                    del active_signals[symbol]
                                
                                # Pozisyonu veritabanından kaldır
                                remove_position_from_db(symbol)
                                del positions[symbol]
                            # Stop kontrolü: High fiyatı stop'a ulaştı mı?
                            elif current_high >= pos["stop"]:
                                print(f"🛑 {symbol} STOP HİT! Çıkış: {format_price(last_price)}")
                                # Stop mesajı gönderilmiyor - sadece hedef mesajları gönderiliyor
                                # Yalnızca bot sahibine STOP bilgisi gönder
                                try:
                                    stop_msg = (
                                        f"🛑 STOP\n"
                                        f"<b>{symbol}</b> işlemi stop oldu.\n"
                                        f"Çıkış Fiyatı: <b>{format_price(last_price)}</b>\n"
                                        f"Stop: <b>{format_price(pos['stop'], pos['open_price'])}</b>"
                                    )
                                    await send_telegram_message(stop_msg, BOT_OWNER_ID)
                                except Exception as _e:
                                    pass
                                 
                                # 4 saat cooldown başlat (stop sonrası)
                                stop_cooldown[symbol] = datetime.now()

                                # Başarısız sinyal olarak kaydet
                                loss_percent = -stop_percent
                                leverage = pos.get("leverage", 10)  # Pozisyondan kaldıracı al
                                loss_usd = -100 * (stop_percent / 100) * leverage
                                failed_signals[symbol] = {
                                    "symbol": symbol,
                                    "type": pos["type"],
                                    "entry_price": format_price(pos["open_price"], pos["open_price"]),
                                    "exit_price": format_price(last_price, pos["open_price"]),
                                    "stop_loss": format_price(pos["stop"], pos["open_price"]),
                                    "signals": pos["signals"],
                                    "completion_time": str(datetime.now()),
                                    "status": "FAILED",
                                    "loss_percent": round(loss_percent, 2),
                                    "loss_usd": round(loss_usd, 2),
                                    "leverage": leverage,
                                    "entry_time": pos.get("entry_time", str(datetime.now())),
                                    "duration_hours": round((datetime.now() - datetime.fromisoformat(pos.get("entry_time", str(datetime.now())))).total_seconds() / 3600, 2)
                                }
                                
                                # İstatistikleri güncelle
                                stats["failed_signals"] += 1
                                stats["total_profit_loss"] += loss_usd
                                
                                if symbol in active_signals:
                                    del active_signals[symbol]
                                
                                # Pozisyonu veritabanından kaldır
                                remove_position_from_db(symbol)
                                del positions[symbol]
                except Exception as e:
                    print(f"Pozisyon kontrol hatası: {symbol} - {str(e)}")
                    continue
            
            # Her zaman yeni sinyal ara (aktif pozisyon varken de)
            new_symbols = await get_active_high_volume_usdt_pairs(100)  # İlk 100 sembol
            print(f"🔍 {len(new_symbols)} kripto taranacak")
            
            # Aktif pozisyonları ve cooldown'daki coinleri koru
            protected_symbols = set()
            protected_symbols.update(positions.keys())  # Aktif pozisyonlar
            protected_symbols.update(stop_cooldown.keys())  # Cooldown'daki coinler
            
            # Yeni sembollere korunan sembolleri ekle
            symbols = list(new_symbols)
            for protected_symbol in protected_symbols:
                if protected_symbol not in symbols:
                    symbols.append(protected_symbol)
            
            print(f"📊 Toplam {len(symbols)} sembol kontrol edilecek")
            potential_signals = []
            
            # Tüm semboller için sinyal potansiyelini kontrol et
            for i, symbol in enumerate(symbols):
                if i % 20 == 0:  # Her 20 sembolde bir ilerleme göster
                    print(f"⏳ {i+1}/{len(symbols)} sembol kontrol edildi...")
                
                signal_result = await check_signal_potential(symbol, positions, stop_cooldown, successful_signals, failed_signals, timeframes, tf_names, previous_signals)
                if signal_result:
                    potential_signals.append(signal_result)
                    print(f"🎯 {symbol} için 7/7 sinyal bulundu!")
            
            print(f"📈 Toplam {len(potential_signals)} potansiyel sinyal bulundu")
            
            # İlk çalıştırmada tarama tamamlandıktan sonra sinyalleri kaydet
            if is_first:
                print(f"💾 İlk çalıştırma: {len(previous_signals)} sinyal kaydediliyor...")
                if len(previous_signals) > 0:
                    save_previous_signals_to_db(previous_signals)
                    print("✅ İlk çalıştırma sinyalleri kaydedildi!")
                else:
                    print("ℹ️ İlk çalıştırmada kayıt edilecek sinyal bulunamadı")
                is_first = False  # Artık ilk çalıştırma değil
            
            # Tüm 7/7 sinyalleri seç (limit yok)
            selected_signals = select_all_signals(potential_signals)
            
            if selected_signals:
                # En yüksek hacimli ilk 5 sinyali hemen işle
                immediate_signals = selected_signals[:5]  # İlk 5 sinyal
                waiting_signals = selected_signals[5:]   # Geri kalanlar beklemeye
                
                print(f"🚀 Hemen işlenecek: {len(immediate_signals)} sinyal (15m onay için)")
                print(f"⏳ Beklemeye alınacak: {len(waiting_signals)} sinyal (30dk sonra)")
                
                # İlk 5 sinyali hemen 15m kapanış onay kuyruğuna al
                for signal_data in immediate_signals:
                    symbol = signal_data['symbol']
                    signal_type = signal_data['signal_type']
                    price = signal_data['price']
                    volume_usd = signal_data['volume_usd']
                    signals = signal_data['signals']

                    # 15 dakikalık mum (15m) son mum rengini kontrol etmek için bekleme başlat
                    try:
                        df15 = await async_get_historical_data(symbol, '15m', 2)
                        if df15 is None or df15.empty or len(df15) < 2:
                            print(f"❌ {symbol} için 15m veri alınamadı, sinyal beklemeye alınmadı - 15m veri eksik")
                            continue  # Bu sinyali atla, sonrakine geç
                        else:
                            prev_close = float(df15['close'].iloc[-2])
                            prev_open = float(df15['open'].iloc[-2])
                            # Bekleme başlangıcı ve istenen renk koşulu
                            desired_color = 'green' if signal_type == 'ALIŞ' else 'red'
                            global_pending_signals[symbol] = {
                                'symbol': symbol,
                                'signal_type': signal_type,
                                'price': price,
                                'volume_usd': volume_usd,
                                'signals': signals,
                                'desired_color': desired_color,
                                'queued_at': datetime.now(),
                            }
                            print(f"⏳ {symbol} {signal_type} 7/7 sinyali 15m kapanış onayı için kuyruğa alındı (hedef renk: {desired_color})")
                    except Exception as e:
                        print(f"❌ {symbol} 15m veri hatası: {e}, sinyal beklemeye alınmadı")
                        continue  # Bu sinyali atla, sonrakine geç
                
                # Geri kalan sinyalleri 30dk beklemeye al
                if waiting_signals:
                    for signal_data in waiting_signals:
                        symbol = signal_data['symbol']
                        signal_type = signal_data['signal_type']
                        price = signal_data['price']
                        volume_usd = signal_data['volume_usd']
                        signals = signal_data['signals']
                        
                        # 30dk beklemeye al
                        global_waiting_signals[symbol] = {
                            'signals': signals,
                            'volume': volume_usd,
                            'type': signal_type,
                            'timestamp': datetime.now(),
                            'wait_until': datetime.now() + timedelta(minutes=30),  # 30dk sonra kontrol
                            'original_signals': signals.copy()  # Orijinal sinyalleri sakla
                        }
                        print(f"⏰ {symbol} {signal_type} 7/7 sinyali 30dk beklemeye alındı (hacim: {volume_usd:,.0f})")
                
                # Bekleme listesini güncelle
                update_waiting_list(waiting_signals)
                
                # Bekleme listesindeki değişen sinyalleri kontrol et
                changed_waiting_signals = await check_waiting_list_changes(positions, stop_cooldown, successful_signals, failed_signals, timeframes, tf_names, previous_signals)
                
                # Değişen sinyaller varsa, hepsini işle (limit yok)
                if changed_waiting_signals:
                    print(f"🔄 {len(changed_waiting_signals)} sinyal bekleme listesinden değişiklik ile geldi")
                    # Tüm değişen sinyalleri hacme göre sırala
                    sorted_changed = sorted(changed_waiting_signals, key=lambda x: -x['volume_usd'])
                    
                    # Tüm değişen sinyalleri gönder
                    for signal_data in sorted_changed:
                        await process_selected_signal(signal_data, positions, active_signals, stats)
                    
                    print(f"✅ {len(sorted_changed)} değişen sinyal işlendi")
                
                # Toplam işlenen sinyal sayısını göster
                total_processed = len(immediate_signals) + len(changed_waiting_signals)
                print(f"📊 Toplam işlenen sinyal: {total_processed} (Hemen: {len(immediate_signals)}, Değişen: {len(changed_waiting_signals)})")
            
            # 7/7 sinyaller için 15m kapanış onayı kontrolü
            await check_general_confirmations(global_pending_signals, positions, active_signals, stats)
            
            # Önceki beklemeye alınan sinyalleri de kontrol et
            if global_pending_signals:
                print(f"🔍 {len(global_pending_signals)} önceki sinyal 15m onay için bekliyor...")
                await check_general_confirmations(global_pending_signals, positions, active_signals, stats)
        
            # Aktif sinyallerin fiyatlarını güncelle
            for symbol in list(active_signals.keys()):
                if symbol not in positions:  # Pozisyon kapandıysa aktif sinyalden kaldır
                    del active_signals[symbol]
                    continue
                try:
                    df = await async_get_historical_data(symbol, '4h', 1)
                    last_price = float(df['close'].iloc[-1])
                    active_signals[symbol]["current_price"] = format_price(last_price, active_signals[symbol]["entry_price_float"])
                    active_signals[symbol]["current_price_float"] = last_price
                    active_signals[symbol]["last_update"] = str(datetime.now())
                except Exception as e:
                    print(f"Aktif sinyal güncelleme hatası: {symbol} - {str(e)}")
                    continue
            
            # İstatistikleri güncelle
            stats["active_signals_count"] = len(active_signals)
            stats["tracked_coins_count"] = len(tracked_coins)
            
            # Global değişkenleri güncelle (bot komutları için)
            global global_stats, global_active_signals, global_successful_signals, global_failed_signals, global_allowed_users, global_admin_users
            global_stats = stats.copy()
            global_active_signals = active_signals.copy()
            global_successful_signals = successful_signals.copy()
            global_failed_signals = failed_signals.copy()
            global_allowed_users = ALLOWED_USERS.copy()
            global_admin_users = ADMIN_USERS.copy()
            
            # MongoDB'ye güncel verileri kaydet
            save_stats_to_db(stats)
            save_active_signals_to_db(active_signals)
            save_positions_to_db(positions)  # ✅ POZİSYONLARI DA KAYDET

            # İstatistik özeti yazdır
            print(f"📊 İSTATİSTİK ÖZETİ:")
            total_display = stats.get('successful_signals', 0) + stats.get('failed_signals', 0) + stats.get('active_signals_count', 0)
            print(f"   Toplam Sinyal: {total_display}")
            print(f"   Başarılı: {stats['successful_signals']}")
            print(f"   Başarısız: {stats['failed_signals']}")
            print(f"   Aktif Sinyal: {stats['active_signals_count']}")
            print(f"   100$ Yatırım Toplam Kar/Zarar: ${stats['total_profit_loss']:.2f}")
            # Sadece kapanmış işlemler için ortalama kar/zarar
            closed_count = stats['successful_signals'] + stats['failed_signals']
            closed_pl = 0.0
            for s in successful_signals.values():
                closed_pl += s.get('profit_usd', 0)
            for f in failed_signals.values():
                closed_pl += f.get('loss_usd', 0)
            if closed_count > 0:
                avg_closed_pl = closed_pl / closed_count
                success_rate = (stats['successful_signals'] / closed_count) * 100
                print(f"   Başarı Oranı: %{success_rate:.1f}")
            else:
                print(f"   Başarı Oranı: %0.0")
            
            # Döngü sonunda bekleme süresi (dinamik)
            if global_pending_signals:
                # Bekleyen sinyaller varsa, en yakın mum kapanışına kadar bekle
                now = datetime.now()
                min_wait_time = 900  # Maksimum 15 dakika
                
                for symbol in list(global_pending_signals.keys()):
                    data = global_pending_signals.get(symbol)
                    if not data:
                        continue
                    
                    # 15m mum kapanış zamanını hesapla
                    current_minute = now.minute
                    if current_minute < 15:
                        target_minute = 15
                    elif current_minute < 30:
                        target_minute = 30
                    elif current_minute < 45:
                        target_minute = 45
                    else:
                        target_minute = 0
                    
                    target_time = now.replace(minute=target_minute, second=0, microsecond=0)
                    if target_time <= now:
                        target_time += timedelta(minutes=15)
                    
                    # Kalan süreyi hesapla
                    remaining_seconds = (target_time - now).total_seconds()
                    if remaining_seconds > 0 and remaining_seconds < min_wait_time:
                        min_wait_time = remaining_seconds
                
                # En yakın mum kapanışına kadar bekle (maksimum 15 dakika)
                wait_seconds = min(min_wait_time, 900)
                wait_minutes = wait_seconds / 60
                print(f"⏰ En yakın 15m mum kapanışına {wait_minutes:.1f} dakika kaldı, bekleniyor...")
                await asyncio.sleep(wait_seconds)
            else:
                # Bekleyen sinyal yoksa 15 dakika bekle
                print("Tüm coinler kontrol edildi. 15 dakika bekleniyor...")
                await asyncio.sleep(900)  # 15 dakika
            
            # Aktif sinyalleri dosyaya kaydet
            with open('active_signals.json', 'w', encoding='utf-8') as f:
                json.dump({
                    "active_signals": active_signals,
                    "count": len(active_signals),
                    "last_update": str(datetime.now())
                }, f, ensure_ascii=False, indent=2)
            
        except Exception as e:
            print(f"Genel hata: {e}")
            await asyncio.sleep(900)  # 15 dakika

async def main():
    # İzin verilen kullanıcıları ve admin gruplarını yükle
    load_allowed_users()
    
    # Bot'u başlat
    await setup_bot()
    
    # Bot'u ve sinyal işleme döngüsünü paralel olarak çalıştır
    await app.initialize()
    await app.start()
    
    # Polling başlatmadan önce webhook'ları temizle
    try:
        await app.bot.delete_webhook(drop_pending_updates=True)
        print("✅ Webhook'lar temizlendi")
        await asyncio.sleep(2)  # Biraz bekle
    except Exception as e:
        print(f"Webhook temizleme hatası: {e}")
    
    # Bot polling'i başlat
    try:
        await app.updater.start_polling(drop_pending_updates=True, allowed_updates=["message", "callback_query", "chat_member", "my_chat_member", "channel_post"])
    except Exception as e:
        print(f"Bot polling hatası: {e}")
    
    # Sinyal işleme döngüsünü başlat
    signal_task = asyncio.create_task(signal_processing_loop())
    
    try:
        await signal_task
    except KeyboardInterrupt:
        print("\n⚠️ Bot kapatılıyor...")
    except asyncio.CancelledError:
        print("\nℹ️ Bot task'ları iptal edildi (normal kapatma)")
    finally:
        # Sinyal task'ını iptal et
        if not signal_task.done():
            signal_task.cancel()
            try:
                await signal_task
            except asyncio.CancelledError:
                print("ℹ️ Sinyal task'ı iptal edildi")
        
        # Bot polling'i durdur
        try:
            await app.updater.stop()
            print("✅ Telegram bot polling durduruldu")
        except Exception as e:
            print(f"⚠️ Bot polling durdurma hatası: {e}")
        
        # Uygulamayı kapat
        try:
            await app.stop()
            await app.shutdown()
            print("✅ Telegram uygulaması kapatıldı")
        except Exception as e:
            print(f"⚠️ Uygulama kapatma hatası: {e}")
        
        # Uygulama kapatılırken MongoDB'yi kapat
        close_mongodb()
        print("✅ MongoDB bağlantısı kapatıldı")

def clear_previous_signals_from_db():
    """MongoDB'deki tüm önceki sinyal kayıtlarını ve işaret dokümanını siler."""
    try:
        # Genel DB fonksiyonunu kullan - önceki sinyalleri sil
        deleted_count = clear_data_by_pattern("^previous_signal_", "önceki sinyal")
        
        # Initialized bayrağını sil
        init_deleted = clear_specific_document("previous_signals_initialized", "initialized bayrağı")
        
        print(f"🧹 MongoDB'den {deleted_count} önceki sinyal silindi; initialized={init_deleted}")
        return deleted_count, init_deleted
    except Exception as e:
        print(f"❌ MongoDB'den önceki sinyaller silinirken hata: {e}")
        return 0, False

def clear_position_data_from_db():
    """MongoDB'deki position_ ile başlayan tüm kayıtları siler (clear_positions.py'den uyarlandı)."""
    try:
        # Genel DB fonksiyonunu kullan
        deleted_count = clear_data_by_pattern("^position_", "pozisyon")
        return deleted_count
    except Exception as e:
        print(f"❌ MongoDB'den pozisyonlar silinirken hata: {e}")
        return 0

async def clear_all_command(update, context):
    """Tüm verileri temizler: pozisyonlar, aktif sinyaller, önceki sinyaller, bekleyen kuyruklar, istatistikler (sadece bot sahibi)"""
    user_id, is_authorized = validate_user_command(update, require_owner=True)
    if not is_authorized:
        return
    
    await send_command_response(update, "🧹 Tüm veriler temizleniyor...")
    try:
        # 1) Pozisyonlar
        pos_deleted = clear_position_data_from_db()
        # Aktif sinyaller
        save_active_signals_to_db({})
        global global_active_signals
        global_active_signals = {}
        try:
            with open('active_signals.json', 'w', encoding='utf-8') as f:
                json.dump({
                    "active_signals": {},
                    "count": 0,
                    "last_update": str(datetime.now())
                }, f, ensure_ascii=False, indent=2)
        except Exception:
            pass
        
        # 2) Önceki sinyaller ve initialized bayrağı
        prev_deleted, init_deleted = clear_previous_signals_from_db()
        
        # 3) Bekleyen kuyruklar/bellek durumları
        global global_pending_signals, global_waiting_signals
        global_pending_signals = {}
        try:
            global_waiting_signals = {}
        except NameError:
            pass
        
        # 4) İstatistikler
        new_stats = {
            "total_signals": 0,
            "successful_signals": 0,
            "failed_signals": 0,
            "total_profit_loss": 0.0,
            "active_signals_count": 0,
            "tracked_coins_count": 0,
        }
        save_stats_to_db(new_stats)
        global global_stats
        if isinstance(global_stats, dict):
            global_stats.clear()
            global_stats.update(new_stats)
        else:
            global_stats = new_stats
        
        # Özet mesaj
        summary = (
            f"✅ Temizleme tamamlandı.\n"
            f"• Pozisyon: {pos_deleted} silindi\n"
            f"• Önceki sinyal: {prev_deleted} silindi (initialized: {'silindi' if init_deleted else 'yok'})\n"
            f"• Bekleyen kuyruklar sıfırlandı\n"
            f"• İstatistikler sıfırlandı"
        )
        await send_command_response(update, summary)
    except Exception as e:
        await send_command_response(update, f"❌ ClearAll hatası: {e}")

# === Genel Sinyal Hesaplama Yardımcı Fonksiyonları ===
async def calculate_signals_for_symbol(symbol, timeframes, tf_names):
    """Bir sembol için tüm zaman dilimlerinde sinyalleri hesaplar"""
    current_signals = {}
    
    for tf_name in tf_names:
        try:
            df = await async_get_historical_data(symbol, timeframes[tf_name], 1000)
            if df is None or df.empty:
                return None
            
            # Pine Script sinyallerini hesapla
            df = calculate_full_pine_signals(df, tf_name)
            
            # Son mumu kontrol et - kapanmış olmalı
            closest_idx = -1  # Son mum
            signal = int(df.iloc[closest_idx]['signal'])
            
            if signal == 0:
                # Eğer signal 0 ise, MACD ile düzelt
                if df['macd'].iloc[closest_idx] > df['macd_signal'].iloc[closest_idx]:
                    signal = 1
                else:
                    signal = -1
            
            current_signals[tf_name] = signal
            
        except Exception as e:
            print(f"❌ {symbol} {tf_name} sinyal hesaplama hatası: {e}")
            return None
    
    return current_signals

def calculate_signal_counts(signals, tf_names):
    """Sinyal sayılarını hesaplar"""
    signal_values = [signals.get(tf, 0) for tf in tf_names]
    buy_count = sum(1 for s in signal_values if s == 1)
    sell_count = sum(1 for s in signal_values if s == -1)
    return buy_count, sell_count

def check_7_7_rule(buy_count, sell_count):
    """7/7 kuralını kontrol eder - tüm 7 zaman dilimi aynı yönde olmalı"""
    return buy_count == 7 or sell_count == 7

def check_cooldown(symbol, cooldown_dict, hours=4):  # ✅ 4 SAAT COOLDOWN - TÜM SİNYALLER İÇİN
    """Cooldown kontrolü yapar - tüm sinyaller için 4 saat"""
    if symbol in cooldown_dict:
        last_time = cooldown_dict[symbol]
        if isinstance(last_time, str):
            last_time = datetime.fromisoformat(last_time)
        time_diff = (datetime.now() - last_time).total_seconds() / 3600
        if time_diff < hours:
            return True  # Cooldown aktif
        else:
            del cooldown_dict[symbol]  # Cooldown doldu
    return False  # Cooldown yok

# === Genel DB Temizleme Yardımcı Fonksiyonları ===
def clear_data_by_pattern(pattern, description="veri"):
    """Regex pattern ile eşleşen verileri MongoDB'den siler"""
    try:
        if mongo_collection is None:
            if not connect_mongodb():
                print(f"❌ MongoDB bağlantısı kurulamadı, {description} silinemedi")
                return 0
        
        delete_result = mongo_collection.delete_many({"_id": {"$regex": pattern}})
        deleted_count = getattr(delete_result, "deleted_count", 0)
        
        print(f"🧹 MongoDB'den {deleted_count} {description} silindi")
        return deleted_count
    except Exception as e:
        print(f"❌ MongoDB'den {description} silinirken hata: {e}")
        return 0

def clear_specific_document(doc_id, description="doküman"):
    """Belirli bir dokümanı MongoDB'den siler"""
    try:
        if mongo_collection is None:
            if not connect_mongodb():
                print(f"❌ MongoDB bağlantısı kurulamadı, {description} silinemedi")
                return False
        
        delete_result = mongo_collection.delete_one({"_id": doc_id})
        deleted_count = getattr(delete_result, "deleted_count", 0)
        
        if deleted_count > 0:
            print(f"🧹 MongoDB'den {description} silindi")
            return True
        else:
            print(f"ℹ️ {description} zaten mevcut değildi")
            return False
    except Exception as e:
        print(f"❌ MongoDB'den {description} silinirken hata: {e}")
        return False

# === Genel DB Okuma Yardımcı Fonksiyonları ===
def load_data_by_pattern(pattern, data_key="data", description="veri", transform_func=None):
    """Regex pattern ile eşleşen verileri MongoDB'den yükler"""
    try:
        if mongo_collection is None:
            if not connect_mongodb():
                print(f"❌ MongoDB bağlantısı kurulamadı, {description} yüklenemedi")
                return {}
        
        result = {}
        docs = mongo_collection.find({"_id": {"$regex": pattern}})
        
        for doc in docs:
            if transform_func:
                result.update(transform_func(doc))
            else:
                # Varsayılan transform: _id'den pattern'i çıkar ve data_key'i al
                key = doc["_id"].replace(pattern.replace("^", "").replace("$", ""), "")
                if data_key and data_key in doc:
                    result[key] = doc[data_key]
                else:
                    result[key] = doc
        
        print(f"✅ MongoDB'den {len(result)} {description} yüklendi")
        return result
    except Exception as e:
        print(f"❌ MongoDB'den {description} yüklenirken hata: {e}")
        return {}

# === Genel Hata Yönetimi Yardımcı Fonksiyonları ===
def safe_mongodb_operation(operation_func, error_message="MongoDB işlemi", default_return=None):
    """MongoDB işlemlerini güvenli şekilde yapar"""
    try:
        if mongo_collection is None:
            if not connect_mongodb():
                print(f"❌ MongoDB bağlantısı kurulamadı, {error_message} yapılamadı")
                return default_return
        return operation_func()
    except Exception as e:
        print(f"❌ {error_message} sırasında hata: {e}")
        return default_return

# === Genel Sinyal Hesaplama Yardımcı Fonksiyonları ===
async def check_general_confirmations(pending_dict, positions, active_signals, stats):
    
    if not pending_dict:
        return
    
    now = datetime.now()
    
    for symbol in list(pending_dict.keys()):
        data = pending_dict.get(symbol)
        if not data:
            continue
        try:
            # 15m mum kapanış zamanını hesapla
            current_minute = now.minute
            
            # Şu anki 15dk periyodun bitiş zamanını hesapla
            if current_minute < 15:
                target_minute = 15
            elif current_minute < 30:
                target_minute = 30
            elif current_minute < 45:
                target_minute = 45
            else:
                target_minute = 0
            
            # Hedef zamana kadar beklenecek süreyi hesapla
            target_time = now.replace(minute=target_minute, second=0, microsecond=0)
            
            # Eğer hedef zaman geçmişse, bir sonraki 15dk periyoda geç
            if target_time <= now:
                target_time += timedelta(minutes=15)
            
            # Henüz 15dk mum kapanmadıysa bekle
            if now < target_time:
                # Kalan süreyi göster (her çağrıda güncel)
                remaining_minutes = (target_time - now).total_seconds() / 60
                print(f"⏳ {symbol} 15m mum kapanışı bekleniyor: {remaining_minutes:.1f} dakika kaldı")
                continue  # Henüz erken, bekle
            
            # 15dk mum kapandı, şimdi kontrol et
            print(f"🔍 {symbol} 15m mum kapanışı kontrol ediliyor...")
            
            df15 = await async_get_historical_data(symbol, '15m', 2)
            if df15 is None or df15.empty or len(df15) < 2:
                print(f"❌ {symbol} 15m veri alınamadı, sinyal iptal edildi")
                del pending_dict[symbol]
                continue
            
            last_open = float(df15['open'].iloc[-1])
            last_close = float(df15['close'].iloc[-1])
            is_green = last_close > last_open
            desired = data.get('desired_color')
            ok = (desired == 'green' and is_green) or (desired == 'red' and not is_green)
            
            if ok:
                # Onay anındaki güncel fiyatı al
                current_price = float(df15['close'].iloc[-1])
                
                # Güncel fiyattan hedef ve stop hesapla
                message, _, target_price, stop_loss, stop_loss_str, leverage, _ = create_signal_message_new_55(
                    data['symbol'], current_price, data['signals'], data['volume_usd'], 3.0, 2.0
                )

                if message:
                    position = {
                        "type": data['signal_type'],
                        "target": target_price,
                        "stop": stop_loss,
                        "open_price": current_price,  # Onay anındaki fiyat
                        "stop_str": stop_loss_str,
                        "signals": data['signals'],
                        "leverage": leverage,
                        "entry_time": datetime.now().strftime('%Y-%m-%d %H:%M'),
                        "entry_timestamp": datetime.now()
                    }
                    positions[data['symbol']] = position
                    save_positions_to_db(positions)
                    active_signals[data['symbol']] = {
                        "symbol": data['symbol'],
                        "type": data['signal_type'],
                        "entry_price": format_price(current_price, current_price),  # Onay anındaki fiyat
                        "entry_price_float": current_price,  # Onay anındaki fiyat
                        "target_price": format_price(target_price, current_price),  # Onay anındaki fiyattan hesaplanan hedef
                        "stop_loss": format_price(stop_loss, current_price),  # Onay anındaki fiyattan hesaplanan stop
                        "signals": data['signals'],
                        "leverage": leverage,
                        "signal_time": datetime.now().strftime('%Y-%m-%d %H:%M'),
                        "current_price": format_price(current_price, current_price),  # Onay anındaki fiyat
                        "current_price_float": current_price,  # Onay anındaki fiyat
                        "last_update": str(datetime.now())
                    }
                    save_active_signals_to_db(active_signals)
                    stats["total_signals"] += 1
                    stats["active_signals_count"] = len(active_signals)
                    save_stats_to_db(stats)
                    await send_signal_to_all_users(message)
                    print(f"✅ {data['symbol']} {data['signal_type']} 7/7 sinyali 15m kapanış onayı ile gönderildi (giriş: {current_price:.6f})")
                del pending_dict[symbol]
            else:
                print(f"❌ {symbol} 15m kapanış onayı sağlanmadı, sinyal iptal edildi")
                del pending_dict[symbol]
        except Exception as e:
            print(f"⚠️ {symbol} genel onay kontrol hatası: {e}")
            # Hata durumunda sinyali iptal et
            del pending_dict[symbol]

def select_all_signals(potential_signals):
    """Tüm 7/7 sinyalleri seçer ve işler."""
    if not potential_signals:
        return []
    
    # Tüm sinyalleri hacme göre sırala
    all_signals = sorted(potential_signals, key=lambda x: x['volume_usd'], reverse=True)
    print(f"✅ {len(all_signals)} 7/7 sinyal seçildi")
    return all_signals

if __name__ == "__main__":
    asyncio.run(main())