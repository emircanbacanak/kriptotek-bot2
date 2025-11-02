import sys
import asyncio
if sys.platform.startswith("win"):
    asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())
import pandas as pd
import ta
from datetime import datetime, timedelta
import pytz
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

CONFIG = {
    "PROFIT_PERCENT": 2.0,
    "STOP_PERCENT": 1.5,
    "LEVERAGE": 10,
    "COOLDOWN_HOURS": 8,  # 8 SAAT COOLDOWN - Hedef ve Stop için
    "MAIN_LOOP_SLEEP_SECONDS": 300,  # Ana sinyal arama döngüsü - 5 dakika
    "MONITOR_LOOP_SLEEP_SECONDS": 10,  # Pozisyon izleme döngüsü - 10 saniye (optimize edildi)
    "API_RETRY_ATTEMPTS": 3,
    "API_RETRY_DELAYS": [1, 3, 5],  # saniye
    "MONITOR_SLEEP_EMPTY": 30,  # Aktif pozisyon yoksa bekle - 30 saniye (optimize edildi)
    "MONITOR_SLEEP_ERROR": 15,  # Hata durumunda bekle - 15 saniye (optimize edildi)
    "MONITOR_SLEEP_NORMAL": 10,  # Normal durumda bekle - 10 saniye (optimize edildi)
    "MAX_SIGNALS_PER_RUN": 5,  # Bir döngüde maksimum bulunacak sinyal sayısı
    "COOLDOWN_MINUTES": 30,  # Çok fazla sinyal bulunduğunda bekleme süresi

}

TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")
MONGODB_URI = os.getenv("MONGODB_URI", "mongodb://localhost:27017/")
MONGODB_DB = os.getenv("MONGODB_DB", "crypto_signal_bot")
MONGODB_COLLECTION = os.getenv("MONGODB_COLLECTION", "allowed_users")
BOT_OWNER_ID = int(os.getenv("BOT_OWNER_ID", "0"))
ADMIN_USERS = set()
BOT_OWNER_GROUPS = set()
ALLOWED_USERS = set()

mongo_client = None
mongo_db = None
mongo_collection = None

client = Client()

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

async def send_command_response(update, message, parse_mode='Markdown'):
    """Komut yanıtını gönderir"""
    await update.message.reply_text(message, parse_mode=parse_mode)

async def api_request_with_retry(session, url, ssl=False, max_retries=None):
    if max_retries is None:
        max_retries = CONFIG["API_RETRY_ATTEMPTS"]
    
    for attempt in range(max_retries):
        try:
            async with session.get(url, ssl=ssl) as resp:
                if resp.status == 200:
                    return await resp.json()
                elif resp.status == 429:  # Rate limit
                    delay = CONFIG["API_RETRY_DELAYS"][min(attempt, len(CONFIG["API_RETRY_DELAYS"])-1)]
                    print(f"⚠️ Rate limit (429), {delay} saniye bekleniyor... (Deneme {attempt+1}/{max_retries})")
                    await asyncio.sleep(delay)
                    continue
                else:
                    print(f"⚠️ API hatası: {resp.status}, Deneme {attempt+1}/{max_retries}")
                    if attempt < max_retries - 1:
                        delay = CONFIG["API_RETRY_DELAYS"][min(attempt, len(CONFIG["API_RETRY_DELAYS"])-1)]
                        await asyncio.sleep(delay)
                        continue
                    else:
                        raise Exception(f"API hatası: {resp.status}")
                        
        except asyncio.TimeoutError:
            print(f"⚠️ Timeout hatası, Deneme {attempt+1}/{max_retries}")
            if attempt < max_retries - 1:
                delay = CONFIG["API_RETRY_DELAYS"][min(attempt, len(CONFIG["API_RETRY_DELAYS"])-1)]
                await asyncio.sleep(delay)
                continue
            else:
                raise Exception("API timeout hatası")
                
        except Exception as e:
            print(f"⚠️ API isteği hatası: {e}, Deneme {attempt+1}/{max_retries}")
            if attempt < max_retries - 1:
                delay = CONFIG["API_RETRY_DELAYS"][min(attempt, len(CONFIG["API_RETRY_DELAYS"])-1)]
                await asyncio.sleep(delay)
                continue
            else:
                raise e
    
    raise Exception(f"API isteği {max_retries} denemeden sonra başarısız")

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
            
        min_trigger_diff = 0.001  # %0.1 minimum fark
        
        # LONG sinyali kontrolü (long pozisyon)
        if signal_type == "LONG" or signal_type == "ALIS" or signal_type == "ALIŞ":
            if high >= target_price:
                print(f"✅ {symbol} - TP tetiklendi! Mum: High={high:.6f}, TP={target_price:.6f}")
                return True, "take_profit", high
            if low <= stop_loss_price:
                print(f"❌ {symbol} - SL tetiklendi! Mum: Low={low:.6f}, SL={stop_loss_price:.6f}")
                return True, "stop_loss", low
                
        # SHORT sinyali kontrolü (short pozisyon)
        elif signal_type == "SATIŞ" or signal_type == "SATIS" or signal_type == "SHORT":
            if low <= target_price:
                print(f"✅ {symbol} - TP tetiklendi! Mum: Low={low:.6f}, TP={target_price:.6f}")
                return True, "take_profit", low
            if high >= stop_loss_price:
                print(f"❌ {symbol} - SL tetiklendi! Mum: High={high:.6f}, SL={stop_loss_price:.6f}")
                return True, "stop_loss", high

        # Hiçbir tetikleme yoksa, false döner ve son mumu döndürür
        final_price = float(df['close'].iloc[-1]) if not df.empty else None
        return False, None, final_price
        
    except Exception as e:
        print(f"❌ check_klines_for_trigger hatası ({signal.get('symbol', 'UNKNOWN')}): {e}")
        return False, None, None

def save_stats_to_db(stats):
    """İstatistik sözlüğünü MongoDB'ye kaydeder."""
    return save_data_to_db("bot_stats", stats, "Stats")

def load_stats_from_db():
    """MongoDB'den son istatistik sözlüğünü döndürür."""
    return load_data_from_db("bot_stats", {})

def update_stats_atomic(updates):
    try:
        if mongo_collection is None:
            if not connect_mongodb():
                print("❌ MongoDB bağlantısı kurulamadı, atomik güncelleme yapılamadı")
                return False
        
        # Atomik $inc operatörü ile güncelleme
        update_data = {}
        for key, value in updates.items():
            update_data[f"data.{key}"] = value
        
        result = mongo_collection.update_one(
            {"_id": "bot_stats"},
            {"$inc": update_data, "$set": {"data.last_updated": str(datetime.now())}},
            upsert=True
        )
        
        if result.modified_count > 0 or result.upserted_id:
            print(f"✅ İstatistikler atomik olarak güncellendi: {updates}")
            return True
        else:
            print(f"⚠️ İstatistik güncellemesi yapılamadı: {updates}")
            return False
            
    except Exception as e:
        print(f"❌ Atomik istatistik güncelleme hatası: {e}")
        return False

def update_position_status_atomic(symbol, status, additional_data=None):
    # Global active_signals değişkenini kullan
    global active_signals
    
    try:
        if mongo_collection is None:
            if not connect_mongodb():
                print("❌ MongoDB bağlantısı kurulamadı, pozisyon durumu güncellenemedi")
                return False
        
        # Önce dokümanın var olup olmadığını kontrol et
        existing_doc = mongo_collection.find_one({"_id": f"active_signal_{symbol}"})
        
        if existing_doc:
            # Üst seviye alanları güncelle (status, last_update ve opsiyonel trigger_type)
            update_set = {"status": status, "last_update": str(datetime.now())}
            if additional_data:
                for key, value in additional_data.items():
                    update_set[key] = value
            result = mongo_collection.update_one(
                {"_id": f"active_signal_{symbol}"},
                {"$set": update_set},
                upsert=False
            )
        else:
            # Doküman yoksa, yeni oluştur
            new_doc = {
                "_id": f"active_signal_{symbol}",
                "status": status,
                "last_update": str(datetime.now())
            }
            
            if additional_data:
                for key, value in additional_data.items():
                    new_doc[key] = value
             
            result = mongo_collection.insert_one(new_doc)
        
        # insert_one için upserted_id, update_one için modified_count kontrol et
        if hasattr(result, 'modified_count') and result.modified_count > 0:
            print(f"✅ {symbol} pozisyon durumu güncellendi: {status}")
            if symbol in active_signals:
                active_signals[symbol]['status'] = status
                if additional_data and 'trigger_type' in additional_data:
                    active_signals[symbol]['trigger_type'] = additional_data['trigger_type']
            return True
        elif hasattr(result, 'upserted_id') and result.upserted_id:
            print(f"✅ {symbol} pozisyon durumu oluşturuldu: {status}")
            if symbol in active_signals:
                active_signals[symbol]['status'] = status
                if additional_data and 'trigger_type' in additional_data:
                    active_signals[symbol]['trigger_type'] = additional_data['trigger_type']
            return True
        else:
            print(f"⚠️ {symbol} pozisyon durumu güncellenemedi: {status} (Result: {result})")
            return False
            
    except Exception as e:
        print(f"❌ Pozisyon durumu güncelleme hatası ({symbol}): {e}")
        return False

def save_active_signals_to_db(active_signals):
    """Aktif sinyalleri MongoDB'ye kaydeder."""
    try:
        if mongo_collection is None:
            if not connect_mongodb():
                print("❌ MongoDB bağlantısı kurulamadı, aktif sinyaller kaydedilemedi")
                return False
        
        # Eğer boş sözlük ise, tüm aktif sinyal dokümanlarını sil
        if not active_signals:
            try:
                delete_result = mongo_collection.delete_many({"_id": {"$regex": "^active_signal_"}})
                deleted_count = getattr(delete_result, "deleted_count", 0)
                print(f"🧹 Boş aktif sinyal listesi için {deleted_count} doküman silindi")
                return True
            except Exception as e:
                print(f"❌ Boş aktif sinyal temizleme hatası: {e}")
                return False
        
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
                "status": signal.get("status", "active"),  # Mevcut durumu kullan, yoksa "active"
                "trigger_type": signal.get("trigger_type", None),  # Trigger type bilgisini ekle
                "saved_at": str(datetime.now())
            }
            
            # Doğrudan MongoDB'ye kaydet (save_data_to_db kullanma)
            try:
                mongo_collection.update_one(
                    {"_id": f"active_signal_{symbol}"},
                    {"$set": signal_doc},
                    upsert=True
                )
            except Exception as e:
                print(f"❌ {symbol} aktif sinyali kaydedilemedi: {e}")
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
            # Artık veri doğrudan dokümanda, data alanında değil
            if "symbol" not in doc:
                continue
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
                "last_update": doc.get("last_update", ""),
                "status": doc.get("status", "active"),  # Varsayılan durum "active"
                "trigger_type": doc.get("trigger_type", None)  # Trigger type bilgisini yükle
            }
        return result
    except Exception as e:
        print(f"❌ MongoDB'den aktif sinyaller yüklenirken hata: {e}")
        return {}

def connect_mongodb():
    """MongoDB bağlantısını kur"""
    global mongo_client, mongo_db, mongo_collection
    try:
        mongo_client = MongoClient(MONGODB_URI, 
                                  serverSelectionTimeoutMS=30000,
                                  connectTimeoutMS=30000,
                                  socketTimeoutMS=30000,
                                  maxPoolSize=10)
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
        
        users_data = load_data_from_db("allowed_users")
        if users_data and 'user_ids' in users_data:
            ALLOWED_USERS = set(users_data['user_ids'])
            print(f"✅ MongoDB'den {len(ALLOWED_USERS)} izin verilen kullanıcı yüklendi (yeni format)")
        else:
            # Eski format kontrolü - data.user_ids içinde olabilir
            raw_doc = mongo_collection.find_one({"_id": "allowed_users"})
            if raw_doc and 'data' in raw_doc and 'user_ids' in raw_doc['data']:
                ALLOWED_USERS = set(raw_doc['data']['user_ids'])
                print(f"✅ MongoDB'den {len(ALLOWED_USERS)} izin verilen kullanıcı yüklendi (eski format)")
                # Eski formatı yeni formata çevir
                save_allowed_users()
            else:
                print("ℹ️ MongoDB'de izin verilen kullanıcı bulunamadı, boş liste ile başlatılıyor")
                ALLOWED_USERS = set()
        
        admin_groups_data = load_data_from_db("admin_groups")
        if admin_groups_data and 'group_ids' in admin_groups_data:
            BOT_OWNER_GROUPS = set(admin_groups_data['group_ids'])
            print(f"✅ MongoDB'den {len(BOT_OWNER_GROUPS)} admin grubu yüklendi (yeni format)")
        else:
            # Eski format kontrolü - data.group_ids içinde olabilir
            raw_doc = mongo_collection.find_one({"_id": "admin_groups"})
            if raw_doc and 'data' in raw_doc and 'group_ids' in raw_doc['data']:
                BOT_OWNER_GROUPS = set(raw_doc['data']['group_ids'])
                print(f"✅ MongoDB'den {len(BOT_OWNER_GROUPS)} admin grubu yüklendi (eski format)")
                # Eski formatı yeni formata çevir
                save_admin_groups()
            else:
                print("ℹ️ MongoDB'de admin grubu bulunamadı, boş liste ile başlatılıyor")
                BOT_OWNER_GROUPS = set()
        
        admin_users_data = load_data_from_db("admin_users")
        if admin_users_data and 'admin_ids' in admin_users_data:
            ADMIN_USERS = set(admin_users_data['admin_ids'])
            print(f"✅ MongoDB'den {len(ADMIN_USERS)} admin kullanıcı yüklendi (yeni format)")
        else:
            # Eski format kontrolü - data.admin_ids içinde olabilir
            raw_doc = mongo_collection.find_one({"_id": "admin_users"})
            if raw_doc and 'data' in raw_doc and 'admin_ids' in raw_doc['data']:
                ADMIN_USERS = set(raw_doc['data']['admin_ids'])
                print(f"✅ MongoDB'den {len(ADMIN_USERS)} admin kullanıcı yüklendi (eski format)")
                # Eski formatı yeni formata çevir
                save_admin_users()
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

async def check_cooldown_status():
    """Cooldown durumunu veritabanından kontrol eder ve döner."""
    try:
        if mongo_collection is None:
            if not connect_mongodb():
                return None
        
        doc = mongo_collection.find_one({"_id": "cooldown"})
        if doc and doc.get("until") and doc["until"] > datetime.now():
            return doc["until"]
        
        return None  # Cooldown yok
    except Exception as e:
        print(f"❌ Cooldown durumu kontrol edilirken hata: {e}")
        return None

async def clear_cooldown_status():
    """Cooldown durumunu veritabanından temizler."""
    try:
        if mongo_collection is None:
            if not connect_mongodb():
                print("❌ MongoDB bağlantısı kurulamadı, cooldown temizlenemedi")
                return False
        
        mongo_collection.delete_one({"_id": "cooldown"})
        print("✅ Cooldown durumu temizlendi.")
        return True
    except Exception as e:
        print(f"❌ Cooldown durumu temizlenirken hata: {e}")
        return False

async def set_signal_cooldown_to_db(symbols, cooldown_delta: timedelta):
    """Belirtilen sembolleri cooldown'a ekler."""
    try:
        if mongo_collection is None:
            if not connect_mongodb():
                print("❌ MongoDB bağlantısı kurulamadı, sinyal cooldown kaydedilemedi")
                return False
        
        cooldown_until = datetime.now() + cooldown_delta
        
        for symbol in symbols:
            mongo_collection.update_one(
                {"_id": f"signal_cooldown_{symbol}"},
                {"$set": {"until": cooldown_until, "timestamp": datetime.now()}},
                upsert=True
            )
        
        print(f"⏳ {len(symbols)} sinyal cooldown'a eklendi: {', '.join(symbols)}")
        return True
    except Exception as e:
        print(f"❌ Sinyal cooldown veritabanına kaydedilirken hata: {e}")
        return False
async def check_signal_cooldown(symbol):
    """Belirli bir sembolün cooldown durumunu kontrol eder."""
    try:
        if mongo_collection is None:
            if not connect_mongodb():
                return False
        
        doc = mongo_collection.find_one({"_id": f"signal_cooldown_{symbol}"})
        if doc and doc.get("until") and doc["until"] > datetime.now():
            return True  # Cooldown'da
        
        return False  # Cooldown yok
    except Exception as e:
        print(f"❌ Sinyal cooldown durumu kontrol edilirken hata: {e}")
        return False

async def get_expired_cooldown_signals():
    """Cooldown süresi biten sinyalleri döndürür ve temizler."""
    try:
        if mongo_collection is None:
            if not connect_mongodb():
                return []
        
        expired_signals = []
        current_time = datetime.now()
        
        # Süresi biten cooldown'ları bul
        expired_docs = mongo_collection.find({
            "_id": {"$regex": "^signal_cooldown_"},
            "until": {"$lte": current_time}
        })
        
        for doc in expired_docs:
            symbol = doc["_id"].replace("signal_cooldown_", "")
            expired_signals.append(symbol)
            # Süresi biten cooldown'ı sil
            mongo_collection.delete_one({"_id": doc["_id"]})
        
        if expired_signals:
            print(f"🔄 {len(expired_signals)} sinyal cooldown süresi bitti: {', '.join(expired_signals)}")
        
        return expired_signals
    except Exception as e:
        print(f"❌ Süresi biten cooldown sinyalleri alınırken hata: {e}")
        return []

async def get_volumes_for_symbols(symbols):
    """Belirtilen semboller için hacim verilerini Binance'den çeker."""
    try:
        volumes = {}
        for symbol in symbols:
            try:
                ticker_data = client.futures_ticker(symbol=symbol)
                
                # API bazen liste döndürüyor, bazen dict
                if isinstance(ticker_data, list):
                    if len(ticker_data) == 0:
                        volumes[symbol] = 0
                        continue
                    ticker = ticker_data[0]  # İlk elementi al
                else:
                    ticker = ticker_data
                
                if ticker and isinstance(ticker, dict) and 'quoteVolume' in ticker:
                    volumes[symbol] = float(ticker['quoteVolume'])
                else:
                    volumes[symbol] = 0
                    
            except Exception as e:
                print(f"⚠️ {symbol} hacim verisi alınamadı: {e}")
                volumes[symbol] = 0
        
        return volumes
    except Exception as e:
        print(f"❌ Hacim verileri alınırken hata: {e}")
        return {symbol: 0 for symbol in symbols}

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
                
        for symbol, position in positions.items():
            doc_id = f"position_{symbol}"
            
            if not position or not isinstance(position, dict):
                print(f"⚠️ {symbol} - Geçersiz pozisyon verisi, atlanıyor")
                continue
                
            required_fields = ['type', 'target', 'stop', 'open_price', 'leverage']
            missing_fields = [field for field in required_fields if field not in position]
            
            if missing_fields:
                print(f"⚠️ {symbol} - Eksik alanlar: {missing_fields}, pozisyon atlanıyor")
                continue
            
            try:
                open_price = float(position['open_price'])
                target_price = float(position['target'])
                stop_price = float(position['stop'])
                
                if open_price <= 0 or target_price <= 0 or stop_price <= 0:
                    print(f"⚠️ {symbol} - Geçersiz fiyat değerleri, pozisyon atlanıyor")
                    print(f"   Giriş: {open_price}, Hedef: {target_price}, Stop: {stop_price}")
                    continue
                    
            except (ValueError, TypeError) as e:
                print(f"⚠️ {symbol} - Fiyat dönüşüm hatası: {e}, pozisyon atlanıyor")
                continue

            # Pozisyon verilerini data alanında kaydet (tutarlı yapı için)
            result = mongo_collection.update_one(
                {"_id": doc_id},
                {
                    "$set": {
                        "symbol": symbol,
                        "data": position,  # TÜM POZİSYON VERİSİ BURAYA GELECEK
                        "timestamp": datetime.now()
                    }
                },
                upsert=True
            )
            
            if result.modified_count > 0 or result.upserted_id:
                print(f"✅ {symbol} pozisyonu güncellendi/eklendi")
                
                # Pozisyon kaydedildikten sonra active_signal dokümanını da oluştur
                try:
                    # Pozisyon verilerinden active_signal dokümanı oluştur
                    active_signal_doc = {
                        "_id": f"active_signal_{symbol}",
                        "symbol": symbol,
                        "type": position.get("type", "ALIŞ"),
                        "entry_price": format_price(position.get("open_price", 0), position.get("open_price", 0)),
                        "entry_price_float": position.get("open_price", 0),
                        "target_price": format_price(position.get("target", 0), position.get("open_price", 0)),
                        "stop_loss": format_price(position.get("stop", 0), position.get("open_price", 0)),
                        "signals": position.get("signals", {}),
                        "leverage": position.get("leverage", 10),
                        "signal_time": position.get("entry_time", datetime.now().strftime('%Y-%m-%d %H:%M')),
                        "current_price": format_price(position.get("open_price", 0), position.get("open_price", 0)),
                        "current_price_float": position.get("open_price", 0),
                        "last_update": str(datetime.now()),
                        "status": "active",
                        "saved_at": str(datetime.now())
                    }
                    
                    # Active signal dokümanını kaydet
                    mongo_collection.update_one(
                        {"_id": f"active_signal_{symbol}"},
                        {"$set": active_signal_doc},
                        upsert=True
                    )
                    print(f"✅ {symbol} active_signal dokümanı oluşturuldu")
                    
                except Exception as e:
                    print(f"⚠️ {symbol} active_signal dokümanı oluşturulurken hata: {e}")
            else:
                print(f"⚠️ {symbol} pozisyonu güncellenemedi")
        
        print(f"✅ {len(positions)} pozisyon MongoDB'ye kaydedildi")
        
        # Pozisyon durumlarını güncelle - artık active_signal dokümanları zaten oluşturuldu
        for symbol in positions.keys():
            try:
                # Durumu "active" olarak güncelle
                update_position_status_atomic(symbol, "active")
            except Exception as e:
                print(f"⚠️ {symbol} pozisyon durumu güncellenirken hata: {e}")
        
        return True
    except Exception as e:
        print(f"❌ Pozisyonlar MongoDB'ye kaydedilirken hata: {e}")
        return False

def load_positions_from_db():
    try:
        if mongo_collection is None:
            if not connect_mongodb():
                print("❌ MongoDB bağlantısı kurulamadı, pozisyonlar yüklenemedi")
                return {}
        
        positions = {}
        docs = mongo_collection.find({"_id": {"$regex": "^position_"}})
        
        for doc in docs:
            symbol = doc["_id"].replace("position_", "")
            position_data = doc.get('data', doc)
            
            if not position_data or not isinstance(position_data, dict):
                print(f"⚠️ {symbol} - Geçersiz pozisyon verisi formatı, atlanıyor")
                continue
            
            required_fields = ['type', 'target', 'stop', 'open_price', 'leverage']
            missing_fields = [field for field in required_fields if field not in position_data]
            
            if missing_fields:
                print(f"⚠️ {symbol} - Eksik alanlar: {missing_fields}, pozisyon atlanıyor")
                continue
            
            try:
                open_price = float(position_data['open_price'])
                target_price = float(position_data['target'])
                stop_price = float(position_data['stop'])
                
                if open_price <= 0 or target_price <= 0 or stop_price <= 0:
                    print(f"⚠️ {symbol} - Geçersiz fiyat değerleri, pozisyon atlanıyor")
                    print(f"   Giriş: {open_price}, Hedef: {target_price}, Stop: {stop_price}")
                    continue
                    
            except (ValueError, TypeError) as e:
                print(f"⚠️ {symbol} - Fiyat dönüşüm hatası: {e}, pozisyon atlanıyor")
                continue
            
            positions[symbol] = position_data
        
        return positions
    except Exception as e:
        print(f"❌ MongoDB'den pozisyonlar yüklenirken hata: {e}")
        return {}

def load_position_from_db(symbol):
    """MongoDB'den tek pozisyon yükler."""
    try:
        if mongo_collection is None:
            if not connect_mongodb():
                print("❌ MongoDB bağlantısı kurulamadı, pozisyon yüklenemedi")
                return None
        
        doc = mongo_collection.find_one({"_id": f"position_{symbol}"})
        if doc:
            # Veriyi hem yeni (data anahtarı) hem de eski yapıdan (doğrudan doküman) almaya çalış
            position_data = doc.get('data', doc)
            
            if "open_price" in position_data:
                try:
                    open_price_raw = position_data.get("open_price", 0)
                    target_price_raw = position_data.get("target", 0)
                    stop_loss_raw = position_data.get("stop", 0)
                    
                    open_price = float(open_price_raw) if open_price_raw is not None else 0.0
                    target_price = float(target_price_raw) if target_price_raw is not None else 0.0
                    stop_loss = float(stop_loss_raw) if stop_loss_raw is not None else 0.0

                    if open_price <= 0 or target_price <= 0 or stop_loss <= 0:
                        print(f"⚠️ {symbol} - Geçersiz pozisyon fiyatları tespit edildi")
                        print(f"   Giriş: {open_price}, Hedef: {target_price}, Stop: {stop_loss}")
                        print(f"   ⚠️ Pozisyon verisi yüklenemedi, ancak silinmedi")
                        return None
                    
                    validated_data = position_data.copy()
                    validated_data["open_price"] = open_price
                    validated_data["target"] = target_price
                    validated_data["stop"] = stop_loss
                    validated_data["leverage"] = int(position_data.get("leverage", 10))
                    return validated_data
                    
                except (ValueError, TypeError) as e:
                    print(f"❌ {symbol} - Pozisyon verisi dönüşüm hatası: {e}")
                    print(f"   Raw doc: {doc}")
                    print(f"   ⚠️ Pozisyon verisi yüklenemedi, ancak silinmedi")
                    return None
        
        # Aktif sinyal dokümanından veri okuma kısmını kaldır - artık pozisyon dokümanlarından okuyoruz
        # Bu kısım kaldırıldı çünkü pozisyon verileri artık doğrudan position_ dokümanlarında
        print(f"❌ {symbol} için hiçbir pozisyon verisi bulunamadı!")
        return None
        
    except Exception as e:
        print(f"❌ MongoDB'den {symbol} pozisyonu yüklenirken hata: {e}")
        return None

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
            # Hem eski format (data) hem de yeni format (until) desteği
            if "until" in doc:
                # Yeni format: until alanı direkt cooldown bitiş zamanı
                cooldown_until = doc["until"]
                if isinstance(cooldown_until, str):
                    try:
                        cooldown_until = datetime.fromisoformat(cooldown_until.replace('Z', '+00:00'))
                    except:
                        cooldown_until = datetime.now()
                elif not isinstance(cooldown_until, datetime):
                    cooldown_until = datetime.now()
                
                # Eğer cooldown süresi geçmişse, bu kripto için cooldown yok
                if cooldown_until > datetime.now():
                    stop_cooldown[symbol] = cooldown_until
                # Geçmişse bu kripto için cooldown yok, ekleme
            elif "data" in doc:
                # Eski format: data'dan until hesapla
                timestamp = doc["data"]
                if isinstance(timestamp, str):
                    try:
                        timestamp = datetime.fromisoformat(timestamp.replace('Z', '+00:00'))
                    except:
                        timestamp = datetime.now()
                elif not isinstance(timestamp, datetime):
                    timestamp = datetime.now()
                
                # Cooldown bitiş zamanını hesapla
                cooldown_end = timestamp + timedelta(hours=CONFIG["COOLDOWN_HOURS"])
                
                # Eğer cooldown süresi geçmişse, bu kripto için cooldown yok
                if cooldown_end > datetime.now():
                    stop_cooldown[symbol] = cooldown_end
                # Geçmişse bu kripto için cooldown yok, ekleme
            else:
                # Hiçbiri yoksa şimdiki zaman + 4 saat
                stop_cooldown[symbol] = datetime.now() + timedelta(hours=CONFIG["COOLDOWN_HOURS"])
        
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
        
        existing_doc = mongo_collection.find_one({"_id": "previous_signals_initialized"})
        if existing_doc:
            print("ℹ️ Önceki sinyaller zaten kaydedilmiş, tekrar kaydedilmiyor")
            return True
        
        for symbol, signals in previous_signals.items():
            signal_doc = {
                "_id": f"previous_signal_{symbol}",
                "symbol": symbol,
                "signals": signals,
                "saved_time": str(datetime.now())
            }
            
            if not save_data_to_db(f"previous_signal_{symbol}", signal_doc, "Önceki Sinyal"):
                return False
        
        if not save_data_to_db("previous_signals_initialized", {"initialized": True, "initialized_time": str(datetime.now())}, "İlk Kayıt"):
            return False
        
        print(f"✅ MongoDB'ye {len(previous_signals)} önceki sinyal kaydedildi (ilk çalıştırma)")
        return True
    except Exception as e:
        print(f"❌ MongoDB'ye önceki sinyaller kaydedilirken hata: {e}")
        return False

def load_previous_signals_from_db():
    try:
        def transform_signal(doc):
            symbol = doc["_id"].replace("previous_signal_", "")
            if "signals" in doc:
                return {symbol: doc["signals"]}
            else:
                return {symbol: doc}
        
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

app = None
global_stats = {
    "total_signals": 0,
    "successful_signals": 0,
    "failed_signals": 0,
    "total_profit_loss": 0.0,
    "active_signals_count": 0,
    "tracked_coins_count": 0
}
global_active_signals = {}
global_waiting_signals = {} 
global_successful_signals = {}
global_failed_signals = {}
global_positions = {} 
global_stop_cooldown = {} 
global_allowed_users = set() 
global_admin_users = set() 
global_last_signal_scan_time = None
active_signals = {}  # Global active_signals değişkeni
position_processing_flags = {}  # Race condition önleme için pozisyon işlem flag'leri

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
    """Sinyal ve Hedef mesajlarını sadece ekli olduğu gruplara/kanallara gönderir"""
    if message and ("STOP" in message.upper() or "🛑" in message):
        return
    
    sent_chats = set() 

    # Sadece bot sahibinin ekli olduğu gruplara/kanallara gönder
    for group_id in BOT_OWNER_GROUPS:
        if str(group_id) not in sent_chats:
            try:
                await send_telegram_message(message, group_id)
                print(f"✅ Gruba/Kanala mesaj gönderildi: {group_id}")
                sent_chats.add(str(group_id))
            except Exception as e:
                print(f"❌ Gruba/Kanala mesaj gönderilemedi ({group_id}): {e}")

async def help_command(update, context):
    if not update.effective_user:
        return
    
    user_id = update.effective_user.id
    if user_id != BOT_OWNER_ID and user_id not in ALLOWED_USERS and user_id not in ADMIN_USERS:
        return 
    
    # Tüm kullanıcılar için aynı yardım metni
    help_text = """
📊 **Kripto Sinyal Botu Komutları:**

📊 **Temel Komutlar:**
/help - Bu yardım mesajını göster
/stats - İstatistikleri göster
/active - Aktif sinyalleri göster

🧹 **Temizleme Komutları:**
/clearall - Tüm verileri temizle (pozisyonlar, önceki sinyaller, bekleyen kuyruklar, istatistikler)
/reducecooldowns - Cooldown sürelerini %95 kısalt
        """
    
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
        await update.message.reply_text(help_text, parse_mode='Markdown')

async def stats_command(update, context):
    if not update.effective_user:
        return
    
    user_id = update.effective_user.id
    
    if not is_admin(user_id):
        return 
    
    # Önce veritabanından stats'ı yükle
    stats = load_stats_from_db() or global_stats
    
    # Güncel aktif sinyal sayısını al (active_signals'dan)
    current_active_signals = load_active_signals_from_db() or {}
    current_active_count = len(current_active_signals)
    
    if not stats:
        stats_text = "📊 **Bot İstatistikleri:**\n\nHenüz istatistik verisi yok."
    else:
        closed_count = stats.get('successful_signals', 0) + stats.get('failed_signals', 0)
        success_rate = 0
        if closed_count > 0:
            success_rate = (stats.get('successful_signals', 0) / closed_count) * 100
        
        # Güncel aktif sinyal sayısını kullan
        computed_total = (
            stats.get('successful_signals', 0)
            + stats.get('failed_signals', 0)
            + current_active_count
        )
        
        status_emoji = "🟢"
        status_text = "Aktif (Sinyal Arama Çalışıyor)"
        stats_text = f"""📊 **Bot İstatistikleri:**

📈 **Genel Durum:**
• Toplam Sinyal: {computed_total}
• Başarılı: {stats.get('successful_signals', 0)}
• Başarısız: {stats.get('failed_signals', 0)}
• Aktif Sinyal: {current_active_count}
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
    
    if user_id != BOT_OWNER_ID and user_id not in ALLOWED_USERS and user_id not in ADMIN_USERS:
        return  # İzin verilmeyen kullanıcılar için hiçbir yanıt verme
    
    active_signals = load_active_signals_from_db() or global_active_signals
    if not active_signals:
        active_text = "📈 **Aktif Sinyaller:**\n\nHenüz aktif sinyal yok."
    else:
        active_text = "📈 **Aktif Sinyaller:**\n\n"
        for symbol, signal in active_signals.items():
            # Tarih formatını düzelt
            try:
                if isinstance(signal['signal_time'], str):
                    # Eğer zaten string ise, datetime objesine çevir
                    if '.' in signal['signal_time']:  # Mikrosaniye varsa
                        signal_time = datetime.strptime(signal['signal_time'], '%Y-%m-%d %H:%M:%S.%f')
                    else:
                        signal_time = datetime.strptime(signal['signal_time'], '%Y-%m-%d %H:%M:%S')
                    formatted_time = signal_time.strftime('%Y-%m-%d %H:%M')
                else:
                    formatted_time = signal['signal_time'].strftime('%Y-%m-%d %H:%M')
            except:
                formatted_time = str(signal['signal_time'])
            
            active_text += f"""🔹 **{symbol}** ({signal['type']})
• Giriş: {signal['entry_price']}
• Hedef: {signal['target_price']}
• Stop: {signal['stop_loss']}
• Şu anki: {signal['current_price']}
• Kaldıraç: {signal['leverage']}x
• Sinyal: {formatted_time}

"""
    
    await update.message.reply_text(active_text, parse_mode='Markdown')

async def error_handler(update, context):
    """Hata handler'ı"""
    error = context.error
    
    # CancelledError'ları görmezden gel (bot kapatılırken normal)
    if isinstance(error, asyncio.CancelledError):
        print("ℹ️ Bot kapatılırken task iptal edildi (normal durum)")
        return
    
    if "Conflict" in str(error) and "getUpdates" in str(error):
        print("⚠️ Conflict hatası tespit edildi. Bot yeniden başlatılıyor...")
        try:
            # Webhook'ları temizle
            await app.bot.delete_webhook(drop_pending_updates=True)
            print("✅ Webhook'lar temizlendi")
            await asyncio.sleep(5)
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

async def handle_all_messages(update, context):
    """Tüm mesajları dinler ve kanal olaylarını yakalar"""
    try:
        chat = update.effective_chat
        if not chat:
            return
        
        # Eğer bu bir kanal mesajıysa
        if chat.type == "channel":
            if chat.id not in BOT_OWNER_GROUPS:
                print(f"📢 Kanal mesajı alındı: {chat.title} ({chat.id})")
                BOT_OWNER_GROUPS.add(chat.id)
                print(f"✅ Kanal eklendi: {chat.title} ({chat.id})")
                save_admin_groups()
            return
        
        # Eğer bu bir grup mesajıysa ve bot ekleme olayıysa
        elif chat.type in ["group", "supergroup"] and update.message and update.message.new_chat_members:
            for new_member in update.message.new_chat_members:
                if new_member.id == context.bot.id:
                    print(f"🔍 Bot grup ekleme olayı: {chat.title} ({chat.id})")
        
        elif chat.type == "private" and update.effective_user:
            user_id = update.effective_user.id
            if user_id == BOT_OWNER_ID:
                print(f"🔍 Bot sahibi mesajı: {update.message.text if update.message else 'N/A'}")
    
    except Exception as e:
        print(f"🔍 handle_all_messages Hatası: {e}")
    return

async def handle_chat_member_update(update, context):
    """Grup ve kanal ekleme/çıkarma olaylarını dinler"""
    chat = update.effective_chat
    
    # Yeni üye eklenme durumu
    if update.message and update.message.new_chat_members:
        for new_member in update.message.new_chat_members:
            if new_member.id == context.bot.id:
                if not update.effective_user:
                    return
                
                user_id = update.effective_user.id
                print(f"🔍 Bot ekleme: chat_type={chat.type}, user_id={user_id}, BOT_OWNER_ID={BOT_OWNER_ID}")
                
                if user_id != BOT_OWNER_ID:
                    try:
                        await context.bot.leave_chat(chat.id)
                        chat_type = "kanalından" if chat.type == "channel" else "grubundan"
                        print(f"❌ Bot sahibi olmayan {user_id} tarafından {chat.title} {chat_type.replace('ndan', 'na')} eklenmeye çalışıldı. Bot {chat_type} çıktı.")
                    except Exception as e:
                        print(f"Gruptan/kanaldan çıkma hatası: {e}")
                else:
                    BOT_OWNER_GROUPS.add(chat.id)
                    chat_type = "kanalına" if chat.type == "channel" else "grubuna"
                    print(f"✅ Bot sahibi tarafından {chat.title} {chat_type} eklendi. Chat ID: {chat.id}")
                    print(f"🔍 BOT_OWNER_GROUPS güncellendi: {BOT_OWNER_GROUPS}")
                    
                    save_admin_groups()
    
    # Üye çıkma durumu
    elif update.message and update.message.left_chat_member:
        left_member = update.message.left_chat_member
        if left_member.id == context.bot.id:
            if chat.id in BOT_OWNER_GROUPS:
                BOT_OWNER_GROUPS.remove(chat.id)
                chat_type = "kanalından" if chat.type == "channel" else "grubundan"
                print(f"Bot {chat.title} {chat_type} çıkarıldı. Chat ID: {chat.id} izin verilen gruplardan kaldırıldı.")
                save_admin_groups()
            else:
                chat_type = "kanalından" if chat.type == "channel" else "grubundan"
                print(f"Bot {chat.title} {chat_type} çıkarıldı.")

async def setup_bot():
    """Bot handler'larını kur"""
    global app
    app = Application.builder().token(TELEGRAM_TOKEN).build()

    try:
        await app.bot.delete_webhook(drop_pending_updates=True)
        await app.bot.delete_webhook(drop_pending_updates=True)
        await app.bot.get_updates(offset=-1, limit=1)
        print("✅ Mevcut webhook'lar temizlendi ve pending updates silindi")
    except Exception as e:
        print(f"⚠️ Webhook temizleme hatası: {e}")
        print("🔄 Polling moduna geçiliyor...")
    
    # Komut handler'ları
    app.add_handler(CommandHandler("help", help_command))
    app.add_handler(CommandHandler("stats", stats_command))
    app.add_handler(CommandHandler("active", active_command))
    app.add_handler(CommandHandler("clearall", clear_all_command))
    app.add_handler(CommandHandler("reducecooldowns", reduce_cooldowns_command))
    
    # Grup ekleme/çıkarma handler'ı - ChatMemberUpdated event'ini dinle
    app.add_handler(MessageHandler(filters.StatusUpdate.NEW_CHAT_MEMBERS, handle_chat_member_update))
    app.add_handler(MessageHandler(filters.StatusUpdate.LEFT_CHAT_MEMBER, handle_chat_member_update))
    
    # Kanal mesajlarını dinle (kanal ekleme olayları için)
    app.add_handler(MessageHandler(filters.ChatType.CHANNEL, handle_all_messages))
    app.add_error_handler(error_handler)
    
    print("Bot handler'ları kuruldu!")

def format_price(price, ref_price=None):
    if ref_price is not None:
        s = str(ref_price)
        if 'e' in s or 'E' in s:
            s = f"{ref_price:.20f}".rstrip('0').rstrip('.')
        if '.' in s:
            dec = len(s.split('.')[-1])
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
    
    timeframes = ['15m', '30m', '1h', '2h', '4h', '8h', '1d']
    signal_values = []
    
    for tf in timeframes:
        signal_values.append(all_timeframes_signals.get(tf, 0))
    
    buy_signals = sum(1 for s in signal_values if s == 1)
    sell_signals = sum(1 for s in signal_values if s == -1)
    
    # BTC ve ETH için 5/7 kuralı, diğerleri için 7/7 kuralı
    is_major_coin = symbol in ['BTCUSDT', 'ETHUSDT']
    
    if is_major_coin:
        # BTC/ETH için 5/7 kuralı kontrol - yeni mantık
        # 7/7 kuralı - tüm zaman dilimleri aynı olmalı
        if buy_signals == 7 and sell_signals == 0:
            sinyal_tipi = "🟢 LONG SİNYALİ 🟢"
            dominant_signal = "LONG"
        elif sell_signals == 7 and buy_signals == 0:
            sinyal_tipi = "🔴 SHORT SİNYALİ 🔴"
            dominant_signal = "SHORT"
        # 6/7 kuralı - 6 timeframe aynı olmalı
        elif buy_signals == 6 and sell_signals == 1:
            sinyal_tipi = "🟢 LONG SİNYALİ 🟢"
            dominant_signal = "LONG"
        elif sell_signals == 6 and buy_signals == 1:
            sinyal_tipi = "🔴 SHORT SİNYALİ 🔴"
            dominant_signal = "SHORT"
        # 5/7 kuralı - 5 timeframe aynı olmalı
        elif buy_signals == 5 and sell_signals == 2:
            sinyal_tipi = "🟢 LONG SİNYALİ 🟢"
            dominant_signal = "LONG"
        elif sell_signals == 5 and buy_signals == 2:
            sinyal_tipi = "🔴 SHORT SİNYALİ 🔴"
            dominant_signal = "SHORT"
        else:
            print(f"❌ {symbol} → 5/7 kuralı sağlanamadı: LONG={buy_signals}, SHORT={sell_signals}")
            return None, None, None, None, None, None, None
    else:
        # Diğer kriptolar için 7/7 kuralı
        required_signals = 7
        if buy_signals != required_signals and sell_signals != required_signals:
            print(f"❌ {symbol} → 7/7 kuralı sağlanamadı: LONG={buy_signals}, SHORT={sell_signals}")
            return None, None, None, None, None, None, None
        
        if buy_signals == required_signals and sell_signals == 0:
            sinyal_tipi = "🟢 LONG SİNYALİ 🟢"
            dominant_signal = "LONG"
        elif sell_signals == required_signals and buy_signals == 0:
            sinyal_tipi = "🔴 SHORT SİNYALİ 🔴"
            dominant_signal = "SHORT"
        else:
            print(f"❌ {symbol} → 7/7 kuralı sağlanamadı: LONG={buy_signals}, SHORT={sell_signals}")
            return None, None, None, None, None, None, None
    
    # Hedef fiyat ve stop loss hesaplama
    if dominant_signal == "LONG":
        target_price = price * (1 + profit_percent / 100)  # Örnek: 100 × 1.02 = 102 (yukarı)
        stop_loss = price * (1 - stop_percent / 100)       # Örnek: 100 × 0.985 = 98.5 (aşağı)
        
        # Debug: Hedef fiyat hesaplamasını kontrol et
        print(f"🔍 DEBUG: {symbol} hedef fiyat hesaplaması:")
        print(f"   Giriş fiyatı: {price}")
        print(f"   Profit yüzde: {profit_percent}%")
        print(f"   Hesaplama: {price} × (1 + {profit_percent}/100) = {price} × {1 + profit_percent/100}")
        print(f"   Hedef fiyat: {target_price}")
        print(f"   Hedef fiyat formatlanmış: {format_price(target_price, price)}")
        
        # Hedef fiyat kontrolü - giriş fiyatından büyük olmalı
        if target_price <= price:
            print(f"❌ HATA: {symbol} hedef fiyat ({target_price}) giriş fiyatından ({price}) büyük olmalı!")
            target_price = price * 1.02  # Zorla %2 artış
            print(f"   Düzeltildi: Hedef fiyat = {target_price}")
    else:  # SHORT
        target_price = price * (1 - profit_percent / 100)  # Örnek: 100 × 0.98 = 98 (aşağı)
        stop_loss = price * (1 + stop_percent / 100)       # Örnek: 100 × 1.015 = 101.5 (yukarı)
        
        # Hedef fiyat kontrolü - giriş fiyatından küçük olmalı
        if target_price >= price:
            print(f"❌ HATA: {symbol} hedef fiyat ({target_price}) giriş fiyatından ({price}) küçük olmalı!")
            target_price = price * 0.98  # Zorla %2 azalış
            print(f"   Düzeltildi: Hedef fiyat = {target_price}")
    
    leverage = 10 
    
    print(f"🧮 TEST HESAPLAMA KONTROLÜ:")
    print(f"   Giriş: ${price:.6f}")
    if dominant_signal == "LONG":
        print(f"   Hedef: ${price:.6f} + %{profit_percent} = ${target_price:.6f} (yukarı)")
        print(f"   Stop: ${price:.6f} - %{stop_percent} = ${stop_loss:.6f} (aşağı)")
    else:  # SHORT
        print(f"   Hedef: ${price:.6f} - %{profit_percent} = ${target_price:.6f} (aşağı)")
        print(f"   Stop: ${price:.6f} + %{stop_percent} = ${stop_loss:.6f} (yukarı)")
    print(f"   Hedef Fark: ${(target_price - price):.6f} (%{((target_price - price) / price * 100):.2f})")
    print(f"   Stop Fark: ${(price - stop_loss):.6f} (%{((price - stop_loss) / price * 100):.2f})")

    leverage_reason = ""
    
    # Kaldıraç hesaplama
    if is_major_coin:
        # BTC/ETH için kaldıraç - yeni mantık
        if buy_signals == 7 or sell_signals == 7:
            leverage = 10
            print(f"{symbol} - 7/7 sinyal (10x kaldıraç)")
        else:
            # 6/6 veya 5/5 kuralı için kaldıraç hesapla
            tf_6h = ['15m', '30m', '1h', '2h', '4h', '8h']
            buy_count_6h = sum(1 for tf in tf_6h if all_timeframes_signals.get(tf, 0) == 1)
            sell_count_6h = sum(1 for tf in tf_6h if all_timeframes_signals.get(tf, 0) == -1)
            
            if buy_count_6h == 6 or sell_count_6h == 6:
                leverage = 10
                print(f"{symbol} - 6/7 sinyal (10x kaldıraç)")
            else:
                # 5/7 kuralı kontrol
                tf_5h = ['15m', '30m', '1h', '2h', '4h']
                buy_count_5h = sum(1 for tf in tf_5h if all_timeframes_signals.get(tf, 0) == 1)
                sell_count_5h = sum(1 for tf in tf_5h if all_timeframes_signals.get(tf, 0) == -1)
                
                if buy_count_5h == 5 or sell_count_5h == 5:
                    leverage = 10
                    print(f"{symbol} - 5/7 sinyal (10x kaldıraç)")
                else:
                    # 5/7 kuralı sağlanamadı, sinyal verilmemeli
                    print(f"{symbol} - 5/7 kuralı sağlanamadı, sinyal verilmiyor")
                    return None, None, None, None, None, None, None
    else:
        # Diğer kriptolar için 7/7 kuralı: Tüm 7 zaman dilimi aynıysa 10x kaldıraçlı
        # Bu noktaya gelindiyse, 7/7 kuralı sağlanmıştır, aksi halde None dönülmüştür.
        leverage = 10
        print(f"{symbol} - 7/7 sinyal (10x kaldıraç)")
    
    target_price_str = format_price(target_price, price)
    stop_loss_str = format_price(stop_loss, price)
    volume_formatted = format_volume(volume)
    
    message = f"""
{sinyal_tipi}

🔹 Kripto Çifti: {symbol}  
💵 Giriş Fiyatı: {price_str}
📈 Hedef Fiyat: {target_price_str}  
📉 Stop Loss: {stop_loss_str}
⚡ Kaldıraç: {leverage}x
📊 24h Hacim: {volume_formatted}

⚠️ <b>ÖNEMLİ UYARILAR:</b>
• Bu paylaşım yatırım tavsiyesi değildir.
• Riskinizi azaltmak için sermayenizin %2'sinden fazlasını tek işlemde kullanmayın.
• Stop-loss kullanmadan işlem yapmayın.

📺 <b>Kanallar:</b>
🔗 <a href="https://www.youtube.com/@kriptotek">YouTube</a> | <a href="https://t.me/kriptotek8907">Telegram</a> | <a href="https://x.com/kriptotek8907">X</a> | <a href="https://www.instagram.com/kriptotek/">Instagram</a>"""

    return message, dominant_signal, target_price, stop_loss, stop_loss_str, leverage, None

async def async_get_historical_data(symbol, interval, lookback):
    """Binance Futures'den geçmiş verileri asenkron çek"""
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

async def fetch_futures_exchange_info():
    """Non-blocking fetch of Binance Futures exchangeInfo."""
    url = "https://fapi.binance.com/fapi/v1/exchangeInfo"
    connector = aiohttp.TCPConnector(limit=20)
    timeout = aiohttp.ClientTimeout(total=20)
    async with aiohttp.ClientSession(connector=connector, timeout=timeout) as session:
        return await api_request_with_retry(session, url, ssl=False)

async def fetch_futures_24h(symbol=None):
    """Non-blocking fetch of 24h stats. If symbol is None, returns list for all symbols."""
    base = "https://fapi.binance.com/fapi/v1/ticker/24hr"
    url = f"{base}?symbol={symbol}" if symbol else base
    connector = aiohttp.TCPConnector(limit=40)
    timeout = aiohttp.ClientTimeout(total=20)
    async with aiohttp.ClientSession(connector=connector, timeout=timeout) as session:
        return await api_request_with_retry(session, url, ssl=False)

def calculate_full_pine_signals(df, timeframe):
    is_higher_tf = timeframe in ['1d', '4h', '1w']
    is_weekly = timeframe == '1w'
    is_daily = timeframe == '1d' 
    is_4h = timeframe == '4h'
    is_2h = timeframe == '2h'
    is_1h = timeframe == '1h'
    is_30m = timeframe == '30m'
    is_15m = timeframe == '15m'

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

    df['rsi'] = ta.momentum.RSIIndicator(df['close'], window=rsi_length).rsi()

    macd = ta.trend.MACD(df['close'], window_slow=macd_slow, window_fast=macd_fast, window_sign=macd_signal)
    df['macd'] = macd.macd()
    df['macd_signal'] = macd.macd_signal()

    def supertrend_dynamic(df, atr_period, timeframe):
        hl2 = (df['high'] + df['low']) / 2
        atr = ta.volatility.AverageTrueRange(df['high'], df['low'], df['close'], window=atr_period).average_true_range()
        atr_dynamic = atr.rolling(window=5).mean()  # SMA(ATR, 5)
        
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
    df['short_ma'] = ta.trend.EMAIndicator(df['close'], window=short_ma_period).ema_indicator()
    df['long_ma'] = ta.trend.EMAIndicator(df['close'], window=long_ma_period).ema_indicator()
    df['ma_bullish'] = df['short_ma'] > df['long_ma']
    df['ma_bearish'] = df['short_ma'] < df['long_ma']

    volume_ma_period = 20
    df['volume_ma'] = df['volume'].rolling(window=volume_ma_period).mean()
    df['enough_volume'] = df['volume'] > df['volume_ma'] * volume_multiplier

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
    
    money_ratio = positive_flow_sum / (negative_flow_sum + 1e-10) 
    df['mfi'] = 100 - (100 / (1 + money_ratio))
    df['mfi_bullish'] = df['mfi'] < 65
    df['mfi_bearish'] = df['mfi'] > 35

    highest_high = df['high'].rolling(window=fib_lookback).max()
    lowest_low = df['low'].rolling(window=fib_lookback).min()
    fib_level1 = highest_high * 0.618
    fib_level2 = lowest_low * 1.382
    df['fib_in_range'] = (df['close'] > fib_level1) & (df['close'] < fib_level2)

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

    for i in range(len(df)):
        if df['signal'].iloc[i] == 0:
            if i > 0:
                df.at[df.index[i], 'signal'] = df['signal'].iloc[i-1]
            else:
                if df['macd'].iloc[i] > df['macd_signal'].iloc[i]:
                    df.at[df.index[i], 'signal'] = 1
                else:
                    df.at[df.index[i], 'signal'] = -1
    return df

async def get_active_high_volume_usdt_pairs(top_n=50, stop_cooldown=None):
    futures_exchange_info = await fetch_futures_exchange_info()
    
    futures_usdt_pairs = set()
    for symbol in futures_exchange_info['symbols']:
        if (
            symbol['quoteAsset'] == 'USDT' and
            symbol['status'] == 'TRADING' and
            symbol['contractType'] == 'PERPETUAL'
        ):
            futures_usdt_pairs.add(symbol['symbol'])

    # Sadece USDT çiftlerinin ticker'larını al
    futures_tickers = await fetch_futures_24h()
    
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

    # Sadece ilk 120 sembolü al (hacme göre sıralanmış) - 100'e ulaşmak için
    high_volume_pairs = high_volume_pairs[:120]

    uygun_pairs = []
    idx = 0
    while len(uygun_pairs) < top_n and idx < len(high_volume_pairs):
        symbol, volume = high_volume_pairs[idx]
        
        # COOLDOWN KONTROLÜ: Eğer stop_cooldown verilmişse, cooldown'daki sembolleri filtrele
        if stop_cooldown and check_cooldown(symbol, stop_cooldown, 4):
            idx += 1
            continue
            
        try:
            df_1d = await async_get_historical_data(symbol, '1d', 30)
            if len(df_1d) < 30:
                idx += 1
                continue
            uygun_pairs.append(symbol)
        except Exception as e:
            idx += 1
            continue
        idx += 1

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
async def check_signal_potential(symbol, positions, stop_cooldown, timeframes, tf_names, previous_signals):
    if symbol in positions:
        print(f"⏸️ {symbol} → Zaten aktif pozisyon var, yeni sinyal aranmıyor")
        return None
    
    # Stop cooldown kontrolü (4 saat)
    if check_cooldown(symbol, stop_cooldown, 4):
        # check_cooldown fonksiyonu zaten detaylı mesaj yazdırıyor
        return None

    try:
        # 1 günlük veri al - 1d timeframe için gerekli
        df_1d = await async_get_historical_data(symbol, timeframes['1d'], 30)
        if df_1d is None or df_1d.empty:
            return None

        current_signals = await calculate_signals_for_symbol(symbol, timeframes, tf_names)
        if current_signals is None:
            return None
        
        buy_count, sell_count = calculate_signal_counts(current_signals, tf_names, symbol)
        
        # BTC ve ETH için 5/7 kuralı, diğerleri için 7/7 kuralı kontrol
        is_major_coin = symbol in ['BTCUSDT', 'ETHUSDT']
        
        if is_major_coin:
            if not check_major_coin_signal_rule(symbol, current_signals, previous_signals.get(symbol, {})):
                previous_signals[symbol] = current_signals.copy()
                return None
            
            # BTC/ETH için sinyal türünü belirle - check_major_coin_signal_rule zaten kuralı kontrol etti
            # Bu noktaya gelindiyse kural sağlanmıştır, spesifik zaman dilimlerini kontrol et
            
            # 7/7 kuralı kontrol - tüm 7 zaman dilimi aynı olmalı
            if buy_count == 7 and sell_count == 0:
                sinyal_tipi = 'ALIŞ'
                dominant_signal = "ALIŞ"
                print(f"✅ {symbol} → ALIŞ sinyali belirlendi (7/7 kuralı)")
            elif sell_count == 7 and buy_count == 0:
                sinyal_tipi = 'SATIŞ'
                dominant_signal = "SATIŞ"
                print(f"✅ {symbol} → SATIŞ sinyali belirlendi (7/7 kuralı)")
            else:
                # 6/7 kuralı kontrol - 15dk, 30dk, 1h, 2h, 4h, 8h aynı olmalı
                tf_6h = ['15m', '30m', '1h', '2h', '4h', '8h']
                buy_count_6h = sum(1 for tf in tf_6h if current_signals.get(tf, 0) == 1)
                sell_count_6h = sum(1 for tf in tf_6h if current_signals.get(tf, 0) == -1)
                
                if buy_count_6h == 6 and sell_count_6h == 0:
                    sinyal_tipi = 'ALIŞ'
                    dominant_signal = "ALIŞ"
                    print(f"✅ {symbol} → ALIŞ sinyali belirlendi (6/7 kuralı - 15dk,30dk,1h,2h,4h,8h)")
                elif sell_count_6h == 6 and buy_count_6h == 0:
                    sinyal_tipi = 'SATIŞ'
                    dominant_signal = "SATIŞ"
                    print(f"✅ {symbol} → SATIŞ sinyali belirlendi (6/7 kuralı - 15dk,30dk,1h,2h,4h,8h)")
                else:
                    # 5/7 kuralı kontrol - 15dk, 30dk, 1h, 2h, 4h aynı olmalı
                    tf_5h = ['15m', '30m', '1h', '2h', '4h']
                    buy_count_5h = sum(1 for tf in tf_5h if current_signals.get(tf, 0) == 1)
                    sell_count_5h = sum(1 for tf in tf_5h if current_signals.get(tf, 0) == -1)
                    
                    if buy_count_5h == 5 and sell_count_5h == 0:
                        sinyal_tipi = 'ALIŞ'
                        dominant_signal = "ALIŞ"
                        print(f"✅ {symbol} → ALIŞ sinyali belirlendi (5/7 kuralı - 15dk,30dk,1h,2h,4h)")
                    elif sell_count_5h == 5 and buy_count_5h == 0:
                        sinyal_tipi = 'SATIŞ'
                        dominant_signal = "SATIŞ"
                        print(f"✅ {symbol} → SATIŞ sinyali belirlendi (5/7 kuralı - 15dk,30dk,1h,2h,4h)")
                    else:
                        # Bu durumda check_major_coin_signal_rule False dönmeli, buraya gelmemeli
                        print(f"❌ {symbol} → Beklenmeyen durum: LONG={buy_count}, SHORT={sell_count}")
                        return None
        else:
            # Diğer kriptolar için 7/7 kuralı
            required_signals = 7
            if not check_signal_rule(buy_count, sell_count, required_signals, symbol):
                previous_signals[symbol] = current_signals.copy()
                return None
            
            # Diğer kriptolar için sinyal türünü belirle
            if buy_count == 7 and sell_count == 0:
                sinyal_tipi = 'ALIŞ'
                dominant_signal = "ALIŞ"
                print(f"✅ {symbol} → ALIŞ sinyali belirlendi (7/7 kuralı)")
            elif sell_count == 7 and buy_count == 0:
                sinyal_tipi = 'SATIŞ'
                dominant_signal = "SATIŞ"
                print(f"✅ {symbol} → SATIŞ sinyali belirlendi (7/7 kuralı)")
            else:
                print(f"❌ {symbol} → 7/7 kuralı sağlanamadı: LONG={buy_count}, SHORT={sell_count}")
                return None
        
        rule_text = "5/7" if is_major_coin else "7/7"
        print(f"✅ {symbol} → {rule_text} kuralı sağlandı! LONG={buy_count}, SHORT={sell_count}")
        print(f"   Detay: {current_signals}")
        
        # 15 dakikalık mum rengi kontrolü - sadece BTC/ETH olmayan kriptolar için
        if not is_major_coin:
            print(f"🔍 {symbol} → 15 dakikalık mum rengi kontrol ediliyor...")
            try:
                df_15m = await async_get_historical_data(symbol, '15m', 1)
                if df_15m is not None and not df_15m.empty:
                    last_candle = df_15m.iloc[-1]
                    open_price = float(last_candle['open'])
                    close_price = float(last_candle['close'])
                    
                    # Mum rengini belirle (yeşil = close > open, kırmızı = close < open)
                    is_green_candle = close_price > open_price
                    is_red_candle = close_price < open_price
                    
                    print(f"🔍 {symbol} → 15m mum: Açılış=${open_price:.6f}, Kapanış=${close_price:.6f}")
                    print(f"🔍 {symbol} → 15m mum rengi: {'🟢 Yeşil' if is_green_candle else '🔴 Kırmızı' if is_red_candle else '⚪ Doğru'}")
                    
                    # Sinyal türü ile mum rengi uyumluluğunu kontrol et
                    if sinyal_tipi == 'ALIŞ' and not is_green_candle:
                        print(f"⚠️ {symbol} → ALIŞ sinyali için 15m mum yeşil değil, sinyal erteleniyor...")
                        print(f"   Beklenen: 🟢 Yeşil mum, Mevcut: {'🔴 Kırmızı' if is_red_candle else '⚪ Doğru'} mum")
                        return None  # Sinyal erteleniyor, sonraki kontrolde tekrar bakılacak
                        
                    elif sinyal_tipi == 'SATIŞ' and not is_red_candle:
                        print(f"⚠️ {symbol} → SATIŞ sinyali için 15m mum kırmızı değil, sinyal erteleniyor...")
                        print(f"   Beklenen: 🔴 Kırmızı mum, Mevcut: {'🟢 Yeşil' if is_green_candle else '⚪ Doğru'} mum")
                        return None  # Sinyal erteleniyor, sonraki kontrolde tekrar bakılacak
                    
                    print(f"✅ {symbol} → 15m mum rengi uygun! Sinyal veriliyor...")
                    
                else:
                    print(f"⚠️ {symbol} → 15m mum verisi alınamadı, sinyal veriliyor (veri eksikliği)")
                    
            except Exception as e:
                print(f"⚠️ {symbol} → 15m mum kontrolünde hata: {e}, sinyal veriliyor (hata durumu)")
        else:
            # BTC/ETH için 15m mum kontrolü yapılmıyor - sinyal hemen veriliyor
            print(f"🔍 {symbol} → Major coin (BTC/ETH) - 15m mum kontrolü atlanıyor, sinyal hemen veriliyor")
        
        # Fiyat ve hacim bilgilerini al
        try:
            ticker_data = await fetch_futures_24h(symbol)
            
            # API bazen liste döndürüyor, bazen dict
            if isinstance(ticker_data, list):
                if len(ticker_data) == 0:
                    print(f"❌ {symbol} → Ticker verisi boş liste, sinyal iptal edildi")
                    return None
                ticker = ticker_data[0]  # İlk elementi al
            else:
                ticker = ticker_data
            
            if not ticker or not isinstance(ticker, dict):
                print(f"❌ {symbol} → Ticker verisi eksik veya hatalı format, sinyal iptal edildi")
                print(f"   Ticker: {ticker}")
                return None  # Sinyal iptal edildi
            
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
    
    # ETH için özel debug log
    if symbol == 'ETHUSDT':
        print(f"🔍 ETHUSDT → process_selected_signal başladı")
        print(f"   Price: {price}, Volume: {volume_usd}, Signal Type: {sinyal_tipi}")
        print(f"   Current signals: {current_signals}")
    
    # Aktif pozisyon kontrolü - eğer zaten aktif pozisyon varsa yeni sinyal gönderme
    if symbol in positions:
        print(f"⏸️ {symbol} → Zaten aktif pozisyon var, yeni sinyal gönderilmiyor")
        return
    
    # Aktif sinyal kontrolü - eğer zaten aktif sinyal varsa yeni sinyal gönderme
    if symbol in active_signals:
        print(f"⏸️ {symbol} → Zaten aktif sinyal var, yeni sinyal gönderilmiyor")
        return
    
    try:
        # ETH/BTC için özel debug log
        if symbol in ['ETHUSDT', 'BTCUSDT']:
            print(f"🔍 {symbol} → create_signal_message_new_55 çağrılıyor...")
            print(f"   Price: {price}, Volume: {volume_usd}")
            print(f"   Current signals: {current_signals}")
        
        # Mesaj oluştur ve gönder
        message, dominant_signal, target_price, stop_loss, stop_loss_str, leverage, _ = create_signal_message_new_55(symbol, price, current_signals, volume_usd, 2.0, 1.5)
        
        # ETH/BTC için özel debug log
        if symbol in ['ETHUSDT', 'BTCUSDT']:
            print(f"🔍 {symbol} → create_signal_message_new_55 sonucu:")
            print(f"   Message: {'✅ Var' if message else '❌ Yok'}")
            print(f"   Dominant signal: {dominant_signal}")
            print(f"   Target price: {target_price}")
            print(f"   Stop loss: {stop_loss}")
            print(f"   Leverage: {leverage}")
        
        if message:
            try:
                entry_price_float = float(price) if price is not None else 0.0
                target_price_float = float(target_price) if target_price is not None else 0.0
                stop_loss_float = float(stop_loss) if stop_loss is not None else 0.0
                leverage_int = int(leverage) if leverage is not None else 10
                
                # Geçerlilik kontrolü
                if entry_price_float <= 0 or target_price_float <= 0 or stop_loss_float <= 0:
                    print(f"⚠️ {symbol} - Geçersiz pozisyon verileri, pozisyon oluşturulmuyor")
                    print(f"   Giriş: {entry_price_float}, Hedef: {target_price_float}, Stop: {stop_loss_float}")
                    return
                
            except (ValueError, TypeError) as e:
                print(f"❌ {symbol} - Fiyat verisi dönüşüm hatası: {e}")
                print(f"   Raw values: price={price}, target={target_price}, stop={stop_loss}")
                return
            
            # Pozisyonu kaydet - DOMINANT_SIGNAL KULLAN VE TÜM DEĞERLER FLOAT OLARAK
            position = {
                "type": str(dominant_signal),  # dominant_signal kullan, sinyal_tipi değil!
                "target": target_price_float,  # Float olarak kaydet
                "stop": stop_loss_float,       # Float olarak kaydet
                "open_price": entry_price_float,  # Float olarak kaydet
                "stop_str": str(stop_loss_str),
                "signals": current_signals,
                "leverage": leverage_int,      # Int olarak kaydet
                "entry_time": str(datetime.now()),
                "entry_timestamp": datetime.now(),
            }
            
            # Pozisyonu dictionary'ye ekle
            positions[symbol] = position
            
            # Aktif sinyali de oluştur
            active_signals[symbol] = {
                "symbol": symbol,
                "type": dominant_signal,
                "entry_price": format_price(entry_price_float, entry_price_float),
                "entry_price_float": entry_price_float,
                "target_price": format_price(target_price_float, entry_price_float),
                "stop_loss": format_price(stop_loss_float, entry_price_float),
                "signals": current_signals,
                "leverage": leverage_int,
                "signal_time": str(datetime.now()),
                "current_price": format_price(entry_price_float, entry_price_float),
                "current_price_float": entry_price_float,
                "last_update": str(datetime.now()),
                "status": "active"
            }
            
            # Pozisyonu MongoDB'ye kaydet
            save_positions_to_db({symbol: position})
            
            # Aktif sinyali de MongoDB'ye kaydet
            save_active_signals_to_db({symbol: active_signals[symbol]})
            
            # İstatistikleri güncelle
            stats["total_signals"] += 1
            stats["active_signals_count"] = len(positions)  # positions kullan
            
            save_stats_to_db(stats)
            
            await send_signal_to_all_users(message)
            
            leverage_text = "10x" 
            print(f"✅ {symbol} {sinyal_tipi} sinyali gönderildi! Kaldıraç: {leverage_text}")
            
            # Başarılı işlem sonucu döndür
            return True
            
    except Exception as e:
        print(f"❌ {symbol} sinyal gönderme hatası: {e}")
        return False

async def check_existing_positions_and_cooldowns(positions, active_signals, stats, stop_cooldown):
    """Bot başlangıcında mevcut pozisyonları ve cooldown'ları kontrol eder"""
    print("🔍 Mevcut pozisyonlar ve cooldown'lar kontrol ediliyor...")

    # MongoDB'den mevcut pozisyonları yükle
    mongo_positions = load_positions_from_db()
    
    # 1. Aktif pozisyonları kontrol et
    for symbol in list(mongo_positions.keys()):
        try:
            print(f"🔍 {symbol} pozisyonu kontrol ediliyor...")
            
            # Pozisyon verilerinin geçerliliğini kontrol et
            position = mongo_positions[symbol]
            if not position or not isinstance(position, dict):
                print(f"⚠️ {symbol} - Geçersiz pozisyon verisi formatı, pozisyon temizleniyor")
                # MongoDB'den sil ama dictionary'den silme
                mongo_collection.delete_one({"_id": f"position_{symbol}"})
                mongo_collection.delete_one({"_id": f"active_signal_{symbol}"})
                continue
            
            # Veriyi hem yeni (data anahtarı) hem de eski yapıdan (doğrudan doküman) almaya çalış
            position_data = position.get('data', position)
            
            # Kritik alanların varlığını kontrol et
            required_fields = ['open_price', 'target', 'stop', 'type']
            missing_fields = [field for field in required_fields if field not in position_data]
            
            if missing_fields:
                print(f"⚠️ {symbol} - Eksik alanlar: {missing_fields}, pozisyon temizleniyor")
                # MongoDB'den sil ama dictionary'den silme
                mongo_collection.delete_one({"_id": f"position_{symbol}"})
                mongo_collection.delete_one({"_id": f"active_signal_{symbol}"})
                continue
            
            # Fiyat değerlerinin geçerliliğini kontrol et
            try:
                entry_price = float(position_data["open_price"])
                target_price = float(position_data["target"])
                stop_loss = float(position_data["stop"])
                signal_type = position_data["type"]
                
                if entry_price <= 0 or target_price <= 0 or stop_loss <= 0:
                    print(f"⚠️ {symbol} - Geçersiz pozisyon verileri, pozisyon temizleniyor")
                    print(f"   Giriş: {entry_price}, Hedef: {target_price}, Stop: {stop_loss}")
                    # MongoDB'den sil ama dictionary'den silme
                    mongo_collection.delete_one({"_id": f"position_{symbol}"})
                    mongo_collection.delete_one({"_id": f"active_signal_{symbol}"})
                    continue
                    
            except (ValueError, TypeError) as e:
                print(f"⚠️ {symbol} - Fiyat dönüşüm hatası: {e}, pozisyon temizleniyor")
                # MongoDB'den sil ama dictionary'den silme
                mongo_collection.delete_one({"_id": f"position_{symbol}"})
                mongo_collection.delete_one({"_id": f"active_signal_{symbol}"})
                continue
            
            # Güncel fiyat bilgisini al
            df1m = await async_get_historical_data(symbol, '1m', 1)
            if df1m is None or df1m.empty:
                continue
            
            close_price = float(df1m['close'].iloc[-1])
            
            if signal_type == "LONG" or signal_type == "ALIS":
                min_target_diff = target_price * 0.001 
                if close_price >= target_price and (close_price - target_price) >= min_target_diff:
                    print(f"🎯 {symbol} HEDEF GERÇEKLEŞTİ!")
                    
                    # Hedef mesajını gönder (yeşil indikatör ile) - Hedef fiyatından çıkış
                    profit_percentage = ((target_price - entry_price) / entry_price) * 100 if entry_price > 0 else 0
                    profit_usd = 100 * (profit_percentage / 100) if entry_price > 0 else 0
                    
                    target_message = f"""🎯 <b>HEDEF GERÇEKLEŞTİ!</b> 🎯

🔹 <b>Kripto Çifti:</b> {symbol}
💰 <b>Kar:</b> %{profit_percentage:.2f} (${profit_usd:.2f})
📈 <b>Giriş:</b> ${entry_price:.6f}
💵 <b>Çıkış:</b> ${target_price:.6f}"""
                    
                    # Pozisyonu kapat ve mesajı gönder
                    await close_position(symbol, "take_profit", target_price, None, position)
                    print(f"📢 Hedef mesajı close_position() tarafından gönderildi")
                    
                    # İstatistikleri güncelle
                    stats["successful_signals"] += 1
                    # Güvenli kâr hesaplaması
                    if entry_price > 0:
                        profit_percentage = ((target_price - entry_price) / entry_price) * 100
                        profit_usd = 100 * (profit_percentage / 100)
                    else:
                        profit_percentage = 0
                        profit_usd = 0
                    stats["total_profit_loss"] += profit_usd
                    
                    # Cooldown'a ekle (8 saat) - Güvenli ekleme
                    cooldown_end_time = add_stop_cooldown_safe(symbol, stop_cooldown)
                    print(f"🔒 {symbol} → HEDEF GERÇEKLEŞTİ! Cooldown bitiş: {cooldown_end_time.strftime('%H:%M:%S')}")
                    save_stop_cooldown_to_db(stop_cooldown)
                    
                    # Pozisyon ve aktif sinyali kaldır
                    del positions[symbol]
                    if symbol in active_signals:
                        del active_signals[symbol]
                    
                    # Veritabanı kayıtlarını kontrol et
                    positions_saved = save_positions_to_db(positions)
                    active_signals_saved = save_active_signals_to_db(active_signals)
                    
                    if not positions_saved or not active_signals_saved:
                        print(f"⚠️ {symbol} veritabanı kaydı başarısız! Pozisyon: {positions_saved}, Aktif Sinyal: {active_signals_saved}")
                        # Hata durumunda tekrar dene
                        await asyncio.sleep(1)
                        positions_saved = save_positions_to_db(positions)
                        active_signals_saved = save_active_signals_to_db(active_signals)
                        if not positions_saved or not active_signals_saved:
                            print(f"❌ {symbol} veritabanı kaydı ikinci denemede de başarısız!")
                    else:
                        print(f"✅ {symbol} veritabanından başarıyla kaldırıldı")
                    print(f"✅ {symbol} - Bot başlangıcında TP tespit edildi ve işlendi!")
                    
                min_stop_diff = stop_loss * 0.001 
                if close_price <= stop_loss and (stop_loss - close_price) >= min_stop_diff:
                    await close_position(symbol, "stop_loss", close_price, None, position)
                    
                    # İstatistikleri güncelle
                    stats["failed_signals"] += 1
                    # Güvenli zarar hesaplaması
                    if entry_price > 0:
                        loss_percentage = ((entry_price - stop_loss) / entry_price) * 100
                        loss_usd = 100 * (loss_percentage / 100)
                    else:
                        loss_percentage = 0
                        loss_usd = 0
                    stats["total_profit_loss"] -= loss_usd
                    
                    # Cooldown'a ekle (8 saat) - Güvenli ekleme
                    cooldown_end_time = add_stop_cooldown_safe(symbol, stop_cooldown)
                    save_stop_cooldown_to_db(stop_cooldown)
                    
                    # Pozisyon ve aktif sinyali kaldır
                    del positions[symbol]
                    if symbol in active_signals:
                        del active_signals[symbol]
                    
                    # Veritabanı kayıtlarını kontrol et
                    positions_saved = save_positions_to_db(positions)
                    active_signals_saved = save_active_signals_to_db(active_signals)
                    
                    if not positions_saved or not active_signals_saved:
                        print(f"⚠️ {symbol} veritabanı kaydı başarısız! Pozisyon: {positions_saved}, Aktif Sinyal: {active_signals_saved}")
                        # Hata durumunda tekrar dene
                        await asyncio.sleep(1)
                        positions_saved = save_positions_to_db(positions)
                        active_signals_saved = save_active_signals_to_db(active_signals)
                        if not positions_saved or not active_signals_saved:
                            print(f"❌ {symbol} veritabanı kaydı ikinci denemede de başarısız!")
                    else:
                        print(f"✅ {symbol} veritabanından başarıyla kaldırıldı")
                    
                                # SHORT sinyali için hedef/stop kontrolü
                elif signal_type == "SHORT" or signal_type == "SATIS":
                    min_target_diff = target_price * 0.001  # %0.1 minimum fark (daha güvenli)
                    if close_price <= target_price and (target_price - close_price) >= min_target_diff:
                        print(f"🎯 {symbol} SHORT HEDEF GERÇEKLEŞTİ!")
                        
                        # Hedef mesajını gönder (yeşil indikatör ile) - Hedef fiyatından çıkış
                        profit_percentage = ((entry_price - target_price) / entry_price) * 100 if entry_price > 0 else 0
                        profit_usd = 100 * (profit_percentage / 100) if entry_price > 0 else 0
                        
                        target_message = f"""🎯 <b>HEDEF GERÇEKLEŞTİ!</b> 🎯

🔹 <b>Kripto Çifti:</b> {symbol}
💰 <b>Kar:</b> %{profit_percentage:.2f} (${profit_usd:.2f})
📈 <b>Giriş:</b> ${entry_price:.6f}
💵 <b>Çıkış:</b> ${target_price:.6f}"""
                        
                        # Pozisyonu kapat ve mesajı gönder
                        await close_position(symbol, "take_profit", target_price, None, position)
                        print(f"📢 SHORT Hedef mesajı close_position() tarafından gönderildi")
                        
                        stats["successful_signals"] += 1
                        if entry_price > 0:
                            profit_percentage = ((entry_price - target_price) / entry_price) * 100
                            profit_usd = 100 * (profit_percentage / 100)
                        else:
                            profit_percentage = 0
                            profit_usd = 0
                        stats["total_profit_loss"] += profit_usd
                        
                        # Cooldown'a ekle (8 saat) - Güvenli ekleme
                        cooldown_end_time = add_stop_cooldown_safe(symbol, stop_cooldown)
                        print(f"🔒 {symbol} → SHORT HEDEF GERÇEKLEŞTİ! Cooldown bitiş: {cooldown_end_time.strftime('%H:%M:%S')}")
                        save_stop_cooldown_to_db(stop_cooldown)
                        
                        # Pozisyon ve aktif sinyali kaldır
                        del positions[symbol]
                        if symbol in active_signals:
                            del active_signals[symbol]
                        # Veritabanı kayıtlarını kontrol et
                        positions_saved = save_positions_to_db(positions)
                        active_signals_saved = save_active_signals_to_db(active_signals)
                        
                        if not positions_saved or not active_signals_saved:
                            print(f"⚠️ {symbol} veritabanı kaydı kaydı başarısız! Pozisyon: {positions_saved}, Aktif Sinyal: {active_signals_saved}")
                            # Hata durumunda tekrar dene
                            await asyncio.sleep(1)
                            positions_saved = save_positions_to_db(positions)
                            active_signals_saved = save_active_signals_to_db(active_signals)
                            if not positions_saved or not active_signals_saved:
                                print(f"❌ {symbol} veritabanı kaydı ikinci denemede de başarısız!")
                        else:
                            print(f"✅ {symbol} veritabanından başarıyla kaldırıldı")
                        
                        print(f"✅ {symbol} - Bot başlangıcında TP tespit edildi ve işlendi!")
                        
                    # Stop kontrolü: Güncel fiyat stop'u geçti mi? (SHORT: yukarı çıkması zarar)
                    elif close_price >= stop_loss:
                        await close_position(symbol, "stop_loss", close_price, None, position)
                        
                        # İstatistikleri güncelle
                        stats["failed_signals"] += 1
                        # Güvenli zarar hesaplaması
                        if entry_price > 0:
                            loss_percentage = ((stop_loss - entry_price) / entry_price) * 100
                            loss_usd = 100 * (loss_percentage / 100)
                        else:
                            loss_percentage = 0
                            loss_usd = 0
                        stats["total_profit_loss"] -= loss_usd
                        
                        # Cooldown'a ekle (8 saat) - Güvenli ekleme
                        cooldown_end_time = add_stop_cooldown_safe(symbol, stop_cooldown)
                        save_stop_cooldown_to_db(stop_cooldown)
                        
                        del positions[symbol]
                        if symbol in active_signals:
                            del active_signals[symbol]
                        
                        positions_saved = save_positions_to_db(positions)
                        active_signals_saved = save_active_signals_to_db(active_signals)
                        
                        if not positions_saved or not active_signals_saved:
                            print(f"⚠️ {symbol} veritabanı kaydı başarısız! Pozisyon: {positions_saved}, Aktif Sinyal: {active_signals_saved}")
                            # Hata durumunda tekrar dene
                            await asyncio.sleep(1)
                            positions_saved = save_positions_to_db(positions)
                            active_signals_saved = save_active_signals_to_db(active_signals)
                            if not positions_saved or not active_signals_saved:
                                print(f"❌ {symbol} veritabanı kaydı ikinci denemede de başarısız!")
                        else:
                            print(f"✅ {symbol} veritabanından başarıyla kaldırıldı")
                    
        except Exception as e:
            print(f"⚠️ {symbol} pozisyon kontrolü sırasında hata: {e}")
            continue
    
    expired_cooldowns = []
    for symbol, cooldown_until in list(stop_cooldown.items()):
        # cooldown_until artık direkt bitiş zamanı
        if isinstance(cooldown_until, str):
            try:
                cooldown_until = datetime.fromisoformat(cooldown_until.replace('Z', '+00:00'))
                # Tarih geçmişse şimdiki zamanı kullan
                if cooldown_until < datetime.now():
                    print(f"⚠️ Cooldown tarihi geçmiş, şimdiki zaman kullanılıyor")
                    cooldown_until = datetime.now()
            except Exception as e:
                print(f"⚠️ Tarih parse hatası: {e}, şimdiki zaman kullanılıyor")
                cooldown_until = datetime.now()
        elif not isinstance(cooldown_until, datetime):
            cooldown_until = datetime.now()
        
        # Şimdi bitiş zamanından geçmiş mi kontrol et
        if datetime.now() >= cooldown_until:  # Süresi bitti mi?
            expired_cooldowns.append(symbol)
            print(f"✅ {symbol} cooldown süresi doldu, yeni sinyal aranabilir")
    
    # Süresi dolan cooldown'ları kaldır
    for symbol in expired_cooldowns:
        del stop_cooldown[symbol]
    if expired_cooldowns:
        save_stop_cooldown_to_db(stop_cooldown)
        print(f"🧹 {len(expired_cooldowns)} cooldown temizlendi")
    
    stats["active_signals_count"] = len(active_signals)
    save_stats_to_db(stats)
    
    print(f"✅ Bot başlangıcı kontrolü tamamlandı: {len(positions)} pozisyon, {len(active_signals)} aktif sinyal, {len(stop_cooldown)} cooldown")
    print("✅ Bot başlangıcı kontrolü tamamlandı")
async def signal_processing_loop():
    """Sinyal arama ve işleme döngüsü"""
    # Global değişkenleri tanımla
    global global_stats, global_active_signals, global_successful_signals, global_failed_signals, global_allowed_users, global_admin_users, global_positions, global_stop_cooldown, active_signals

    positions = dict()  # {symbol: position_info}
    stop_cooldown = dict()  # {symbol: datetime}
    previous_signals = dict()  # {symbol: {tf: signal}} - İlk çalıştığında kaydedilen sinyaller
    active_signals = dict()  # {symbol: {...}} - Aktif sinyaller
    successful_signals = dict()  # {symbol: {...}} - Başarılı sinyaller (hedefe ulaşan)
    failed_signals = dict()  # {symbol: {...}} - Başarısız sinyaller (stop olan)
    tracked_coins = set()  # Takip edilen tüm coinlerin listesi
    
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
        
        # Global stop_cooldown değişkenini güncelle
        global_stop_cooldown = stop_cooldown.copy()
        
        # Bot başlangıcında eski sinyal cooldown'ları temizle
        print("🧹 Bot başlangıcında eski sinyal cooldown'ları temizleniyor...")
        await clear_cooldown_status()
    
    # Periyodik pozisyon kontrolü için sayaç
    position_check_counter = 0

    # Race condition önleme için pozisyon işlem flag'leri
    global position_processing_flags

    while True:
        try:
            if not ensure_mongodb_connection():
                print("⚠️ MongoDB bağlantısı kurulamadı, 30 saniye bekleniyor...")
                await asyncio.sleep(30)
                continue
            
            # DÖNGÜ BAŞINDA SÜRESİ DOLAN COOLDOWN'LARI TEMİZLE
            await cleanup_expired_stop_cooldowns()
            
            positions = load_positions_from_db()
            active_signals = load_active_signals_from_db()
            stats = load_stats_from_db()
            stop_cooldown = load_stop_cooldown_from_db()
            
            # Orphaned signals kontrolü - pozisyonu olmayan aktif sinyalleri temizle
            orphaned_signals = []
            for symbol in list(active_signals.keys()):
                # Pozisyon veritabanında var mı kontrol et
                position_exists = mongo_collection.find_one({"_id": f"position_{symbol}"}) is not None
                if symbol not in positions or not position_exists:
                    print(f"⚠️ {symbol} → Pozisyonu yok veya silinmiş, aktif sinyallerden kaldırılıyor")
                    orphaned_signals.append(symbol)
                    del active_signals[symbol]
                    
                    # Global active_signals'dan da kaldır
                    if symbol in global_active_signals:
                        del global_active_signals[symbol]
                        print(f"✅ {symbol} global_active_signals'dan da kaldırıldı")
            
            # Orphaned sinyalleri veritabanından da sil
            if orphaned_signals:
                for symbol in orphaned_signals:
                    try:
                        mongo_collection.delete_one({"_id": f"active_signal_{symbol}"})
                        print(f"✅ {symbol} active_signal veritabanından silindi")
                    except Exception as e:
                        print(f"⚠️ {symbol} active_signal silinirken hata: {e}")
            
            # TERS DURUM KONTROLÜ: Pozisyon var ama aktif sinyal yok - orfan pozisyonları temizle
            orphaned_positions = []
            for symbol in list(positions.keys()):
                # Aktif sinyal var mı kontrol et
                active_signal_exists = mongo_collection.find_one({"_id": f"active_signal_{symbol}"}) is not None
                if symbol not in active_signals or not active_signal_exists:
                    print(f"⚠️ {symbol} → Aktif sinyali yok, orfan pozisyon temizleniyor")
                    orphaned_positions.append(symbol)
                    del positions[symbol]
            
            # Orphaned pozisyonları veritabanından da sil
            if orphaned_positions:
                for symbol in orphaned_positions:
                    try:
                        mongo_collection.delete_one({"_id": f"position_{symbol}"})
                        print(f"✅ {symbol} position veritabanından silindi")
                    except Exception as e:
                        print(f"⚠️ {symbol} position silinirken hata: {e}")
            
            # Her 30 döngüde bir pozisyon kontrolü yap (yaklaşık 5 dakikada bir)
            position_check_counter += 1
            if position_check_counter >= 30:
                print("🔄 Periyodik pozisyon kontrolü yapılıyor...")
                await check_existing_positions_and_cooldowns(positions, active_signals, stats, stop_cooldown)
                position_check_counter = 0
                
                # Global stop_cooldown değişkenini güncelle
                global_stop_cooldown = stop_cooldown.copy()
            
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
            
            # Her döngüde güncel durumu yazdır (senkronizasyon kontrolü için)
            # Döngü sayacını artır
            if not hasattr(signal_processing_loop, '_loop_count'):
                signal_processing_loop._loop_count = 1
            else:
                signal_processing_loop._loop_count += 1
            
            # Türkiye saati kontrolü - 23:15-03:15 arasında yeni sinyal arama
            turkey_timezone = pytz.timezone('Europe/Istanbul')
            turkey_time = datetime.now(turkey_timezone)
            current_hour = turkey_time.hour
            current_minute = turkey_time.minute
            
            # 23:15 (23*60 + 15 = 1395) ile 03:15 (3*60 + 15 = 195) arası kontrol
            current_time_minutes = current_hour * 60 + current_minute
            is_signal_search_time = not (1395 <= current_time_minutes or current_time_minutes <= 195)
            
            print("=" * 60)
            print("🚀 YENİ SİNYAL ARAMA DÖNGÜSÜ BAŞLIYOR")
            print(f"📊 Mevcut durum: {len(positions)} pozisyon, {len(active_signals)} aktif sinyal, {len(stop_cooldown)} cooldown")
            print(f"⏰ Döngü başlangıç: {datetime.now().strftime('%H:%M:%S')}")
            print(f"🇹🇷 Türkiye saati: {turkey_time.strftime('%H:%M:%S')}")
            print(f"🔍 Sinyal arama: {'✅ AÇIK' if is_signal_search_time else '🚫 KAPALI (23:15-03:15)'}")
            print(f"🔄 Döngü #: {signal_processing_loop._loop_count}")
            print("=" * 60)
            
            # Aktif pozisyonları ve cooldown'daki coinleri korumalı semboller listesine ekle
            protected_symbols = set(positions.keys()) | set(stop_cooldown.keys())
            
            # Sinyal arama için kullanılacak sembolleri filtrele
            # STOP COOLDOWN'DAKİ COİNLERİ KESİNLİKLE SİNYAL ARAMA LİSTESİNE EKLEME!
            print(f"🔍 Cooldown filtresi uygulanıyor... Mevcut cooldown sayısı: {len(stop_cooldown)}")
            if stop_cooldown:
                print(f"🚫 STOP cooldown'daki coinler: {', '.join(list(stop_cooldown.keys())[:5])}")
                if len(stop_cooldown) > 5:
                    print(f"   ... ve {len(stop_cooldown) - 5} tane daha")
            
            # Cooldown'daki coinleri sinyal arama listesine hiç ekleme
            new_symbols = await get_active_high_volume_usdt_pairs(100, stop_cooldown)  # İlk 100 sembol (cooldown filtrelenmiş)
            print(f"✅ Cooldown filtresi uygulandı. Filtrelenmiş sembol sayısı: {len(new_symbols)}")
            
            # STOP COOLDOWN'DAKİ COİNLERİ KESİNLİKLE ÇIKAR
            symbols = [s for s in new_symbols if s not in stop_cooldown and s not in positions]
            
            if not symbols:
                # Sadece ilk kez mesaj yazdır
                if not hasattr(signal_processing_loop, '_first_all_protected'):
                    print("⚠️ Tüm coinler korumalı (aktif pozisyon veya cooldown)")
                    signal_processing_loop._first_all_protected = False
                await asyncio.sleep(60)
                continue
            
            # Cooldown durumunu kontrol et (sadece önceki döngüde çok fazla sinyal bulunduysa)
            cooldown_until = await check_cooldown_status()
            if cooldown_until and datetime.now() < cooldown_until:
                remaining_time = cooldown_until - datetime.now()
                # Negatif süreleri kontrol et
                if remaining_time.total_seconds() > 0:
                    remaining_minutes = int(remaining_time.total_seconds() / 60)
                    print(f"⏳ Sinyal cooldown modunda, {remaining_minutes} dakika sonra tekrar sinyal aranacak.")
                    print(f"   (Önceki döngüde çok fazla sinyal bulunduğu için)")
                    await asyncio.sleep(60)  # 1 dakika bekle
                    continue
                else:
                    print(f"⏳ Cooldown süresi geçmiş, devam ediliyor...")
                    continue
            
            # Cooldown'daki kriptoların detaylarını göster
            if stop_cooldown:
                print(f"⏳ Cooldown'daki kriptolar ({len(stop_cooldown)} adet):")
                current_time = datetime.now()
                for symbol, cooldown_until in stop_cooldown.items():
                    # Artık cooldown_until direkt datetime objesi
                    if isinstance(cooldown_until, str):
                        try:
                            cooldown_until = datetime.fromisoformat(cooldown_until.replace('Z', '+00:00'))
                            # Tarih geçmişse şimdiki zamanı kullan
                            if cooldown_until < current_time:
                                print(f"⚠️ {symbol}: Cooldown tarihi geçmiş, şimdiki zaman kullanılıyor")
                                cooldown_until = current_time
                        except Exception as e:
                            print(f"⚠️ {symbol}: Tarih parse hatası: {e}, şimdiki zaman kullanılıyor")
                            cooldown_until = current_time
                    elif not isinstance(cooldown_until, datetime):
                        cooldown_until = current_time
                    
                    remaining_time = cooldown_until - current_time
                    if remaining_time.total_seconds() > 0:
                        remaining_minutes = int(remaining_time.total_seconds() / 60)
                        remaining_seconds = int(remaining_time.total_seconds() % 60)
                        print(f"   🔴 {symbol}: {remaining_minutes}dk {remaining_seconds}sn kaldı")
                    else:
                        print(f"   🔴 {symbol}: Cooldown süresi geçmiş")
                        print(f"   🟢 {symbol}: Cooldown süresi bitti")
                print()  # Boş satır ekle

            if not hasattr(signal_processing_loop, '_first_signal_search'):
                print("🚀 YENİ SİNYAL ARAMA BAŞLATILIYOR (aktif sinyal varken de devam eder)")
                signal_processing_loop._first_signal_search = False
            
            # Türkiye saati kontrolü - 23:15-03:15 arasında yeni sinyal arama yapma
            if not is_signal_search_time:
                print(f"🚫 Türkiye saati {turkey_time.strftime('%H:%M')} - Yeni sinyal arama kapalı (23:15-03:15)")
                print(f"   Mevcut sinyaller kontrol edilmeye devam ediyor, cooldown sayacı azalıyor...")
                
                # Mevcut sinyalleri kontrol etmeye devam et ama yeni sinyal arama
                await asyncio.sleep(CONFIG["MAIN_LOOP_SLEEP_SECONDS"])
                continue
            
            # Sinyal bulma mantığı - tüm uygun sinyalleri topla
            found_signals = {}  # Bulunan tüm sinyaller bu sözlükte toplanacak
            print(f"🔍 {len(symbols)} coin'de sinyal aranacak (aktif pozisyon: {len(positions)}, cooldown: {len(stop_cooldown)})")
            
            # Sadece ilk kez mesaj yazdır
            if not hasattr(signal_processing_loop, '_first_crypto_count'):
                print(f"🔍 {len(symbols)} kripto taranacak")
                signal_processing_loop._first_crypto_count = False
            
            # STOP COOLDOWN'DAKİ COİNLER ZATEN YUKARIDA FİLTRELENDİ
            # Şimdi sadece aktif pozisyonları da çıkar
            symbols = [s for s in symbols if s not in positions]
            
            # Sadece ilk kez mesaj yazdır
            if not hasattr(signal_processing_loop, '_first_symbol_count'):
                print(f"📊 Toplam {len(symbols)} sembol kontrol edilecek (aktif pozisyonlar ve cooldown'daki coinler hariç)")
                signal_processing_loop._first_symbol_count = False

            print(f"📊 Toplam {len(symbols)} sembol kontrol edilecek...")
            processed_signals_in_loop = 0  # Bu döngüde işlenen sinyal sayacı
            
            # Cooldown süresi biten sinyalleri kontrol et ve aktif hale getir
            expired_cooldown_signals = await get_expired_cooldown_signals()
            if expired_cooldown_signals:
                print(f"🔄 Cooldown süresi biten {len(expired_cooldown_signals)} sinyal tekrar değerlendirilecek: {', '.join(expired_cooldown_signals[:5])}")
                if len(expired_cooldown_signals) > 5:
                    print(f"   ... ve {len(expired_cooldown_signals) - 5} tane daha")
                # Cooldown'dan çıkan sembolleri yeni sinyal arama listesinin BAŞINA ekle (öncelik ver)
                new_symbols_from_cooldown = [symbol for symbol in expired_cooldown_signals if symbol not in symbols]
                if new_symbols_from_cooldown:
                    # Cooldown'dan çıkanları başa ekle
                    symbols = new_symbols_from_cooldown + symbols
                    print(f"📊 Cooldown'dan çıkan {len(new_symbols_from_cooldown)} sembol öncelikli olarak eklendi. Toplam {len(symbols)} sembol kontrol edilecek")
                    print(f"   🏆 Öncelikli semboller: {', '.join(new_symbols_from_cooldown[:5])}")
                    if len(new_symbols_from_cooldown) > 5:
                        print(f"      ... ve {len(new_symbols_from_cooldown) - 5} tane daha")
            else:
                print("ℹ️ Cooldown süresi biten sinyal bulunamadı")
            
            # 20'li gruplar halinde işleme sistemi
            batch_size = 20
            total_batches = (len(symbols) + batch_size - 1) // batch_size
            pending_signals = {}  # Bekleyen sinyaller (hacim bilgisi ile)
            
            print(f"🔄 {total_batches} grup halinde işlenecek (her grup {batch_size} kripto)")
            
            for batch_num in range(total_batches):
                start_idx = batch_num * batch_size
                end_idx = min(start_idx + batch_size, len(symbols))
                batch_symbols = symbols[start_idx:end_idx]
                
                print(f"📊 Grup {batch_num + 1}/{total_batches}: {len(batch_symbols)} kripto kontrol ediliyor...")
                
                # Bu grup için sinyal arama
                batch_signals = {}
                for symbol in batch_symbols:
                    # Halihazırda pozisyon varsa veya stop cooldown'daysa atla
                    if symbol in positions:
                        continue
                    
                    # STOP COOLDOWN KONTROLÜ - 4 saat boyunca kesinlikle sinyal verilmez!
                    if check_cooldown(symbol, stop_cooldown, CONFIG["COOLDOWN_HOURS"]):
                        continue
                    
                    # Sinyal cooldown kontrolü - süresi bitenler hariç
                    if await check_signal_cooldown(symbol):
                        # Cooldown süresi biten sinyaller tekrar değerlendirilecek
                        if symbol in expired_cooldown_signals:
                            print(f"🔄 {symbol} sinyal cooldown süresi bitti, tekrar değerlendiriliyor")
                        else:
                            # Hala cooldown'da olan sinyaller atlanır
                            continue
                    
                    # Sinyal potansiyelini kontrol et
                    signal_result = await check_signal_potential(
                        symbol, positions, stop_cooldown, timeframes, tf_names, previous_signals
                    )
                    
                    # EĞER SİNYAL BULUNDUYSA, batch_signals'a ekle
                    if signal_result:
                        print(f"🔥 SİNYAL YAKALANDI: {symbol}!")
                        if symbol in ['BTCUSDT', 'ETHUSDT']:
                            print(f"   🎯 Major coin (BTC/ETH) - 5/7 kuralı sağlandı!")
                        else:
                            print(f"   🎯 15m mum kontrolü başarılı - Sinyal kalitesi onaylandı!")
                        
                        # TÜM SİNYALLER (major coinler dahil) batch_signals'a eklenir
                        batch_signals[symbol] = signal_result
                
                # Bu grup için sinyal işleme
                if batch_signals:
                    print(f"📊 Grup {batch_num + 1}: {len(batch_signals)} sinyal bulundu")
                    
                    # Major coinler için özel işlem - hemen gönder
                    major_coin_signals = {k: v for k, v in batch_signals.items() if k in ['BTCUSDT', 'ETHUSDT']}
                    regular_signals = {k: v for k, v in batch_signals.items() if k not in ['BTCUSDT', 'ETHUSDT']}
                    
                    # Major coinler varsa hemen işle
                    if major_coin_signals:
                        print(f"🚀 MAJOR COIN SİNYALLERİ BULUNDU! Hemen gönderiliyor...")
                        for symbol, signal_data in major_coin_signals.items():
                            print(f"   ⚡ {symbol} major coin sinyali hemen gönderiliyor!")
                            
                            # Hacim verisini çek
                            volumes = await get_volumes_for_symbols([symbol])
                            volume = volumes.get(symbol, 0)
                            
                            # Major coin sinyalini hemen işle
                            await process_selected_signal(signal_data, positions, active_signals, stats)
                            
                            # Cooldown'a ekle (30 dakika)
                            await set_signal_cooldown_to_db([symbol], timedelta(minutes=CONFIG["COOLDOWN_MINUTES"]))
                    
                    # Normal coinler için hacim bazlı seçim
                    if regular_signals:
                        # Hacim verilerini çek
                        volumes = await get_volumes_for_symbols(list(regular_signals.keys()))
                        
                        # Hacim verisine göre sırala
                        sorted_regular_signals = sorted(
                            regular_signals.items(),
                            key=lambda item: volumes.get(item[0], 0),
                            reverse=True
                        )
                        
                        # En yüksek hacimli sinyali seç ve anında gönder
                        best_signal = sorted_regular_signals[0]
                        symbol, signal_data = best_signal
                        volume = volumes.get(symbol, 0)
                        print(f"🏆 Grup {batch_num + 1} en iyi normal sinyal: {symbol} (Hacim: {volume:,.0f})")
                        
                        # Sinyali hemen işle/gönder
                        await process_selected_signal(signal_data, positions, active_signals, stats)
                        processed_signals_in_loop += 1
                        
                        # Aynı sembol için tekrar spam'ı önlemek adına kısa cooldown uygula
                        await set_signal_cooldown_to_db([symbol], timedelta(minutes=CONFIG["COOLDOWN_MINUTES"]))
                else:
                    print(f"📊 Grup {batch_num + 1}: Sinyal bulunamadı")
                
                # Önceki gruplardan kalan sinyallerle birlikte en iyi 5'i seç
                if len(pending_signals) >= 5 or batch_num == total_batches - 1:
                    # En yüksek hacimli 5 sinyali seç
                    sorted_pending = sorted(
                        pending_signals.items(),
                        key=lambda item: item[1]['volume'],
                        reverse=True
                    )
                    
                    # En fazla 5 sinyal gönder
                    signals_to_send = sorted_pending[:5]
                    
                    if signals_to_send:
                        print(f"🚀 {len(signals_to_send)} sinyal gönderiliyor (en yüksek hacimli):")
                        for i, (symbol, data) in enumerate(signals_to_send, 1):
                            print(f"   {i}. {symbol} - Hacim: {data['volume']:,.0f} (Grup {data['batch_num']})")
                        
                        # Sinyalleri işle
                        for symbol, data in signals_to_send:
                            found_signals[symbol] = data['signal_data']
                        
                        # Gönderilen sinyalleri pending'den çıkar
                        for symbol, _ in signals_to_send:
                            del pending_signals[symbol]
                    
                    # Eğer tüm gruplar işlendiyse döngüden çık
                    if batch_num == total_batches - 1:
                        break
            
            # Bulunan sinyalleri işle
            if not found_signals:
                print("🔍 Yeni sinyal bulunamadı.")
                print("   ℹ️ Bazı sinyaller 7/7 kuralını sağladı ancak 15m mum rengi uygun değildi")
                print("   🔄 Bu sinyaller sonraki kontrolde tekrar değerlendirilecek")
                # Sinyal bulunamadığında cooldown'ı temizle (normal çalışma modunda)
                await clear_cooldown_status()
                continue

            # Debug: Cooldown durumunu kontrol et
            cooldown_count = 0
            for symbol in symbols:
                if await check_signal_cooldown(symbol):
                    cooldown_count += 1
            print(f"📊 Cooldown durumu: {cooldown_count}/{len(symbols)} sembol cooldown'da")

            print(f"🎯 Toplam {len(found_signals)} sinyal bulundu!")
            
            # Sinyal işleme mantığı: Grup bazında seçilen en yüksek hacimli sinyaller işlenir
            if len(found_signals) > CONFIG["MAX_SIGNALS_PER_RUN"]:
                # 5'ten fazla sinyal varsa: En yüksek hacimli 5'i hemen gönder, kalanları cooldown'a al
                print("🚨 SİNYAL COOLDOWN SİSTEMİ AKTİF!")
                print(f"📊 {len(found_signals)} adet sinyal bulundu")
                print(f"   ✅ En yüksek hacimli {CONFIG['MAX_SIGNALS_PER_RUN']} sinyal hemen gönderilecek")
                print(f"   ⏳ Kalan {len(found_signals) - CONFIG['MAX_SIGNALS_PER_RUN']} sinyal 30 dakika cooldown'a girecek")
                print(f"   🔄 Cooldown'daki sinyaller bir sonraki döngüde (15 dk sonra) tekrar değerlendirilecek")
                
                # En yüksek hacimli 5 sinyali hemen işle
                top_signals = list(found_signals.items())[:CONFIG["MAX_SIGNALS_PER_RUN"]]
                
                # Kalan sinyalleri cooldown'a ekle
                remaining_signals = list(found_signals.keys())[CONFIG["MAX_SIGNALS_PER_RUN"]:]
                if remaining_signals:
                    print(f"⏳ Cooldown'a eklenen sinyaller: {', '.join(remaining_signals[:8])}")
                    if len(remaining_signals) > 8:
                        print(f"   ... ve {len(remaining_signals) - 8} tane daha")
                    await set_signal_cooldown_to_db(remaining_signals, timedelta(minutes=CONFIG["COOLDOWN_MINUTES"]))
            else:
                # 5 veya daha az sinyal varsa: Hepsi işlensin
                top_signals = list(found_signals.items())
                print(f"📊 {len(found_signals)} sinyal bulundu. Tümü işlenecek.")

            # Seçilen sinyalleri işleme
            print(f"✅ En yüksek hacimli {len(top_signals)} sinyal işleniyor...")
            for symbol, signal_result in top_signals:
                print(f"🚀 {symbol} sinyali işleniyor")
                
                # ETH için özel debug log
                if symbol == 'ETHUSDT':
                    print(f"🔍 ETHUSDT → process_selected_signal başlatılıyor...")
                    print(f"   Signal data: {signal_result}")
                
                result = await process_selected_signal(signal_result, positions, active_signals, stats)
                
                # ETH için özel debug log
                if symbol == 'ETHUSDT':
                    print(f"🔍 ETHUSDT → process_selected_signal tamamlandı, sonuç: {result}")
                
                processed_signals_in_loop += 1
            
            print(f"✅ Tarama döngüsü tamamlandı. Bu turda {processed_signals_in_loop} yeni sinyal işlendi.")

            if is_first:
                print(f"💾 İlk çalıştırma: {len(previous_signals)} sinyal kaydediliyor...")
                if len(previous_signals) > 0:
                    save_previous_signals_to_db(previous_signals)
                    print("✅ İlk çalıştırma sinyalleri kaydedildi!")
                else:
                    print("ℹ️ İlk çalıştırmada kayıt edilecek sinyal bulunamadı")
                is_first = False  # Artık ilk çalıştırma değil

            if not hasattr(signal_processing_loop, '_first_loop'):
                print("🚀 Yeni sinyal aramaya devam ediliyor...")
                signal_processing_loop._first_loop = False
            for symbol in list(active_signals.keys()):
                if symbol not in positions:  # Pozisyon kapandıysa aktif sinyalden kaldır
                    del active_signals[symbol]
                    continue

                # Race condition kontrolü: Monitor_signals bu pozisyonu işliyorsa atla
                current_time = datetime.now()
                if symbol in position_processing_flags:
                    flag_time = position_processing_flags[symbol]
                    # 30 saniye içinde işlenmişse atla
                    if isinstance(flag_time, datetime) and (current_time - flag_time).seconds < 30:
                        continue
                    else:
                        # Eski flag'i temizle
                        del position_processing_flags[symbol]

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
                    
                    # LONG sinyali için hedef/stop kontrolü
                    if signal_type == "LONG" or signal_type == "ALIS":
                        # Sadece ilk kez mesaj yazdır
                        attr_name3 = f'_first_alish_check_{symbol}'
                        if not hasattr(signal_processing_loop, attr_name3):
                            print(f"   🔍 {symbol} LONG sinyali kontrol ediliyor...")
                            setattr(signal_processing_loop, attr_name3, False)

                        min_target_diff = target_price * 0.001  # %0.1 minimum fark
                        if last_price >= target_price and (last_price - target_price) >= min_target_diff:
                            if entry_price > 0:
                                profit_percentage = ((target_price - entry_price) / entry_price) * 100
                                profit_usd = 100 * (profit_percentage / 100)  # 100$ yatırım için
                            else:
                                profit_percentage = 0
                                profit_usd = 0
                            
                            print(f"🎯 HEDEF GERÇEKLEŞTİ! {symbol} - Giriş: ${entry_price:.4f}, Hedef: ${target_price:.4f}, Çıkış: ${last_price:.4f}")
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
                            
                            # Stop cooldown'a ekle (8 saat) - Pozisyon kapandığı zamandan itibaren
                            current_time = datetime.now()
                            stop_cooldown[symbol] = current_time + timedelta(hours=CONFIG["COOLDOWN_HOURS"])
                            
                            # Cooldown'ı veritabanına kaydet
                            save_stop_cooldown_to_db(stop_cooldown)
                            
                            # İşlem flag'i set et (race condition önleme)
                            position_processing_flags[symbol] = current_time

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

                            # MESAJ GÖNDERİMİ KALDIRILDI - monitor_signals() fonksiyonu mesaj gönderecek
                            print(f"📢 {symbol} hedefe ulaştı - monitor_signals() mesaj gönderecek")
                            
                        # Stop kontrolü: Güncel fiyat stop'u geçti mi? (LONG: aşağı düşmesi zarar)
                        # GÜVENLİK KONTROLÜ: Fiyat gerçekten stop'u geçti mi?
                        # Minimum fark kontrolü: Fiyat stop'u en az 0.1% geçmeli (daha güvenli)
                        min_stop_diff = stop_loss * 0.001  # %0.1 minimum fark
                        if last_price <= stop_loss and (stop_loss - last_price) >= min_stop_diff:
                            if entry_price > 0:
                                loss_percentage = ((entry_price - stop_loss) / entry_price) * 100
                                loss_usd = 100 * (loss_percentage / 100)
                            else:
                                loss_percentage = 0
                                loss_usd = 0
                            
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
                            
                            # Stop cooldown'a ekle (8 saat) - Pozisyon kapandığı zamandan itibaren
                            current_time = datetime.now()
                            stop_cooldown[symbol] = current_time + timedelta(hours=CONFIG["COOLDOWN_HOURS"])
                            
                            # Sinyal cooldown'a da ekle (30 dakika)
                            await set_signal_cooldown_to_db([symbol], timedelta(minutes=CONFIG["COOLDOWN_MINUTES"]))

                            # Cooldown'ı veritabanına kaydet
                            save_stop_cooldown_to_db(stop_cooldown)

                            # İşlem flag'i set et (race condition önleme)
                            position_processing_flags[symbol] = current_time

                            # Pozisyonu ve aktif sinyali kaldır
                            if symbol in positions:
                                del positions[symbol]
                            del active_signals[symbol]

                    # SHORT sinyali için hedef/stop kontrolü
                    elif signal_type == "SATIŞ" or signal_type == "SATIS":
                        # Sadece ilk kez mesaj yazdır
                        attr_name4 = f'_first_satish_check_{symbol}'
                        if not hasattr(signal_processing_loop, attr_name4):
                            print(f"   🔍 {symbol} SHORT sinyali kontrol ediliyor...")
                            setattr(signal_processing_loop, attr_name4, False)
                        # Hedef kontrolü: Güncel fiyat hedefi geçti mi? (SHORT: aşağı düşmesi gerekir)
                        # GÜVENLİK KONTROLÜ: Fiyat gerçekten hedefi geçti mi?
                        # Minimum fark kontrolü: Fiyat hedefi en az 0.1% geçmeli (daha güvenli)
                        min_target_diff = target_price * 0.001  # %0.1 minimum fark
                        if last_price <= target_price and (target_price - last_price) >= min_target_diff:
                            # HEDEF GERÇEKLEŞTİ! 🎯
                            # Güvenli kâr hesaplaması
                            if entry_price > 0:
                                profit_percentage = ((entry_price - target_price) / entry_price) * 100
                                profit_usd = 100 * (profit_percentage / 100)  # 100$ yatırım için
                            else:
                                profit_percentage = 0
                                profit_usd = 0
                            
                            print(f"🎯 HEDEF GERÇEKLEŞTİ! {symbol} - Giriş: ${entry_price:.4f}, Hedef: ${target_price:.4f}, Çıkış: ${last_price:.4f}")
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
                            
                            # Stop cooldown'a ekle (8 saat) - Pozisyon kapandığı zamandan itibaren
                            current_time = datetime.now()
                            stop_cooldown[symbol] = current_time + timedelta(hours=CONFIG["COOLDOWN_HOURS"])
                            
                            # Cooldown'ı veritabanına kaydet
                            save_stop_cooldown_to_db(stop_cooldown)

                            # İşlem flag'i set et (race condition önleme)
                            position_processing_flags[symbol] = current_time

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

                            print(f"📢 {symbol} hedefe ulaştı - monitor_signals() mesaj gönderecek")
                            
                        # Stop kontrolü: Güncel fiyat stop'u geçti mi? (SHORT: yukarı çıkması zarar)
                        # GÜVENLİK KONTROLÜ: Fiyat gerçekten stop'u geçti mi?
                        # Minimum fark kontrolü: Fiyat stop'u en az 0.1% geçmeli (daha güvenli)
                        min_stop_diff = stop_loss * 0.001  # %0.1 minimum fark
                        if last_price >= stop_loss and (last_price - stop_loss) >= min_stop_diff:
                            if entry_price > 0:
                                loss_percentage = ((stop_loss - entry_price) / entry_price) * 100
                                loss_usd = 100 * (loss_percentage / 100)
                            else:
                                loss_percentage = 0
                                loss_usd = 0
                            
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
                            
                            # Stop cooldown'a ekle (8 saat) - Pozisyon kapandığı zamandan itibaren
                            current_time = datetime.now()
                            stop_cooldown[symbol] = current_time + timedelta(hours=CONFIG["COOLDOWN_HOURS"])
                            
                            # Sinyal cooldown'a da ekle (30 dakika)
                            await set_signal_cooldown_to_db([symbol], timedelta(minutes=CONFIG["COOLDOWN_MINUTES"]))
                            
                            # Cooldown'ı veritabanına kaydet
                            save_stop_cooldown_to_db(stop_cooldown)

                            # İşlem flag'i set et (race condition önleme)
                            position_processing_flags[symbol] = current_time

                            # Pozisyonu ve aktif sinyali kaldır
                            if symbol in positions:
                                del positions[symbol]
                            del active_signals[symbol]
                    
                except Exception as e:
                    print(f"Aktif sinyal güncelleme hatası: {symbol} - {str(e)}")
                    continue
            
            # Aktif sinyal kontrolü özeti
            if active_signals:
                print(f"✅ AKTİF SİNYAL KONTROLÜ TAMAMLANDI ({len(active_signals)} sinyal)")
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
            
            save_stats_to_db(stats)
            save_active_signals_to_db(active_signals)
            save_positions_to_db(positions)  # ✅ POZİSYONLARI DA KAYDET

            # İstatistik özeti yazdır - veritabanından güncel verileri al
            print(f"📊 İSTATİSTİK ÖZETİ:")
            
            # Veritabanından güncel istatistikleri yükle
            db_stats = load_stats_from_db()
            if db_stats:
                stats = db_stats
            
            # Güncel aktif sinyal sayısını al (veritabanından)
            try:
                # Veritabanından aktif sinyal sayısını al
                active_signals_docs = mongo_collection.count_documents({"_id": {"$regex": "^active_signal_"}})
                current_active_count = active_signals_docs
            except:
                # Hata durumunda yerel değişkenden al
                current_active_count = len(active_signals)
            
            # Toplam sinyal sayısını hesapla
            total_signals = stats.get('successful_signals', 0) + stats.get('failed_signals', 0) + current_active_count
            
            print(f"   Toplam Sinyal: {total_signals}")
            print(f"   Başarılı: {stats.get('successful_signals', 0)}")
            print(f"   Başarısız: {stats.get('failed_signals', 0)}")
            print(f"   Aktif Sinyal: {current_active_count}")
            print(f"   100$ Yatırım Toplam Kar/Zarar: ${stats.get('total_profit_loss', 0):.2f}")
            
            # Başarı oranını hesapla
            closed_count = stats.get('successful_signals', 0) + stats.get('failed_signals', 0)
            if closed_count > 0:
                success_rate = (stats.get('successful_signals', 0) / closed_count) * 100
                print(f"   Başarı Oranı: %{success_rate:.1f}")
            else:
                print(f"   Başarı Oranı: %0.0")
            
            
            # Mevcut sinyal cooldown sayısını da göster
            try:
                if mongo_collection is not None:
                    current_signal_cooldowns = mongo_collection.count_documents({"_id": {"$regex": "^signal_cooldown_"}})
                    print(f"⏳ Sinyal cooldown'daki sembol: {current_signal_cooldowns}")
            except:
                pass
            print("=" * 60)
            print("⏰ 15 dakika sonra yeni sinyal arama döngüsü başlayacak...")
            print("   - Tüm coinler tekrar taranacak")
            print("   - Cooldown süresi biten sinyaller tekrar değerlendirilecek")
            print("   - Yeni sinyaller + cooldown'dan çıkanlar birlikte işlenecek")
            print("=" * 60)
            await asyncio.sleep(900)  # 15 dakika (900 saniye)
            
        except Exception as e:
            print(f"Genel hata: {e}")
            await asyncio.sleep(30)  # 30 saniye (çok daha hızlı)
async def monitor_signals():
    print("🚀 Sinyal izleme sistemi başlatıldı! (Veri Karışıklığı Düzeltildi)")

    # Global değişkenleri kullan
    global active_signals, position_processing_flags, global_active_signals
    
    while True:
        try:
            # MONITOR DÖNGÜSÜ BAŞINDA DA SÜRESİ DOLAN COOLDOWN'LARI TEMİZLE
            await cleanup_expired_stop_cooldowns()
            
            active_signals = load_active_signals_from_db()

            # 'active' olmayan sinyalleri temizle
            removed = []
            for sym in list(active_signals.keys()):
                if str(active_signals[sym].get('status', 'active')).lower() != 'active':
                    removed.append(sym)
                    del active_signals[sym]
                    try:
                        if mongo_collection is not None:
                            mongo_collection.delete_one({"_id": f"active_signal_{sym}"})
                            print(f"🧹 {sym} aktif değil (status!=active), veritabanından silindi")
                    except Exception as e:
                        print(f"⚠️ {sym} silinirken hata: {e}")

            if not active_signals:
                await asyncio.sleep(CONFIG["MONITOR_SLEEP_EMPTY"]) 
                continue

            positions = load_positions_from_db()
            orphaned_signals = []
            for symbol in list(active_signals.keys()):
                # Pozisyon veritabanında var mı kontrol et
                position_exists = mongo_collection.find_one({"_id": f"position_{symbol}"}) is not None
                if symbol not in positions or not position_exists:
                    print(f"⚠️ {symbol} → Positions'da yok veya veritabanında silinmiş, aktif sinyallerden kaldırılıyor")
                    orphaned_signals.append(symbol)
                    del active_signals[symbol]
                    
                    # Global active_signals'dan da kaldır
                    if symbol in global_active_signals:
                        del global_active_signals[symbol]
                        print(f"✅ {symbol} global_active_signals'dan da kaldırıldı")
            
            # Orphaned sinyalleri veritabanından da sil - HATA DURUMUNDA TEKRAR DENE
            if orphaned_signals:
                for symbol in orphaned_signals:
                    try:
                        delete_result = mongo_collection.delete_one({"_id": f"active_signal_{symbol}"})
                        if delete_result.deleted_count > 0:
                            print(f"✅ {symbol} aktif sinyali veritabanından silindi")
                        else:
                            print(f"⚠️ {symbol} aktif sinyali zaten silinmiş veya bulunamadı")
                    except Exception as e:
                        print(f"❌ {symbol} aktif sinyali silinirken hata: {e}")
                        # Hata durumunda tekrar dene
                        try:
                            mongo_collection.delete_one({"_id": f"active_signal_{symbol}"})
                            print(f"✅ {symbol} aktif sinyali ikinci denemede silindi")
                        except Exception as e2:
                            print(f"❌ {symbol} aktif sinyali ikinci denemede de silinemedi: {e2}")
                
                # Güncellenmiş aktif sinyalleri kaydet
                save_active_signals_to_db(active_signals)
                print(f"✅ {len(orphaned_signals)} tutarsız sinyal temizlendi")
            
            # Eğer temizlik sonrası aktif sinyal kalmadıysa bekle
            if not active_signals:
                await asyncio.sleep(CONFIG["MONITOR_SLEEP_EMPTY"]) 
                continue

            # Gereksiz detaylı yazdırmalar kaldırıldı - sadece TP/SL tetiklendiğinde yazdırılacak
            for symbol, signal in list(active_signals.items()):
                try:
                    # Ek güvenlik kontrolü: Pozisyon belgesi var mı?
                    position_doc = mongo_collection.find_one({"_id": f"position_{symbol}"})
                    if not position_doc:
                        print(f"⚠️ {symbol} → Position belgesi yok, aktif sinyallerden kaldırılıyor")
                        # Veritabanından da sil
                        try:
                            mongo_collection.delete_one({"_id": f"active_signal_{symbol}"})
                            print(f"✅ {symbol} active_signal belgesi veritabanından silindi")
                        except Exception as e:
                            print(f"❌ {symbol} active_signal belgesi silinirken hata: {e}")
                        
                        del active_signals[symbol]
                        continue
                    
                    if not mongo_collection.find_one({"_id": f"active_signal_{symbol}"}):
                        print(f"ℹ️ {symbol} sinyali DB'de bulunamadı, bellekten kaldırılıyor.")
                        del active_signals[symbol]
                        continue

                    signal_status = signal.get("status", "pending")
                    if signal_status != "active":
                        print(f"ℹ️ {symbol} sinyali henüz aktif değil (durum: {signal_status}), atlanıyor.")
                        continue

                    symbol_entry_price_raw = signal.get('entry_price_float', signal.get('entry_price', 0))
                    symbol_entry_price = float(str(symbol_entry_price_raw).replace('$', '').replace(',', '')) if symbol_entry_price_raw is not None else 0.0
                    symbol_target_price = float(str(signal.get('target_price', 0)).replace('$', '').replace(',', ''))
                    symbol_stop_loss_price = float(str(signal.get('stop_loss', 0)).replace('$', '').replace(',', ''))
                    symbol_signal_type = signal.get('type', 'ALIŞ')
                                            
                    # 3. ANLIK FİYAT KONTROLÜ
                    try:
                        ticker = await fetch_futures_24h(symbol)
                        last_price = float(ticker['lastPrice'])
                        is_triggered_realtime = False
                        trigger_type_realtime = None
                        final_price_realtime = None
                        min_trigger_diff = 0.001  # %0.1 minimum fark

                        if symbol_signal_type == "ALIŞ" or symbol_signal_type == "ALIS" or symbol_signal_type == "LONG":
                            
                            # TP: Fiyat hedefin üstüne çıktığında (LONG için kâr)
                            if last_price >= symbol_target_price:
                                is_triggered_realtime = True
                                trigger_type_realtime = "take_profit"
                                final_price_realtime = last_price
                                print(f"✅ {symbol} - TP tetiklendi (LONG): ${last_price:.6f} >= ${symbol_target_price:.6f}")
                            # SL: Fiyat stop'un altına düştüğünde (LONG için zarar)
                            elif last_price <= symbol_stop_loss_price:
                                is_triggered_realtime = True
                                trigger_type_realtime = "stop_loss"
                                final_price_realtime = last_price
                                print(f"❌ {symbol} - SL tetiklendi (LONG): ${last_price:.6f} <= ${symbol_stop_loss_price:.6f}")
                        elif symbol_signal_type == "SATIŞ" or symbol_signal_type == "SATIS" or symbol_signal_type == "SHORT":
                            # SHORT pozisyonu için kapanış koşulları    
                            # TP: Fiyat hedefin altına düştüğünde (SHORT için kâr)
                            if last_price <= symbol_target_price:
                                is_triggered_realtime = True
                                trigger_type_realtime = "take_profit"
                                final_price_realtime = last_price
                                print(f"✅ {symbol} - TP tetiklendi (SHORT): ${last_price:.6f} <= ${symbol_target_price:.6f}")
                            # SL: Fiyat stop'un üstüne çıktığında (SHORT için zarar)
                            elif last_price >= symbol_stop_loss_price:
                                is_triggered_realtime = True
                                trigger_type_realtime = "stop_loss"
                                final_price_realtime = last_price
                                print(f"❌ {symbol} - SL tetiklendi (SHORT): ${last_price:.6f} >= ${symbol_stop_loss_price:.6f}")
                        
                        # 4. POZİSYON KAPATMA İŞLEMİ
                        if is_triggered_realtime:
                            print(f"💥 ANLIK TETİKLENDİ: {symbol}, Tip: {trigger_type_realtime}, Fiyat: {final_price_realtime}")
                            
                            # Pozisyon durumu kontrolü kaldırıldı - her tetikleme işlenmeli
                            print(f"🔄 {symbol} - Anlık tetikleme işleniyor...")
                            
                            update_position_status_atomic(symbol, "closing", {"trigger_type": trigger_type_realtime, "final_price": final_price_realtime})
                            
                            position_data = load_position_from_db(symbol)
                            if position_data:
                                if position_data.get('open_price', 0) <= 0:
                                    print(f"⚠️ {symbol} - Geçersiz pozisyon verileri, pozisyon temizleniyor")
                                    mongo_collection.delete_one({"_id": f"position_{symbol}"})
                                    mongo_collection.delete_one({"_id": f"active_signal_{symbol}"})
                                    active_signals.pop(symbol, None)
                                    continue
                            else:
                                print(f"❌ {symbol} pozisyon verisi yüklenemedi!")
                                continue

                            # Race condition kontrolü: signal_processing_loop bu pozisyonu işliyorsa atla
                            current_time = datetime.now()
                            if symbol in position_processing_flags:
                                flag_time = position_processing_flags[symbol]
                                if isinstance(flag_time, datetime) and (current_time - flag_time).seconds < 30:
                                    print(f"⏳ {symbol} signal_processing_loop tarafından işleniyor, bekleniyor...")
                                    continue

                            await close_position(symbol, trigger_type_realtime, final_price_realtime, signal, position_data)
                            # close_position zaten active_signals'dan kaldırıyor, burada tekrar yapmaya gerek yok
                            continue # Bu sembol bitti, sonraki sinyale geç.
                            
                    except Exception as e:
                        print(f"⚠️ {symbol} - Anlık ticker fiyatı alınamadı: {e}")
                    
                    try:
                        url = f"https://fapi.binance.com/fapi/v1/klines?symbol={symbol}&interval=1m&limit=100"
                        async with aiohttp.ClientSession(connector=aiohttp.TCPConnector(limit=10)) as session:
                            klines = await api_request_with_retry(session, url, ssl=False)
                        
                    except Exception as e:
                        print(f"⚠️ {symbol} - Mum verisi alınamadı (retry sonrası): {e}")
                        continue

                    if not klines:
                        continue
                    
                    is_triggered, trigger_type, final_price = check_klines_for_trigger(signal, klines)
                    
                    if is_triggered:
                        print(f"💥 MUM TETİKLEDİ: {symbol}, Tip: {trigger_type}, Fiyat: {final_price}")
                        
                        # Pozisyon durumu kontrolü kaldırıldı - her tetikleme işlenmeli
                        print(f"🔄 {symbol} - Mum tetikleme işleniyor...")
                        
                        update_position_status_atomic(symbol, "closing", {"trigger_type": trigger_type, "final_price": final_price})
                        position_data = load_position_from_db(symbol)

                        if position_data:
                            if position_data.get('open_price', 0) <= 0:
                                print(f"⚠️ {symbol} - Geçersiz pozisyon verileri, pozisyon temizleniyor")
                                # Geçersiz pozisyonu temizle
                                mongo_collection.delete_one({"_id": f"position_{symbol}"})
                                mongo_collection.delete_one({"_id": f"active_signal_{symbol}"})
                                del active_signals[symbol]
                                continue
                        else:
                            print(f"❌ {symbol} pozisyon verisi yüklenemedi!")
                            continue

                        # Race condition kontrolü: signal_processing_loop bu pozisyonu işliyorsa atla
                        current_time = datetime.now()
                        if symbol in position_processing_flags:
                            flag_time = position_processing_flags[symbol]
                            if isinstance(flag_time, datetime) and (current_time - flag_time).seconds < 30:
                                print(f"⏳ {symbol} signal_processing_loop tarafından işleniyor, bekleniyor...")
                                continue

                        await close_position(symbol, trigger_type, final_price, signal, position_data)
                        # close_position zaten active_signals'dan kaldırıyor, burada tekrar yapmaya gerek yok
                        
                        print(f"✅ {symbol} izleme listesinden kaldırıldı. Bir sonraki sinyale geçiliyor.")
                        continue # Bir sonraki sinyale geç
                    else:
                        # Tetikleme yoksa, anlık fiyatı güncelle
                        if final_price:
                            active_signals[symbol]['current_price'] = format_price(final_price, signal.get('entry_price_float'))
                            active_signals[symbol]['current_price_float'] = final_price
                            active_signals[symbol]['last_update'] = str(datetime.now())
                            # DB'ye anlık fiyatı kaydetmek için (opsiyonel ama iyi bir pratik)
                            save_data_to_db(f"active_signal_{symbol}", active_signals[symbol])
                    
                except Exception as e:
                    print(f"❌ {symbol} sinyali işlenirken döngü içinde hata oluştu: {e}")
                    if symbol in active_signals:
                        del active_signals[symbol]
                    continue

            await asyncio.sleep(CONFIG["MONITOR_LOOP_SLEEP_SECONDS"]) # Daha hızlı kontrol için 3 saniye
        
        except Exception as e:
            print(f"❌ Ana sinyal izleme döngüsü hatası: {e}")
            await asyncio.sleep(CONFIG["MONITOR_SLEEP_ERROR"])  # Hata durumunda bekle
            active_signals = load_active_signals_from_db()

async def main():
    load_allowed_users()
    await setup_bot()
    await app.initialize()
    await app.start()
    
    # MongoDB'deki bozuk pozisyon verilerini temizle
    cleanup_corrupted_positions()
    
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

    signal_task = asyncio.create_task(signal_processing_loop())
    monitor_task = asyncio.create_task(monitor_signals())
    try:
        # Tüm task'ları bekle
        await asyncio.gather(signal_task, monitor_task)
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
        
        try:
            await asyncio.gather(signal_task, monitor_task, return_exceptions=True)
        except Exception:
            pass

        try:
            await app.updater.stop()
            print("✅ Telegram bot polling durduruldu")
        except Exception as e:
            print(f"⚠️ Bot polling durdurma hatası: {e}")

        try:
            await app.stop()
            await app.shutdown()
            print("✅ Telegram uygulaması kapatıldı")
        except Exception as e:
            print(f"⚠️ Uygulama kapatma hatası: {e}")
        
        close_mongodb()
        print("✅ MongoDB bağlantısı kapatıldı")

def clear_previous_signals_from_db():
    """MongoDB'deki tüm önceki sinyal kayıtlarını ve işaret dokümanını siler."""
    try:
        deleted_count = clear_data_by_pattern("^previous_signal_", "önceki sinyal")
        init_deleted = clear_specific_document("previous_signals_initialized", "initialized bayrağı")
        
        print(f"🧹 MongoDB'den {deleted_count} önceki sinyal silindi; initialized={init_deleted}")
        return deleted_count, init_deleted
    except Exception as e:
        print(f"❌ MongoDB'den önceki sinyaller silinirken hata: {e}")
        return 0, False

def clear_position_data_from_db():
    """MongoDB'deki position_ ile başlayan tüm kayıtları siler (clear_positions.py'den uyarlandı)."""
    try:
        deleted_count = clear_data_by_pattern("^position_", "pozisyon")
        return deleted_count
    except Exception as e:
        print(f"❌ MongoDB'den pozisyonlar silinirken hata: {e}")
        return 0

async def reduce_cooldowns_command(update, context):
    """Cooldown sürelerini %95 kısalt - Sadece bot sahibi"""
    if not update.effective_user:
        return
    
    user_id = update.effective_user.id
    if user_id != BOT_OWNER_ID:
        await send_command_response(update, "❌ Bu komut sadece bot sahibi tarafından kullanılabilir.")
        return
    
    try:
        # Cooldown'ları kısalt
        success, message = await reduce_cooldowns_by_95_percent()
        
        if success:
            await send_command_response(update, message)
        else:
            await send_command_response(update, f"❌ {message}")
            
    except Exception as e:
        await send_command_response(update, f"❌ Cooldown kısaltma işlemi sırasında hata: {e}")

async def clear_all_command(update, context):
    """Tüm verileri temizler: pozisyonlar, aktif sinyaller, önceki sinyaller, bekleyen kuyruklar, istatistikler (sadece bot sahibi)"""
    global global_active_signals
    
    user_id, is_authorized = validate_user_command(update, require_owner=True)
    if not is_authorized:
        return
    
    await send_command_response(update, "🧹 Tüm veriler temizleniyor...")
    try:
        # 1) Pozisyonları temizle
        pos_deleted = clear_position_data_from_db()
        
        # 2) Aktif sinyalleri temizle - daha güçlü temizleme
        active_deleted = clear_data_by_pattern("^active_signal_", "aktif sinyal")
        
        # 3) Kalan aktif sinyalleri manuel olarak kontrol et ve sil
        try:
            remaining_active = mongo_collection.find({"_id": {"$regex": "^active_signal_"}})
            remaining_count = 0
            for doc in remaining_active:
                mongo_collection.delete_one({"_id": doc["_id"]})
                remaining_count += 1
            if remaining_count > 0:
                print(f"🧹 Manuel olarak {remaining_count} kalan aktif sinyal silindi")
                active_deleted += remaining_count
        except Exception as e:
            print(f"⚠️ Manuel aktif sinyal temizleme hatası: {e}")
        
        # 4) Global değişkenleri temizle
        global_active_signals = {}
        
        # Boş aktif sinyal listesi kaydet - bu artık tüm dokümanları silecek
        save_active_signals_to_db({})
        
        cooldown_deleted = clear_data_by_pattern("^stop_cooldown_", "stop cooldown")
        
        # 5.5) Sinyal cooldown'ları temizle
        signal_cooldown_deleted = clear_data_by_pattern("^signal_cooldown_", "sinyal cooldown")
        
        # 6) JSON dosyasını da temizle
        try:
            with open('active_signals.json', 'w', encoding='utf-8') as f:
                json.dump({
                    "active_signals": {},
                    "count": 0,
                    "last_update": str(datetime.now())
                }, f, ensure_ascii=False, indent=2)
        except Exception:
            pass
        
        prev_deleted, init_deleted = clear_previous_signals_from_db()
        global global_waiting_signals

        try:
            global_waiting_signals = {}
        except NameError:
            pass
        
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
        
        # Son kontrol - kalan dokümanları say
        try:
            final_positions = mongo_collection.count_documents({"_id": {"$regex": "^position_"}})
            final_active = mongo_collection.count_documents({"_id": {"$regex": "^active_signal_"}})
            final_cooldown = mongo_collection.count_documents({"_id": {"$regex": "^stop_cooldown_"}})
            final_signal_cooldown = mongo_collection.count_documents({"_id": {"$regex": "^signal_cooldown_"}})
            
            print(f"🔍 Temizleme sonrası kontrol:")
            print(f"   Kalan pozisyon: {final_positions}")
            print(f"   Kalan aktif sinyal: {final_active}")
            print(f"   Kalan stop cooldown: {final_cooldown}")
            print(f"   Kalan sinyal cooldown: {final_signal_cooldown}")
            
        except Exception as e:
            print(f"⚠️ Son kontrol hatası: {e}")
        
        # Özet mesaj
        summary = (
            f"✅ Temizleme tamamlandı.\n"
            f"• Pozisyon: {pos_deleted} silindi\n"
            f"• Aktif sinyal: {active_deleted} silindi\n"
            f"• Stop cooldown: {cooldown_deleted} silindi\n"
            f"• Sinyal cooldown: {signal_cooldown_deleted} silindi\n"
            f"• Önceki sinyal: {prev_deleted} silindi (initialized: {'silindi' if init_deleted else 'yok'})\n"
            f"• Bekleyen kuyruklar sıfırlandı\n"
            f"• İstatistikler sıfırlandı"
        )
        await send_command_response(update, summary)
    except Exception as e:
        await send_command_response(update, f"❌ ClearAll hatası: {e}")

async def calculate_signals_for_symbol(symbol, timeframes, tf_names):
    """Bir sembol için tüm zaman dilimlerinde sinyalleri hesaplar"""
    current_signals = {}
    
    for tf_name in tf_names:
        try:
            df = await async_get_historical_data(symbol, timeframes[tf_name], 1000)
            if df is None or df.empty:
                return None
            
            df = calculate_full_pine_signals(df, tf_name)
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

def calculate_signal_counts(signals, tf_names, symbol=None):
    """Sinyal sayılarını hesaplar"""
    signal_values = [signals.get(tf, 0) for tf in tf_names]
    buy_count = sum(1 for s in signal_values if s == 1)
    sell_count = sum(1 for s in signal_values if s == -1)
    return buy_count, sell_count

def check_signal_rule(buy_count, sell_count, required_signals, symbol):
    """Esnek sinyal kuralını kontrol eder - BTC/ETH için 6/7, diğerleri için 7/7"""
    result = buy_count == required_signals or sell_count == required_signals
    return result

def check_major_coin_signal_rule(symbol, current_signals, previous_signals):
    """BTC/ETH için 5/7 kuralını kontrol eder"""
    tf_names = ['15m', '30m', '1h', '2h', '4h', '8h', '1d']
    
    # Mevcut sinyal sayılarını hesapla
    buy_count, sell_count = calculate_signal_counts(current_signals, tf_names, symbol)
    
    print(f"🔍 {symbol} → Major coin 5/7 kural kontrolü: LONG={buy_count}, SHORT={sell_count}")
    
    # 7/7 kuralı - tüm zaman dilimleri aynı olmalı (15dk değişmiş olma şartı yok)
    if buy_count == 7 and sell_count == 0:
        print(f"✅ {symbol} → 7/7 kuralı sağlandı (tüm zaman dilimleri LONG)")
        return True
    elif sell_count == 7 and buy_count == 0:
        print(f"✅ {symbol} → 7/7 kuralı sağlandı (tüm zaman dilimleri SHORT)")
        return True
    
    # 6/7 kuralı - 15dk, 30dk, 1h, 2h, 4h, 8h aynı olmalı (15dk değişmiş olma şartı yok)
    tf_6h = ['15m', '30m', '1h', '2h', '4h', '8h']
    buy_count_6h = sum(1 for tf in tf_6h if current_signals.get(tf, 0) == 1)
    sell_count_6h = sum(1 for tf in tf_6h if current_signals.get(tf, 0) == -1)
    
    if buy_count_6h == 6 and sell_count_6h == 0:
        print(f"✅ {symbol} → 6/7 kuralı sağlandı (15dk,30dk,1h,2h,4h,8h LONG)")
        return True
    elif sell_count_6h == 6 and buy_count_6h == 0:
        print(f"✅ {symbol} → 6/7 kuralı sağlandı (15dk,30dk,1h,2h,4h,8h SHORT)")
        return True
    
    # 5/7 kuralı - 15dk, 30dk, 1h, 2h, 4h aynı olmalı (15dk değişmiş olma şartı yok)
    tf_5h = ['15m', '30m', '1h', '2h', '4h']
    buy_count_5h = sum(1 for tf in tf_5h if current_signals.get(tf, 0) == 1)
    sell_count_5h = sum(1 for tf in tf_5h if current_signals.get(tf, 0) == -1)
    
    if buy_count_5h == 5 and sell_count_5h == 0:
        print(f"✅ {symbol} → 5/7 kuralı sağlandı (15dk,30dk,1h,2h,4h LONG)")
        return True
    elif sell_count_5h == 5 and buy_count_5h == 0:
        print(f"✅ {symbol} → 5/7 kuralı sağlandı (15dk,30dk,1h,2h,4h SHORT)")
        return True
    
    print(f"❌ {symbol} → 5/7 kuralı sağlanamadı: LONG={buy_count}, SHORT={sell_count}")
    return False

def check_cooldown(symbol, cooldown_dict, hours=4):
    """
    Bir sembolün cooldown sözlüğünde olup olmadığını kontrol eder.
    Temizleme işlemi artık ana döngüdeki 'cleanup_expired_stop_cooldowns' tarafından yapılıyor.
    """
    if symbol in cooldown_dict:
        # Sembol sözlükte varsa, hala cooldown'dadır.
        return True
    # Sözlükte yoksa, cooldown'da değildir.
    return False

def add_stop_cooldown_safe(symbol, stop_cooldown_dict):
    """
    Stop cooldown eklerken mevcut cooldown'ı kontrol eder.
    Aynı kripto için yeni stop/hedelf olduğunda cooldown SIFIRLANIR (uzatılmaz).
    """
    current_time = datetime.now()
    new_cooldown_end = current_time + timedelta(hours=CONFIG["COOLDOWN_HOURS"])
    
    if symbol in stop_cooldown_dict:
        existing_end = stop_cooldown_dict[symbol]
        
        # Eğer mevcut cooldown henüz bitmemişse
        if existing_end > current_time:
            # Aynı kripto için yeni stop/hedelf olduğunda cooldown SIFIRLANIR
            stop_cooldown_dict[symbol] = new_cooldown_end
            print(f"🔄 {symbol} → Yeni stop/hedelf! Cooldown sıfırlandı: {existing_end.strftime('%H:%M:%S')} → {new_cooldown_end.strftime('%H:%M:%S')}")
        else:
            # Mevcut cooldown bitmişse, yeni cooldown başlat
            stop_cooldown_dict[symbol] = new_cooldown_end
            print(f"🆕 {symbol} → Cooldown süresi dolmuştu, yeni cooldown başlatıldı: {new_cooldown_end.strftime('%H:%M:%S')}")
    else:
        # Hiç cooldown yoksa, yeni cooldown başlat
        stop_cooldown_dict[symbol] = new_cooldown_end
        print(f"🆕 {symbol} → İlk cooldown başlatıldı: {new_cooldown_end.strftime('%H:%M:%S')}")
    
    return stop_cooldown_dict[symbol]
async def reduce_cooldowns_by_95_percent():
    """
    Mevcut cooldown sürelerini %95 kısaltır.
    Sadece bot sahibi tarafından kullanılabilir.
    """
    try:
        if mongo_collection is None:
            if not connect_mongodb():
                print("❌ MongoDB bağlantısı kurulamadı, cooldown'lar kısaltılamadı")
                return False, "MongoDB bağlantısı kurulamadı"
        
        # Mevcut cooldown'ları yükle
        stop_cooldown = load_stop_cooldown_from_db()
        
        if not stop_cooldown:
            return True, "Cooldown'da hiç kripto bulunmuyor"
        
        current_time = datetime.now()
        reduced_count = 0
        reduced_symbols = []
        
        # Her cooldown'ı %95 kısalt
        for symbol, cooldown_until in stop_cooldown.items():
            if isinstance(cooldown_until, str):
                try:
                    cooldown_until = datetime.fromisoformat(cooldown_until.replace('Z', '+00:00'))
                except:
                    continue
            elif not isinstance(cooldown_until, datetime):
                continue
            
            # Eğer cooldown henüz bitmemişse
            if cooldown_until > current_time:
                # Kalan süreyi hesapla
                remaining_time = cooldown_until - current_time
                
                # %95 kısalt (sadece %5'i kalacak)
                new_remaining_time = remaining_time * 0.05
                new_cooldown_until = current_time + new_remaining_time
                
                # Cooldown'ı güncelle
                stop_cooldown[symbol] = new_cooldown_until
                reduced_count += 1
                
                # Kısaltılan süreleri kaydet
                old_hours = remaining_time.total_seconds() / 3600
                new_hours = new_remaining_time.total_seconds() / 3600
                reduced_symbols.append(f"{symbol}: {old_hours:.1f}h → {new_hours:.1f}h")
        
        if reduced_count > 0:
            # Güncellenmiş cooldown'ları kaydet
            save_stop_cooldown_to_db(stop_cooldown)
            
            result_message = f"✅ {reduced_count} kripto için cooldown %95 kısaltıldı!\n\n"
            result_message += "Kısaltılan kriptolar:\n"
            for symbol_info in reduced_symbols[:10]:  # İlk 10'unu göster
                result_message += f"• {symbol_info}\n"
            
            if len(reduced_symbols) > 10:
                result_message += f"... ve {len(reduced_symbols) - 10} tane daha"
            
            print(f"🔄 {reduced_count} cooldown %95 kısaltıldı")
            return True, result_message
        else:
            return True, "Kısaltılacak aktif cooldown bulunamadı"
            
    except Exception as e:
        error_msg = f"Cooldown kısaltma hatası: {e}"
        print(f"❌ {error_msg}")
        return False, error_msg

def clear_data_by_pattern(pattern, description="veri"):
    """Regex pattern ile eşleşen verileri MongoDB'den siler"""
    try:
        if mongo_collection is None:
            if not connect_mongodb():
                print(f"❌ MongoDB bağlantısı kurulamadı, {description} silinemedi")
                return 0
        
        # Önce kaç tane doküman olduğunu kontrol et
        before_count = mongo_collection.count_documents({"_id": {"$regex": pattern}})
        print(f"🔍 {description} temizleme öncesi: {before_count} doküman bulundu")
        
        delete_result = mongo_collection.delete_many({"_id": {"$regex": pattern}})
        deleted_count = getattr(delete_result, "deleted_count", 0)
        
        # Sonra kaç tane kaldığını kontrol et
        after_count = mongo_collection.count_documents({"_id": {"$regex": pattern}})
        print(f"🧹 MongoDB'den {deleted_count} {description} silindi, {after_count} kaldı")
        
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
    
def save_stop_cooldown_to_db(stop_cooldown):
    """Stop cooldown verilerini MongoDB'ye kaydet"""
    try:
        if mongo_collection is None:
            if not connect_mongodb():
                print("❌ MongoDB bağlantısı kurulamadı, stop cooldown kaydedilemedi")
                return False
        
        # Mevcut cooldown'ları güncelle, yeni olanları ekle
        for symbol, cooldown_until in stop_cooldown.items():
            doc_id = f"stop_cooldown_{symbol}"
            # cooldown_until artık direkt bitiş zamanı (timestamp + 8 saat)
            
            # Upsert kullan: varsa güncelle, yoksa ekle
            mongo_collection.update_one(
                {"_id": doc_id},
                {
                    "$set": {
                        "data": cooldown_until - timedelta(hours=CONFIG["COOLDOWN_HOURS"]),  # Başlangıç zamanı
                        "until": cooldown_until,  # Bitiş zamanı
                        "timestamp": datetime.now()
                    }
                },
                upsert=True  # Yoksa ekle, varsa güncelle
            )
        
        print(f"✅ {len(stop_cooldown)} stop cooldown MongoDB'ye kaydedildi")
        return True
    except Exception as e:
        print(f"❌ Stop cooldown MongoDB'ye kaydedilirken hata: {e}")
        return False

async def cleanup_expired_stop_cooldowns():
    """Veritabanındaki süresi dolmuş stop cooldown'ları temizler."""
    try:
        if mongo_collection is None:
            if not connect_mongodb():
                print("❌ MongoDB bağlantısı kurulamadı, cooldown temizlenemedi")
                return 0
        
        current_time = datetime.now()
        
        # Süresi dolmuş cooldown'ları bul ('until' alanı şu anki zamandan küçük veya eşit olanlar)
        expired_docs_cursor = mongo_collection.find({
            "_id": {"$regex": "^stop_cooldown_"},
            "until": {"$lte": current_time}
        })
        
        expired_symbols = [doc["_id"].replace("stop_cooldown_", "") for doc in expired_docs_cursor]
        
        if not expired_symbols:
            # print("ℹ️ Süresi dolmuş stop cooldown bulunamadı.")
            return 0
            
        print(f"🧹 Süresi dolmuş {len(expired_symbols)} stop cooldown bulundu: {', '.join(expired_symbols)}")
        
        # Bulunan süresi dolmuş cooldown'ları sil
        delete_result = mongo_collection.delete_many({
            "_id": {"$in": [f"stop_cooldown_{symbol}" for symbol in expired_symbols]}
        })
        
        deleted_count = getattr(delete_result, "deleted_count", 0)
        print(f"✅ Veritabanından {deleted_count} süresi dolmuş stop cooldown temizlendi.")
        return deleted_count

    except Exception as e:
        print(f"❌ Süresi dolmuş stop cooldown'lar temizlenirken hata: {e}")
        return 0

async def close_position(symbol, trigger_type, final_price, signal, position_data=None):
    # Global değişkenleri kullan
    global active_signals, position_processing_flags, global_active_signals

    # İşlem flag'i set et (race condition önleme)
    position_processing_flags[symbol] = datetime.now()
    
    print(f"--- Pozisyon Kapatılıyor: {symbol} ({trigger_type}) ---")
    try:
        # Pozisyon durumu kontrolü kaldırıldı - her tetikleme işlenmeli
        print(f"🔄 {symbol} - {trigger_type} işleniyor...")
        
        # Pozisyon durumunu 'closing' olarak işaretle ve trigger_type'ı kaydet
        if symbol in active_signals:
            active_signals[symbol]['status'] = 'closing'
            active_signals[symbol]['trigger_type'] = trigger_type
        
        # Mesaj tekrarını engellemek için kontrol
        message_sent_key = f"message_sent_{symbol}_{trigger_type}"
        if message_sent_key in position_processing_flags:
            print(f"⚠️ {symbol} - {trigger_type} mesajı zaten gönderilmiş, tekrar gönderilmiyor.")
            return
        try:
            if position_data:
                entry_price_raw = position_data.get('open_price', 0)
                target_price_raw = position_data.get('target', 0)
                stop_loss_raw = position_data.get('stop', 0)
                entry_price = float(entry_price_raw) if entry_price_raw is not None else 0.0
                target_price = float(target_price_raw) if target_price_raw is not None else 0.0
                stop_loss_price = float(stop_loss_raw) if stop_loss_raw is not None else 0.0
                signal_type = str(position_data.get('type', 'ALIŞ'))
                leverage = int(position_data.get('leverage', 10))
                
            else:
                entry_price_raw = signal.get('entry_price_float', 0)
                target_price_raw = signal.get('target_price', '0')
                stop_loss_raw = signal.get('stop_loss', '0')
                entry_price = float(entry_price_raw) if entry_price_raw is not None else 0.0
                
                # String formatındaki fiyatları temizle
                if isinstance(target_price_raw, str):
                    target_price = float(target_price_raw.replace('$', '').replace(',', ''))
                else:
                    target_price = float(target_price_raw) if target_price_raw is not None else 0.0
                
                if isinstance(stop_loss_raw, str):
                    stop_loss_price = float(stop_loss_raw.replace('$', '').replace(',', ''))
                else:
                    stop_loss_price = float(stop_loss_raw) if stop_loss_raw is not None else 0.0
                
                signal_type = str(signal.get('type', 'ALIŞ'))
                leverage = int(signal.get('leverage', 10))
                        
        except (ValueError, TypeError) as e:
            print(f"❌ {symbol} - Pozisyon verisi dönüşüm hatası: {e}")
            print(f"   position_data: {position_data}")
            print(f"   signal: {signal}")
            # Hatalı pozisyonu temizle
            mongo_collection.delete_one({"_id": f"position_{symbol}"})
            mongo_collection.delete_one({"_id": f"active_signal_{symbol}"})
            global_positions.pop(symbol, None)
            global_active_signals.pop(symbol, None)
            return
        
        # Giriş fiyatı 0 ise pozisyonu temizle ve çık
        if entry_price <= 0:
            print(f"⚠️ {symbol} - Geçersiz giriş fiyatı ({entry_price}), pozisyon temizleniyor")
            # Pozisyonu veritabanından sil
            mongo_collection.delete_one({"_id": f"position_{symbol}"})
            mongo_collection.delete_one({"_id": f"active_signal_{symbol}"})
            # Bellekteki global değişkenlerden de temizle
            global_positions.pop(symbol, None)
            global_active_signals.pop(symbol, None)
            return
        
        # trigger_type None ise pozisyon durumunu analiz et ve otomatik belirle
        if trigger_type is None:
            print(f"🔍 {symbol} - trigger_type None, pozisyon durumu analiz ediliyor...")
            
            # Güncel fiyat bilgisini al
            try:
                df1m = await async_get_historical_data(symbol, '1m', 1)
                if df1m is not None and not df1m.empty:
                    current_price = float(df1m['close'].iloc[-1])
                    print(f"🔍 {symbol} - Güncel fiyat: ${current_price:.6f}")
                    
                    # Pozisyon tipine göre hedef ve stop kontrolü
                    if signal_type == "LONG" or signal_type == "ALIS":
                        # LONG pozisyonu için
                        if current_price >= target_price:
                            trigger_type = "take_profit"
                            final_price = current_price
                            print(f"✅ {symbol} - Otomatik TP tespit edildi: ${current_price:.6f} >= ${target_price:.6f}")
                        elif current_price <= stop_loss_price:
                            trigger_type = "stop_loss"
                            final_price = current_price
                            print(f"❌ {symbol} - Otomatik SL tespit edildi: ${current_price:.6f} <= ${stop_loss_price:.6f}")
                        else:
                            # Pozisyon hala aktif, varsayılan olarak TP kabul et
                            trigger_type = "take_profit"
                            final_price = target_price
                            print(f"⚠️ {symbol} - Pozisyon hala aktif, varsayılan TP: ${target_price:.6f}")
                    
                    elif signal_type == "SHORT" or signal_type == "SATIS":
                        # SHORT pozisyonu için
                        if current_price <= target_price:
                            trigger_type = "take_profit"
                            final_price = current_price
                            print(f"✅ {symbol} - Otomatik TP tespit edildi: ${current_price:.6f} <= ${target_price:.6f}")
                        elif current_price >= stop_loss_price:
                            trigger_type = "stop_loss"
                            final_price = current_price
                            print(f"❌ {symbol} - Otomatik SL tespit edildi: ${current_price:.6f} >= ${stop_loss_price:.6f}")
                        else:
                            # Pozisyon hala aktif, varsayılan olarak TP kabul et
                            trigger_type = "take_profit"
                            final_price = target_price
                            print(f"⚠️ {symbol} - Pozisyon hala aktif, varsayılan TP: ${target_price:.6f}")
                    
                else:
                    # Fiyat bilgisi alınamadı, varsayılan değerler
                    trigger_type = "take_profit"
                    final_price = target_price
                    print(f"⚠️ {symbol} - Fiyat bilgisi alınamadı, varsayılan TP: ${target_price:.6f}")
                    
            except Exception as e:
                print(f"⚠️ {symbol} - Güncel fiyat alınamadı: {e}, varsayılan TP kullanılıyor")
                trigger_type = "take_profit"
                final_price = target_price
        
        # Final price'ı güvenli şekilde dönüştür
        try:
            final_price_float = float(final_price) if final_price is not None else 0.0
        except (ValueError, TypeError) as e:
            print(f"❌ {symbol} - Final price dönüşüm hatası: {e}")
            final_price_float = 0.0
        
        # Kar/Zarar hesaplaması - SL/TP fiyatlarından hesaplama (gerçek piyasa fiyatından değil)
        profit_loss_percent = 0
        if entry_price > 0:
            try:
                if trigger_type == "take_profit":
                    # Take-profit: Hedef fiyatından çıkış (ne kadar yükselirse yükselsin)
                    if signal_type == "LONG" or signal_type == "ALIS":
                        profit_loss_percent = ((target_price - entry_price) / entry_price) * 100
                    else: # SHORT veya SATIS
                        profit_loss_percent = ((entry_price - target_price) / entry_price) * 100
                    print(f"🎯 {symbol} - TP hesaplaması: Hedef fiyatından (${target_price:.6f}) çıkış")
                    
                elif trigger_type == "stop_loss":
                    # Stop-loss: Stop fiyatından çıkış (ne kadar düşerse düşsün)
                    if signal_type == "LONG" or signal_type == "ALIS":
                        profit_loss_percent = ((stop_loss_price - entry_price) / entry_price) * 100
                    else: # SHORT veya SATIS
                        profit_loss_percent = ((entry_price - stop_loss_price) / entry_price) * 100
                    
                else:
                    # Varsayılan durum (final_price kullan)
                    if signal_type == "LONG" or signal_type == "ALIS":
                        profit_loss_percent = ((final_price_float - entry_price) / entry_price) * 100
                    else: # SHORT veya SATIS
                        profit_loss_percent = ((entry_price - final_price_float) / entry_price) * 100
                    print(f"⚠️ {symbol} - Varsayılan hesaplama: Final fiyattan (${final_price_float:.6f}) çıkış")
                 
            except Exception as e:
                print(f"❌ {symbol} - Kâr/zarar hesaplama hatası: {e}")
                profit_loss_percent = 0
        else:
            print(f"⚠️ {symbol} - Geçersiz giriş fiyatı ({entry_price}), kâr/zarar hesaplanamadı")
            profit_loss_percent = 0
        
        profit_loss_usd = (100 * (profit_loss_percent / 100)) * leverage # 100$ ve kaldıraç ile
        
        # İstatistikleri atomik olarak güncelle (Race condition'ları önler)
        print(f"🔍 {symbol} - Pozisyon kapatılıyor: {trigger_type} - ${final_price_float:.6f}")
            
        if trigger_type == "take_profit":
            # Atomik güncelleme ile istatistikleri güncelle
            update_stats_atomic({
                "successful_signals": 1,
                "total_profit_loss": profit_loss_usd
            })
            
            # Take-profit mesajında hedef fiyatından çıkış göster
            exit_price = target_price if trigger_type == "take_profit" else final_price_float
            message = (
                f"🎯 <b>HEDEF GERÇEKLEŞTİ!</b> 🎯\n\n"
                f"🔹 <b>Kripto Çifti:</b> {symbol}\n"
                f"💰 <b>Kar:</b> %{profit_loss_percent:.2f} (${profit_loss_usd:.2f})\n"
                f"📈 <b>Giriş:</b> ${entry_price:.6f}\n"
                f"💵 <b>Çıkış:</b> ${exit_price:.6f}"
            )
            await send_signal_to_all_users(message)
            # Mesaj gönderildi flag'ini set et
            position_processing_flags[message_sent_key] = datetime.now()
            # Bot sahibine hedef mesajı gönderme
        
        elif trigger_type == "stop_loss":
            update_stats_atomic({
                "failed_signals": 1,
                "total_profit_loss": profit_loss_usd
            })
            position_processing_flags[message_sent_key] = datetime.now()
        
        # Pozisyonu veritabanından sil - GÜÇLÜ TEMİZLEME
        try:
            # Önce active_signal belgesini sil
            delete_result = mongo_collection.delete_one({"_id": f"active_signal_{symbol}"})
            if delete_result.deleted_count > 0:
                print(f"✅ {symbol} active_signal belgesi veritabanından silindi")
            else:
                print(f"⚠️ {symbol} active_signal belgesi zaten silinmiş veya bulunamadı")
            
            # Sonra position belgesini sil
            delete_result = mongo_collection.delete_one({"_id": f"position_{symbol}"})
            if delete_result.deleted_count > 0:
                print(f"✅ {symbol} position belgesi veritabanından silindi")
            else:
                print(f"⚠️ {symbol} position belgesi zaten silinmiş veya bulunamadı")
            
            # EK GÜVENLİK: Boş aktif sinyal listesi kaydet (eğer başka aktif sinyal kalmadıysa)
            remaining_positions = mongo_collection.count_documents({"_id": {"$regex": "^position_"}})
            if remaining_positions == 0:
                # Tüm pozisyonlar kapandıysa, boş aktif sinyal listesi kaydet
                save_active_signals_to_db({})
                print(f"🧹 Tüm pozisyonlar kapandı, boş aktif sinyal listesi kaydedildi")
                
        except Exception as e:
            print(f"❌ {symbol} veritabanından silinirken hata: {e}")
            # Hata durumunda tekrar dene
            try:
                mongo_collection.delete_one({"_id": f"active_signal_{symbol}"})
                mongo_collection.delete_one({"_id": f"position_{symbol}"})
                print(f"✅ {symbol} veritabanından ikinci denemede silindi")
            except Exception as e2:
                print(f"❌ {symbol} veritabanından ikinci denemede de silinemedi: {e2}")
        
        # Cooldown'a ekle (8 saat) - Güvenli ekleme
        global global_stop_cooldown
        cooldown_end_time = add_stop_cooldown_safe(symbol, global_stop_cooldown)
        
        # Cooldown'ı veritabanına kaydet
        save_stop_cooldown_to_db({symbol: cooldown_end_time})
        
        # Bellekteki global değişkenlerden de temizle
        global_positions.pop(symbol, None)
        global_active_signals.pop(symbol, None)
        
        # Ek güvenlik: active_signals listesinden de kaldır
        if symbol in active_signals:
            del active_signals[symbol]
            print(f"✅ {symbol} active_signals listesinden kaldırıldı")
        
        # Global active_signals'ı da güncelle
        if symbol in global_active_signals:
            del global_active_signals[symbol]
            print(f"✅ {symbol} global_active_signals'dan kaldırıldı")
        
        # Pozisyon ve aktif sinyalleri veritabanına kaydet (güncel durumu yansıtmak için)
        try:
            updated_positions = load_positions_from_db()
            updated_active_signals = load_active_signals_from_db()
            
            # Eğer hala veritabanında kalan pozisyon/aktif sinyal varsa, onları da temizle
            if symbol in updated_positions:
                print(f"⚠️ {symbol} veritabanında hala position var, manuel temizleniyor")
                mongo_collection.delete_one({"_id": f"position_{symbol}"})
            if symbol in updated_active_signals:
                print(f"⚠️ {symbol} veritabanında hala active_signal var, manuel temizleniyor")
                mongo_collection.delete_one({"_id": f"active_signal_{symbol}"})
        except Exception as e:
            print(f"⚠️ {symbol} veritabanı temizliği sırasında hata: {e}")
        
        print(f"✅ {symbol} pozisyonu başarıyla kapatıldı ve 8 saat cooldown'a eklendi (pozisyon kapandığı zamandan itibaren)")
        
    except Exception as e:
        print(f"❌ {symbol} pozisyon kapatılırken hata: {e}")
        # Hata durumunda da pozisyonu temizlemeye çalış
        try:
            mongo_collection.delete_one({"_id": f"position_{symbol}"})
            mongo_collection.delete_one({"_id": f"active_signal_{symbol}"})
            global_positions.pop(symbol, None)
            global_active_signals.pop(symbol, None)
            print(f"✅ {symbol} pozisyonu hata sonrası temizlendi")
        except:
            pass

def cleanup_corrupted_positions():
    """MongoDB'deki bozuk pozisyon verilerini temizler"""
    try:
        if mongo_collection is None:
            if not connect_mongodb():
                print("❌ MongoDB bağlantısı kurulamadı, bozuk pozisyonlar temizlenemedi")
                return False
        
        print("🧹 Bozuk pozisyon verileri temizleniyor...")
        
        # Tüm pozisyon belgelerini kontrol et
        docs = mongo_collection.find({"_id": {"$regex": "^position_"}})
        corrupted_count = 0
        
        for doc in docs:
            symbol = doc["_id"].replace("position_", "")
            data = doc.get("data", {})
            
            # Kritik alanların varlığını kontrol et
            required_fields = ['type', 'target', 'stop', 'open_price', 'leverage']
            missing_fields = [field for field in required_fields if field not in data]
            
            if missing_fields:
                print(f"⚠️ {symbol} - Eksik alanlar: {missing_fields}, pozisyon siliniyor")
                mongo_collection.delete_one({"_id": f"position_{symbol}"})
                mongo_collection.delete_one({"_id": f"active_signal_{symbol}"})
                corrupted_count += 1
                continue
            
            # Fiyat değerlerinin geçerliliğini kontrol et
            try:
                open_price = float(data['open_price'])
                target_price = float(data['target'])
                stop_price = float(data['stop'])
                
                if open_price <= 0 or target_price <= 0 or stop_price <= 0:
                    print(f"⚠️ {symbol} - Geçersiz fiyat değerleri, pozisyon siliniyor")
                    print(f"   Giriş: {open_price}, Hedef: {target_price}, Stop: {stop_price}")
                    mongo_collection.delete_one({"_id": f"position_{symbol}"})
                    mongo_collection.delete_one({"_id": f"active_signal_{symbol}"})
                    corrupted_count += 1
                    continue
                    
            except (ValueError, TypeError) as e:
                print(f"⚠️ {symbol} - Fiyat dönüşüm hatası: {e}, pozisyon siliniyor")
                mongo_collection.delete_one({"_id": f"position_{symbol}"})
                mongo_collection.delete_one({"_id": f"active_signal_{symbol}"})
                corrupted_count += 1
                continue
        
        if corrupted_count > 0:
            print(f"✅ {corrupted_count} bozuk pozisyon verisi temizlendi")
        else:
            print("✅ Bozuk pozisyon verisi bulunamadı")
        
        return True
        
    except Exception as e:
        print(f"❌ Bozuk pozisyonlar temizlenirken hata: {e}")
        return False

if __name__ == "__main__":
    asyncio.run(main())