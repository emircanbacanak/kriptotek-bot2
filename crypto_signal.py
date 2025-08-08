import sys
import asyncio
if sys.platform.startswith("win"):
    asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())
from collections import defaultdict
from binance.client import Client
from binance.enums import *
import pandas as pd
import numpy as np
import ta
import time
from datetime import datetime, timedelta
import telegram
from telegram.ext import Application, CommandHandler, MessageHandler, filters, ContextTypes
import requests
import certifi
from urllib3.exceptions import InsecureRequestWarning
import urllib3
from decimal import Decimal, ROUND_DOWN, getcontext
import json
import aiohttp
import tqdm
from dotenv import load_dotenv
import os
from pymongo import MongoClient
from pymongo.errors import ConnectionFailure, ServerSelectionTimeoutError

load_dotenv()

TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")

# MongoDB bağlantı bilgileri
MONGODB_URI = os.getenv("MONGODB_URI", "mongodb://localhost:27017/")
MONGODB_DB = os.getenv("MONGODB_DB", "crypto_signal_bot")
MONGODB_COLLECTION = os.getenv("MONGODB_COLLECTION", "allowed_users")

# TR saat dilimi için zaman alma fonksiyonu
try:
    from zoneinfo import ZoneInfo
    def get_tr_time():
        return datetime.now(ZoneInfo("Europe/Istanbul"))
except ImportError:
    import pytz
    def get_tr_time():
        return datetime.now(pytz.timezone("Europe/Istanbul"))

# SSL uyarılarını kapat
urllib3.disable_warnings(InsecureRequestWarning)

# Binance client oluştur (globalde)
client = Client()

# Telegram bot oluştur
bot = telegram.Bot(token=TELEGRAM_TOKEN)

# Bot sahibinin ID'si (bu değeri .env dosyasından alabilirsiniz)
BOT_OWNER_ID = int(os.getenv("BOT_OWNER_ID", "0"))

# Admin kullanıcıları listesi (bot sahibi tarafından yönetilir)
ADMIN_USERS = set()

# Binance interval çevirimi
BINANCE_INTERVALS = {
    '15m': '15m',
    '30m': '30m',
    '1h': '1h',
    '2h': '2h',
    '4h': '4h',
    '8h': '8h',
    '12h': '12h',
    '1d': '1d',
    '1w': '1w'
}

# MongoDB bağlantısı
mongo_client = None
mongo_db = None
mongo_collection = None

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
        
        # MongoDB'den kullanıcıları çek
        if mongo_collection is not None:
            users_doc = mongo_collection.find_one({"_id": "allowed_users"})
            if users_doc:
                ALLOWED_USERS = set(users_doc.get('user_ids', []))
                print(f"✅ MongoDB'den {len(ALLOWED_USERS)} izin verilen kullanıcı yüklendi")
            else:
                print("ℹ️ MongoDB'de izin verilen kullanıcı bulunamadı, boş liste ile başlatılıyor")
                ALLOWED_USERS = set()
            
            # Admin gruplarını çek
            admin_doc = mongo_collection.find_one({"_id": "admin_groups"})
            if admin_doc:
                BOT_OWNER_GROUPS = set(admin_doc.get('group_ids', []))
                print(f"✅ MongoDB'den {len(BOT_OWNER_GROUPS)} admin grubu yüklendi")
            else:
                print("ℹ️ MongoDB'de admin grubu bulunamadı, boş liste ile başlatılıyor")
                BOT_OWNER_GROUPS = set()
            
            # Admin kullanıcılarını çek
            admin_users_doc = mongo_collection.find_one({"_id": "admin_users"})
            if admin_users_doc:
                ADMIN_USERS = set(admin_users_doc.get('admin_ids', []))
                print(f"✅ MongoDB'den {len(ADMIN_USERS)} admin kullanıcı yüklendi")
            else:
                print("ℹ️ MongoDB'de admin kullanıcı bulunamadı, boş liste ile başlatılıyor")
                ADMIN_USERS = set()
        else:
            print("⚠️ MongoDB collection bulunamadı, boş liste ile başlatılıyor")
            ALLOWED_USERS = set()
            BOT_OWNER_GROUPS = set()
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
                return
        
        # Upsert ile kaydet (varsa güncelle, yoksa ekle)
        mongo_collection.update_one(
            {"_id": "allowed_users"},
            {
                "$set": {
                    "user_ids": list(ALLOWED_USERS),
                    "last_updated": str(datetime.now()),
                    "count": len(ALLOWED_USERS)
                }
            },
            upsert=True
        )
        print(f"✅ MongoDB'ye {len(ALLOWED_USERS)} izin verilen kullanıcı kaydedildi")
    except Exception as e:
        print(f"❌ MongoDB'ye kullanıcılar kaydedilirken hata: {e}")

def save_admin_groups():
    """Admin gruplarını MongoDB'ye kaydet"""
    try:
        if mongo_collection is None:
            if not connect_mongodb():
                print("❌ MongoDB bağlantısı kurulamadı, admin grupları kaydedilemedi")
                return
        
        # Upsert ile kaydet (varsa güncelle, yoksa ekle)
        mongo_collection.update_one(
            {"_id": "admin_groups"},
            {
                "$set": {
                    "group_ids": list(BOT_OWNER_GROUPS),
                    "last_updated": str(datetime.now()),
                    "count": len(BOT_OWNER_GROUPS)
                }
            },
            upsert=True
        )
        print(f"✅ MongoDB'ye {len(BOT_OWNER_GROUPS)} admin grubu kaydedildi")
    except Exception as e:
        print(f"❌ MongoDB'ye admin grupları kaydedilirken hata: {e}")

def save_admin_users():
    """Admin kullanıcılarını MongoDB'ye kaydet"""
    try:
        if mongo_collection is None:
            if not connect_mongodb():
                print("❌ MongoDB bağlantısı kurulamadı, admin kullanıcıları kaydedilemedi")
                return
        
        # Upsert ile kaydet (varsa güncelle, yoksa ekle)
        mongo_collection.update_one(
            {"_id": "admin_users"},
            {
                "$set": {
                    "admin_ids": list(ADMIN_USERS),
                    "last_updated": str(datetime.now()),
                    "count": len(ADMIN_USERS)
                }
            },
            upsert=True
        )
        print(f"✅ MongoDB'ye {len(ADMIN_USERS)} admin kullanıcı kaydedildi")
    except Exception as e:
        print(f"❌ MongoDB'ye admin kullanıcıları kaydedilirken hata: {e}")

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
                "is_special": position.get("is_special", False),
                "last_updated": str(datetime.now())
            }
            
            mongo_collection.update_one(
                {"_id": f"position_{symbol}"},
                {"$set": position_doc},
                upsert=True
            )
        
        print(f"✅ MongoDB'ye {len(positions)} pozisyon kaydedildi")
        return True
    except Exception as e:
        print(f"❌ MongoDB'ye pozisyonlar kaydedilirken hata: {e}")
        return False

def load_positions_from_db():
    """Pozisyonları MongoDB'den yükle"""
    try:
        if mongo_collection is None:
            if not connect_mongodb():
                print("❌ MongoDB bağlantısı kurulamadı, pozisyonlar yüklenemedi")
                return {}
        
        positions = {}
        position_docs = mongo_collection.find({"_id": {"$regex": "^position_"}})
        
        for doc in position_docs:
            symbol = doc["symbol"]
            positions[symbol] = {
                "type": doc["type"],
                "target": doc["target"],
                "stop": doc["stop"],
                "open_price": doc["open_price"],
                "stop_str": doc["stop_str"],
                "signals": doc["signals"],
                "leverage": doc.get("leverage", 10),
                "entry_time": doc["entry_time"],
                "entry_timestamp": datetime.fromisoformat(doc["entry_timestamp"]) if isinstance(doc["entry_timestamp"], str) else doc["entry_timestamp"],
                "is_special": doc.get("is_special", False)
            }
        
        print(f"✅ MongoDB'den {len(positions)} pozisyon yüklendi")
        return positions
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
            
            mongo_collection.update_one(
                {"_id": f"previous_signal_{symbol}"},
                {"$set": signal_doc},
                upsert=True
            )
        
        # İlk kayıt işaretini koy
        mongo_collection.update_one(
            {"_id": "previous_signals_initialized"},
            {"$set": {"initialized": True, "initialized_time": str(datetime.now())}},
            upsert=True
        )
        
        print(f"✅ MongoDB'ye {len(previous_signals)} önceki sinyal kaydedildi (ilk çalıştırma)")
        return True
    except Exception as e:
        print(f"❌ MongoDB'ye önceki sinyaller kaydedilirken hata: {e}")
        return False

def load_previous_signals_from_db():
    """Önceki sinyalleri MongoDB'den yükle"""
    try:
        if mongo_collection is None:
            if not connect_mongodb():
                print("❌ MongoDB bağlantısı kurulamadı, önceki sinyaller yüklenemedi")
                return {}
        
        previous_signals = {}
        signal_docs = mongo_collection.find({"_id": {"$regex": "^previous_signal_"}})
        
        for doc in signal_docs:
            symbol = doc["symbol"]
            previous_signals[symbol] = doc["signals"]
        
        print(f"✅ MongoDB'den {len(previous_signals)} önceki sinyal yüklendi")
        return previous_signals
    except Exception as e:
        print(f"❌ MongoDB'den önceki sinyaller yüklenirken hata: {e}")
        return {}

def is_first_run():
    """İlk çalıştırma mı kontrol et"""
    try:
        if mongo_collection is None:
            if not connect_mongodb():
                return True  # Bağlantı yoksa ilk çalıştırma kabul et
        
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
    except Exception as e:
        print(f"❌ İlk çalıştırma kontrolünde hata: {e}")
        return True

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
        
        mongo_collection.update_one(
            {"_id": f"previous_signal_{symbol}"},
            {"$set": signal_doc},
            upsert=True
        )
        
        return True
    except Exception as e:
        print(f"❌ Önceki sinyal güncellenirken hata: {e}")
        return False

def remove_position_from_db(symbol):
    """Pozisyonu MongoDB'den kaldır"""
    try:
        if mongo_collection is None:
            if not connect_mongodb():
                return False
        
        mongo_collection.delete_one({"_id": f"position_{symbol}"})
        return True
    except Exception as e:
        print(f"❌ Pozisyon kaldırılırken hata: {e}")
        return False

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
global_successful_signals = {}
global_failed_signals = {}
# global_changed_symbols artık kullanılmıyor
global_allowed_users = set()  # İzin verilen kullanıcılar
global_admin_users = set()  # Admin kullanıcılar

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
    
    # Bot sahibine gönder (her zaman)
    if BOT_OWNER_ID:
        try:
            await send_telegram_message(message, BOT_OWNER_ID)
            sent_chats.add(str(BOT_OWNER_ID))
            print(f"✅ Bot sahibine sinyal gönderildi: {BOT_OWNER_ID}")
        except Exception as e:
            print(f"❌ Bot sahibine sinyal gönderilemedi: {e}")
    
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

🔧 **Özel Yetkiler:**
• Tüm komutlara erişim
• Admin ekleme/silme
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
    
    # Global istatistikleri kullan
    stats = global_stats
    if not stats:
        stats_text = "📊 **Bot İstatistikleri:**\n\nHenüz istatistik verisi yok."
    else:
        closed_count = stats.get('successful_signals', 0) + stats.get('failed_signals', 0)
        success_rate = 0
        if closed_count > 0:
            success_rate = (stats.get('successful_signals', 0) / closed_count) * 100
        
        # Bot durumu
        status_emoji = "🟢"
        status_text = "Aktif (Sinyal Arama Çalışıyor)"
        
        stats_text = f"""📊 **Bot İstatistikleri:**

📈 **Genel Durum:**
• Toplam Sinyal: {stats.get('total_signals', 0)}
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
    
    # Global aktif sinyalleri kullan
    active_signals = global_active_signals
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
    if not update.effective_user:
        return
    
    user_id = update.effective_user.id
    if not is_admin(user_id):
        return  # İzin verilmeyen kullanıcılar için hiçbir yanıt verme
    
    if not context.args:
        await update.message.reply_text("❌ Kullanım: /adduser <user_id>")
        return
    
    try:
        new_user_id = int(context.args[0])
        if new_user_id == BOT_OWNER_ID:
            await update.message.reply_text("❌ Bot sahibi zaten her zaman erişime sahiptir.")
            return
        
        if new_user_id in ALLOWED_USERS:
            await update.message.reply_text("❌ Bu kullanıcı zaten izin verilen kullanıcılar listesinde.")
            return
        
        if new_user_id in ADMIN_USERS:
            await update.message.reply_text("❌ Bu kullanıcı zaten admin listesinde.")
            return
        
        ALLOWED_USERS.add(new_user_id)
        save_allowed_users()  # MongoDB'ye kaydet
        await update.message.reply_text(f"✅ Kullanıcı {new_user_id} başarıyla eklendi ve kalıcı olarak kaydedildi.")
    except ValueError:
        await update.message.reply_text("❌ Geçersiz user_id. Lütfen sayısal bir değer girin.")

async def removeuser_command(update, context):
    """Kullanıcı çıkarma komutu (sadece bot sahibi ve adminler)"""
    if not update.effective_user:
        return
    
    user_id = update.effective_user.id
    if not is_admin(user_id):
        return  # İzin verilmeyen kullanıcılar için hiçbir yanıt verme
    
    if not context.args:
        await update.message.reply_text("❌ Kullanım: /removeuser <user_id>")
        return
    
    try:
        remove_user_id = int(context.args[0])
        if remove_user_id in ALLOWED_USERS:
            ALLOWED_USERS.remove(remove_user_id)
            save_allowed_users()  # MongoDB'ye kaydet
            await update.message.reply_text(f"✅ Kullanıcı {remove_user_id} başarıyla çıkarıldı ve kalıcı olarak kaydedildi.")
        else:
            await update.message.reply_text(f"❌ Kullanıcı {remove_user_id} zaten izin verilen kullanıcılar listesinde yok.")
    except ValueError:
        await update.message.reply_text("❌ Geçersiz user_id. Lütfen sayısal bir değer girin.")

async def listusers_command(update, context):
    """İzin verilen kullanıcıları listeleme komutu (sadece bot sahibi ve adminler)"""
    if not update.effective_user:
        return
    
    user_id = update.effective_user.id
    if not is_admin(user_id):
        return  # İzin verilmeyen kullanıcılar için hiçbir yanıt verme
    
    if not ALLOWED_USERS:
        users_text = "📋 **İzin Verilen Kullanıcılar:**\n\nHenüz izin verilen kullanıcı yok."
    else:
        users_list = "\n".join([f"• {user_id}" for user_id in ALLOWED_USERS])
        users_text = f"📋 **İzin Verilen Kullanıcılar:**\n\n{users_list}"
    
    await update.message.reply_text(users_text, parse_mode='Markdown')

async def adminekle_command(update, context):
    """Admin ekleme komutu (sadece bot sahibi)"""
    if not update.effective_user:
        return
    
    user_id = update.effective_user.id
    if user_id != BOT_OWNER_ID:
        return  # İzin verilmeyen kullanıcılar için hiçbir yanıt verme
    
    if not context.args:
        await update.message.reply_text("❌ Kullanım: /adminekle <user_id>")
        return
    
    try:
        new_admin_id = int(context.args[0])
        if new_admin_id == BOT_OWNER_ID:
            await update.message.reply_text("❌ Bot sahibi zaten admin yetkilerine sahiptir.")
            return
        
        if new_admin_id in ADMIN_USERS:
            await update.message.reply_text("❌ Bu kullanıcı zaten admin listesinde.")
            return
        
        ADMIN_USERS.add(new_admin_id)
        save_admin_users()  # MongoDB'ye kaydet
        await update.message.reply_text(f"✅ Admin {new_admin_id} başarıyla eklendi ve kalıcı olarak kaydedildi.")
    except ValueError:
        await update.message.reply_text("❌ Geçersiz user_id. Lütfen sayısal bir değer girin.")

async def adminsil_command(update, context):
    """Admin silme komutu (sadece bot sahibi)"""
    if not update.effective_user:
        return
    
    user_id = update.effective_user.id
    if user_id != BOT_OWNER_ID:
        return  # İzin verilmeyen kullanıcılar için hiçbir yanıt verme
    
    if not context.args:
        await update.message.reply_text("❌ Kullanım: /adminsil <user_id>")
        return
    
    try:
        remove_admin_id = int(context.args[0])
        if remove_admin_id in ADMIN_USERS:
            ADMIN_USERS.remove(remove_admin_id)
            save_admin_users()  # MongoDB'ye kaydet
            await update.message.reply_text(f"✅ Admin {remove_admin_id} başarıyla silindi ve kalıcı olarak kaydedildi.")
        else:
            await update.message.reply_text(f"❌ Admin {remove_admin_id} zaten admin listesinde yok.")
    except ValueError:
        await update.message.reply_text("❌ Geçersiz user_id. Lütfen sayısal bir değer girin.")

async def listadmins_command(update, context):
    """Admin listesini gösterme komutu (sadece bot sahibi ve adminler)"""
    if not update.effective_user:
        return
    
    user_id = update.effective_user.id
    if not is_admin(user_id):
        return  # İzin verilmeyen kullanıcılar için hiçbir yanıt verme
    
    if not ADMIN_USERS:
        admins_text = "👑 **Admin Kullanıcıları:**\n\nHenüz admin kullanıcı yok.\n\nBot Sahibi: {BOT_OWNER_ID}"
    else:
        admins_list = "\n".join([f"• {admin_id}" for admin_id in ADMIN_USERS])
        admins_text = f"👑 **Admin Kullanıcıları:**\n\n{admins_list}\n\nBot Sahibi: {BOT_OWNER_ID}"
    
    await update.message.reply_text(admins_text, parse_mode='Markdown')

async def handle_message(update, context):
    """Genel mesaj handler'ı"""
    if not update.effective_user:
        return
    
    user_id = update.effective_user.id
    
    # Sadece yetkili kullanıcılar için yardım mesajı göster
    if user_id == BOT_OWNER_ID or user_id in ALLOWED_USERS or user_id in ADMIN_USERS:
        await update.message.reply_text("🤖 Bu bot sadece komutları destekler. /help yazarak mevcut komutları görebilirsiniz.")
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
            user_id = update.effective_user.id
            if user_id == BOT_OWNER_ID or user_id in ALLOWED_USERS or user_id in ADMIN_USERS:
                await update.message.reply_text("❌ Bir hata oluştu. Lütfen daha sonra tekrar deneyin.")
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
                
                # Bot sahibine bildirim gönder
                success_msg = f"✅ **Kanal Otomatik Ekleme**\n\nKanal '{chat.title}' otomatik olarak izin verilen gruplara eklendi.\n\nChat ID: {chat.id}\nBot artık bu kanalda çalışabilir."
                await send_telegram_message(success_msg)
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
                        
                        # Bot sahibine bildirim gönder
                        warning_msg = f"⚠️ **GÜVENLİK UYARISI** ⚠️\n\nBot sahibi olmayan bir kullanıcı ({user_id}) bot'u '{chat.title}' {chat_type.replace('ndan', 'na')} eklemeye çalıştı.\n\nBot otomatik olarak {chat_type} çıktı.\n\nChat ID: {chat.id}"
                        await send_telegram_message(warning_msg)
                        
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
                    
                    # Bot sahibine bildirim gönder
                    success_msg = f"✅ **Bot {chat_type.title()} Ekleme Başarılı**\n\nBot '{chat.title}' {chat_type} başarıyla eklendi.\n\nChat ID: {chat.id}\nBot artık bu {chat_type.replace('na', 'da')} çalışabilir."
                    await send_telegram_message(success_msg)
    
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
                
                # Bot sahibine bildirim gönder
                leave_msg = f"ℹ️ **Bot {chat_type.title()} Çıkışı**\n\nBot '{chat.title}' {chat_type} çıkarıldı.\n\nChat ID: {chat.id}\nBu {chat_type.replace('ndan', '')} artık izin verilen gruplar listesinde değil."
                await send_telegram_message(leave_msg)
            else:
                chat_type = "kanalından" if chat.type == "channel" else "grubundan"
                print(f"Bot {chat.title} {chat_type} çıkarıldı.")

async def setup_bot():
    """Bot handler'larını kur"""
    global app
    app = Application.builder().token(TELEGRAM_TOKEN).build()
    
    # Mevcut webhook'ları temizle ve offset'i sıfırla
    try:
        await app.bot.delete_webhook(drop_pending_updates=True)
        print("✅ Mevcut webhook'lar temizlendi ve pending updates silindi")
    except Exception as e:
        print(f"⚠️ Webhook temizleme hatası: {e}")
    
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
    if volume >= 1_000_000_000:  # Milyar
        return f"${volume/1_000_000_000:.1f}B"
    elif volume >= 1_000_000:  # Milyon
        return f"${volume/1_000_000:.1f}M"
    elif volume >= 1_000:  # Bin
        return f"${volume/1_000:.1f}K"
    else:
        return f"${volume:,.0f}"

def create_signal_message(symbol, price, signals, volume, profit_percent=1.5, stop_percent=1.0, signal_8h=None):
    """Sinyal mesajını oluştur (AL/SAT başlıkta)"""
    price_str = format_price(price, price)
    signal_4h = "ALIŞ" if signals.get('4h', 0) == 1 else "SATIŞ"
    signal_1d = "ALIŞ" if signals.get('1d', 0) == 1 else "SATIŞ"
    buy_count = sum(1 for s in signals.values() if s == 1)
    sell_count = sum(1 for s in signals.values() if s == -1)
    
    if buy_count == 3:  # 3/3 ALIŞ
        dominant_signal = "ALIŞ"
        target_price = price * (1 + profit_percent / 100)  # Dinamik kar hedefi
        stop_loss = price * (1 - stop_percent / 100)       # Dinamik stop loss
        sinyal_tipi = "AL SİNYALİ"
        leverage = 10
        
        # 8h kontrolü - 4/3 kuralı
        if signal_8h == 1:  # 8h da da ALIŞ
            leverage = 15  # 4/3 kuralı - kaldıraç artır
            print(f"🚀 {symbol} 8h da da ALIŞ! Kaldıraç 15x'e yükseltildi!")
        else:
            print(f"📊 {symbol} 8h da ALIŞ değil, 10x kaldıraç korundu")
            
    elif sell_count == 3:  # 3/3 SATIŞ
        dominant_signal = "SATIŞ"
        target_price = price * (1 - profit_percent / 100)  # Dinamik kar hedefi
        stop_loss = price * (1 + stop_percent / 100)       # Dinamik stop loss
        sinyal_tipi = "SAT SİNYALİ"
        leverage = 10
        
        # 8h kontrolü - 4/3 kuralı
        if signal_8h == -1:  # 8h da da SATIŞ
            leverage = 15  # 4/3 kuralı - kaldıraç artır
            print(f"🚀 {symbol} 8h da da SATIŞ! Kaldıraç 15x'e yükseltildi!")
        else:
            print(f"📊 {symbol} 8h da SATIŞ değil, 10x kaldıraç korundu")
    else:
        return None, None, None, None, None
    
    target_price_str = format_price(target_price, price)
    stop_loss_str = format_price(stop_loss, price)
    volume_formatted = format_volume(volume)  # Yeni hacim formatı
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

    return message, dominant_signal, target_price, stop_loss, stop_loss_str, leverage

def create_special_signal_message(symbol, price, signals, volume, profit_percent=1.5, stop_percent=1.0):
    """Özel yıldızlı sinyal mesajını oluştur"""
    price_str = format_price(price, price)
    volume_formatted = format_volume(volume) if volume else "N/A"
    
    # Sinyal türünü belirle
    signal_values = [signals.get(tf, 0) for tf in ['1h', '2h', '4h', '8h', '12h']]
    buy_count = sum(1 for s in signal_values if s == 1)
    sell_count = sum(1 for s in signal_values if s == -1)
    
    if buy_count == 5:
        signal_type = "ALIŞ"
        emoji = "🚀"
        target_price = price * (1 + profit_percent / 100)
        stop_loss = price * (1 - stop_percent / 100)
    elif sell_count == 5:
        signal_type = "SATIŞ"
        emoji = "📉"
        target_price = price * (1 - profit_percent / 100)
        stop_loss = price * (1 + stop_percent / 100)
    else:
        return None
    
    target_str = format_price(target_price, price)
    stop_str = format_price(stop_loss, price)
    
    # Yıldızlı özel mesaj formatı
    message = f"""
⭐️ <b>YILDIZLI ÖZEL SİNYAL</b> ⭐️

{emoji} <b>{symbol}</b> - <b>{signal_type}</b>

💰 <b>Giriş Fiyatı:</b> {price_str}
🎯 <b>Hedef:</b> {target_str} (+{profit_percent}%)
🛑 <b>Stop Loss:</b> {stop_str} (-{stop_percent}%)
⚡ <b>Kaldıraç:</b> 20x
📊 <b>Hacim (24h):</b> {volume_formatted}

⏰ <b>Zaman Dilimleri:</b> 1h + 2h + 4h + 8h + 12h
🎯 <b>Filtre:</b> 5/5 (Tüm zaman dilimleri uyumlu)

<b>⚠️ ÖZEL SİNYAL - 24 saatte sadece 1 kez!</b>
<b>🚀 Yüksek başarı oranı bekleniyor!</b>

#ÖzelSinyal #YıldızlıSinyal #5ZamanDilimi
"""
    
    return message, signal_type, target_price, stop_loss, stop_str

async def get_24h_volume_usd(symbol):
    """Binance Futures'den 24 saatlik USD hacmini çek"""
    # Futures sembolü için USDT ekle (eğer yoksa)
    if not symbol.endswith('USDT'):
        symbol = symbol + 'USDT'
    
    url = f"https://fapi.binance.com/fapi/v1/ticker/24hr?symbol={symbol}"
    try:
        async with aiohttp.ClientSession(connector=aiohttp.TCPConnector(limit=10)) as session:
            async with session.get(url, ssl=False) as resp:
                if resp.status != 200:
                    raise Exception(f"Futures API hatası: {resp.status} - {await resp.text()}")
                ticker = await resp.json()
                quote_volume = ticker.get('quoteVolume', 0)  # USD hacmi
                if quote_volume is None:
                    return 0
                return float(quote_volume)
    except Exception as e:
        print(f"Futures 24h USD hacim çekme hatası: {symbol} - {str(e)}")
        return 0

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

def calculate_full_pine_signals(df, timeframe, fib_filter_enabled=False):
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
    if fib_filter_enabled:
        df['fib_in_range'] = (df['close'] > fib_level1) & (df['close'] < fib_level2)
    else:
        df['fib_in_range'] = True

    def crossover(s1, s2):
        return (s1.shift(1) < s2.shift(1)) & (s1 > s2)

    def crossunder(s1, s2):
        return (s1.shift(1) > s2.shift(1)) & (s1 < s2)

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

async def get_active_high_volume_usdt_pairs(top_n=100):
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

async def check_special_signal(symbol, special_timeframes, special_tf_names):
    """Özel sinyal kontrolü - 5 zaman diliminde aynı sinyal"""
    try:
        # 5 timeframe verilerini çek
        current_signals = dict()
        for tf_name in special_tf_names:
            try:
                df = await async_get_historical_data(symbol, special_timeframes[tf_name], 1000)
                df = calculate_full_pine_signals(df, tf_name)
                
                # En yakın mumun sinyalini al
                current_time = datetime.now()
                closest_idx = df['timestamp'].searchsorted(current_time)
                if closest_idx < len(df):
                    signal = int(df.iloc[closest_idx]['signal'])
                    # Sinyal 0 ise MACD ile düzelt
                    if signal == 0:
                        if df['macd'].iloc[closest_idx] > df['macd_signal'].iloc[closest_idx]:
                            signal = 1
                        else:
                            signal = -1
                    current_signals[tf_name] = signal
                else:
                    current_signals[tf_name] = 0
            except Exception as e:
                return None  # Hata varsa özel sinyal yok
        
        # 5/5 şartı kontrolü - tüm zaman dilimleri aynı sinyali vermeli
        signal_values = [current_signals.get(tf, 0) for tf in special_tf_names]
        buy_count = sum(1 for s in signal_values if s == 1)
        sell_count = sum(1 for s in signal_values if s == -1)
        
        if buy_count == 5:  # 5/5 ALIŞ
            return "ALIS", current_signals
        elif sell_count == 5:  # 5/5 SATIŞ
            return "SATIS", current_signals
        else:
            return None  # 5/5 şartı sağlanmadı
            
    except Exception as e:
        return None

async def process_symbol(symbol, positions, stop_cooldown, successful_signals, failed_signals, timeframes, tf_names, previous_signals, cooldown_signals, sent_signals, active_signals, stats, profit_percent, stop_percent, changed_symbols=None):
    # Debug log
    # print(f"🔍 {symbol} işleniyor...")  # Debug mesajını kaldır
    
    # Eğer pozisyon açıksa, yeni sinyal arama
    if symbol in positions:
        # print(f"⏸️ {symbol} zaten pozisyon açık, atlanıyor")  # Debug mesajını kaldır
        return
    # Stop sonrası 4 saatlik cooldown kontrolü (Backtest ile aynı)
    if symbol in stop_cooldown:
        last_stop = stop_cooldown[symbol]
        if (datetime.now() - last_stop) < timedelta(hours=4):
            return  # 4 saat dolmadıysa sinyal arama
        else:
            del stop_cooldown[symbol]  # 4 saat dolduysa tekrar sinyal aranabilir
    # Başarılı/başarısız sinyal sonrası 4 saatlik cooldown kontrolü (Backtest ile aynı)
    for sdict in [successful_signals, failed_signals]:
        if symbol in sdict:
            last_time = sdict[symbol].get("completion_time")
            if last_time:
                last_time_dt = datetime.fromisoformat(last_time)
                if (datetime.now() - last_time_dt) < timedelta(hours=4):
                    return  # 4 saat dolmadıysa sinyal arama
                else:
                    del sdict[symbol]  # 4 saat dolduysa tekrar sinyal aranabilir
    # 1d'lik veri kontrolü
    try:
        df_1d = await async_get_historical_data(symbol, timeframes['1d'], 30)
        if len(df_1d) < 30:
            # print(f"UYARI: {symbol} için 1d veri 30'dan az, sinyal aranmıyor.")  # Debug mesajını kaldır
            return
    except Exception as e:
        # print(f"UYARI: {symbol} için 1d veri çekilemedi: {str(e)}")  # Debug mesajını kaldır
        return
    # Mevcut sinyalleri al (Backtest ile aynı mantık - henüz kapanmamış mumlar)
    current_signals = dict()
    for tf_name in tf_names:
        try:
            df = await async_get_historical_data(symbol, timeframes[tf_name], 1000)
            df = calculate_full_pine_signals(df, tf_name)
            
            # Backtest ile aynı: En yakın mumun sinyalini al (henüz kapanmamış olabilir)
            current_time = datetime.now()
            closest_idx = df['timestamp'].searchsorted(current_time)
            if closest_idx < len(df):
                signal = int(df.iloc[closest_idx]['signal'])
                # Sinyal 0 ise MACD ile düzelt
                if signal == 0:
                    if df['macd'].iloc[closest_idx] > df['macd_signal'].iloc[closest_idx]:
                        signal = 1
                    else:
                        signal = -1
                current_signals[tf_name] = signal
            else:
                # Son mumu al
                signal = int(df.iloc[-1]['signal'])
                if signal == 0:
                    if df['macd'].iloc[-1] > df['macd_signal'].iloc[-1]:
                        signal = 1
                    else:
                        signal = -1
                current_signals[tf_name] = signal
            # print(f"📊 {symbol} - {tf_name}: {signal}")  # Debug mesajını kaldır
        except Exception as e:
            # print(f"Hata: {symbol} - {tf_name} - {str(e)}")  # Debug mesajını kaldır
            return  # Hata varsa bu coin için sinyal üretme
    
    # İlk çalıştırmada sadece sinyalleri kaydet
    if not previous_signals.get(symbol):
        previous_signals[symbol] = current_signals.copy()
        # Kaydedilen sinyalleri ekranda göster
        signal_str = ', '.join([f'{tf}: {current_signals[tf]}' for tf in tf_names])
        print(f"📊 {symbol} ({signal_str})")
        return  # İlk çalıştırmada sadece kaydet, sinyal gönderme
    
    # İlk çalıştırma değilse, sinyal kontrolü yap
    prev_signals = previous_signals[symbol]
    
    # 3 zaman diliminde aynı olan coinler için değişiklik kontrolü
    signal_values = [current_signals.get(tf, 0) for tf in tf_names]
    buy_count = sum(1 for s in signal_values if s == 1)
    sell_count = sum(1 for s in signal_values if s == -1)
    
    # İlk çalıştırmada kaydedilen sinyalleri kontrol et
    prev_signal_values = [prev_signals.get(tf, 0) for tf in tf_names]
    prev_buy_count = sum(1 for s in prev_signal_values if s == 1)
    prev_sell_count = sum(1 for s in prev_signal_values if s == -1)
    
    # Eğer ilk çalıştırmada 3'ü de aynıysa, değişiklik bekler
    if (prev_buy_count == 3 or prev_sell_count == 3):
        # Değişiklik var mı kontrol et
        signal_changed = False
        for tf in tf_names:
            if prev_signals[tf] != current_signals[tf]:
                signal_changed = True
                print(f"🔄 {symbol} - {tf} sinyali değişti: {prev_signals[tf]} -> {current_signals[tf]}")
                break
        
        # Değişiklik yoksa sinyal arama
        if not signal_changed:
            previous_signals[symbol] = current_signals.copy()
            return
    
    # Sinyal kontrolü yap (3/3 şartı)
    if buy_count == 3:  # 3/3 ALIŞ
        sinyal_tipi = 'ALIS'
        print(f"🚨 {symbol} ALIŞ sinyali bulundu! (3/3)")
        
        # 8h kontrolü - 4/3 kuralı
        signal_8h = None
        try:
            df_8h = await async_get_historical_data(symbol, '8h', 1000)
            df_8h = calculate_full_pine_signals(df_8h, '8h')
            current_time = datetime.now()
            closest_idx_8h = df_8h['timestamp'].searchsorted(current_time)
            if closest_idx_8h < len(df_8h):
                signal_8h = int(df_8h.iloc[closest_idx_8h]['signal'])
                if signal_8h == 0:
                    if df_8h['macd'].iloc[closest_idx_8h] > df_8h['macd_signal'].iloc[closest_idx_8h]:
                        signal_8h = 1
                    else:
                        signal_8h = -1
        except Exception as e:
            print(f"⚠️ {symbol} 8h kontrolünde hata: {str(e)}")
            
    elif sell_count == 3:  # 3/3 SATIŞ
        sinyal_tipi = 'SATIS'
        print(f"🚨 {symbol} SATIŞ sinyali bulundu! (3/3)")
        
        # 8h kontrolü - 4/3 kuralı
        signal_8h = None
        try:
            df_8h = await async_get_historical_data(symbol, '8h', 1000)
            df_8h = calculate_full_pine_signals(df_8h, '8h')
            current_time = datetime.now()
            closest_idx_8h = df_8h['timestamp'].searchsorted(current_time)
            if closest_idx_8h < len(df_8h):
                signal_8h = int(df_8h.iloc[closest_idx_8h]['signal'])
                if signal_8h == 0:
                    if df_8h['macd'].iloc[closest_idx_8h] > df_8h['macd_signal'].iloc[closest_idx_8h]:
                        signal_8h = 1
                    else:
                        signal_8h = -1
        except Exception as e:
            print(f"⚠️ {symbol} 8h kontrolünde hata: {str(e)}")
    else:
        # print(f"❌ {symbol} 3/3 şartı sağlanmadı, atlanıyor")  # Debug mesajını kaldır
        previous_signals[symbol] = current_signals.copy()
        return
    
    # Sinyal onay kuralları kaldırıldı - direkt devam et
    
    # 4 saatlik cooldown kontrolü (Backtest ile aynı)
    cooldown_key = (symbol, sinyal_tipi)
    if cooldown_key in cooldown_signals:
        last_time = cooldown_signals[cooldown_key]
        if (datetime.now() - last_time) < timedelta(hours=4):
            previous_signals[symbol] = current_signals.copy()
            return
        else:
            del cooldown_signals[cooldown_key]
    # Aynı sinyal daha önce gönderilmiş mi kontrol et
    signal_key = (symbol, sinyal_tipi)
    if sent_signals.get(signal_key) == signal_values:
        previous_signals[symbol] = current_signals.copy()
        return
    
    # Yeni sinyal gönder
    sent_signals[signal_key] = signal_values.copy()
    # Sinyal türünü belirle
    if sinyal_tipi == 'ALIS':
        dominant_signal = "ALIŞ"
    else:
        dominant_signal = "SATIŞ"
    
    # Fiyat ve USD hacmini çek
    try:
        df = await async_get_historical_data(symbol, '1h', 1)
        if df is None or df.empty:
            print(f"❌ {symbol} için fiyat verisi alınamadı")
            previous_signals[symbol] = current_signals.copy()
            return
        
        price = float(df['close'].iloc[-1])
        if price is None or price <= 0:
            print(f"❌ {symbol} için geçersiz fiyat: {price}")
            previous_signals[symbol] = current_signals.copy()
            return
            
        volume_usd = await get_24h_volume_usd(symbol)  # 24 saatlik USD hacmi
        message, _, target_price, stop_loss, stop_loss_str, leverage = create_signal_message(symbol, price, current_signals, volume_usd, profit_percent, stop_percent, signal_8h)
        
        if message is None:
            print(f"❌ {symbol} için mesaj oluşturulamadı")
            previous_signals[symbol] = current_signals.copy()
            return
            
    except Exception as e:
        print(f"❌ {symbol} fiyat/hacim çekme hatası: {str(e)}")
        previous_signals[symbol] = current_signals.copy()
        return
    
    # Pozisyonu kaydet (Backtest ile aynı format)
    positions[symbol] = {
        "type": dominant_signal,
        "target": float(target_price),
        "stop": float(stop_loss),
        "open_price": float(price),
        "stop_str": stop_loss_str,
        "signals": current_signals,
        "leverage": leverage,
        "entry_time": str(datetime.now()),
        "entry_timestamp": datetime.now()  # Backtest ile uyumlu
    }
    
    # Pozisyonu veritabanına kaydet
    save_positions_to_db(positions)

    # Aktif sinyal olarak kaydet (her durumda)
    active_signals[symbol] = {
        "symbol": symbol,
        "type": dominant_signal,
        "entry_price": format_price(price, price),
        "entry_price_float": price,
        "target_price": format_price(target_price, price),
        "stop_loss": format_price(stop_loss, price),
        "signals": current_signals,
        "leverage": leverage,
        "signal_time": str(datetime.now()),
        "current_price": format_price(price, price),
        "current_price_float": price,
        "last_update": str(datetime.now())
    }

    # İstatistikleri güncelle (her durumda)
    stats["total_signals"] += 1
    stats["active_signals_count"] = len(active_signals)

    if message:
        await send_signal_to_all_users(message)
    else:
        print(f"❌ {symbol} için mesaj oluşturulamadı!")

    # Sinyal gönderildikten sonra previous_signals'ı güncelle
    previous_signals[symbol] = current_signals.copy()
    # Veritabanına da kaydet
    update_previous_signal_in_db(symbol, current_signals)

async def signal_processing_loop():
    """Sinyal arama ve işleme döngüsü"""
    # Profit/Stop parametreleri (backtest ile aynı)
    profit_percent = 1.5
    stop_percent = 1.0
    
    sent_signals = dict()  # {(symbol, sinyal_tipi): signal_values}
    positions = dict()  # {symbol: position_info}
    cooldown_signals = dict()  # {(symbol, sinyal_tipi): datetime} (Backtest ile aynı)
    stop_cooldown = dict()  # {symbol: datetime}
    previous_signals = dict()  # {symbol: {tf: signal}} - İlk çalıştığında kaydedilen sinyaller
    # changed_symbols artık kullanılmıyor (sinyal aramaya başlayabilir)
    stopped_coins = dict()  # {symbol: {...}}
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
    
    # Normal sinyal için timeframe'ler
    timeframes = {
        '1h': '1h',
        '2h': '2h',
        '1d': '1d'
    }
    tf_names = ['1h', '2h', '1d']  # 1h+2h+1d kombinasyonu
    
    # Özel sinyal için timeframe'ler (5 zaman dilimi)
    special_timeframes = {
        '1h': '1h',
        '2h': '2h',
        '4h': '4h',
        '8h': '8h',
        '12h': '12h'
    }
    special_tf_names = ['1h', '2h', '4h', '8h', '12h']  # Özel sinyal için 5 zaman dilimi
    
    # Özel sinyal kontrol değişkenleri
    special_signal_sent = False  # 24 saatte sadece 1 kez özel sinyal
    special_signal_time = None   # Son özel sinyal zamanı
    special_signal_active = False  # Özel sinyal aktif mi?
    special_signal_symbol = None   # Aktif özel sinyal sembolü 
    
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
        
        # Aktif sinyalleri pozisyonlardan oluştur
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
                "signal_time": pos.get("entry_time", str(datetime.now())),
                "current_price": format_price(pos["open_price"], pos["open_price"]),
                "current_price_float": pos["open_price"],
                "last_update": str(datetime.now()),
                "is_special": pos.get("is_special", False)
            }
        
        # İstatistikleri güncelle
        stats["active_signals_count"] = len(active_signals)
        stats["total_signals"] = len(active_signals)  # Yeniden başlatmada toplam sinyal sayısı
        
        print(f"📊 {len(positions)} aktif pozisyon ve {len(previous_signals)} önceki sinyal yüklendi")
        print(f"📈 {len(active_signals)} aktif sinyal oluşturuldu")
    
    while True:
        try:
                                    # Aktif pozisyonları kontrol etmeye devam et (Backtest ile aynı mantık)
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
                    
                    # Backtest ile aynı: 1h mumunun high/low değerlerine bak
                    if pos["type"] == "ALIŞ":
                        # Hedef kontrolü: High fiyatı hedefe ulaştı mı? (Backtest ile aynı)
                        if current_high >= pos["target"]:
                            msg = f"🎯 <b>HEDEF BAŞARIYLA GERÇEKLEŞTİ!</b> 🎯\n\n<b>{symbol}</b> işlemi için hedef fiyatına ulaşıldı!\nÇıkış Fiyatı: <b>{format_price(last_price)}</b>\n"
                            await send_signal_to_all_users(msg)
                            cooldown_signals[(symbol, "ALIS")] = datetime.now()
                            
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
                        # Stop kontrolü: Low fiyatı stop'a ulaştı mı? (Backtest ile aynı)
                        elif current_low <= pos["stop"]:
                            # Stop mesajı gönderilmiyor - sadece hedef mesajları gönderiliyor
                            cooldown_signals[(symbol, "ALIS")] = datetime.now()
                            stop_cooldown[symbol] = datetime.now()
                            
                            # Stop olan coini stopped_coins'e ekle (tüm detaylarla)
                            stopped_coins[symbol] = {
                                "symbol": symbol,
                                "type": pos["type"],
                                "entry_price": format_price(pos["open_price"], pos["open_price"]),
                                "stop_time": str(datetime.now()),
                                "target_price": format_price(pos["target"], pos["open_price"]),
                                "stop_loss": format_price(pos["stop"], pos["open_price"]),
                                "signals": pos["signals"],
                                "min_price": format_price(last_price, pos["open_price"]),
                                "max_drawdown_percent": 0.0,
                                "reached_target": False
                            }
                            
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
                            
                            if symbol in active_signals:
                                del active_signals[symbol]
                            
                            # Pozisyonu veritabanından kaldır
                            remove_position_from_db(symbol)
                            del positions[symbol]
                        elif pos["type"] == "SATIŞ":
                            # Hedef kontrolü: Low fiyatı hedefe ulaştı mı? (Backtest ile aynı)
                            if current_low <= pos["target"]:
                                msg = f"🎯 <b>HEDEF BAŞARIYLA GERÇEKLEŞTİ!</b> 🎯\n\n<b>{symbol}</b> işlemi için hedef fiyatına ulaşıldı!\nÇıkış Fiyatı: <b>{format_price(last_price)}</b>\n"
                                await send_signal_to_all_users(msg)
                                cooldown_signals[(symbol, "SATIS")] = datetime.now()
                                
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
                            # Stop kontrolü: High fiyatı stop'a ulaştı mı? (Backtest ile aynı)
                            elif current_high >= pos["stop"]:
                                # Stop mesajı gönderilmiyor - sadece hedef mesajları gönderiliyor
                                cooldown_signals[(symbol, "SATIS")] = datetime.now()
                                stop_cooldown[symbol] = datetime.now()
                                
                                # Stop olan coini stopped_coins'e ekle (tüm detaylarla)
                                stopped_coins[symbol] = {
                                    "symbol": symbol,
                                    "type": pos["type"],
                                    "entry_price": format_price(pos["open_price"], pos["open_price"]),
                                    "stop_time": str(datetime.now()),
                                    "target_price": format_price(pos["target"], pos["open_price"]),
                                    "stop_loss": format_price(pos["stop"], pos["open_price"]),
                                    "signals": pos["signals"],
                                    "max_price": format_price(last_price, pos["open_price"]),
                                    "max_drawup_percent": 0.0,
                                    "reached_target": False
                                }
                                
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
                
            # Aktif pozisyonlar varsa 15 dakika bekle, sonra yeni sinyal aramaya devam et
            if positions:
                print(f"⏰ {len(positions)} aktif pozisyon var, 15 dakika bekleniyor...")
                await asyncio.sleep(900)  # 15 dakika
                
            # Eğer sinyal aramaya izin verilen saatlerdeysek normal işlemlere devam et
            new_symbols = await get_active_high_volume_usdt_pairs(100)  # Sadece ilk 100 sembol
            
            # Aktif pozisyonları ve cooldown'daki coinleri koru
            protected_symbols = set()
            protected_symbols.update(positions.keys())  # Aktif pozisyonlar
            protected_symbols.update(cooldown_signals.keys())  # Cooldown'daki coinler
            
            # Yeni sembollere korunan sembolleri ekle
            symbols = list(new_symbols)
            for protected_symbol in protected_symbols:
                if protected_symbol not in symbols:
                    symbols.append(protected_symbol)
            
            tracked_coins.update(symbols)  # Takip edilen coinleri güncelle

            tasks = [process_symbol(symbol, positions, stop_cooldown, successful_signals, failed_signals, timeframes, tf_names, previous_signals, cooldown_signals, sent_signals, active_signals, stats, profit_percent, stop_percent, None) for symbol in symbols]
            await asyncio.gather(*tasks)
            # print("✅ Sinyal arama tamamlandı")  # Debug mesajını kaldır
            
            # ÖZEL SİNYAL KONTROLÜ
            if not special_signal_active:  # Aktif özel sinyal yoksa kontrol et
                # 24 saat kontrolü
                can_send_special = True
                if special_signal_time:
                    time_diff = (datetime.now() - special_signal_time).total_seconds() / 3600
                    if time_diff < 24:
                        can_send_special = False
                        print(f"⏰ Özel sinyal için 24 saat dolmadı: {24 - time_diff:.1f} saat kaldı")
                
                if can_send_special:
                    print("🔍 Özel sinyal aranıyor (5/5 kuralı)...")
                    for symbol in symbols:
                        # Özel sinyal kontrolü
                        special_result = await check_special_signal(symbol, special_timeframes, special_tf_names)
                        if special_result:
                            sinyal_tipi, special_signals = special_result
                            print(f"⭐️ YILDIZLI ÖZEL SİNYAL BULUNDU! {symbol} - {sinyal_tipi}")
                            
                            # Fiyat ve hacim bilgilerini al
                            try:
                                df = await async_get_historical_data(symbol, '1h', 1)
                                price = float(df['close'].iloc[-1])
                                volume_usd = await get_24h_volume_usd(symbol)
                                
                                # Özel sinyal mesajını oluştur
                                message, signal_type, target_price, stop_loss, stop_str = create_special_signal_message(symbol, price, special_signals, volume_usd, profit_percent, stop_percent)
                                
                                if message:
                                    # Özel sinyal pozisyonunu kaydet (20x kaldıraç)
                                    positions[symbol] = {
                                        "type": signal_type,
                                        "target": float(target_price),
                                        "stop": float(stop_loss),
                                        "open_price": float(price),
                                        "stop_str": stop_str,
                                        "signals": special_signals,
                                        "leverage": 20,  # Özel sinyal için 20x
                                        "entry_time": str(datetime.now()),
                                        "entry_timestamp": datetime.now(),
                                        "is_special": True  # Özel sinyal işareti
                                    }
                                    
                                    # Özel sinyal pozisyonunu veritabanına kaydet
                                    save_positions_to_db(positions)
                                    
                                    # Aktif sinyal olarak kaydet
                                    active_signals[symbol] = {
                                        "symbol": symbol,
                                        "type": signal_type,
                                        "entry_price": format_price(price, price),
                                        "entry_price_float": price,
                                        "target_price": format_price(target_price, price),
                                        "stop_loss": format_price(stop_loss, price),
                                        "signals": special_signals,
                                        "leverage": 20,
                                        "signal_time": str(datetime.now()),
                                        "current_price": format_price(price, price),
                                        "current_price_float": price,
                                        "last_update": str(datetime.now()),
                                        "is_special": True
                                    }
                                    
                                    # İstatistikleri güncelle
                                    stats["total_signals"] += 1
                                    stats["active_signals_count"] = len(active_signals)
                                    
                                    # Özel sinyal mesajını gönder
                                    await send_signal_to_all_users(message)
                                    
                                    # Özel sinyal durumunu güncelle
                                    special_signal_sent = True
                                    special_signal_time = datetime.now()
                                    special_signal_active = True
                                    special_signal_symbol = symbol
                                    
                                    print(f"⭐️ Özel sinyal gönderildi: {symbol}")
                                    break  # Sadece 1 özel sinyal
                                    
                            except Exception as e:
                                print(f"Özel sinyal işleme hatası: {symbol} - {str(e)}")
                                continue
            else:
                # Aktif özel sinyal varsa, kapanıp kapanmadığını kontrol et
                if special_signal_symbol and special_signal_symbol not in positions:
                    print(f"⭐️ Özel sinyal kapandı: {special_signal_symbol}")
                    special_signal_active = False
                    special_signal_symbol = None
        
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
            

            
            # İstatistik özeti yazdır
            print(f"📊 İSTATİSTİK ÖZETİ:")
            print(f"   Toplam Sinyal: {stats['total_signals']}")
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
            # İlk çalıştırmada önceki sinyalleri kaydet
            if is_first and len(previous_signals) > 0:
                save_previous_signals_to_db(previous_signals)
                is_first = False  # Artık ilk çalıştırma değil
            
            # Döngü sonunda bekleme süresi (15 dakika)
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

if __name__ == "__main__":
    asyncio.run(main())