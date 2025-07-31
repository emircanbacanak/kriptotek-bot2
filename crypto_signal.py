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
    """İzin verilen kullanıcıları MongoDB'den yükle"""
    global ALLOWED_USERS
    try:
        if not connect_mongodb():
            print("⚠️ MongoDB bağlantısı kurulamadı, boş liste ile başlatılıyor")
            ALLOWED_USERS = set()
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
        else:
            print("⚠️ MongoDB collection bulunamadı, boş liste ile başlatılıyor")
            ALLOWED_USERS = set()
    except Exception as e:
        print(f"❌ MongoDB'den kullanıcılar yüklenirken hata: {e}")
        ALLOWED_USERS = set()

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

def close_mongodb():
    """MongoDB bağlantısını kapat"""
    global mongo_client
    if mongo_client:
        try:
            mongo_client.close()
            print("✅ MongoDB bağlantısı kapatıldı")
        except Exception as e:
            print(f"⚠️ MongoDB bağlantısı kapatılırken hata: {e}")

# Bot handler'ları için global değişkenler
app = None

# MongoDB kullanıldığı için dosya referansını kaldırıyoruz

# Global değişkenler (main fonksiyonundan erişim için)
global_stats = {}
global_active_signals = {}
global_successful_signals = {}
global_failed_signals = {}

async def send_telegram_message(message, chat_id=None):
    """Telegram'a mesaj gönder"""
    if chat_id is None:
        chat_id = TELEGRAM_CHAT_ID
    
    try:
        await bot.send_message(chat_id=chat_id, text=message, parse_mode='HTML')
        return True
    except Exception as e:
        print(f"❌ Mesaj gönderme hatası (chat_id: {chat_id}): {e}")
        return False

async def send_signal_to_all_users(message):
    """Sinyali tüm kayıtlı kullanıcılara gönder"""
    sent_chats = set()  # Gönderilen chat'leri takip et
    
    # Ana chat'e gönder (öncelikli)
    if TELEGRAM_CHAT_ID:
        await send_telegram_message(message, TELEGRAM_CHAT_ID)
        sent_chats.add(str(TELEGRAM_CHAT_ID))
        print(f"✅ Ana chat'e sinyal gönderildi: {TELEGRAM_CHAT_ID}")
    
    # İzin verilen kullanıcılara gönder (ana chat'te olmayanlar)
    for user_id in ALLOWED_USERS:
        if str(user_id) not in sent_chats:  # Ana chat'te değilse
            try:
                await send_telegram_message(message, user_id)
                print(f"✅ Kullanıcıya sinyal gönderildi: {user_id}")
                sent_chats.add(str(user_id))
            except Exception as e:
                print(f"❌ Kullanıcıya sinyal gönderilemedi ({user_id}): {e}")
    
    # Bot sahibine ayrıca gönder (eğer ana chat'te ve ALLOWED_USERS'da değilse)
    if BOT_OWNER_ID and str(BOT_OWNER_ID) not in sent_chats:
        await send_telegram_message(message, BOT_OWNER_ID)
        print(f"✅ Bot sahibine sinyal gönderildi: {BOT_OWNER_ID}")
    elif BOT_OWNER_ID and str(BOT_OWNER_ID) in sent_chats:
        print(f"ℹ️ Bot sahibi zaten mesaj aldı (chat_id: {BOT_OWNER_ID})")

async def start_command(update, context):
    """Bot başlatma komutu"""
    if update.effective_chat.type != "private":
        return  # Sadece özel sohbetlerde çalışsın, grupsa hiçbir şey yapma
    
    user_id = update.effective_user.id
    if user_id != BOT_OWNER_ID and user_id not in ALLOWED_USERS:
        await update.message.reply_text("❌ Bu botu kullanma yetkiniz yok. Sadece bot sahibi ve izin verilen kullanıcılar bu botu kullanabilir.")
        return
    
    # Yasaklı saatlerde de komutlar çalışsın
    await update.message.reply_text("🚀 Kripto Sinyal Botu başlatıldı!\n\nBu bot kripto para sinyallerini takip eder ve size bildirim gönderir.")

async def help_command(update, context):
    """Yardım komutu"""
    if update.effective_chat.type != "private":
        return  # Sadece özel sohbetlerde çalışsın, grupsa hiçbir şey yapma
    
    user_id = update.effective_user.id
    if user_id != BOT_OWNER_ID and user_id not in ALLOWED_USERS:
        await update.message.reply_text("❌ Bu botu kullanma yetkiniz yok.")
        return
    
    help_text = """
🤖 **Kripto Sinyal Botu Komutları:**

/start - Botu başlat
/help - Bu yardım mesajını göster
/stats - İstatistikleri göster
/active - Aktif sinyalleri göster
/adduser <user_id> - Kullanıcı ekle (sadece bot sahibi)
/removeuser <user_id> - Kullanıcı çıkar (sadece bot sahibi)
/listusers - İzin verilen kullanıcıları listele (sadece bot sahibi)
    """
    await update.message.reply_text(help_text, parse_mode='Markdown')

async def stats_command(update, context):
    """İstatistik komutu"""
    if update.effective_chat.type != "private":
        return  # Sadece özel sohbetlerde çalışsın, grupsa hiçbir şey yapma
    
    user_id = update.effective_user.id
    if user_id != BOT_OWNER_ID and user_id not in ALLOWED_USERS:
        await update.message.reply_text("❌ Bu botu kullanma yetkiniz yok.")
        return
    
    # Global istatistikleri kullan
    stats = global_stats
    if not stats:
        stats_text = "📊 **Bot İstatistikleri:**\n\nHenüz istatistik verisi yok."
    else:
        closed_count = stats.get('successful_signals', 0) + stats.get('failed_signals', 0)
        success_rate = 0
        if closed_count > 0:
            success_rate = (stats.get('successful_signals', 0) / closed_count) * 100
        
        # Yasaklı saatlerde olup olmadığını kontrol et
        current_time = get_tr_time()
        is_restricted_hours = not is_signal_search_allowed()
        status_emoji = "⏸️" if is_restricted_hours else "🟢"
        status_text = "Yasaklı Saatler (Sinyal Arama Durduruldu)" if is_restricted_hours else "Aktif (Sinyal Arama Çalışıyor)"
        
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
    if update.effective_chat.type != "private":
        return  # Sadece özel sohbetlerde çalışsın, grupsa hiçbir şey yapma
    
    user_id = update.effective_user.id
    if user_id != BOT_OWNER_ID and user_id not in ALLOWED_USERS:
        await update.message.reply_text("❌ Bu botu kullanma yetkiniz yok.")
        return
    
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
    
    # Yasaklı saatlerde olup olmadığını kontrol et
    is_restricted_hours = not is_signal_search_allowed()
    if is_restricted_hours:
        active_text += "\n⚠️ **Not:** Şu anda yasaklı saatlerde olduğunuz için yeni sinyal arama durdurulmuştur, ancak mevcut aktif sinyaller takip edilmeye devam etmektedir."
    
    await update.message.reply_text(active_text, parse_mode='Markdown')

async def adduser_command(update, context):
    """Kullanıcı ekleme komutu (sadece bot sahibi)"""
    if update.effective_chat.type != "private":
        return  # Sadece özel sohbetlerde çalışsın, grupsa hiçbir şey yapma
    
    user_id = update.effective_user.id
    if user_id != BOT_OWNER_ID:
        await update.message.reply_text("❌ Bu komutu sadece bot sahibi kullanabilir.")
        return
    
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
        
        ALLOWED_USERS.add(new_user_id)
        save_allowed_users()  # Dosyaya kaydet
        await update.message.reply_text(f"✅ Kullanıcı {new_user_id} başarıyla eklendi ve kalıcı olarak kaydedildi.")
    except ValueError:
        await update.message.reply_text("❌ Geçersiz user_id. Lütfen sayısal bir değer girin.")

async def removeuser_command(update, context):
    """Kullanıcı çıkarma komutu (sadece bot sahibi)"""
    if update.effective_chat.type != "private":
        return  # Sadece özel sohbetlerde çalışsın, grupsa hiçbir şey yapma
    
    user_id = update.effective_user.id
    if user_id != BOT_OWNER_ID:
        await update.message.reply_text("❌ Bu komutu sadece bot sahibi kullanabilir.")
        return
    
    if not context.args:
        await update.message.reply_text("❌ Kullanım: /removeuser <user_id>")
        return
    
    try:
        remove_user_id = int(context.args[0])
        if remove_user_id in ALLOWED_USERS:
            ALLOWED_USERS.remove(remove_user_id)
            save_allowed_users()  # Dosyaya kaydet
            await update.message.reply_text(f"✅ Kullanıcı {remove_user_id} başarıyla çıkarıldı ve kalıcı olarak kaydedildi.")
        else:
            await update.message.reply_text(f"❌ Kullanıcı {remove_user_id} zaten izin verilen kullanıcılar listesinde yok.")
    except ValueError:
        await update.message.reply_text("❌ Geçersiz user_id. Lütfen sayısal bir değer girin.")

async def listusers_command(update, context):
    """İzin verilen kullanıcıları listeleme komutu (sadece bot sahibi)"""
    if update.effective_chat.type != "private":
        return  # Sadece özel sohbetlerde çalışsın, grupsa hiçbir şey yapma
    
    user_id = update.effective_user.id
    if user_id != BOT_OWNER_ID:
        await update.message.reply_text("❌ Bu komutu sadece bot sahibi kullanabilir.")
        return
    
    if not ALLOWED_USERS:
        users_text = "📋 **İzin Verilen Kullanıcılar:**\n\nHenüz izin verilen kullanıcı yok."
    else:
        users_list = "\n".join([f"• {user_id}" for user_id in ALLOWED_USERS])
        users_text = f"📋 **İzin Verilen Kullanıcılar:**\n\n{users_list}"
    
    await update.message.reply_text(users_text, parse_mode='Markdown')

async def handle_message(update, context):
    """Genel mesaj handler'ı"""
    if update.effective_chat.type != "private":
        return  # Sadece özel sohbetlerde çalışsın, grupsa hiçbir şey yapma
    
    user_id = update.effective_user.id
    if user_id != BOT_OWNER_ID and user_id not in ALLOWED_USERS:
        await update.message.reply_text("❌ Bu botu kullanma yetkiniz yok. Sadece bot sahibi ve izin verilen kullanıcılar bu botu kullanabilir.")
        return
    
    await update.message.reply_text("🤖 Bu bot sadece komutları destekler. /help yazarak mevcut komutları görebilirsiniz.")

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
            await app.updater.start_polling(drop_pending_updates=True, allowed_updates=["message", "callback_query"])
            print("✅ Bot yeniden başlatıldı")
            
        except Exception as e:
            print(f"❌ Bot yeniden başlatma hatası: {e}")
        return
    
    # Diğer hataları logla
    print(f"Bot hatası: {error}")
    
    if update and update.effective_chat:
        if update.effective_chat.type == "private":
            user_id = update.effective_user.id
            if user_id == BOT_OWNER_ID or user_id in ALLOWED_USERS:
                await update.message.reply_text("❌ Bir hata oluştu. Lütfen daha sonra tekrar deneyin.")

async def handle_chat_member_update(update, context):
    """Grup ekleme/çıkarma olaylarını dinler"""
    chat = update.effective_chat
    
    # Yeni üye eklenme durumu
    if update.message and update.message.new_chat_members:
        for new_member in update.message.new_chat_members:
            # Bot'un kendisi eklenmiş mi?
            if new_member.id == context.bot.id:
                # Bot sahibi tarafından mı eklendi?
                user_id = update.effective_user.id
                
                if user_id != BOT_OWNER_ID:
                    # Bot sahibi olmayan biri ekledi, gruptan çık
                    try:
                        await context.bot.leave_chat(chat.id)
                        print(f"❌ Bot sahibi olmayan {user_id} tarafından {chat.title} grubuna eklenmeye çalışıldı. Bot gruptan çıktı.")
                        
                        # Bot sahibine bildirim gönder
                        warning_msg = f"⚠️ **GÜVENLİK UYARISI** ⚠️\n\nBot sahibi olmayan bir kullanıcı ({user_id}) bot'u '{chat.title}' grubuna eklemeye çalıştı.\n\nBot otomatik olarak gruptan çıktı.\n\nGrup ID: {chat.id}"
                        await send_telegram_message(warning_msg)
                        
                    except Exception as e:
                        print(f"Gruptan çıkma hatası: {e}")
                else:
                    print(f"✅ Bot sahibi tarafından {chat.title} grubuna eklendi.")
    
    # Üye çıkma durumu
    elif update.message and update.message.left_chat_member:
        left_member = update.message.left_chat_member
        # Bot'un kendisi çıkarılmış mı?
        if left_member.id == context.bot.id:
            print(f"Bot {chat.title} grubundan çıkarıldı.")

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
    app.add_handler(CommandHandler("start", start_command))
    app.add_handler(CommandHandler("help", help_command))
    app.add_handler(CommandHandler("stats", stats_command))
    app.add_handler(CommandHandler("active", active_command))
    app.add_handler(CommandHandler("adduser", adduser_command))
    app.add_handler(CommandHandler("removeuser", removeuser_command))
    app.add_handler(CommandHandler("listusers", listusers_command))
    
    # Genel mesaj handler'ı
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))
    
    # Grup ekleme/çıkarma handler'ı - ChatMemberUpdated event'ini dinle
    app.add_handler(MessageHandler(filters.StatusUpdate.NEW_CHAT_MEMBERS, handle_chat_member_update))
    app.add_handler(MessageHandler(filters.StatusUpdate.LEFT_CHAT_MEMBER, handle_chat_member_update))
    
    # Hata handler'ı
    app.add_error_handler(error_handler)
    
    print("Bot handler'ları kuruldu!")

def is_signal_search_allowed():
    """Sinyal aramaya izin verilen saatleri kontrol eder"""
    now = get_tr_time()
    current_hour = now.hour
    # Yasaklı saat aralıkları: 01:00-08:00 ve 13:00-17:00
    if (1 <= current_hour < 8) or (13 <= current_hour < 17):
        return False
    return True

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

def create_signal_message(symbol, price, signals, volume):
    """Sinyal mesajını oluştur (AL/SAT başlıkta)"""
    price_str = format_price(price, price)
    signal_1h = "ALIŞ" if signals.get('1h', 0) == 1 else "SATIŞ"
    signal_2h = "ALIŞ" if signals.get('2h', 0) == 1 else "SATIŞ" 
    signal_4h = "ALIŞ" if signals.get('4h', 0) == 1 else "SATIŞ"
    signal_1d = "ALIŞ" if signals.get('1d', 0) == 1 else "SATIŞ"
    buy_count = sum(1 for s in signals.values() if s == 1)
    sell_count = sum(1 for s in signals.values() if s == -1)
    
    if buy_count == 4:
        dominant_signal = "ALIŞ"
        target_price = price * 1.03  # %3 hedef
        stop_loss = price * 0.985    # %1.5 stop
        sinyal_tipi = "AL SİNYALİ"
        leverage = 10
    elif sell_count == 4:
        dominant_signal = "SATIŞ"
        target_price = price * 0.97  # %3 hedef
        stop_loss = price * 1.015    # %1.5 stop
        sinyal_tipi = "SAT SİNYALİ"
        leverage = 10
    else:
        return None, None, None, None, None
    
    target_price_str = format_price(target_price, price)
    stop_loss_str = format_price(stop_loss, price)
    volume_int = int(volume)  # Volume to integer
    message = f"""
    🚨 {sinyal_tipi} 🚨

    🔹 Kripto Çifti: {symbol}  
    💵 Fiyat: {price_str}
    📈 Hedef Fiyat: {target_price_str}  
    🛑 Stop Loss: {stop_loss_str}  
    📊 Kaldıraç Önerisi: {leverage}x
    📉 Hacim: {volume_int}

    ⚠️ YATIRIM TAVSİYESİ DEĞİLDİR ⚠️

    📋 Dikkat Edilmesi Gerekenler:  
    • Stop loss kullanmayı unutmayın  
    • Acele karar vermeyin  
    • Kendi araştırmanızı yapın"""

    return message, dominant_signal, target_price, stop_loss, stop_loss_str

async def async_get_historical_data(symbol, interval, lookback):
    """Binance'den geçmiş verileri asenkron çek"""
    url = f"https://api.binance.com/api/v3/klines?symbol={symbol}&interval={interval}&limit={lookback}"
    try:
        async with aiohttp.ClientSession(connector=aiohttp.TCPConnector(limit=10)) as session:
            async with session.get(url, ssl=False) as resp:
                if resp.status != 200:
                    raise Exception(f"API hatası: {resp.status} - {await resp.text()}")
                klines = await resp.json()
                if not klines or len(klines) == 0:
                    raise Exception(f"{symbol} için veri yok")
    except Exception as e:
        raise Exception(f"Veri çekme hatası: {symbol} - {interval} - {str(e)}")
    
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
    Pine Script'e birebir uyumlu AL/SAT sinyal hesaplaması.
    Zaman dilimine göre özel parametreler içerir.
    """
    # --- Zaman dilimine göre sabit parametreler ---
    if timeframe in ['1h', '2h']:
        rsi_length = 14
        macd_fast = 10
        macd_slow = 20
        macd_signal = 9
        short_ma_period = 9
        long_ma_period = 50
        mfi_length = 14
        fib_lookback = 50
    elif timeframe == '4h':
        rsi_length = 18
        macd_fast = 11
        macd_slow = 22
        macd_signal = 8
        short_ma_period = 12
        long_ma_period = 60
        mfi_length = 16
        fib_lookback = 70
    elif timeframe == '1d':
        rsi_length = 21
        macd_fast = 13
        macd_slow = 26
        macd_signal = 10
        short_ma_period = 20
        long_ma_period = 100
        mfi_length = 20
        fib_lookback = 100
    else:
        raise ValueError(f"Desteklenmeyen zaman dilimi: {timeframe}")

    atr_period = 10
    atr_multiplier = 3

    # EMA 200 ve trend
    df['ema200'] = ta.trend.EMAIndicator(df['close'], window=200).ema_indicator()
    df['trend_bullish'] = df['close'] > df['ema200']
    df['trend_bearish'] = df['close'] < df['ema200']

    # RSI
    df['rsi'] = ta.momentum.RSIIndicator(df['close'], window=rsi_length).rsi()
    rsi_overbought = 60
    rsi_oversold = 40

    # MACD
    macd = ta.trend.MACD(df['close'], window_slow=macd_slow, window_fast=macd_fast, window_sign=macd_signal)
    df['macd'] = macd.macd()
    df['macd_signal'] = macd.macd_signal()

    # Supertrend sabit değerlerle
    def supertrend(df, atr_period, multiplier):
        hl2 = (df['high'] + df['low']) / 2
        atr = ta.volatility.AverageTrueRange(df['high'], df['low'], df['close'], window=atr_period).average_true_range()
        upperband = hl2 + (multiplier * atr)
        lowerband = hl2 - (multiplier * atr)
        direction = [1]
        for i in range(1, len(df)):
            if df['close'].iloc[i] > upperband.iloc[i-1]:
                direction.append(1)
            elif df['close'].iloc[i] < lowerband.iloc[i-1]:
                direction.append(-1)
            else:
                direction.append(direction[-1])
        return pd.Series(direction, index=df.index)

    df['supertrend_dir'] = supertrend(df, atr_period, atr_multiplier)

    # MA'lar
    df['short_ma'] = ta.trend.EMAIndicator(df['close'], window=short_ma_period).ema_indicator()
    df['long_ma'] = ta.trend.EMAIndicator(df['close'], window=long_ma_period).ema_indicator()
    df['ma_bullish'] = df['short_ma'] > df['long_ma']
    df['ma_bearish'] = df['short_ma'] < df['long_ma']

    # Hacim & MFI
    df['volume_ma'] = df['volume'].rolling(window=20).mean()
    df['enough_volume'] = df['volume'] > df['volume_ma'] * 0.4

    df['mfi'] = ta.volume.MFIIndicator(df['high'], df['low'], df['close'], df['volume'], window=mfi_length).money_flow_index()
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

    # MACD fallback
    if df['signal'].iloc[-1] == 0:
        if df['macd'].iloc[-1] > df['macd_signal'].iloc[-1]:
            df.at[df.index[-1], 'signal'] = 1
        else:
            df.at[df.index[-1], 'signal'] = -1

    return df

async def get_active_high_volume_usdt_pairs(top_n=40):
    """
    Sadece spotta aktif, USDT bazlı coinlerden hacme göre sıralanmış ilk top_n kadar uygun coin döndürür.
    1 günlük verisi 30 mumdan az olan coin'ler elenir.
    """
    exchange_info = client.get_exchange_info()
    tickers = client.get_ticker()
    spot_usdt_pairs = set()
    for symbol in exchange_info['symbols']:
        if (
            symbol['quoteAsset'] == 'USDT' and
            symbol['status'] == 'TRADING' and
            symbol['isSpotTradingAllowed']
        ):
            spot_usdt_pairs.add(symbol['symbol'])

    high_volume_pairs = []
    for ticker in tickers:
        symbol = ticker['symbol']
        if symbol in ['USDCUSDT', 'FDUSDUSDT', 'TUSDUSDT', 'BUSDUSDT', 'USDPUSDT', 'USDTUSDT']:
            continue
        if symbol in spot_usdt_pairs:
            try:
                quote_volume = float(ticker['quoteVolume'])
                high_volume_pairs.append((symbol, quote_volume))
            except Exception:
                continue

    high_volume_pairs.sort(key=lambda x: x[1], reverse=True)

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
        except Exception as e:
            # Sessizce atla, mesaj yazdırma
            idx += 1
            continue
        idx += 1

    return uygun_pairs

async def signal_processing_loop():
    """Sinyal arama ve işleme döngüsü"""
    sent_signals = dict()  # {(symbol, sinyal_tipi): signal_values}
    positions = dict()  # {symbol: position_info}
    cooldown_signals = dict()  # {(symbol, sinyal_tipi): datetime}
    stop_cooldown = dict()  # {symbol: datetime}
    previous_signals = dict()  # {symbol: {tf: signal}} - İlk çalıştığında kaydedilen sinyaller
    stopped_coins = dict()  # {symbol: {...}}
    active_signals = dict()  # {symbol: {...}} - Aktif sinyaller
    successful_signals = dict()  # {symbol: {...}} - Başarılı sinyaller (hedefe ulaşan)
    failed_signals = dict()  # {symbol: {...}} - Başarısız sinyaller (stop olan)
    tracked_coins = set()  # Takip edilen tüm coinlerin listesi
    first_run = True  # İlk çalıştırma kontrolü
    
    # Genel istatistikler
    stats = {
        "total_signals": 0,
        "successful_signals": 0,
        "failed_signals": 0,
        "total_profit_loss": 0.0,  # 100$ yatırım için
        "active_signals_count": 0,
        "tracked_coins_count": 0
    }
    
    timeframes = {
        '1h': '1h',
        '2h': '2h', 
        '4h': '4h',
        '1d': '1d'
    }
    tf_names = ['1h', '2h', '4h', '1d'] 
    
    print("Sinyal botu başlatıldı!")
    print("İlk çalıştırma: Mevcut sinyaller kaydediliyor, değişiklik bekleniyor...")
    
    while True:
        try:
            # Mevcut saati kontrol et
            if not is_signal_search_allowed():
                current_time = get_tr_time()
                print(f"⏸️ Şu an saat {current_time.strftime('%H:%M')} - Sinyal arama durduruldu (01:00-08:00 ve 13:00-17:00 arası)")
                print(f"ℹ️ Bot komutları (/start, /help, /stats, /active, /adduser, /removeuser, /listusers) her zaman kullanılabilir")
                # Aktif pozisyonları kontrol etmeye devam et
                for symbol, pos in list(positions.items()):
                    try:
                        df = await async_get_historical_data(symbol, '2h', 3)  # Son 3 mumu al
                        last_price = float(df['close'].iloc[-1])
                        current_open = float(df['open'].iloc[-1])
                        current_high = float(df['high'].iloc[-1])
                        current_low = float(df['low'].iloc[-1])
                        
                        # Aktif sinyal bilgilerini güncelle
                        if symbol in active_signals:
                            active_signals[symbol]["current_price"] = format_price(last_price, pos["open_price"])
                            active_signals[symbol]["current_price_float"] = last_price
                            active_signals[symbol]["last_update"] = str(datetime.now())
                        
                        if pos["type"] == "ALIŞ":
                            # Hedef kontrolü: High fiyatı hedefe ulaştı mı? (daha hassas)
                            if current_high >= pos["target"]:
                                msg = f"🎯 <b>HEDEF BAŞARIYLA GERÇEKLEŞTİ!</b> 🎯\n\n<b>{symbol}</b> işlemi için hedef fiyatına ulaşıldı!\nÇıkış Fiyatı: <b>{format_price(last_price)}</b>\n"
                                await send_signal_to_all_users(msg)
                                cooldown_signals[(symbol, "ALIS")] = datetime.now()
                                
                                # Başarılı sinyal olarak kaydet
                                profit_percent = 2
                                profit_usd = 100 * 0.02 * 10
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
                                    "leverage": pos.get("leverage", 1),
                                    "entry_time": pos.get("entry_time", str(datetime.now())),
                                    "duration_hours": round((datetime.now() - datetime.fromisoformat(pos.get("entry_time", str(datetime.now())))).total_seconds() / 3600, 2)
                                }
                                
                                # İstatistikleri güncelle
                                stats["successful_signals"] += 1
                                stats["total_profit_loss"] += profit_usd
                                
                                if symbol in active_signals:
                                    del active_signals[symbol]
                                
                                del positions[symbol]
                            # Stop kontrolü: Low fiyatı stop'a ulaştı mı? (daha hassas)
                            elif current_low <= pos["stop"]:
                                msg = f"❌ {symbol} işlemi stop oldu! Stop fiyatı: {pos['stop_str']}, Şu anki fiyat: {format_price(last_price, pos['stop'])}"
                                await send_signal_to_all_users(msg)
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
                                loss_percent = -1
                                loss_usd = -100 * 0.01 * 10
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
                                    "leverage": pos.get("leverage", 1),
                                    "entry_time": pos.get("entry_time", str(datetime.now())),
                                    "duration_hours": round((datetime.now() - datetime.fromisoformat(pos.get("entry_time", str(datetime.now())))).total_seconds() / 3600, 2)
                                }
                                
                                # İstatistikleri güncelle
                                stats["failed_signals"] += 1
                                stats["total_profit_loss"] += loss_usd
                                
                                if symbol in active_signals:
                                    del active_signals[symbol]
                                
                                del positions[symbol]
                        elif pos["type"] == "SATIŞ":
                            # Hedef kontrolü: Low fiyatı hedefe ulaştı mı? (daha hassas)
                            if current_low <= pos["target"]:
                                msg = f"🎯 <b>HEDEF BAŞARIYLA GERÇEKLEŞTİ!</b> 🎯\n\n<b>{symbol}</b> işlemi için hedef fiyatına ulaşıldı!\nÇıkış Fiyatı: <b>{format_price(last_price)}</b>\n"
                                await send_signal_to_all_users(msg)
                                cooldown_signals[(symbol, "SATIS")] = datetime.now()
                                
                                # Başarılı sinyal olarak kaydet
                                profit_percent = 2
                                profit_usd = 100 * 0.02 * 10
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
                                    "leverage": pos.get("leverage", 1),
                                    "entry_time": pos.get("entry_time", str(datetime.now())),
                                    "duration_hours": round((datetime.now() - datetime.fromisoformat(pos.get("entry_time", str(datetime.now())))).total_seconds() / 3600, 2)
                                }
                                
                                # İstatistikleri güncelle
                                stats["successful_signals"] += 1
                                stats["total_profit_loss"] += profit_usd
                                
                                if symbol in active_signals:
                                    del active_signals[symbol]
                                
                                del positions[symbol]
                            # Stop kontrolü: High fiyatı stop'a ulaştı mı? (daha hassas)
                            elif current_high >= pos["stop"]:
                                msg = f"❌ {symbol} işlemi stop oldu! Stop fiyatı: {pos['stop_str']}, Şu anki fiyat: {format_price(last_price, pos['stop'])}"
                                await send_signal_to_all_users(msg)
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
                                loss_percent = -1
                                loss_usd = -100 * 0.01 * 10
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
                                    "leverage": pos.get("leverage", 1),
                                    "entry_time": pos.get("entry_time", str(datetime.now())),
                                    "duration_hours": round((datetime.now() - datetime.fromisoformat(pos.get("entry_time", str(datetime.now())))).total_seconds() / 3600, 2)
                                }
                                
                                # İstatistikleri güncelle
                                stats["failed_signals"] += 1
                                stats["total_profit_loss"] += loss_usd
                                
                                if symbol in active_signals:
                                    del active_signals[symbol]
                                
                                del positions[symbol]
                    except Exception as e:
                        print(f"Pozisyon kontrol hatası: {symbol} - {str(e)}")
                        continue
                
                # Aktif sinyaller için 2 dakika bekle
                await asyncio.sleep(120)  # 2 dakika
                continue
                
            # Eğer sinyal aramaya izin verilen saatlerdeysek normal işlemlere devam et
            symbols = await get_active_high_volume_usdt_pairs()
            tracked_coins.update(symbols)  # Takip edilen coinleri güncelle
            print(f"Takip edilen coin sayısı: {len(symbols)}")
            
            # 2. Sinyal arama
            async def process_symbol(symbol):
                # Eğer pozisyon açıksa, yeni sinyal arama
                if symbol in positions:
                    return
                # Stop sonrası 8 saatlik cooldown kontrolü
                if symbol in stop_cooldown:
                    last_stop = stop_cooldown[symbol]
                    if (datetime.now() - last_stop) < timedelta(hours=8):
                        return  # 8 saat dolmadıysa sinyal arama
                    else:
                        del stop_cooldown[symbol]  # 8 saat dolduysa tekrar sinyal aranabilir
                # Başarılı sinyal sonrası 8 saatlik cooldown kontrolü
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
                        print(f"UYARI: {symbol} için 1d veri 30'dan az, sinyal aranmıyor.")
                        return
                except Exception as e:
                    print(f"UYARI: {symbol} için 1d veri çekilemedi: {str(e)}")
                    return
                # Mevcut sinyalleri al
                current_signals = dict()
                for tf_name in tf_names:
                    try:
                        df = await async_get_historical_data(symbol, timeframes[tf_name], 200)
                        df = calculate_full_pine_signals(df, tf_name)
                        signal = int(df['signal'].iloc[-1])
                        # Sinyal 0 ise MACD ile düzelt
                        if signal == 0:
                            if df['macd'].iloc[-1] > df['macd_signal'].iloc[-1]:
                                signal = 1
                            else:
                                signal = -1
                        current_signals[tf_name] = signal
                    except Exception as e:
                        print(f"Hata: {symbol} - {tf_name} - {str(e)}")
                        return  # Hata varsa bu coin için sinyal üretme
                # İlk çalıştırmada sadece sinyalleri kaydet
                if first_run:
                    previous_signals[symbol] = current_signals.copy()
                    print(f"İlk çalıştırma - {symbol} sinyalleri kaydedildi: {current_signals}")
                    return
                # İlk çalıştırma değilse, değişiklik kontrolü yap
                if symbol in previous_signals:
                    prev_signals = previous_signals[symbol]
                    signal_changed = False
                    for tf in tf_names:
                        if prev_signals[tf] != current_signals[tf]:
                            signal_changed = True
                            print(f"{symbol} - {tf} sinyali değişti: {prev_signals[tf]} -> {current_signals[tf]}")
                            break
                    if not signal_changed:
                        return  # Değişiklik yoksa devam et
                    # --- SAAT FİLTRESİ ---
                    # --- 4 ZAMAN DİLİMİ ŞARTI ---
                    signal_values = [current_signals[tf] for tf in tf_names]
                    if all(s == 1 for s in signal_values):
                        sinyal_tipi = 'ALIS'
                        leverage = 10
                    elif all(s == -1 for s in signal_values):
                        sinyal_tipi = 'SATIS'
                        leverage = 10
                    else:
                        previous_signals[symbol] = current_signals.copy()
                        return
                    
                    # Mum kapanışı kontrolü
                    try:
                        next_candle_df = await async_get_historical_data(symbol, '2h', 2) # Sadece son 2 mumu çek
                        if len(next_candle_df) < 2: # En azından 2 mum olmalı (şimdiki ve bir önceki)
                            print(f"UYARI: {symbol} için mum kapanışı kontrolü için yeterli veri yok.")
                            previous_signals[symbol] = current_signals.copy()
                            return
                        
                        # Bir önceki mumun kapanışı
                        prev_candle_close = next_candle_df['close'].iloc[-2]
                        prev_candle_open = next_candle_df['open'].iloc[-2]

                        # Sinyal onay kontrolü
                        signal_approved = False
                        if sinyal_tipi == 'ALIS':
                            if prev_candle_close > prev_candle_open: # Yeşil mum
                                signal_approved = True
                            else:
                                print(f"{symbol} AL sinyali iptal edildi (mum kapanışı kırmızı). Cooldown yok.")
                        elif sinyal_tipi == 'SATIS':
                            if prev_candle_close < prev_candle_open: # Kırmızı mum
                                signal_approved = True
                            else:
                                print(f"{symbol} SAT sinyali iptal edildi (mum kapanışı yeşil). Cooldown yok.")
                        
                        if not signal_approved:
                            previous_signals[symbol] = current_signals.copy()
                            return # Sinyal onaylanmadı, devam etme
                    except Exception as e:
                        print(f"Mum kapanışı kontrol hatası: {symbol} - {str(e)}")
                        previous_signals[symbol] = current_signals.copy()
                        return

                    # 4 saatlik cooldown kontrolü (hem başarılı hem başarısız için)
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
                    # Fiyat ve hacmi çek
                    df = await async_get_historical_data(symbol, '2h', 2)
                    price = float(df['close'].iloc[-1])
                    volume = float(df['volume'].iloc[-1])  # Son mumun hacmini al
                    message, dominant_signal, target_price, stop_loss, stop_loss_str = create_signal_message(symbol, price, current_signals, volume)
                    if message:
                        print(f"Telegram'a gönderiliyor: {symbol} - {dominant_signal}")
                        await send_signal_to_all_users(message)
                        # Pozisyonu kaydet
                        positions[symbol] = {
                            "type": dominant_signal,
                            "target": float(target_price),
                            "stop": float(stop_loss),
                            "open_price": float(price),
                            "stop_str": stop_loss_str,
                            "signals": {k: ("ALIŞ" if v == 1 else "SATIŞ") for k, v in current_signals.items()},
                            "leverage": leverage,
                            "entry_time": str(datetime.now())
                        }
                        # Aktif sinyal olarak kaydet
                        active_signals[symbol] = {
                            "symbol": symbol,
                            "type": dominant_signal,
                            "entry_price": format_price(price, price),
                            "entry_price_float": price,
                            "target_price": format_price(target_price, price),
                            "stop_loss": format_price(stop_loss, price),
                            "signals": {k: ("ALIŞ" if v == 1 else "SATIŞ") for k, v in current_signals.items()},
                            "leverage": leverage,
                            "signal_time": str(datetime.now()),
                            "current_price": format_price(price, price),
                            "current_price_float": price,
                            "last_update": str(datetime.now())
                        }
                        stats["total_signals"] += 1
                        stats["active_signals_count"] = len(active_signals)
                    previous_signals[symbol] = current_signals.copy()
                await asyncio.sleep(0)  # Task'ler arası context switch için

            # Paralel task listesi oluştur
            tasks = [process_symbol(symbol) for symbol in symbols]
            await asyncio.gather(*tasks)
            
            # İlk çalıştırma tamamlandıysa
            if first_run:
                first_run = False
                print("İlk çalıştırma tamamlandı! Artık değişiklikler takip ediliyor...")
            
            # Aktif sinyallerin fiyatlarını güncelle
            for symbol in list(active_signals.keys()):
                if symbol not in positions:  # Pozisyon kapandıysa aktif sinyalden kaldır
                    del active_signals[symbol]
                    continue
                try:
                    df = await async_get_historical_data(symbol, '2h', 2)
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
            global global_stats, global_active_signals, global_successful_signals, global_failed_signals
            global_stats = stats.copy()
            global_active_signals = active_signals.copy()
            global_successful_signals = successful_signals.copy()
            global_failed_signals = failed_signals.copy()
            
            # STOP OLAN COINLERİ TAKİP ET
            for symbol, info in list(stopped_coins.items()):
                try:
                    df = await async_get_historical_data(symbol, '2h', 2)
                    last_price = float(df['close'].iloc[-1])
                    entry_price = float(info["entry_price"])
                    if info["type"] == "ALIŞ":
                        # Min fiyatı güncelle
                        min_price = float(info["min_price"])
                        if last_price < min_price:
                            min_price = last_price
                        info["min_price"] = format_price(min_price, entry_price)
                        # Max terse gidiş (drawdown)
                        drawdown = (min_price - entry_price) / entry_price * 100
                        if drawdown < float(info.get("max_drawdown_percent", 0.0)):
                            info["max_drawdown_percent"] = round(drawdown, 2)
                        else:
                            info["max_drawdown_percent"] = round(float(info.get("max_drawdown_percent", drawdown)), 2)
                        # Hedefe ulaşıldı mı?
                        if not info["reached_target"] and last_price >= float(info["target_price"]):
                            info["reached_target"] = True
                        # Sadece ALIŞ için min_price ve max_drawdown_percent kaydet
                        info_to_save = {k: v for k, v in info.items() if k in ["symbol", "type", "entry_price", "stop_time", "target_price", "stop_loss", "signals", "min_price", "max_drawdown_percent", "reached_target"]}
                        with open(f'stopped_{symbol}.json', 'w', encoding='utf-8') as f:
                            json.dump(info_to_save, f, ensure_ascii=False, indent=2)
                        if info["reached_target"]:
                            del stopped_coins[symbol]
                    elif info["type"] == "SATIŞ":
                        # Max fiyatı güncelle
                        max_price = float(info["max_price"])
                        if last_price > max_price:
                            max_price = last_price
                        info["max_price"] = format_price(max_price, entry_price)
                        # Max terse gidiş (drawup)
                        drawup = (max_price - entry_price) / entry_price * 100
                        if drawup > float(info.get("max_drawup_percent", 0.0)):
                            info["max_drawup_percent"] = round(drawup, 2)
                        else:
                            info["max_drawup_percent"] = round(float(info.get("max_drawup_percent", drawup)), 2)
                        # Hedefe ulaşıldı mı?
                        if not info["reached_target"] and last_price <= float(info["target_price"]):
                            info["reached_target"] = True
                        # Sadece SATIŞ için max_price ve max_drawup_percent kaydet
                        info_to_save = {k: v for k, v in info.items() if k in ["symbol", "type", "entry_price", "stop_time", "target_price", "stop_loss", "signals", "max_price", "max_drawup_percent", "reached_target"]}
                        with open(f'stopped_{symbol}.json', 'w', encoding='utf-8') as f:
                            json.dump(info_to_save, f, ensure_ascii=False, indent=2)
                        if info["reached_target"]:
                            del stopped_coins[symbol]
                except Exception as e:
                    print(f"Stop sonrası takip hatası: {symbol} - {str(e)}")
                    continue
            
            # İstatistik özeti yazdır
            print(f"📊 İSTATİSTİK ÖZETİ:")
            print(f"   Toplam Sinyal: {stats['total_signals']}")
            print(f"   Başarılı: {stats['successful_signals']}")
            print(f"   Başarısız: {stats['failed_signals']}")
            print(f"   Aktif Sinyal: {stats['active_signals_count']}")
            print(f"   Toplam Görülen Coin: {stats['tracked_coins_count']}")
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
            # Döngü sonunda bekleme süresi
            print("Tüm coinler kontrol edildi. 2 dakika bekleniyor...")
            await asyncio.sleep(120)
            
            # Aktif sinyalleri dosyaya kaydet
            with open('active_signals.json', 'w', encoding='utf-8') as f:
                json.dump({
                    "active_signals": active_signals,
                    "count": len(active_signals),
                    "last_update": str(datetime.now())
                }, f, ensure_ascii=False, indent=2)
            
        except Exception as e:
            print(f"Genel hata: {e}")
            await asyncio.sleep(120)  # 2 dakika

async def main():
    # İzin verilen kullanıcıları yükle
    load_allowed_users()
    
    # Bot'u başlat
    await setup_bot()
    
    # Bot'u ve sinyal işleme döngüsünü paralel olarak çalıştır
    await app.initialize()
    await app.start()
    
    # Polling başlatmadan önce ek güvenlik
    try:
        await app.bot.delete_webhook(drop_pending_updates=True)
        print("✅ Ana fonksiyonda webhook'lar temizlendi")
    except Exception as e:
        print(f"⚠️ Ana fonksiyonda webhook temizleme hatası: {e}")
    
    # Polling başlatmadan önce ek güvenlik
    try:
        await app.bot.delete_webhook(drop_pending_updates=True)
        print("✅ Polling öncesi webhook'lar temizlendi")
        await asyncio.sleep(3)  # Daha uzun bekleme
        
        # Webhook durumunu kontrol et
        webhook_info = await app.bot.get_webhook_info()
        print(f"Webhook durumu: {webhook_info}")
        
    except Exception as e:
        print(f"⚠️ Polling öncesi webhook temizleme hatası: {e}")
    
    # Telegram bot polling'i başlat
    print("🤖 Telegram bot polling başlatılıyor...")
    
    # Bot polling'i başlat
    try:
        await app.updater.start_polling(drop_pending_updates=True, allowed_updates=["message", "callback_query"])
        print("✅ Telegram bot polling başarıyla başlatıldı")
    except Exception as e:
        print(f"❌ Telegram bot polling başlatma hatası: {e}")
        print("⚠️ Bot sadece sinyal işleme modunda çalışacak")
    
    # Sinyal işleme döngüsünü başlat
    signal_task = asyncio.create_task(signal_processing_loop())
    
    try:
        # Sinyal işleme döngüsünü çalıştır (bot polling arka planda çalışacak)
        print("🚀 Bot başarıyla başlatıldı! Telegram komutları kullanılabilir.")
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