import sys
import asyncio
if sys.platform.startswith("win"):
    asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())
import pandas as pd
import ta
from datetime import datetime, timedelta
import telegram
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

def check_klines_for_trigger(signal, klines):
    """
    Mum verilerini kontrol ederek stop-loss veya take-profit tetiklenmesi olup olmadığını belirler.
    Her mumun high/low değerlerini kontrol ederek daha doğru tetikleme sağlar.
    Dönüş: (tetiklendi mi?, tetiklenme tipi, son fiyat)
    """
    try:
        signal_type = signal.get('type', 'ALIŞ')
        symbol = signal.get('symbol', 'UNKNOWN')
        
        # Hedef ve stop fiyatlarını al
        if 'target_price' in signal and 'stop_loss' in signal:
            target_price = float(str(signal['target_price']).replace('$', '').replace(',', ''))
            stop_loss_price = float(str(signal['stop_loss']).replace('$', '').replace(',', ''))
        else:
            # Pozisyon verilerinden al
            target_price = float(str(signal.get('target', 0)).replace('$', '').replace(',', ''))
            stop_loss_price = float(str(signal.get('stop', 0)).replace('$', '').replace(',', ''))
        
        if target_price <= 0 or stop_loss_price <= 0:
            print(f"⚠️ {symbol} - Geçersiz hedef/stop fiyatları: TP={target_price}, SL={stop_loss_price}")
            return False, None, None
        
        if not klines:
            print(f"⚠️ {symbol} - Mum verisi boş")
            return False, None, None
        
        # Mum verilerini DataFrame'e dönüştür
        if isinstance(klines, list) and len(klines) > 0:
            # Binance API formatından DataFrame oluştur
            if len(klines[0]) >= 6:  # OHLCV formatı
                df = pd.DataFrame(klines, columns=['open_time', 'open', 'high', 'low', 'close', 'volume', 'close_time', 'quote_asset_volume', 'number_of_trades', 'taker_buy_base_asset_volume', 'taker_buy_quote_asset_volume', 'ignore'])
                df = df[['open', 'high', 'low', 'close']].astype(float)
            else:
                print(f"⚠️ {signal.get('symbol', 'UNKNOWN')} - Geçersiz mum veri formatı")
                return False, None, None
        else:
            print(f"⚠️ {signal.get('symbol', 'UNKNOWN')} - Mum verisi bulunamadı")
            return False, None, None
        
        symbol = signal.get('symbol', 'UNKNOWN')
        
        for index, row in df.iterrows():
            high = float(row['high'])
            low = float(row['low'])
            
            # ALIŞ sinyali kontrolü (long pozisyon)
            if signal_type == "ALIŞ":
                # Önce hedef kontrolü (kural olarak kar alma öncelikli)
                if high >= target_price:
                    print(f"✅ {symbol} - TP tetiklendi! Mum: High={high:.6f}, TP={target_price:.6f}")
                    return True, "take_profit", target_price
                # Sonra stop-loss kontrolü - eşit veya geçmişse
                if low <= stop_loss_price:
                    print(f"❌ {symbol} - SL tetiklendi! Mum: Low={low:.6f}, SL={stop_loss_price:.6f}")
                    return True, "stop_loss", stop_loss_price
                    
            # SATIŞ sinyali kontrolü (short pozisyon)
            elif signal_type == "SATIŞ":
                # Önce hedef kontrolü
                if low <= target_price:
                    print(f"✅ {symbol} - TP tetiklendi! Mum: Low={low:.6f}, TP={target_price:.6f}")
                    return True, "take_profit", target_price
                # Sonra stop-loss kontrolü - eşit veya geçmişse
                if high >= stop_loss_price:
                    print(f"❌ {symbol} - SL tetiklendi! Mum: High={high:.6f}, SL={stop_loss_price:.6f}")
                    return True, "stop_loss", stop_loss_price

        # Hiçbir tetikleme yoksa, false döner ve son mumu döndürür
        final_price = float(df['close'].iloc[-1]) if not df.empty else None
        return False, None, final_price
        
    except Exception as e:
        print(f"❌ check_klines_for_trigger hatası ({signal.get('symbol', 'UNKNOWN')}): {e}")
        return False, None, None

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
            # Data alanı varsa onu kullan, yoksa doğrudan dokümanı kullan
            if "data" in doc:
                data = doc["data"]
                if "symbol" not in data:
                    continue
                symbol = data["symbol"]
                result[symbol] = {
                    "symbol": data.get("symbol", symbol),
                    "type": data.get("type", "ALIŞ"),
                    "entry_price": data.get("entry_price", "0"),
                    "entry_price_float": data.get("entry_price_float", 0.0),
                    "target_price": data.get("target_price", "0"),
                    "stop_loss": data.get("stop_loss", "0"),
                    "signals": data.get("signals", {}),
                    "leverage": data.get("leverage", 10),
                    "signal_time": data.get("signal_time", ""),
                    "current_price": data.get("current_price", "0"),
                    "current_price_float": data.get("current_price_float", 0.0),
                    "last_update": data.get("last_update", "")
                }
            elif "symbol" in doc:
                # Doğrudan doküman formatı
                symbol = doc["symbol"]
                result[symbol] = {
                        "symbol": doc.get("symbol", symbol),
                        "type": doc.get("type", "ALIŞ"),
                        "entry_price": doc.get("entry_price", "0"),
                        "entry_price_float": doc.get("entry_price_float", 0.0),
                        "target_price": doc.get("target_price", "0"),
                        "stop_loss": doc.get("stop_loss", "0"),
                        "signals": doc.get("signals", {}),
                        "leverage": doc.get("leverage", 10),
                        "signal_time": doc.get("signal_time", ""),
                        "current_price": doc.get("current_price", "0"),
                        "current_price_float": doc.get("current_price_float", 0.0),
                        "last_update": doc.get("last_update", "")
                    }
            else:
                # Symbol bulunamadı, bu dokümanı atla
                continue
        
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
        mongo_client = MongoClient(MONGODB_URI, 
                                  serverSelectionTimeoutMS=30000,
                                  connectTimeoutMS=30000,
                                  socketTimeoutMS=30000,
                                  maxPoolSize=10)
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

def ensure_mongodb_connection():
    """MongoDB bağlantısının aktif olduğundan emin ol, değilse yeniden bağlan"""
    global mongo_collection
    try:
        if mongo_collection is None:
            return connect_mongodb()
        
        # Bağlantıyı test et
        mongo_client.admin.command('ping')
        return True
    except Exception as e:
        print(f"⚠️ MongoDB bağlantısı koptu, yeniden bağlanılıyor: {e}")
        return connect_mongodb()

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
        
        # Önce tüm pozisyonları sil
        mongo_collection.delete_many({"_id": {"$regex": "^position_"}})
        
        # Yeni pozisyonları ekle
        for symbol, position in positions.items():
            doc_id = f"position_{symbol}"
            mongo_collection.insert_one({
                "_id": doc_id,
                "data": position,
                "timestamp": datetime.now()
            })
        
        print(f"✅ {len(positions)} pozisyon MongoDB'ye kaydedildi")
        return True
    except Exception as e:
        print(f"❌ Pozisyonlar MongoDB'ye kaydedilirken hata: {e}")
        return False

def load_positions_from_db():
    """MongoDB'den pozisyonları yükler"""
    try:
        if mongo_collection is None:
            if not connect_mongodb():
                print("❌ MongoDB bağlantısı kurulamadı, pozisyonlar yüklenemedi")
                return {}
        
        positions = {}
        docs = mongo_collection.find({"_id": {"$regex": "^position_"}})
        
        for doc in docs:
            symbol = doc["_id"].replace("position_", "")
            positions[symbol] = doc["data"]
        
        print(f"📊 MongoDB'den {len(positions)} pozisyon yüklendi")
        return positions
    except Exception as e:
        print(f"❌ MongoDB'den pozisyonlar yüklenirken hata: {e}")
        return {}

def load_stop_cooldown_from_db():
    """MongoDB'den stop cooldown verilerini yükler"""
    try:
        if mongo_collection is None:
            if not connect_mongodb():
                print("❌ MongoDB bağlantısı kurulamadı, stop cooldown yüklenemedi")
                return {}
        
        stop_cooldown = {}
        docs = mongo_collection.find({"_id": {"$regex": "^stop_cooldown_"}})
        
        for doc in docs:
            symbol = doc["_id"].replace("stop_cooldown_", "")
            stop_cooldown[symbol] = doc["data"]
        
        print(f"📊 MongoDB'den {len(stop_cooldown)} stop cooldown yüklendi")
        return stop_cooldown
    except Exception as e:
        print(f"❌ MongoDB'den stop cooldown yüklenirken hata: {e}")
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
global_positions = {}  # Aktif pozisyonlar
global_stop_cooldown = {}  # Stop cooldown listesi
# global_changed_symbols artık kullanılmıyor
global_allowed_users = set()  # İzin verilen kullanıcılar
global_admin_users = set()  # Admin kullanıcılar
# Saatlik yeni sinyal taraması için zaman damgası
global_last_signal_scan_time = None

# 15m mum onayı kaldırıldı - direkt sinyal sistemi

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

async def send_admin_message(message):
    """Bot sahibine özel mesaj gönder (sadece stop durumları için)"""
    # Sadece bot sahibine mesaj gönder
    try:
        await send_telegram_message(message, BOT_OWNER_ID)
        print(f"✅ Bot sahibine stop mesajı gönderildi: {BOT_OWNER_ID}")
    except Exception as e:
        print(f"❌ Bot sahibine stop mesajı gönderilemedi: {e}")

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
            if is_authorized and not isinstance(context.error, telegram.error.TimedOut):
                try:
                    await send_command_response(update, "❌ Bir hata oluştu. Lütfen daha sonra tekrar deneyin.")
                except Exception as e:
                    print(f"❌ Error handler'da mesaj gönderme hatası: {e}")
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
        
        # Debug: ALIŞ sinyali hesaplama
        print(f"🔍 ALIŞ SİNYALİ HESAPLAMA:")
        print(f"   Giriş: ${price:.6f}")
        print(f"   Hedef: ${price:.6f} × (1 + {profit_percent}/100) = ${price:.6f} × 1.02 = ${target_price:.6f}")
        print(f"   Stop: ${price:.6f} × (1 - {stop_percent}/100) = ${price:.6f} × 0.985 = ${stop_loss:.6f}")
    else:
        sinyal_tipi = "SATIŞ SİNYALİ"
        target_price = price * (1 - profit_percent / 100)
        stop_loss = price * (1 + stop_percent / 100)
        dominant_signal = "SATIŞ"
        
        # Debug: SATIŞ sinyali hesaplama
        print(f"🔍 SATIŞ SİNYALİ HESAPLAMA:")
        print(f"   Giriş: ${price:.6f}")
        print(f"   Hedef: ${price:.6f} × (1 - {profit_percent}/100) = ${price:.6f} × 0.98 = ${target_price:.6f}")
        print(f"   Stop: ${price:.6f} × (1 + {stop_percent}/100) = ${price:.6f} × 1.015 = ${stop_loss:.6f}")
    
    # Kaldıraç seviyesini belirle - sabit 10x
    leverage = 10  # Sabit 10x kaldıraç
    
    # Test hesaplama kontrolü
    print(f"🧮 TEST HESAPLAMA KONTROLÜ:")
    print(f"   Giriş: ${price:.6f}")
    print(f"   Hedef: ${price:.6f} + %{profit_percent} = ${target_price:.6f}")
    print(f"   Stop: ${price:.6f} - %{stop_percent} = ${stop_loss:.6f}")
    print(f"   Hedef Fark: ${(target_price - price):.6f} (%{((target_price - price) / price * 100):.2f})")
    print(f"   Stop Fark: ${(price - stop_loss):.6f} (%{((price - stop_loss) / price * 100):.2f})")

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
    # --- Zaman dilimine göre dinamik parametreler (Pine Script'teki gibi) ---
    is_higher_tf = timeframe in ['1d', '4h', '1w']
    is_weekly = timeframe == '1w'
    is_daily = timeframe == '1d' 
    is_4h = timeframe == '4h'
    is_2h = timeframe == '2h'
    is_1h = timeframe == '1h'
    is_30m = timeframe == '30m'
    is_15m = timeframe == '15m'
    
    # Pine Script'teki gibi dinamik parametreler
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
    elif is_2h:
        # 2h için özel parametreler (Pine Script'teki gibi)
        rsi_length = 16
        macd_fast = 10
        macd_slow = 21
        macd_signal = 8
        short_ma_period = 10
        long_ma_period = 55
        mfi_length = 15
        fib_lookback = 60
        atr_period = 8
        volume_multiplier = 0.25
        rsi_overbought = 60
        rsi_oversold = 40
    elif is_1h:
        # 1h için özel parametreler
        rsi_length = 15
        macd_fast = 10
        macd_slow = 20
        macd_signal = 9
        short_ma_period = 9
        long_ma_period = 50
        mfi_length = 14
        fib_lookback = 50
        atr_period = 9
        volume_multiplier = 0.35
        rsi_overbought = 60
        rsi_oversold = 40
    elif is_30m:
        # 30m için özel parametreler
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
    elif is_15m:
        # 15m için özel parametreler
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
    elif timeframe == '8h':
        # 8h için özel parametreler (Pine Script'teki gibi)
        rsi_length = 17
        macd_fast = 10
        macd_slow = 21
        macd_signal = 8
        short_ma_period = 11
        long_ma_period = 65
        mfi_length = 16
        fib_lookback = 80
        atr_period = 8
        volume_multiplier = 0.2
        rsi_overbought = 60
        rsi_oversold = 40
    else:
        # Diğer zaman dilimleri için varsayılan
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
        elif is_2h:
            multiplier = atr_dynamic / 1.4
        elif is_1h:
            multiplier = atr_dynamic / 1.45
        elif timeframe == '8h':
            multiplier = atr_dynamic / 1.35
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
    # Aktif pozisyon kontrolü - eğer zaten aktif pozisyon varsa yeni sinyal arama
    if symbol in positions:
        print(f"⏸️ {symbol} → Zaten aktif pozisyon var, yeni sinyal aranmıyor")
        return None
    
    # Stop cooldown kontrolü (4 saat)
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
        print(f"🔍 {symbol} → Sinyal analizi: ALIŞ={buy_count}, SATIŞ={sell_count}")
        
        if not check_7_7_rule(buy_count, sell_count):
            if buy_count > 0 or sell_count > 0:
                print(f"❌ {symbol} → 7/7 kuralı sağlanmadı: ALIŞ={buy_count}, SATIŞ={sell_count} (7/7 olmalı!)")
                print(f"   Detay: {current_signals}")
            previous_signals[symbol] = current_signals.copy()
            return None
        
        print(f"✅ {symbol} → 7/7 kuralı sağlandı! ALIŞ={buy_count}, SATIŞ={sell_count}")
        print(f"   Detay: {current_signals}")
        
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
    
    # Aktif pozisyon kontrolü - eğer zaten aktif pozisyon varsa yeni sinyal gönderme
    if symbol in positions:
        print(f"⏸️ {symbol} → Zaten aktif pozisyon var, yeni sinyal gönderilmiyor")
        return
    
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
            # Aktif pozisyon kontrolü - eğer zaten aktif pozisyon varsa bekleme listesinden çıkar
            if symbol in positions:
                print(f"⏸️ {symbol} → Zaten aktif pozisyon var, bekleme listesinden çıkarılıyor")
                del global_waiting_signals[symbol]
                if symbol in global_waiting_previous_signals:
                    del global_waiting_previous_signals[symbol]
                continue
            
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

async def check_existing_positions_and_cooldowns(positions, active_signals, stats, stop_cooldown):
    """Bot başlangıcında mevcut pozisyonları ve cooldown'ları kontrol eder"""
    print("🔍 Mevcut pozisyonlar ve cooldown'lar kontrol ediliyor...")
    
    # 1. Aktif pozisyonları kontrol et
    for symbol in list(positions.keys()):
        try:
            print(f"🔍 {symbol} pozisyonu kontrol ediliyor...")
            
            # Güncel fiyat bilgisini al
            df1m = await async_get_historical_data(symbol, '1m', 1)
            if df1m is None or df1m.empty:
                continue
            
            # Güncel fiyat
            close_price = float(df1m['close'].iloc[-1])
            
            position = positions[symbol]
            entry_price = position["open_price"]
            target_price = position["target"]
            stop_loss = position["stop"]
            signal_type = position["type"]
            
            # ALIŞ sinyali için hedef/stop kontrolü
            if signal_type == "ALIŞ":
                # Hedef kontrolü: Güncel fiyat hedefi geçti mi?
                if close_price >= target_price:
                    print(f"🎯 {symbol} HEDEF BAŞARIYLA GERÇEKLEŞTİ! (Bot başlangıcında tespit edildi)")
                    
                    # İstatistikleri güncelle
                    stats["successful_signals"] += 1
                    profit_percentage = ((target_price - entry_price) / entry_price) * 100
                    profit_usd = 100 * profit_percentage / 100
                    stats["total_profit_loss"] += profit_usd
                    
                    # Cooldown'a ekle (4 saat)
                    stop_cooldown[symbol] = datetime.now()
                    save_stop_cooldown_to_db(stop_cooldown)
                    
                    # Pozisyon ve aktif sinyali kaldır
                    del positions[symbol]
                    if symbol in active_signals:
                        del active_signals[symbol]
                    save_positions_to_db(positions)
                    save_active_signals_to_db(active_signals)
                    
                    # Herkese hedef mesajı gönder
                    target_message = f"🎯 HEDEF BAŞARIYLA GERÇEKLEŞTİ!\n\n🔹 Kripto Çifti: {symbol}\n💰 Kar: %{profit_percentage:.2f} (${profit_usd:.2f})\n📈 Giriş: ${entry_price:.4f}\n🎯 Hedef: ${target_price:.4f}\n💵 Çıkış: ${close_price:.4f}"
                    await send_signal_to_all_users(target_message)
                    
                # Stop kontrolü: Güncel fiyat stop'u geçti mi?
                elif close_price <= stop_loss:
                    print(f"🛑 {symbol} STOP BAŞARIYLA GERÇEKLEŞTİ! (Bot başlangıcında tespit edildi)")
                    
                    # İstatistikleri güncelle
                    stats["failed_signals"] += 1
                    loss_percentage = ((entry_price - stop_loss) / entry_price) * 100
                    loss_usd = 100 * loss_percentage / 100
                    stats["total_profit_loss"] -= loss_usd
                    
                    # Cooldown'a ekle (4 saat)
                    stop_cooldown[symbol] = datetime.now()
                    save_stop_cooldown_to_db(stop_cooldown)
                    
                    # Pozisyon ve aktif sinyali kaldır
                    del positions[symbol]
                    if symbol in active_signals:
                        del active_signals[symbol]
                    save_positions_to_db(positions)
                    save_active_signals_to_db(active_signals)
                    
                    # Sadece bot sahibine stop mesajı gönder
                    stop_message = f"🛑 STOP BAŞARIYLA GERÇEKLEŞTİ!\n\n🔹 Kripto Çifti: {symbol}\n💸 Zarar: %{loss_percentage:.2f} (${loss_usd:.2f})\n📈 Giriş: ${entry_price:.4f}\n🛑 Stop: ${stop_loss:.4f}\n💵 Çıkış: ${close_price:.4f}"
                    await send_admin_message(stop_message)
            
            # SATIŞ sinyali için hedef/stop kontrolü
            elif signal_type == "SATIŞ":
                # Hedef kontrolü: Güncel fiyat hedefi geçti mi?
                if close_price <= target_price:
                    print(f"🎯 {symbol} HEDEF BAŞARIYLA GERÇEKLEŞTİ! (Bot başlangıcında tespit edildi)")
                    
                    # İstatistikleri güncelle
                    stats["successful_signals"] += 1
                    profit_percentage = ((entry_price - target_price) / entry_price) * 100
                    profit_usd = 100 * profit_percentage / 100
                    stats["total_profit_loss"] += profit_usd
                    
                    # Cooldown'a ekle (4 saat)
                    stop_cooldown[symbol] = datetime.now()
                    save_stop_cooldown_to_db(stop_cooldown)
                    
                    # Pozisyon ve aktif sinyali kaldır
                    del positions[symbol]
                    if symbol in active_signals:
                        del active_signals[symbol]
                    save_positions_to_db(positions)
                    save_active_signals_to_db(active_signals)
                    
                    # Herkese hedef mesajı gönder
                    target_message = f"🎯 HEDEF BAŞARIYLA GERÇEKLEŞTİ!\n\n🔹 Kripto Çifti: {symbol}\n💰 Kar: %{profit_percentage:.2f} (${profit_usd:.2f})\n📈 Giriş: ${entry_price:.4f}\n🎯 Hedef: ${target_price:.4f}\n💵 Çıkış: ${close_price:.4f}"
                    await send_signal_to_all_users(target_message)
                    
                # Stop kontrolü: Güncel fiyat stop'u geçti mi?
                elif close_price >= stop_loss:
                    print(f"🛑 {symbol} STOP BAŞARIYLA GERÇEKLEŞTİ! (Bot başlangıcında tespit edildi)")
                    
                    # İstatistikleri güncelle
                    stats["failed_signals"] += 1
                    loss_percentage = ((stop_loss - entry_price) / entry_price) * 100
                    loss_usd = 100 * loss_percentage / 100
                    stats["total_profit_loss"] -= loss_usd
                    
                    # Cooldown'a ekle (4 saat)
                    stop_cooldown[symbol] = datetime.now()
                    save_stop_cooldown_to_db(stop_cooldown)
                    
                    # Pozisyon ve aktif sinyali kaldır
                    del positions[symbol]
                    if symbol in active_signals:
                        del active_signals[symbol]
                    save_positions_to_db(positions)
                    save_active_signals_to_db(active_signals)
                    
                    # Sadece bot sahibine stop mesajı gönder
                    stop_message = f"🛑 STOP BAŞARIYLA GERÇEKLEŞTİ!\n\n🔹 Kripto Çifti: {symbol}\n💸 Zarar: %{loss_percentage:.2f} (${loss_usd:.2f})\n📈 Giriş: ${entry_price:.4f}\n🛑 Stop: ${stop_loss:.4f}\n💵 Çıkış: ${close_price:.4f}"
                    await send_admin_message(stop_message)
                    
        except Exception as e:
            print(f"⚠️ {symbol} pozisyon kontrolü sırasında hata: {e}")
            continue
    
    # 2. Cooldown'ları kontrol et (süresi dolmuş mu?)
    expired_cooldowns = []
    for symbol, cooldown_time in list(stop_cooldown.items()):
        if isinstance(cooldown_time, str):
            cooldown_time = datetime.fromisoformat(cooldown_time)
        
        time_diff = (datetime.now() - cooldown_time).total_seconds() / 3600
        if time_diff >= 4:  # 4 saat geçmişse
            expired_cooldowns.append(symbol)
            print(f"✅ {symbol} cooldown süresi doldu, yeni sinyal aranabilir")
    
    # Süresi dolan cooldown'ları kaldır
    for symbol in expired_cooldowns:
        del stop_cooldown[symbol]
    if expired_cooldowns:
        save_stop_cooldown_to_db(stop_cooldown)
        print(f"🧹 {len(expired_cooldowns)} cooldown temizlendi")
    
    # 3. Stats'ı güncelle
    stats["active_signals_count"] = len(active_signals)
    save_stats_to_db(stats)
    
    print(f"✅ Bot başlangıcı kontrolü tamamlandı: {len(positions)} pozisyon, {len(active_signals)} aktif sinyal, {len(stop_cooldown)} cooldown")
    
    print("✅ Bot başlangıcı kontrolü tamamlandı")

async def check_active_signals_quick(active_signals, positions, stats, stop_cooldown, successful_signals, failed_signals):
    """Aktif sinyalleri hızlı kontrol et (hedef/stop için)"""
    for symbol in list(active_signals.keys()):
        if symbol not in positions:  # Pozisyon kapandıysa aktif sinyalden kaldır
            del active_signals[symbol]
            continue
        
        try:
            # Güncel fiyat bilgisini al
            df1m = await async_get_historical_data(symbol, '1m', 1)
            if df1m is None or df1m.empty:
                continue
            
            # Güncel fiyat
            last_price = float(df1m['close'].iloc[-1])
            active_signals[symbol]["current_price"] = format_price(last_price, active_signals[symbol]["entry_price_float"])
            active_signals[symbol]["current_price_float"] = last_price
            active_signals[symbol]["last_update"] = str(datetime.now())
            
            # Hedef ve stop kontrolü
            entry_price = active_signals[symbol]["entry_price_float"]
            target_price = float(active_signals[symbol]["target_price"].replace('$', '').replace(',', ''))
            stop_loss = float(active_signals[symbol]["stop_loss"].replace('$', '').replace(',', ''))
            signal_type = active_signals[symbol]["type"]
            
            # ALIŞ sinyali için hedef/stop kontrolü
            if signal_type == "ALIŞ":
                # Hedef kontrolü: Güncel fiyat hedefi geçti mi?
                if last_price >= target_price:
                    # HEDEF OLDU! 🎯
                    profit_percentage = ((target_price - entry_price) / entry_price) * 100
                    profit_usd = 100 * profit_percentage / 100
                    
                    print(f"🎯 HEDEF OLDU! {symbol} - Giriş: ${entry_price:.4f}, Hedef: ${target_price:.4f}, Çıkış: ${last_price:.4f}")
                    print(f"💰 Kar: %{profit_percentage:.2f} (${profit_usd:.2f})")
                    
                    # Başarılı sinyali kaydet
                    successful_signals[symbol] = {
                        "symbol": symbol,
                        "type": signal_type,
                        "entry_price": entry_price,
                        "target_price": target_price,
                        "exit_price": last_price,
                        "profit_percentage": profit_percentage,
                        "profit_usd": profit_usd,
                        "entry_time": active_signals[symbol]["signal_time"],
                        "exit_time": datetime.now().strftime('%Y-%m-%d %H:%M'),
                        "duration": "Hedef"
                    }
                    
                    # İstatistikleri güncelle
                    stats["successful_signals"] += 1
                    stats["total_profit_loss"] += profit_usd
                    
                    # Stop cooldown'a ekle
                    stop_cooldown[symbol] = datetime.now()
                    
                    # Pozisyonu ve aktif sinyali kaldır
                    if symbol in positions:
                        del positions[symbol]
                    del active_signals[symbol]
                    
                    # Global değişkenleri hemen güncelle (hızlı kontrol için)
                    global global_active_signals, global_positions, global_stats, global_stop_cooldown, global_successful_signals, global_failed_signals
                    global_active_signals = active_signals.copy()
                    global_positions = positions.copy()
                    global_stats = stats.copy()
                    global_stop_cooldown = stop_cooldown.copy()
                    global_successful_signals = successful_signals.copy()
                    global_failed_signals = failed_signals.copy()
                    
                    # Bot sahibine stop mesajı gönder
                    stop_message = f"🛑 STOP OLDU!\n\n🔹 Kripto Çifti: {symbol}\n💸 Zarar: %{loss_percentage:.2f} (${loss_usd:.2f})\n📈 Giriş: ${entry_price:.4f}\n🛑 Stop: ${stop_loss:.4f}\n💵 Çıkış: ${last_price:.4f}"
                    await send_admin_message(stop_message)
                    
                # Stop kontrolü: Güncel fiyat stop'u geçti mi? (GELİŞMİŞ KONTROL)
                elif last_price <= stop_loss:
                    # STOP OLDU! 🛑
                    loss_percentage = ((entry_price - stop_loss) / entry_price) * 100
                    loss_usd = 100 * loss_percentage / 100
                    
                    print(f"🛑 STOP OLDU! {symbol} - Giriş: ${entry_price:.4f}, Stop: ${stop_loss:.4f}, Çıkış: ${last_price:.4f}")
                    print(f"💸 Zarar: %{loss_percentage:.2f} (${loss_usd:.2f})")
                    
                    # Başarısız sinyali kaydet
                    failed_signals[symbol] = {
                        "symbol": symbol,
                        "type": signal_type,
                        "entry_price": entry_price,
                        "stop_loss": stop_loss,
                        "exit_price": last_price,
                        "loss_percentage": loss_percentage,
                        "loss_usd": loss_usd,
                        "entry_time": active_signals[symbol]["signal_time"],
                        "exit_time": datetime.now().strftime('%Y-%m-%d %H:%M'),
                        "duration": "Stop"
                    }
                    
                    # İstatistikleri güncelle
                    stats["failed_signals"] += 1
                    stats["total_profit_loss"] -= loss_usd
                    
                    # Stop cooldown'a ekle
                    stop_cooldown[symbol] = datetime.now()
                    
                    # Pozisyonu ve aktif sinyali kaldır
                    if symbol in positions:
                        del positions[symbol]
                    del active_signals[symbol]
                    
                    # Global değişkenleri hemen güncelle (hızlı kontrol için)
                    global_active_signals = active_signals.copy()
                    global_positions = positions.copy()
                    global_stats = stats.copy()
                    global_stop_cooldown = stop_cooldown.copy()
                    global_successful_signals = successful_signals.copy()
                    global_failed_signals = failed_signals.copy()
                    
                    # Bot sahibine stop mesajı gönder
                    stop_message = f"🛑 STOP OLDU!\n\n🔹 Kripto Çifti: {symbol}\n💸 Zarar: %{loss_percentage:.2f} (${loss_usd:.2f})\n📈 Giriş: ${entry_price:.4f}\n🛑 Stop: ${stop_loss:.4f}\n💵 Çıkış: ${last_price:.4f}"
                    await send_admin_message(stop_message)
                    
                    # Yeni sistem için de bildirim gönder
                    await handle_stop_loss(symbol, active_signals.get(symbol, {}))
            
            # SATIŞ sinyali için hedef/stop kontrolü
            elif signal_type == "SATIŞ":
                # Hedef kontrolü: Güncel fiyat hedefi geçti mi?
                if last_price <= target_price:
                    # HEDEF OLDU! 🎯
                    profit_percentage = ((entry_price - target_price) / entry_price) * 100
                    profit_usd = 100 * profit_percentage / 100
                    
                    print(f"🎯 HEDEF OLDU! {symbol} - Giriş: ${entry_price:.4f}, Hedef: ${target_price:.4f}, Çıkış: ${last_price:.4f}")
                    print(f"💰 Kar: %{profit_percentage:.2f} (${profit_usd:.2f})")
                    
                    # Başarılı sinyali kaydet
                    successful_signals[symbol] = {
                        "symbol": symbol,
                        "type": signal_type,
                        "entry_price": entry_price,
                        "target_price": target_price,
                        "exit_price": last_price,
                        "profit_percentage": profit_percentage,
                        "profit_usd": profit_usd,
                        "entry_time": active_signals[symbol]["signal_time"],
                        "exit_time": datetime.now().strftime('%Y-%m-%d %H:%M'),
                        "duration": "Hedef"
                    }
                    
                    # İstatistikleri güncelle
                    stats["successful_signals"] += 1
                    stats["total_profit_loss"] += profit_usd
                    
                    # Stop cooldown'a ekle
                    stop_cooldown[symbol] = datetime.now()
                    
                    # Pozisyonu ve aktif sinyali kaldır
                    if symbol in positions:
                        del positions[symbol]
                    del active_signals[symbol]
                    
                    # Global değişkenleri hemen güncelle (hızlı kontrol için)
                    global_active_signals = active_signals.copy()
                    global_positions = positions.copy()
                    global_stats = stats.copy()
                    global_stop_cooldown = stop_cooldown.copy()
                    global_successful_signals = successful_signals.copy()
                    global_failed_signals = failed_signals.copy()
                    
                    # Bot sahibine stop mesajı gönder
                    stop_message = f"🛑 STOP OLDU!\n\n🔹 Kripto Çifti: {symbol}\n💸 Zarar: %{loss_percentage:.2f} (${loss_usd:.2f})\n📈 Giriş: ${entry_price:.4f}\n🛑 Stop: ${stop_loss:.4f}\n💵 Çıkış: ${last_price:.4f}"
                    await send_admin_message(stop_message)
                    
                # Stop kontrolü: Güncel fiyat stop'u geçti mi?
                elif last_price >= stop_loss:
                    # STOP OLDU! 🛑
                    loss_percentage = ((stop_loss - entry_price) / entry_price) * 100
                    loss_usd = 100 * loss_percentage / 100
                    
                    print(f"🛑 STOP OLDU! {symbol} - Giriş: ${entry_price:.4f}, Stop: ${stop_loss:.4f}, Çıkış: ${last_price:.4f}")
                    print(f"💸 Zarar: %{loss_percentage:.2f} (${loss_usd:.2f})")
                    
                    # Başarısız sinyali kaydet
                    failed_signals[symbol] = {
                        "symbol": symbol,
                        "type": signal_type,
                        "entry_price": entry_price,
                        "stop_loss": stop_loss,
                        "exit_price": last_price,
                        "loss_percentage": loss_percentage,
                        "loss_usd": loss_usd,
                        "entry_time": active_signals[symbol]["signal_time"],
                        "exit_time": datetime.now().strftime('%Y-%m-%d %H:%M'),
                        "duration": "Stop"
                    }
                    
                    # İstatistikleri güncelle
                    stats["failed_signals"] += 1
                    stats["total_profit_loss"] -= loss_usd
                    
                    # Stop cooldown'a ekle
                    stop_cooldown[symbol] = datetime.now()
                    
                    # Pozisyonu ve aktif sinyali kaldır
                    if symbol in positions:
                        del positions[symbol]
                    del active_signals[symbol]
                    
                    # Global değişkenleri hemen güncelle (hızlı kontrol için)
                    global_active_signals = active_signals.copy()
                    global_positions = positions.copy()
                    global_stats = stats.copy()
                    global_stop_cooldown = stop_cooldown.copy()
                    global_successful_signals = successful_signals.copy()
                    global_failed_signals = failed_signals.copy()
                    
                    # Bot sahibine stop mesajı gönder
                    stop_message = f"🛑 STOP OLDU!\n\n🔹 Kripto Çifti: {symbol}\n💸 Zarar: %{loss_percentage:.2f} (${loss_usd:.2f})\n📈 Giriş: ${entry_price:.4f}\n🛑 Stop: ${stop_loss:.4f}\n💵 Çıkış: ${last_price:.4f}"
                    await send_admin_message(stop_message)
                    
                    # Yeni sistem için de bildirim gönder
                    await handle_stop_loss(symbol, active_signals.get(symbol, {}))
                    
        except Exception as e:
            print(f"Aktif sinyal hızlı kontrol hatası: {symbol} - {str(e)}")
            continue

async def signal_processing_loop():
    """Sinyal arama ve işleme döngüsü"""
    # Global değişkenleri tanımla
    global global_stats, global_active_signals, global_successful_signals, global_failed_signals, global_allowed_users, global_admin_users, global_positions, global_stop_cooldown
    
    # Profit/Stop parametreleri - TAM DEĞERLER
    profit_percent = 2.0  # %2 hedef
    stop_percent = 1.5    # %1.5 stop

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
        
        # Bot başlangıcında mevcut durumları kontrol et
        print("🔄 Bot başlangıcında mevcut durumlar kontrol ediliyor...")
        await check_existing_positions_and_cooldowns(positions, active_signals, stats, stop_cooldown)
    
    while True:
        try:
            # MongoDB bağlantısını kontrol et
            if not ensure_mongodb_connection():
                print("⚠️ MongoDB bağlantısı kurulamadı, 30 saniye bekleniyor...")
                await asyncio.sleep(30)
                continue
            
            # MongoDB'den güncel verileri yükle (her döngüde)
            positions = load_positions_from_db()
            active_signals = load_active_signals_from_db()
            stats = load_stats_from_db()
            stop_cooldown = load_stop_cooldown_from_db()
            
            # Aktif sinyalleri positions ile senkronize et (her döngüde)
            for symbol in list(active_signals.keys()):
                if symbol not in positions:
                    # Sadece ilk kez mesaj yazdır
                    attr_name7 = f'_first_position_missing_{symbol}'
                    if not hasattr(signal_processing_loop, attr_name7):
                        print(f"⚠️ {symbol} → Positions'da yok, aktif sinyallerden kaldırılıyor")
                        setattr(signal_processing_loop, attr_name7, False)
                    del active_signals[symbol]
                    save_active_signals_to_db(active_signals)
                else:
                    # Positions'daki güncel verileri active_signals'a yansıt
                    position = positions[symbol]
                    if "entry_price_float" in active_signals[symbol]:
                        active_signals[symbol].update({
                            "target_price": format_price(position["target"], active_signals[symbol]["entry_price_float"]),
                            "stop_loss": format_price(position["stop"], active_signals[symbol]["entry_price_float"]),
                            "leverage": position.get("leverage", 10)
                        })
            
            # Stats'ı güncelle
            stats["active_signals_count"] = len(active_signals)
            save_stats_to_db(stats)
            
            # Sadece ilk kez mesaj yazdır
            if not hasattr(signal_processing_loop, '_first_status'):
                print(f"📊 Güncel durum: {len(positions)} pozisyon, {len(active_signals)} aktif sinyal")
                signal_processing_loop._first_status = False
            
            # Aktif pozisyonları ve cooldown'daki coinleri korumalı semboller listesine ekle
            protected_symbols = set(positions.keys()) | set(stop_cooldown.keys())
            
            # Sinyal arama için kullanılacak sembolleri filtrele
            new_symbols = await get_active_high_volume_usdt_pairs(100)  # İlk 100 sembol
            symbols = [s for s in new_symbols if s not in protected_symbols]
            
            if not symbols:
                # Sadece ilk kez mesaj yazdır
                if not hasattr(signal_processing_loop, '_first_all_protected'):
                    print("⚠️ Tüm coinler korumalı (aktif pozisyon veya cooldown)")
                    signal_processing_loop._first_all_protected = False
                await asyncio.sleep(60)
                continue
            
            # Sadece ilk kez mesaj yazdır
            if not hasattr(signal_processing_loop, '_first_coin_search'):
                print(f"🔍 {len(symbols)} coin'de sinyal aranacak (aktif pozisyon: {len(positions)}, cooldown: {len(stop_cooldown)})")
                signal_processing_loop._first_coin_search = False
            
            # Aktif pozisyonları kontrol etmeye devam et
            for symbol, pos in list(positions.items()):
                try:
                    # Güncel fiyat bilgisini al
                    df1m = await async_get_historical_data(symbol, '1m', 1)
                    if df1m is None or df1m.empty:
                        continue
                    
                    # Güncel fiyat
                    last_price = float(df1m['close'].iloc[-1])
                    
                    if last_price is None or last_price <= 0:
                        print(f"❌ {symbol} pozisyon kontrolü için geçersiz fiyat: {last_price}")
                        continue
                    
                    # Aktif sinyal bilgilerini güncelle
                    if symbol in active_signals:
                        active_signals[symbol]["current_price"] = format_price(last_price, pos["open_price"])
                        active_signals[symbol]["current_price_float"] = last_price
                        active_signals[symbol]["last_update"] = str(datetime.now())
                    
                    # Sadece ilk kez mesaj yazdır
                    attr_name5 = f'_first_price_check_{symbol}'
                    if not hasattr(signal_processing_loop, attr_name5):
                        print(f"🔍 {symbol} fiyat kontrolü: Güncel: {last_price:.6f}, Stop: {pos['stop']:.6f}, Hedef: {pos['target']:.6f}")
                        setattr(signal_processing_loop, attr_name5, False)
                    
                    # Hedef/stop kontrollerine bak
                    if pos["type"] == "ALIŞ":
                        # Hedef kontrolü: Güncel fiyat hedefi geçti mi?
                        if last_price >= pos["target"]:
                            print(f"🎯 {symbol} HEDEF BAŞARIYLA GERÇEKLEŞTİ! Çıkış: {format_price(last_price)}")
                            msg = f"🎯 <b>HEDEF BAŞARIYLA GERÇEKLEŞTİ!</b> 🎯\n\n<b>{symbol}</b> işlemi için hedef fiyatına ulaşıldı!\nÇıkış Fiyatı: <b>{format_price(last_price)}</b>\n"
                            await send_signal_to_all_users(msg)
                            # 4 saat cooldown başlat (hedef sonrası)
                            stop_cooldown[symbol] = datetime.now()
                            save_stop_cooldown_to_db(stop_cooldown)
                            
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
                            
                            # Pozisyonu veritabanından kaldır
                            remove_position_from_db(symbol)
                            del positions[symbol]
                            
                            # Global değişkenleri hemen güncelle (hızlı kontrol için)
                            global global_active_signals, global_positions, global_stats, global_stop_cooldown, global_successful_signals, global_failed_signals
                            global_active_signals = active_signals.copy()
                            global_positions = positions.copy()
                            global_stats = stats.copy()
                            global_stop_cooldown = stop_cooldown.copy()
                            global_successful_signals = successful_signals.copy()
                            global_failed_signals = failed_signals.copy()
                            
                            # Yeni sistem için de bildirim gönder
                            await handle_take_profit(symbol, {"type": pos["type"], "entry_price": pos["open_price"], "current_price_float": last_price})
                        # Stop kontrolü: Güncel fiyat stop'u geçti mi?
                        elif last_price <= pos["stop"]:
                            print(f"🛑 {symbol} STOP HİT! Çıkış: {format_price(last_price)}")
                            print(f"   📊 Stop Detayı: Giriş: ${pos['open_price']:.6f}, Stop: ${pos['stop']:.6f}, Güncel: ${last_price:.6f}")
                            
                            # Bot sahibine stop mesajı gönder
                            stop_message = f"🛑 STOP OLDU!\n\n🔹 Kripto Çifti: {symbol}\n💸 Zarar: %{((pos['open_price'] - pos['stop']) / pos['open_price'] * 100):.2f}\n📈 Giriş: ${pos['open_price']:.6f}\n🛑 Stop: ${pos['stop']:.6f}\n💵 Çıkış: ${last_price:.6f}"
                            await send_admin_message(stop_message)
                             
                            # 4 saat cooldown başlat (stop sonrası)
                            stop_cooldown[symbol] = datetime.now()
                            save_stop_cooldown_to_db(stop_cooldown)

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
                            
                            # Pozisyonu veritabanından kaldır
                            remove_position_from_db(symbol)
                            del positions[symbol]
                            
                            # Global değişkenleri hemen güncelle (hızlı kontrol için)
                            global_active_signals.clear()
                            global_active_signals.update(active_signals)
                            global_positions.clear()
                            global_positions.update(positions)
                            global_stats.clear()
                            global_stats.update(stats)
                            global_stop_cooldown.clear()
                            global_stop_cooldown.update(stop_cooldown)
                            global_successful_signals.clear()
                            global_successful_signals.update(successful_signals)
                            global_failed_signals.clear()
                            global_failed_signals.update(failed_signals)
                            
                            # Yeni sistem için de bildirim gönder
                            await handle_stop_loss(symbol, {"type": pos["type"], "entry_price": pos["open_price"], "current_price_float": last_price})
                        elif pos["type"] == "SATIŞ":
                            # Hedef kontrolü: Güncel fiyat hedefi geçti mi?
                            if last_price <= pos["target"]:
                                print(f"🎯 {symbol} HEDEF BAŞARIYLA GERÇEKLEŞTİ! Çıkış: {format_price(last_price)}")
                                msg = f"🎯 <b>HEDEF BAŞARIYLA GERÇEKLEŞTİ!</b> 🎯\n\n<b>{symbol}</b> işlemi için hedef fiyatına ulaşıldı!\nÇıkış Fiyatı: <b>{format_price(last_price)}</b>\n"
                                await send_signal_to_all_users(msg)
                                # 4 saat cooldown başlat (hedef sonrası)
                                stop_cooldown[symbol] = datetime.now()
                                save_stop_cooldown_to_db(stop_cooldown)
                                
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
                                
                                # Global değişkenleri hemen güncelle (hızlı kontrol için)
                                global_active_signals.clear()
                                global_active_signals.update(active_signals)
                                global_positions.clear()
                                global_positions.update(positions)
                                global_stats.clear()
                                global_stats.update(stats)
                                global_stop_cooldown.clear()
                                global_stop_cooldown.update(stop_cooldown)
                                global_successful_signals.clear()
                                global_successful_signals.update(successful_signals)
                                global_failed_signals.clear()
                                global_failed_signals.update(failed_signals)
                                
                                # Yeni sistem için de bildirim gönder
                                await handle_take_profit(symbol, {"type": pos["type"], "entry_price": pos["open_price"], "current_price_float": last_price})
                            # Stop kontrolü: Güncel fiyat stop'u geçti mi?
                            elif last_price >= pos["stop"]:
                                print(f"🛑 {symbol} STOP HİT! Çıkış: {format_price(last_price)}")
                                print(f"   📊 Stop Detayı: Giriş: ${pos['open_price']:.6f}, Stop: ${pos['stop']:.6f}, Güncel: ${last_price:.6f}")
                                
                                # Bot sahibine stop mesajı gönder
                                stop_message = f"🛑 STOP OLDU!\n\n🔹 Kripto Çifti: {symbol}\n💸 Zarar: %{((pos['stop'] - pos['open_price']) / pos['open_price'] * 100):.2f}\n📈 Giriş: ${pos['open_price']:.6f}\n🛑 Stop: ${pos['stop']:.6f}\n💵 Çıkış: ${last_price:.6f}"
                                await send_admin_message(stop_message)
                                 
                                # 4 saat cooldown başlat (stop sonrası)
                                stop_cooldown[symbol] = datetime.now()
                                save_stop_cooldown_to_db(stop_cooldown)

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
                                
                                # Global değişkenleri hemen güncelle (hızlı kontrol için)
                                global_active_signals.clear()
                                global_active_signals.update(active_signals)
                                global_positions.clear()
                                global_positions.update(positions)
                                global_stats.clear()
                                global_stats.update(stats)
                                global_stop_cooldown.clear()
                                global_stop_cooldown.update(stop_cooldown)
                                global_successful_signals.clear()
                                global_successful_signals.update(successful_signals)
                                global_failed_signals.clear()
                                global_failed_signals.update(failed_signals)
                                
                                # Yeni sistem için de bildirim gönder
                                await handle_stop_loss(symbol, {"type": pos["type"], "entry_price": pos["open_price"], "current_price_float": last_price})
                except Exception as e:
                    print(f"Pozisyon kontrol hatası: {symbol} - {str(e)}")
                    continue
            
            # Her zaman yeni sinyal ara (aktif pozisyon varken de) - ZORUNLU!
            # Sadece ilk kez mesaj yazdır
            if not hasattr(signal_processing_loop, '_first_signal_search'):
                print("🚀 YENİ SİNYAL ARAMA BAŞLATILIYOR (aktif sinyal varken de devam eder)")
                signal_processing_loop._first_signal_search = False
            new_symbols = await get_active_high_volume_usdt_pairs(100)  # İlk 100 sembol
            # Sadece ilk kez mesaj yazdır
            if not hasattr(signal_processing_loop, '_first_crypto_count'):
                print(f"🔍 {len(new_symbols)} kripto taranacak")
                signal_processing_loop._first_crypto_count = False
            
            # Aktif pozisyonları ve cooldown'daki coinleri koru
            protected_symbols = set()
            protected_symbols.update(positions.keys())  # Aktif pozisyonlar
            protected_symbols.update(stop_cooldown.keys())  # Cooldown'daki coinler
            
            # Yeni sembollere korunan sembolleri ekle
            symbols = list(new_symbols)
            for protected_symbol in protected_symbols:
                if protected_symbol not in symbols:
                    symbols.append(protected_symbol)
            
            # Aktif pozisyonları ve cooldown'daki coinleri yeni sembol listesinden çıkar
            symbols = [s for s in symbols if s not in protected_symbols]
            
            # Sadece ilk kez mesaj yazdır
            if not hasattr(signal_processing_loop, '_first_symbol_count'):
                print(f"📊 Toplam {len(symbols)} sembol kontrol edilecek (aktif pozisyonlar ve cooldown'daki coinler hariç)")
                signal_processing_loop._first_symbol_count = False
            potential_signals = []
            
            # Tüm semboller için sinyal potansiyelini kontrol et
            for i, symbol in enumerate(symbols):
                if i % 20 == 0:  # Her 20 sembolde bir ilerleme göster
                    print(f"⏳ {i+1}/{len(symbols)} sembol kontrol edildi...")
                
                signal_result = await check_signal_potential(symbol, positions, stop_cooldown, successful_signals, failed_signals, timeframes, tf_names, previous_signals)
                if signal_result:
                    potential_signals.append(signal_result)
                    # Sadece ilk kez mesaj yazdır
                    attr_name6 = f'_first_signal_found_{symbol}'
                    if not hasattr(signal_processing_loop, attr_name6):
                        print(f"🎯 {symbol} için 7/7 sinyal bulundu!")
                        setattr(signal_processing_loop, attr_name6, False)
            
            # Sadece ilk kez mesaj yazdır
            if not hasattr(signal_processing_loop, '_first_potential_count'):
                print(f"📈 Toplam {len(potential_signals)} potansiyel sinyal bulundu")
                signal_processing_loop._first_potential_count = False
            
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
                

                
                # Sadece ilk kez mesaj yazdır
                if not hasattr(signal_processing_loop, '_first_signal_processing'):
                    print(f"🚀 Hemen işlenecek: {len(immediate_signals)} sinyal (direkt sinyal)")
                    print(f"⏳ Beklemeye alınacak: {len(waiting_signals)} sinyal (30dk sonra)")
                    print("💡 Yeni sinyaller direkt işleniyor veya 30dk beklemeye alınıyor...")
                    signal_processing_loop._first_signal_processing = False
                
                # İlk 5 sinyal direkt sinyal veriyor
                for signal_data in immediate_signals:
                    symbol = signal_data['symbol']
                    signal_type = signal_data['signal_type']
                    price = signal_data['price']
                    volume_usd = signal_data['volume_usd']
                    signals = signal_data['signals']

                    # Direkt sinyal ver
                    await process_selected_signal(signal_data, positions, active_signals, stats)
                    print(f"🚀 {symbol} {signal_type} 7/7 sinyali direkt gönderildi (hacim: {volume_usd:,.0f})")
                
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
                    # Sadece ilk kez mesaj yazdır
                    if not hasattr(signal_processing_loop, '_first_changed_signals'):
                        print(f"🔄 {len(changed_waiting_signals)} sinyal bekleme listesinden değişiklik ile geldi")
                        signal_processing_loop._first_changed_signals = False
                    # Tüm değişen sinyalleri hacme göre sırala
                    sorted_changed = sorted(changed_waiting_signals, key=lambda x: -x['volume_usd'])
                    
                    # Tüm değişen sinyalleri gönder
                    for signal_data in sorted_changed:
                        await process_selected_signal(signal_data, positions, active_signals, stats)
                    
                    # Sadece ilk kez mesaj yazdır
                    if not hasattr(signal_processing_loop, '_first_changed_processed'):
                        print(f"✅ {len(sorted_changed)} değişen sinyal işlendi")
                        signal_processing_loop._first_changed_processed = False
                
                # Toplam işlenen sinyal sayısını göster (sadece ilk kez)
                total_processed = len(immediate_signals) + len(changed_waiting_signals)
                if not hasattr(signal_processing_loop, '_first_total_processed'):
                    print(f"📊 Toplam işlenen sinyal: {total_processed} (Hemen: {len(immediate_signals)}, Değişen: {len(changed_waiting_signals)})")
                    signal_processing_loop._first_total_processed = False
            
            # Yeni sinyal aramaya devam et
            
            # Sadece ilk kez mesaj yazdır
            if not hasattr(signal_processing_loop, '_first_loop'):
                print("🚀 Yeni sinyal aramaya devam ediliyor...")
                signal_processing_loop._first_loop = False
        
            # Aktif sinyallerin fiyatlarını güncelle ve hedef/stop kontrolü yap
            if active_signals:
                # Sadece ilk kez mesaj yazdır
                if not hasattr(signal_processing_loop, '_first_active_check'):
                    print(f"🔍 AKTİF SİNYAL KONTROLÜ BAŞLATILIYOR... ({len(active_signals)} aktif sinyal)")
                    for symbol in list(active_signals.keys()):
                        print(f"   📊 {symbol}: {active_signals[symbol].get('type', 'N/A')} - Giriş: ${active_signals[symbol].get('entry_price_float', 0):.6f}")
                    signal_processing_loop._first_active_check = False
            else:
                # Sadece ilk kez mesaj yazdır
                if not hasattr(signal_processing_loop, '_first_no_active'):
                    print("ℹ️ Aktif sinyal yok, kontrol atlanıyor")
                    signal_processing_loop._first_no_active = False
                continue
            
            for symbol in list(active_signals.keys()):
                if symbol not in positions:  # Pozisyon kapandıysa aktif sinyalden kaldır
                    del active_signals[symbol]
                    continue
                
                try:
                    # Sadece ilk kez mesaj yazdır
                    attr_name = f'_first_active_check_{symbol}'
                    if not hasattr(signal_processing_loop, attr_name):
                        print(f"🔍 {symbol} aktif sinyal kontrolü başlatılıyor...")
                        setattr(signal_processing_loop, attr_name, False)
                    
                    # Güncel fiyat bilgisini al
                    df1m = await async_get_historical_data(symbol, '1m', 1)
                    if df1m is None or df1m.empty:
                        continue
                    
                    # Güncel fiyat
                    last_price = float(df1m['close'].iloc[-1])
                    active_signals[symbol]["current_price"] = format_price(last_price, active_signals[symbol]["entry_price_float"])
                    active_signals[symbol]["current_price_float"] = last_price
                    active_signals[symbol]["last_update"] = str(datetime.now())
                    
                    # Hedef ve stop kontrolü
                    entry_price = active_signals[symbol]["entry_price_float"]
                    target_price = float(active_signals[symbol]["target_price"].replace('$', '').replace(',', ''))
                    stop_loss = float(active_signals[symbol]["stop_loss"].replace('$', '').replace(',', ''))
                    signal_type = active_signals[symbol]["type"]
                    
                    # Sadece ilk kez mesaj yazdır
                    attr_name2 = f'_first_control_values_{symbol}'
                    if not hasattr(signal_processing_loop, attr_name2):
                        print(f"   📊 {symbol} kontrol değerleri:")
                        print(f"      Giriş: ${entry_price:.6f}")
                        print(f"      Hedef: ${target_price:.6f}")
                        print(f"      Stop: ${stop_loss:.6f}")
                        print(f"      Güncel: ${last_price:.6f}")
                        print(f"      Güncel: ${last_price:.6f}")
                        print(f"      Sinyal: {signal_type}")
                        setattr(signal_processing_loop, attr_name2, False)
                    
                    # ALIŞ sinyali için hedef/stop kontrolü
                    if signal_type == "ALIŞ":
                        # Sadece ilk kez mesaj yazdır
                        attr_name3 = f'_first_alish_check_{symbol}'
                        if not hasattr(signal_processing_loop, attr_name3):
                            print(f"   🔍 {symbol} ALIŞ sinyali kontrol ediliyor...")
                            setattr(signal_processing_loop, attr_name3, False)
                        
                        # Hedef kontrolü: Güncel fiyat hedefi geçti mi?
                        if last_price >= target_price:
                            # HEDEF OLDU! 🎯
                            profit_percentage = ((target_price - entry_price) / entry_price) * 100
                            profit_usd = 100 * profit_percentage / 100  # 100$ yatırım için
                            
                            print(f"🎯 HEDEF OLDU! {symbol} - Giriş: ${entry_price:.4f}, Hedef: ${target_price:.4f}, Çıkış: ${last_price:.4f}")
                            print(f"💰 Kar: %{profit_percentage:.2f} (${profit_usd:.2f})")
                            
                            # Başarılı sinyali kaydet
                            successful_signals[symbol] = {
                                "symbol": symbol,
                                "type": signal_type,
                                "entry_price": entry_price,
                                "target_price": target_price,
                                "exit_price": last_price,
                                "profit_percentage": profit_percentage,
                                "profit_usd": profit_usd,
                                "entry_time": active_signals[symbol]["signal_time"],
                                "exit_time": datetime.now().strftime('%Y-%m-%d %H:%M'),
                                "duration": "Hedef"
                            }
                            
                            # İstatistikleri güncelle
                            stats["successful_signals"] += 1
                            stats["total_profit_loss"] += profit_usd
                            
                            # Stop cooldown'a ekle
                            stop_cooldown[symbol] = datetime.now()
                            
                            # Pozisyonu ve aktif sinyali kaldır
                            if symbol in positions:
                                del positions[symbol]
                            del active_signals[symbol]
                            
                            # Global değişkenleri hemen güncelle (hızlı kontrol için)
                            global_active_signals = active_signals.copy()
                            global_positions = positions.copy()
                            global_stats = stats.copy()
                            global_stop_cooldown = stop_cooldown.copy()
                            global_successful_signals = successful_signals.copy()
                            global_failed_signals = failed_signals.copy()
                            
                            # Herkese hedef mesajı gönder
                            target_message = f"🎯 HEDEF OLDU!\n\n🔹 Kripto Çifti: {symbol}\n💰 Kar: %{profit_percentage:.2f} (${profit_usd:.2f})\n📈 Giriş: ${entry_price:.4f}\n🎯 Hedef: ${target_price:.4f}\n💵 Çıkış: ${last_price:.4f}"
                            await send_signal_to_all_users(target_message)
                            
                        # Stop kontrolü: Güncel fiyat stop'u geçti mi?
                        elif last_price <= stop_loss:
                            # STOP OLDU! 🛑
                            loss_percentage = ((entry_price - stop_loss) / entry_price) * 100
                            loss_usd = 100 * loss_percentage / 100  # 100$ yatırım için
                            
                            print(f"🛑 STOP OLDU! {symbol} - Giriş: ${entry_price:.4f}, Stop: ${stop_loss:.4f}, Çıkış: ${last_price:.4f}")
                            print(f"💸 Zarar: %{loss_percentage:.2f} (${loss_usd:.2f})")
                            
                            # Başarısız sinyali kaydet
                            failed_signals[symbol] = {
                                "symbol": symbol,
                                "type": signal_type,
                                "entry_price": entry_price,
                                "stop_loss": stop_loss,
                                "exit_price": last_price,
                                "loss_percentage": loss_percentage,
                                "loss_usd": loss_usd,
                                "entry_time": active_signals[symbol]["signal_time"],
                                "exit_time": datetime.now().strftime('%Y-%m-%d %H:%M'),
                                "duration": "Stop"
                            }
                            
                            # İstatistikleri güncelle
                            stats["failed_signals"] += 1
                            stats["total_profit_loss"] -= loss_usd
                            
                            # Stop cooldown'a ekle
                            stop_cooldown[symbol] = datetime.now()
                            
                            # Pozisyonu ve aktif sinyali kaldır
                            if symbol in positions:
                                del positions[symbol]
                            del active_signals[symbol]
                            
                            # Sadece bot sahibine stop mesajı gönder
                            stop_message = f"🛑 STOP OLDU!\n\n🔹 Kripto Çifti: {symbol}\n💸 Zarar: %{loss_percentage:.2f} (${loss_usd:.2f})\n📈 Giriş: ${entry_price:.4f}\n🛑 Stop: ${stop_loss:.4f}\n💵 Çıkış: ${last_price:.4f}"
                            await send_admin_message(stop_message)
                    
                    # SATIŞ sinyali için hedef/stop kontrolü
                    elif signal_type == "SATIŞ":
                        # Sadece ilk kez mesaj yazdır
                        attr_name4 = f'_first_satish_check_{symbol}'
                        if not hasattr(signal_processing_loop, attr_name4):
                            print(f"   🔍 {symbol} SATIŞ sinyali kontrol ediliyor...")
                            setattr(signal_processing_loop, attr_name4, False)
                        # Hedef kontrolü: Güncel fiyat hedefi geçti mi?
                        if last_price <= target_price:
                            # HEDEF OLDU! 🎯
                            profit_percentage = ((entry_price - target_price) / entry_price) * 100
                            profit_usd = 100 * profit_percentage / 100  # 100$ yatırım için
                            
                            print(f"🎯 HEDEF OLDU! {symbol} - Giriş: ${entry_price:.4f}, Hedef: ${target_price:.4f}, Çıkış: ${last_price:.4f}")
                            print(f"💰 Kar: %{profit_percentage:.2f} (${profit_usd:.2f})")
                            
                            # Başarılı sinyali kaydet
                            successful_signals[symbol] = {
                                "symbol": symbol,
                                "type": signal_type,
                                "entry_price": entry_price,
                                "target_price": target_price,
                                "exit_price": last_price,
                                "profit_percentage": profit_percentage,
                                "profit_usd": profit_usd,
                                "entry_time": active_signals[symbol]["signal_time"],
                                "exit_time": datetime.now().strftime('%Y-%m-%d %H:%M'),
                                "duration": "Hedef"
                            }
                            
                            # İstatistikleri güncelle
                            stats["successful_signals"] += 1
                            stats["total_profit_loss"] += profit_usd
                            
                            # Stop cooldown'a ekle
                            stop_cooldown[symbol] = datetime.now()
                            
                            # Pozisyonu ve aktif sinyali kaldır
                            if symbol in positions:
                                del positions[symbol]
                            del active_signals[symbol]
                            
                            # Global değişkenleri hemen güncelle (hızlı kontrol için)
                            global_active_signals = active_signals.copy()
                            global_positions = positions.copy()
                            global_stats = stats.copy()
                            global_stop_cooldown = stop_cooldown.copy()
                            global_successful_signals = successful_signals.copy()
                            global_failed_signals = failed_signals.copy()
                            
                            # Herkese hedef mesajı gönder
                            target_message = f"🎯 HEDEF OLDU!\n\n🔹 Kripto Çifti: {symbol}\n💰 Kar: %{profit_percentage:.2f} (${profit_usd:.2f})\n📈 Giriş: ${entry_price:.4f}\n🎯 Hedef: ${target_price:.4f}\n💵 Çıkış: ${last_price:.4f}"
                            await send_signal_to_all_users(target_message)
                            
                        # Stop kontrolü: Güncel fiyat stop'u geçti mi?
                        elif last_price >= stop_loss:
                            # STOP OLDU! 🛑
                            loss_percentage = ((stop_loss - entry_price) / entry_price) * 100
                            loss_usd = 100 * loss_percentage / 100  # 100$ yatırım için
                            
                            print(f"🛑 STOP OLDU! {symbol} - Giriş: ${entry_price:.4f}, Stop: ${stop_loss:.4f}, Çıkış: ${last_price:.4f}")
                            print(f"💸 Zarar: %{loss_percentage:.2f} (${loss_usd:.2f})")
                            
                            # Başarısız sinyali kaydet
                            failed_signals[symbol] = {
                                "symbol": symbol,
                                "type": signal_type,
                                "entry_price": entry_price,
                                "stop_loss": stop_loss,
                                "exit_price": last_price,
                                "loss_percentage": loss_percentage,
                                "loss_usd": loss_usd,
                                "entry_time": active_signals[symbol]["signal_time"],
                                "exit_time": datetime.now().strftime('%Y-%m-%d %H:%M'),
                                "duration": "Stop"
                            }
                            
                            # İstatistikleri güncelle
                            stats["failed_signals"] += 1
                            stats["total_profit_loss"] -= loss_usd
                            
                            # Stop cooldown'a ekle
                            stop_cooldown[symbol] = datetime.now()
                            
                            # Pozisyonu ve aktif sinyali kaldır
                            if symbol in positions:
                                del positions[symbol]
                            del active_signals[symbol]
                            
                            # Sadece bot sahibine stop mesajı gönder
                            stop_message = f"🛑 STOP OLDU!\n\n🔹 Kripto Çifti: {symbol}\n💸 Zarar: %{loss_percentage:.2f} (${loss_usd:.2f})\n📈 Giriş: ${entry_price:.4f}\n🛑 Stop: ${stop_loss:.4f}\n💵 Çıkış: ${last_price:.4f}"
                            await send_admin_message(stop_message)
                    
                except Exception as e:
                    print(f"Aktif sinyal güncelleme hatası: {symbol} - {str(e)}")
                    continue
            
            # Aktif sinyal kontrolü özeti
            if active_signals:
                print(f"✅ AKTİF SİNYAL KONTROLÜ TAMAMLANDI ({len(active_signals)} sinyal)")
                for symbol in list(active_signals.keys()):
                    if symbol in active_signals:
                        current_price = active_signals[symbol].get("current_price_float", 0)
                        entry_price = active_signals[symbol].get("entry_price_float", 0)
                        if current_price > 0 and entry_price > 0:
                            change_percent = ((current_price - entry_price) / entry_price) * 100
                            print(f"   📊 {symbol}: Giriş: ${entry_price:.6f} → Güncel: ${current_price:.6f} (%{change_percent:+.2f})")
            else:
                print("ℹ️ Aktif sinyal kalmadı")
            
            # Aktif sinyalleri dosyaya kaydet
            with open('active_signals.json', 'w', encoding='utf-8') as f:
                json.dump({
                    "active_signals": active_signals,
                    "count": len(active_signals),
                    "last_update": str(datetime.now())
                }, f, ensure_ascii=False, indent=2)
            
            # İstatistikleri güncelle
            stats["active_signals_count"] = len(active_signals)
            stats["tracked_coins_count"] = len(tracked_coins)
            
            # Global değişkenleri güncelle (bot komutları için)
            global_stats = stats.copy()
            global_active_signals = active_signals.copy()
            global_successful_signals = successful_signals.copy()
            global_failed_signals = failed_signals.copy()
            global_positions = positions.copy()
            global_stop_cooldown = stop_cooldown.copy()
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
            
            # Yeni sinyal aramaya devam et
            print("🚀 Yeni sinyal aramaya devam ediliyor...")
            
            # Ana döngü tamamlandı
            print("Tüm coinler kontrol edildi. 5 dakika bekleniyor...")
            await asyncio.sleep(300)  # 5 dakika
            
        except Exception as e:
            print(f"Genel hata: {e}")
            await asyncio.sleep(30)  # 30 saniye (çok daha hızlı)

async def monitor_signals():
    """
    Aktif sinyalleri sürekli olarak izler, sinyal verildiğinden beri oluşan mum verilerini kontrol eder
    ve hedef/stop loss tetiklendiğinde işlem yapar.
    """
    print("🚀 Yeni sinyal izleme sistemi başlatıldı!")
    
    while True:
        try:
            # MongoDB'den aktif sinyalleri yükle
            active_signals = load_active_signals_from_db()

            if not active_signals:
                print("🔍 Aktif sinyal bulunamadı, bekleniyor...")
                await asyncio.sleep(5)
                continue

            print(f"🔍 {len(active_signals)} aktif sinyal izleniyor...")
            
            # Aktif sinyallerin durumlarını göster
            for symbol, signal in active_signals.items():
                try:
                    entry_price = float(str(signal.get('entry_price_float', signal.get('entry_price', 0))).replace('$', '').replace(',', ''))
                    current_price = float(str(signal.get('current_price_float', entry_price)).replace('$', '').replace(',', ''))
                    target_price = float(str(signal.get('target_price', 0)).replace('$', '').replace(',', ''))
                    stop_price = float(str(signal.get('stop_loss', 0)).replace('$', '').replace(',', ''))
                    signal_type = signal.get('type', 'ALIŞ')
                    
                    if entry_price > 0:
                        # Kar/Zarar yüzdesini hesapla - SATIŞ sinyallerinde mantık tersine
                        if signal_type == "ALIŞ":
                            change_percent = ((current_price - entry_price) / entry_price) * 100
                        else:  # SATIŞ
                            change_percent = ((entry_price - current_price) / entry_price) * 100
                        
                        # 10x kaldıraç ile 100$ yatırım kar/zarar hesapla
                        investment_amount = 100  # 100$ yatırım
                        leverage = 10  # 10x kaldıraç
                        actual_investment = investment_amount * leverage  # 1000$ efektif yatırım
                        
                        # Kar/zarar USD hesaplama - doğru mantık
                        profit_loss_usd = (actual_investment * change_percent) / 100
                        
                        # Hedefe ne kadar kaldığını hesapla
                        if signal_type == "ALIŞ":
                            target_distance = ((target_price - current_price) / current_price) * 100
                            stop_distance = ((current_price - stop_price) / current_price) * 100
                        else:
                            target_distance = ((current_price - target_price) / current_price) * 100
                            stop_distance = ((current_price - stop_price) / current_price) * 100
                        
                        # Durum ikonu - doğru mantık
                        if signal_type == "ALIŞ":
                            # ALIŞ sinyali: fiyat yükselirse yeşil (kar), düşerse kırmızı (zarar)
                            if change_percent > 0:
                                status_icon = "🟢"  # Karda
                            elif change_percent < 0:
                                status_icon = "🔴"  # Zararda
                            else:
                                status_icon = "⚪"  # Başabaş
                        else:
                            # SATIŞ sinyali: fiyat düşerse yeşil (kar), yükselirse kırmızı (zarar)
                            if change_percent > 0:
                                status_icon = "🟢"  # Karda
                            elif change_percent < 0:
                                status_icon = "🔴"  # Zararda
                            else:
                                status_icon = "⚪"  # Başabaş
                        
                        print(f"   {status_icon} {symbol} ({signal_type}): Giriş: ${entry_price:.6f} → Güncel: ${current_price:.6f} ({change_percent:+.2f}%)")
                        print(f"      💰 10x Kaldıraç: ${profit_loss_usd:+.2f} | 📈 Hedefe: {target_distance:.2f}% | 🛑 Stop'a: {stop_distance:.2f}%")
                except:
                    print(f"   ⚪ {symbol}: Durum hesaplanamadı")
            
            for symbol, signal in list(active_signals.items()):
                try:
                    # 1 dakikalık mum verilerini al - Futures API kullan
                    try:
                        print(f"🔍 {symbol} için mum verisi çekiliyor")
                        
                        # Futures API'den mum verisi çek
                        url = f"https://fapi.binance.com/fapi/v1/klines?symbol={symbol}&interval=1m&limit=100"
                        async with aiohttp.ClientSession(connector=aiohttp.TCPConnector(limit=10)) as session:
                            async with session.get(url, ssl=False) as resp:
                                if resp.status != 200:
                                    raise Exception(f"Futures API hatası: {resp.status}")
                                klines = await resp.json()
                        
                    except Exception as e:
                        print(f"⚠️ {symbol} - Mum verisi alınamadı: {e}")
                        continue

                    if not klines:
                        print(f"⚠️ {symbol} - Henüz yeni mum oluşmamış olabilir")   
                        continue

                    # Mum verilerini kontrol et
                    is_triggered, trigger_type, final_price = check_klines_for_trigger(signal, klines)
                    
                    # Manuel stop kontrolü de ekle (güvenlik için)
                    if not is_triggered and klines:
                        current_price = float(klines[-1][4])  # Son mumun kapanış fiyatı
                        entry_price = float(str(signal.get('entry_price_float', signal.get('entry_price', 0))).replace('$', '').replace(',', ''))
                        stop_price = float(str(signal.get('stop_loss', 0)).replace('$', '').replace(',', ''))
                        signal_type = signal.get('type', 'ALIŞ')
                        
                        print(f"🔍 {symbol} Manuel Stop Kontrolü:")
                        print(f"   Tip: {signal_type} | Giriş: ${entry_price:.6f} | Güncel: ${current_price:.6f} | Stop: ${stop_price:.6f}")
                        
                        # Manuel stop kontrolü - eşit veya geçmişse tetikle
                        if signal_type == "ALIŞ" and current_price <= stop_price:
                            print(f"🛑 {symbol} - Manuel SL tetiklendi! Güncel: ${current_price:.6f} <= Stop: ${stop_price:.6f}")
                            is_triggered = True
                            trigger_type = "stop_loss"
                            final_price = current_price
                        elif signal_type == "SATIŞ" and current_price >= stop_price:
                            print(f"🛑 {symbol} - Manuel SL tetiklendi! Güncel: ${current_price:.6f} >= Stop: ${stop_price:.6f}")
                            is_triggered = True
                            trigger_type = "stop_loss"
                            final_price = current_price
                    
                    if is_triggered:
                        # Tetikleme türüne göre işlemi yap
                        if trigger_type == "take_profit":
                            # Hedefe ulaşıldı, herkese mesaj gönder
                            signal['current_price_float'] = final_price
                            await handle_take_profit(symbol, signal)
                            
                            # Pozisyonu ve aktif sinyali kaldır
                            if symbol in global_positions:
                                del global_positions[symbol]
                            del active_signals[symbol]
                            
                            # Cooldown'a ekle
                            global_stop_cooldown[symbol] = datetime.now()
                            save_stop_cooldown_to_db(global_stop_cooldown)
                            
                            # İstatistikleri güncelle
                            global_stats["successful_signals"] += 1
                            global_stats["total_profit_loss"] += 100 * 0.02  # %2 kar
                            save_stats_to_db(global_stats)
                            
                        elif trigger_type == "stop_loss":
                            # Stop-loss oldu, sadece admin'e mesaj gönder
                            signal['current_price_float'] = final_price
                            await handle_stop_loss(symbol, signal)
                            
                            # Pozisyonu ve aktif sinyali kaldır
                            if symbol in global_positions:
                                del global_positions[symbol]
                            del active_signals[symbol]
                            
                            # Cooldown'a ekle
                            global_stop_cooldown[symbol] = datetime.now()
                            save_stop_cooldown_to_db(global_stop_cooldown)
                            
                            # İstatistikleri güncelle
                            global_stats["failed_signals"] += 1
                            global_stats["total_profit_loss"] -= 100 * 0.015  # %1.5 zarar
                            save_stats_to_db(global_stats)
                    else:
                        # Tetikleme yoksa, sadece anlık fiyatı güncelle
                        if final_price:
                            signal['current_price'] = f"{final_price:.6f}"
                            signal['current_price_float'] = final_price
                            
                            entry_price = float(str(signal.get('entry_price_float', signal.get('entry_price', 0))).replace('$', '').replace(',', ''))
                            if entry_price > 0:
                                # Kâr/Zarar Yüzdesini Hesapla ve Güncelle
                                change_percent = ((final_price - entry_price) / entry_price) * 100
                                if signal.get('type', 'ALIŞ') == "SATIŞ":
                                    change_percent *= -1
                                signal['profit_loss_percent'] = change_percent
                    
                except Exception as e:
                    print(f"❌ {symbol} sinyali işlenirken hata oluştu: {e}")
                    continue
            
            # Güncel aktif sinyalleri DB'ye kaydet
            save_active_signals_to_db(active_signals)
            
            # Global değişkenleri güncelle
            global global_active_signals
            global_active_signals = active_signals.copy()
            
            # API çağrı limitlerini aşmamak için bekleme süresi
            await asyncio.sleep(5)
        
        except Exception as e:
            print(f"❌ Ana sinyal izleme döngüsü hatası: {e}")
            await asyncio.sleep(10)  # Hata durumunda bekle

async def active_signals_monitor_loop():
    """Aktif sinyalleri sürekli izleyen paralel döngü (hedef/stop için) - ESKİ SİSTEM"""
    print("🚀 Eski aktif sinyal izleme döngüsü başlatıldı!")
    
    while True:
        try:
            # Global değişkenleri al
            if global_active_signals:
                print(f"🔍 Aktif sinyaller izleniyor ({len(global_active_signals)} sinyal)...")
                await check_active_signals_quick(global_active_signals, global_positions, global_stats, global_stop_cooldown, global_successful_signals, global_failed_signals)
            
            # Her 10 saniyede bir kontrol et (daha hızlı stop loss kontrolü için)
            await asyncio.sleep(10)
            
        except Exception as e:
            print(f"❌ Aktif sinyal izleme hatası: {e}")
            await asyncio.sleep(10)  # Hata durumunda da 10 saniye bekle

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
    
    # Ana sinyal işleme döngüsünü başlat
    signal_task = asyncio.create_task(signal_processing_loop())
    
    # Yeni sinyal izleme döngüsünü paralel olarak başlat (daha hızlı ve güvenilir)
    monitor_task = asyncio.create_task(monitor_signals())
    
    # Eski aktif sinyal izleme döngüsünü de paralel olarak başlat (yedek sistem)
    old_monitor_task = asyncio.create_task(active_signals_monitor_loop())
    
    try:
        # Tüm task'ları bekle
        await asyncio.gather(signal_task, monitor_task, old_monitor_task)
    except KeyboardInterrupt:
        print("\n⚠️ Bot kapatılıyor...")
    except asyncio.CancelledError:
        print("\nℹ️ Bot task'ları iptal edildi (normal kapatma)")
    finally:
        # Task'ları iptal et
        if not signal_task.done():
            signal_task.cancel()
        if not monitor_task.done():
            monitor_task.cancel()
        if not old_monitor_task.done():
            old_monitor_task.cancel()
        
        try:
            await asyncio.gather(signal_task, monitor_task, old_monitor_task, return_exceptions=True)
        except Exception:
            pass
        
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
        
        # 2) Aktif sinyaller - MongoDB'den tamamen sil
        active_deleted = clear_data_by_pattern("^active_signal_", "aktif sinyal")
        save_active_signals_to_db({})
        global global_active_signals
        global_active_signals = {}
        
        # 3) Stop cooldown verileri - MongoDB'den tamamen sil
        cooldown_deleted = clear_data_by_pattern("^stop_cooldown_", "stop cooldown")
        
        # 4) JSON dosyasını da temizle
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
        global global_waiting_signals
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
            f"• Aktif sinyal: {active_deleted} silindi\n"
            f"• Stop cooldown: {cooldown_deleted} silindi\n"
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
    
    print(f"🔍 Sinyal sayımı: {tf_names}")
    print(f"   Sinyal değerleri: {signal_values}")
    print(f"   ALIŞ sayısı: {buy_count}, SATIŞ sayısı: {sell_count}")
    return buy_count, sell_count

def check_7_7_rule(buy_count, sell_count):
    """7/7 kuralını kontrol eder - tüm 7 zaman dilimi aynı yönde olmalı"""
    result = buy_count == 7 or sell_count == 7
    print(f"🔍 7/7 kural kontrolü: ALIŞ={buy_count}, SATIŞ={sell_count} → Sonuç: {result}")
    return result

def check_cooldown(symbol, cooldown_dict, hours=4):  # ✅ 4 SAAT COOLDOWN - TÜM SİNYALLER İÇİN
    """Cooldown kontrolü yapar - tüm sinyaller için 4 saat"""
    if symbol in cooldown_dict:
        last_time = cooldown_dict[symbol]
        if isinstance(last_time, str):
            last_time = datetime.fromisoformat(last_time)
        time_diff = (datetime.now() - last_time).total_seconds() / 3600
        if time_diff < hours:
            remaining_hours = hours - time_diff
            print(f"⏰ {symbol} → Cooldown aktif: {remaining_hours:.1f} saat kaldı")
            return True  # Cooldown aktif
        else:
            del cooldown_dict[symbol]  # Cooldown doldu
            print(f"✅ {symbol} → Cooldown süresi doldu, yeni sinyal aranabilir")
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
# 15m onay sistemi kaldırıldı

def select_all_signals(potential_signals):
    """Tüm 7/7 sinyalleri seçer ve işler."""
    if not potential_signals:
        return []
    
    # Tüm sinyalleri hacme göre sırala
    all_signals = sorted(potential_signals, key=lambda x: x['volume_usd'], reverse=True)
    print(f"✅ {len(all_signals)} 7/7 sinyal seçildi")
    return all_signals

def save_stop_cooldown_to_db(stop_cooldown):
    """Stop cooldown verilerini MongoDB'ye kaydet"""
    try:
        if mongo_collection is None:
            if not connect_mongodb():
                print("❌ MongoDB bağlantısı kurulamadı, stop cooldown kaydedilemedi")
                return False
        
        # Önce tüm stop cooldown verilerini sil
        mongo_collection.delete_many({"_id": {"$regex": "^stop_cooldown_"}})
        
        # Yeni stop cooldown verilerini ekle
        for symbol, timestamp in stop_cooldown.items():
            doc_id = f"stop_cooldown_{symbol}"
            mongo_collection.insert_one({
                "_id": doc_id,
                "data": timestamp,
                "timestamp": datetime.now()
            })
        
        print(f"✅ {len(stop_cooldown)} stop cooldown MongoDB'ye kaydedildi")
        return True
    except Exception as e:
        print(f"❌ Stop cooldown MongoDB'ye kaydedilirken hata: {e}")
        return False

async def handle_take_profit(symbol, signal):
    """
    Hedef fiyatına ulaşıldığında çağrılır. Kar hesaplaması yapar ve tüm kullanıcılara mesaj gönderir.
    """
    try:
        print(f"🎯 {symbol} HEDEF BAŞARIYLA GERÇEKLEŞTİ!")
        
        # Giriş ve çıkış fiyatlarını al
        entry_price = float(str(signal.get('entry_price_float', signal.get('entry_price', 0))).replace('$', '').replace(',', ''))
        exit_price = float(str(signal.get('current_price_float', 0)).replace('$', '').replace(',', ''))
        
        if entry_price <= 0 or exit_price <= 0:
            print(f"⚠️ {symbol} - Geçersiz fiyat verileri: Giriş={entry_price}, Çıkış={exit_price}")
            return
        
        # Kar hesaplaması
        signal_type = signal.get('type', 'ALIŞ')
        if signal_type == "ALIŞ":
            profit_percentage = ((exit_price - entry_price) / entry_price) * 100
        else:  # SATIŞ
            profit_percentage = ((entry_price - exit_price) / entry_price) * 100
        
        profit_usd = 100 * profit_percentage / 100  # 100$ yatırım için
        
        print(f"💰 Kar: %{profit_percentage:.2f} (${profit_usd:.2f})")
        
        # Herkese hedef mesajı gönder
        target_message = f"🎯 <b>HEDEF BAŞARIYLA GERÇEKLEŞTİ!</b> 🎯\n\n<b>{symbol}</b> işlemi için hedef fiyatına ulaşıldı!\n💰 Kar: %{profit_percentage:.2f} (${profit_usd:.2f})\n📈 Giriş: ${entry_price:.6f}\n🎯 Hedef: ${exit_price:.6f}\n💵 Çıkış: ${exit_price:.6f}"
        await send_signal_to_all_users(target_message)
        
        print(f"✅ {symbol} hedef mesajı tüm kullanıcılara gönderildi")
        
    except Exception as e:
        print(f"❌ handle_take_profit hatası ({symbol}): {e}")

async def handle_stop_loss(symbol, signal):
    """
    Stop-loss fiyatına ulaşıldığında çağrılır. Zarar hesaplaması yapar ve sadece bot sahibine mesaj gönderir.
    """
    try:
        print(f"🛑 {symbol} STOP BAŞARIYLA GERÇEKLEŞTİ!")
        
        # Giriş ve çıkış fiyatlarını al
        entry_price = float(str(signal.get('entry_price_float', signal.get('entry_price', 0))).replace('$', '').replace(',', ''))
        exit_price = float(str(signal.get('current_price_float', 0)).replace('$', '').replace(',', ''))
        
        if entry_price <= 0 or exit_price <= 0:
            print(f"⚠️ {symbol} - Geçersiz fiyat verileri: Giriş={entry_price}, Çıkış={exit_price}")
            return
        
        # Zarar hesaplaması
        signal_type = signal.get('type', 'ALIŞ')
        if signal_type == "ALIŞ":
            loss_percentage = ((entry_price - exit_price) / entry_price) * 100
        else:  # SATIŞ
            loss_percentage = ((exit_price - entry_price) / entry_price) * 100
        
        loss_usd = 100 * loss_percentage / 100  # 100$ yatırım için
        
        print(f"💸 Zarar: %{loss_percentage:.2f} (${loss_usd:.2f})")
        
        # Sadece bot sahibine stop mesajı gönder
        stop_message = f"🛑 STOP OLDU!\n\n🔹 Kripto Çifti: {symbol}\n💸 Zarar: %{loss_percentage:.2f} (${loss_usd:.2f})\n📈 Giriş: ${entry_price:.6f}\n🛑 Stop: ${exit_price:.6f}\n💵 Çıkış: ${exit_price:.6f}"
        await send_admin_message(stop_message)
        
        print(f"✅ {symbol} stop mesajı bot sahibine gönderildi")
        
    except Exception as e:
        print(f"❌ handle_stop_loss hatası ({symbol}): {e}")

if __name__ == "__main__":
    asyncio.run(main())