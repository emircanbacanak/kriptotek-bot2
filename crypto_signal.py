import sys
import asyncio
import signal
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
import requests
import certifi
from urllib3.exceptions import InsecureRequestWarning
import urllib3
from decimal import Decimal, ROUND_DOWN, getcontext
import json
import aiohttp

# SSL uyarılarını kapat
urllib3.disable_warnings(InsecureRequestWarning)

# Telegram Bot ayarları
TELEGRAM_TOKEN = "8091816386:AAFl-t7GNyUsKJ7uX5wu9D-HzPLp30NYg_c"
TELEGRAM_CHAT_ID = "847081095"

# Binance client oluştur (globalde)
client = Client()

# Telegram bot oluştur
bot = telegram.Bot(token=TELEGRAM_TOKEN)

async def send_telegram_message(message):
    """Telegram'a mesaj gönder"""
    await bot.send_message(chat_id=TELEGRAM_CHAT_ID, text=message, parse_mode='HTML')

# Dinamik fiyat formatlama fonksiyonu
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

def create_signal_message(symbol, price, signals):
    """Sinyal mesajını oluştur (AL/SAT başlıkta)"""
    price_str = format_price(price, price)  # Fiyatın kendi basamağı kadar
    signal_1h = "ALIŞ" if signals['1h'] == 1 else "SATIŞ"
    signal_4h = "ALIŞ" if signals['4h'] == 1 else "SATIŞ"
    signal_1d = "ALIŞ" if signals['1d'] == 1 else "SATIŞ"
    buy_count = sum(1 for s in signals.values() if s == 1)
    sell_count = sum(1 for s in signals.values() if s == -1)
    if buy_count >= 2:
        dominant_signal = "ALIŞ"
        target_price = price * 1.02  # %2 hedef
        stop_loss = price * 0.99     # %1 stop
        sinyal_tipi = "AL SİNYALİ"
        leverage = 10 if buy_count == 3 else 5
    elif sell_count >= 2:
        dominant_signal = "SATIŞ"
        target_price = price * 0.98  # %2 hedef
        stop_loss = price * 1.01     # %1 stop
        sinyal_tipi = "SAT SİNYALİ"
        leverage = 10 if sell_count == 3 else 5
    else:
        return None, None, None, None, None
    target_price_str = format_price(target_price, price)
    stop_loss_str = format_price(stop_loss, price)
    
    message = f"""
🚨 {sinyal_tipi} 

Kripto Çifti: {symbol}
Fiyat: {price_str}

⏰ Zaman Dilimleri:
1 Saat: {signal_1h}
4 Saat: {signal_4h}
1 Gün: {signal_1d}

Kaldıraç Önerisi: {leverage}x

💰 Hedef Fiyat: {target_price_str}
🛑 Stop Loss: {stop_loss_str}

⚠️ YATIRIM TAVSİYESİ DEĞİLDİR ⚠️

📋 DİKKAT:
• Portföyünüzün max %5-10'unu kullanın
• Stop loss'u mutlaka uygulayın
• FOMO ile acele karar vermeyin
"""
    return message, dominant_signal, target_price, stop_loss, stop_loss_str

# YENİ: Asenkron veri çekme fonksiyonu - OPTİMİZE EDİLMİŞ
_session = None

async def get_session():
    global _session
    if _session is None or _session.closed:
        connector = aiohttp.TCPConnector(limit=100, limit_per_host=50, ttl_dns_cache=300)
        timeout = aiohttp.ClientTimeout(total=10, connect=5)
        _session = aiohttp.ClientSession(connector=connector, timeout=timeout)
    return _session

async def async_get_historical_data(symbol, interval, lookback):
    """Binance'den geçmiş verileri asenkron çek - OPTİMİZE EDİLMİŞ"""
    session = await get_session()
    url = f"https://api.binance.com/api/v3/klines?symbol={symbol}&interval={interval}&limit={lookback}"
    try:
        async with session.get(url, ssl=False) as resp:
            if resp.status == 200:
                klines = await resp.json()
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
                return df
            else:
                raise Exception(f"HTTP {resp.status}")
    except Exception as e:
        raise Exception(f"Veri çekme hatası: {e}")

def calculate_full_pine_signals(df, timeframe, fib_filter_enabled=False):
    # Zaman dilimine göre parametreler
    is_higher_tf = timeframe in ['1d', '4h', '1w']
    is_weekly = timeframe == '1w'
    is_daily = timeframe == '1d'
    is_4h = timeframe == '4h'
    rsi_length = 28 if is_weekly else 21 if is_daily else 18 if is_4h else 14
    macd_fast = 18 if is_weekly else 13 if is_daily else 11 if is_4h else 10
    macd_slow = 36 if is_weekly else 26 if is_daily else 22 if is_4h else 20
    macd_signal = 12 if is_weekly else 10 if is_daily else 8 if is_4h else 9
    short_ma_period = 30 if is_weekly else 20 if is_daily else 12 if is_4h else 9
    long_ma_period = 150 if is_weekly else 100 if is_daily else 60 if is_4h else 50
    mfi_length = 25 if is_weekly else 20 if is_daily else 16 if is_4h else 14
    fib_lookback = 150 if is_weekly else 100 if is_daily else 70 if is_4h else 50

    # EMA ve trend
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

    # Supertrend (özel fonksiyon)
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

    atr_period = 7 if is_4h else 10
    atr_dynamic = ta.volatility.AverageTrueRange(df['high'], df['low'], df['close'], window=atr_period).average_true_range().rolling(window=5).mean()
    atr_multiplier = atr_dynamic / 2 if is_weekly else atr_dynamic / 1.2 if is_daily else atr_dynamic / 1.3 if is_4h else atr_dynamic / 1.5
    df['supertrend_dir'] = supertrend(df, atr_period, atr_multiplier.bfill())

    # Hareketli Ortalamalar
    df['short_ma'] = ta.trend.EMAIndicator(df['close'], window=short_ma_period).ema_indicator()
    df['long_ma'] = ta.trend.EMAIndicator(df['close'], window=long_ma_period).ema_indicator()
    df['ma_bullish'] = df['short_ma'] > df['long_ma']
    df['ma_bearish'] = df['short_ma'] < df['long_ma']

    # Hacim Analizi
    volume_ma_period = 20
    df['volume_ma'] = df['volume'].rolling(window=volume_ma_period).mean()
    df['enough_volume'] = df['volume'] > df['volume_ma'] * (0.15 if is_higher_tf else 0.4)

    # MFI
    df['mfi'] = ta.volume.MFIIndicator(df['high'], df['low'], df['close'], df['volume'], window=mfi_length).money_flow_index()
    df['mfi_bullish'] = df['mfi'] < 65
    df['mfi_bearish'] = df['mfi'] > 35

    # Fibonacci Seviyeleri
    highest_high = df['high'].rolling(window=fib_lookback).max()
    lowest_low = df['low'].rolling(window=fib_lookback).min()
    fib_level1 = highest_high * 0.618
    fib_level2 = lowest_low * 1.382
    if fib_filter_enabled:
        df['fib_in_range'] = (df['close'] > fib_level1) & (df['close'] < fib_level2)
    else:
        df['fib_in_range'] = True

    # --- PineScript ile birebir AL/SAT sinyal mantığı ---
    def crossover(series1, series2):
        return (series1.shift(1) < series2.shift(1)) & (series1 > series2)
    def crossunder(series1, series2):
        return (series1.shift(1) > series2.shift(1)) & (series1 < series2)

    buy_signal = (
        crossover(df['macd'], df['macd_signal']) |
        (
            (df['rsi'] < rsi_oversold) &
            (df['supertrend_dir'] == 1) &
            (df['ma_bullish']) &
            (df['enough_volume']) &
            (df['mfi_bullish']) &
            (df['trend_bullish'])
        )
    ) & df['fib_in_range']

    sell_signal = (
        crossunder(df['macd'], df['macd_signal']) |
        (
            (df['rsi'] > rsi_overbought) &
            (df['supertrend_dir'] == -1) &
            (df['ma_bearish']) &
            (df['enough_volume']) &
            (df['mfi_bearish']) &
            (df['trend_bearish'])
        )
    ) & df['fib_in_range']

    df['signal'] = 0
    df.loc[buy_signal, 'signal'] = 1
    df.loc[sell_signal, 'signal'] = -1
    
    if df['signal'].iloc[-1] == 0:
        if df['macd'].iloc[-1] > df['macd_signal'].iloc[-1]:
            df.at[df.index[-1], 'signal'] = 1
        else:
            df.at[df.index[-1], 'signal'] = -1

    return df

# Hedef ve stop hesaplama fonksiyonu

def calc_targets(price, signal_type):
    if signal_type == "ALIŞ":
        return price * 1.02, price * 0.99
    else:
        return price * 0.98, price * 1.01

# Pozisyon kaydetme fonksiyonu

def kaydet_pozisyon(positions, symbol, signal_type, price, target, stop, stop_str, signals, leverage, entry_time):
    positions[symbol] = {
        "type": signal_type,
        "target": float(target),
        "stop": float(stop),
        "open_price": float(price),
        "stop_str": stop_str,
        "signals": signals,
        "leverage": leverage,
        "entry_time": entry_time
    }

# Aktif sinyal kaydetme fonksiyonu

def kaydet_aktif_sinyal(active_signals, symbol, signal_type, price, target, stop, signals, leverage, entry_time):
    active_signals[symbol] = {
        "symbol": symbol,
        "type": signal_type,
        "entry_price": format_price(price, price),
        "entry_price_float": price,
        "target_price": format_price(target, price),
        "stop_loss": format_price(stop, price),
        "signals": signals,
        "leverage": leverage,
        "signal_time": entry_time,
        "current_price": format_price(price, price),
        "current_price_float": price,
        "last_update": entry_time
    }

# İstatistik güncelleme fonksiyonu (güncel)
def update_stats(stats, active_signals, total_signals_delta=0, successful_delta=0, failed_delta=0, profit_delta=0.0):
    stats["total_signals"] += total_signals_delta
    stats["active_signals_count"] = len(active_signals)
    stats["successful_signals"] += successful_delta
    stats["failed_signals"] += failed_delta
    stats["total_profit_loss"] += profit_delta

# --- YENİ ANA DÖNGÜ VE MANTIK ---
async def get_active_high_volume_usdt_pairs(min_volume=45000000):
    """
    Sadece spotta aktif, USDT bazlı ve 24s hacmi min_volume üstü tüm coinleri döndürür.
    1 günlük (1d) verisi 30'dan az olan yeni coinler otomatik olarak atlanır.
    USDCUSDT, FDUSDUSDT gibi 1:1 stablecoin çiftleri hariç tutulur.
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
    # Hacim kontrolü ve sıralama
    high_volume_pairs = []
    for ticker in tickers:
        symbol = ticker['symbol']
        # Stablecoin çiftlerini ve problematik coinleri hariç tut
        if symbol in ['USDCUSDT', 'FDUSDUSDT', 'TUSDUSDT', 'BUSDUSDT', 'USDPUSDT', 'USDTUSDT', 'NEWTUSDT']:
            continue
        if symbol in spot_usdt_pairs:
            try:
                quote_volume = float(ticker['quoteVolume'])
                if quote_volume >= min_volume:
                    high_volume_pairs.append((symbol, quote_volume))
            except Exception:
                continue
    # Hacme göre sırala ve sadece ilk 100'ü al (hız için)
    high_volume_pairs.sort(key=lambda x: x[1], reverse=True)
    high_volume_pairs = high_volume_pairs[:100]  # Sadece en yüksek hacimli 100 coin
    
    # 1d verisi 30'dan az olanları paralel kontrol et
    async def check_symbol_data(symbol, volume):
        try:
            df_1d = await async_get_historical_data(symbol, '1d', 40)
            if len(df_1d) >= 30:
                return symbol
            else:
                return None  # Sessizce filtrele, print etme
        except Exception as e:
            return None  # Sessizce filtrele, print etme
    
    # Paralel kontrol
    check_tasks = [check_symbol_data(symbol, volume) for symbol, volume in high_volume_pairs]
    results = await asyncio.gather(*check_tasks, return_exceptions=True)
    
    # Sonuçları filtrele
    uygun_pairs = [symbol for symbol in results if symbol is not None and not isinstance(symbol, Exception)]
    
    # Eğer hiç uygun coin bulunamazsa, minimum veri sayısını düşür
    if not uygun_pairs and high_volume_pairs:
        print("⚠️ 30 günlük veri kriteri çok sıkı, 20 güne düşürülüyor...")
        async def check_symbol_data_relaxed(symbol, volume):
            try:
                df_1d = await async_get_historical_data(symbol, '1d', 30)
                if len(df_1d) >= 20:
                    return symbol
                else:
                    return None
            except Exception as e:
                return None
        
        check_tasks_relaxed = [check_symbol_data_relaxed(symbol, volume) for symbol, volume in high_volume_pairs]
        results_relaxed = await asyncio.gather(*check_tasks_relaxed, return_exceptions=True)
        uygun_pairs = [symbol for symbol in results_relaxed if symbol is not None and not isinstance(symbol, Exception)]
    
    return uygun_pairs

def save_all_state(active_signals, waiting_signals, successful_signals, failed_signals):
    # Aktif sinyalleri dosyaya kaydet
    with open('active_signals.json', 'w', encoding='utf-8') as f:
        json.dump({
            "active_signals": active_signals,
            "count": len(active_signals),
            "last_update": str(datetime.now())
        }, f, ensure_ascii=False, indent=2)
    # Bekleyen sinyalleri dosyaya kaydet
    with open('waiting_signals.json', 'w', encoding='utf-8') as f:
        json.dump({
            "waiting_signals": waiting_signals,
            "count": len(waiting_signals),
            "last_update": str(datetime.now())
        }, f, ensure_ascii=False, indent=2)
    # Başarılı sinyalleri dosyaya kaydet
    with open('successful_signals.json', 'w', encoding='utf-8') as f:
        json.dump({
            "successful_signals": successful_signals,
            "count": len(successful_signals),
            "total_profit_usd": sum(s.get("profit_usd", 0) for s in successful_signals.values()),
            "total_profit_percent": sum(s.get("profit_percent", 0) for s in successful_signals.values()),
            "average_profit_per_signal": (sum(s.get("profit_usd", 0) for s in successful_signals.values()) / len(successful_signals)) if successful_signals else 0.0,
            "average_duration_hours": (sum(s.get("duration_hours", 0) for s in successful_signals.values()) / len(successful_signals)) if successful_signals else 0.0,
            "last_update": str(datetime.now())
        }, f, ensure_ascii=False, indent=2)
    # Başarısız sinyalleri dosyaya kaydet
    with open('failed_signals.json', 'w', encoding='utf-8') as f:
        json.dump({
            "failed_signals": failed_signals,
            "count": len(failed_signals),
            "total_loss_usd": sum(s.get("loss_usd", 0) for s in failed_signals.values()),
            "total_loss_percent": sum(s.get("loss_percent", 0) for s in failed_signals.values()),
            "average_loss_per_signal": (sum(s.get("loss_usd", 0) for s in failed_signals.values()) / len(failed_signals)) if failed_signals else 0.0,
            "average_duration_hours": (sum(s.get("duration_hours", 0) for s in failed_signals.values()) / len(failed_signals)) if failed_signals else 0.0,
            "last_update": str(datetime.now())
        }, f, ensure_ascii=False, indent=2)

def print_stats(stats, waiting_signals=None):
    print(f"📊 İSTATİSTİK ÖZETİ:")
    print(f"   Toplam Sinyal: {stats['total_signals']}")
    print(f"   Başarılı: {stats['successful_signals']}")
    print(f"   Başarısız: {stats['failed_signals']}")
    print(f"   Aktif Sinyal: {stats['active_signals_count']}")
    if waiting_signals is not None:
        print(f"   Bekleyen Sinyal: {len(waiting_signals)}")
    print(f"   Toplam Görülen Coin: {stats['tracked_coins_count']}")
    print(f"   100$ Yatırım Toplam Kar/Zarar: ${stats['total_profit_loss']:.2f}")

async def main_loop():
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
    waiting_signals = dict()  # {symbol: {...}} - Daha iyi giriş bekleyen sinyaller
    # first_run değişkenini sadece ilk döngüde True yap, sonra False'a çek
    first_run = True
    
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
        '4h': '4h',
        '1d': '1d'
    }
    tf_names = ['1h', '4h', '1d']
    
    print("Sinyal botu başlatıldı!")
    print("İlk çalıştırma: Mevcut sinyaller kaydediliyor, değişiklik bekleniyor...")
    
    while True:
        try:
            symbols = await get_active_high_volume_usdt_pairs(min_volume=45000000)
            tracked_coins.update(symbols)  # Takip edilen coinleri güncelle
            stats['tracked_coins_count'] = len(tracked_coins)  # İstatistikleri güncelle
            
            # 1. Bekleyen sinyalleri paralel kontrol et (daha iyi giriş için)
            waiting_items = list(waiting_signals.items())
            if waiting_items:
                async def check_waiting_signal(symbol, wait_info):
                    try:
                        df = await async_get_historical_data(symbol, '1h', 2)
                        current_price = float(df['close'].iloc[-1])
                        signal_price = wait_info["signal_price"]
                        
                        # Fiyat değişim yüzdesi hesapla
                        price_change_percent = ((current_price - signal_price) / signal_price) * 100
                        
                        # Alış sinyali için %1 düşüş kontrolü
                        if wait_info["type"] == "ALIŞ" and price_change_percent <= -1.0:
                            # Daha iyi giriş noktası bulundu, işlemi başlat
                            
                            # Pozisyonu başlat
                            target_price, stop_loss = calc_targets(current_price, "ALIŞ")
                            stop_loss_str = format_price(stop_loss, current_price)
                            kaydet_pozisyon(positions, symbol, "ALIŞ", current_price, target_price, stop_loss, stop_loss_str, wait_info["signals"], wait_info["leverage"], str(datetime.now()))
                            
                            # Aktif sinyal olarak kaydet
                            kaydet_aktif_sinyal(active_signals, symbol, "ALIŞ", current_price, target_price, stop_loss, wait_info["signals"], wait_info["leverage"], str(datetime.now()))
                            
                            # İstatistikleri güncelle
                            update_stats(stats, active_signals, total_signals_delta=1)
                            
                            # Bekleyen sinyalden kaldır
                            del waiting_signals[symbol]
                            
                        # Satış sinyali için %1 yükseliş kontrolü
                        elif wait_info["type"] == "SATIŞ" and price_change_percent >= 1.0:
                            # Daha iyi giriş noktası bulundu, işlemi başlat
                            
                            # Pozisyonu başlat
                            target_price, stop_loss = calc_targets(current_price, "SATIŞ")
                            stop_loss_str = format_price(stop_loss, current_price)
                            kaydet_pozisyon(positions, symbol, "SATIŞ", current_price, target_price, stop_loss, stop_loss_str, wait_info["signals"], wait_info["leverage"], str(datetime.now()))
                            
                            # Aktif sinyal olarak kaydet
                            kaydet_aktif_sinyal(active_signals, symbol, "SATIŞ", current_price, target_price, stop_loss, wait_info["signals"], wait_info["leverage"], str(datetime.now()))
                            
                            # İstatistikleri güncelle
                            update_stats(stats, active_signals, total_signals_delta=1)
                            
                            # Bekleyen sinyalden kaldır
                            del waiting_signals[symbol]
                            
                        # 1 saatlik sinyal değişti mi kontrol et
                        try:
                            df_1h = await async_get_historical_data(symbol, '1h', 200)
                            df_1h = calculate_full_pine_signals(df_1h, '1h')
                            current_1h_signal = int(df_1h['signal'].iloc[-1])
                        except Exception as e:
                            print(f"1h sinyal kontrol hatası: {symbol} - {str(e)}")
                            return
                        
                        if wait_info["type"] == "ALIŞ" and current_1h_signal == 1:
                            # 1 saat de alış oldu, hemen işlem başlat
                            
                            # Pozisyonu başlat
                            target_price, stop_loss = calc_targets(current_price, "ALIŞ")
                            stop_loss_str = format_price(stop_loss, current_price)
                            kaydet_pozisyon(positions, symbol, "ALIŞ", current_price, target_price, stop_loss, stop_loss_str, wait_info["signals"], wait_info["leverage"], str(datetime.now()))
                            
                            # Aktif sinyal olarak kaydet
                            kaydet_aktif_sinyal(active_signals, symbol, "ALIŞ", current_price, target_price, stop_loss, wait_info["signals"], wait_info["leverage"], str(datetime.now()))
                            
                            # İstatistikleri güncelle
                            update_stats(stats, active_signals, total_signals_delta=1)
                            
                            # Bekleyen sinyalden kaldır
                            del waiting_signals[symbol]
                            
                        elif wait_info["type"] == "SATIŞ" and current_1h_signal == -1:
                            # 1 saat de satış oldu, hemen işlem başlat
                            
                            # Pozisyonu başlat
                            target_price, stop_loss = calc_targets(current_price, "SATIŞ")
                            stop_loss_str = format_price(stop_loss, current_price)
                            kaydet_pozisyon(positions, symbol, "SATIŞ", current_price, target_price, stop_loss, stop_loss_str, wait_info["signals"], wait_info["leverage"], str(datetime.now()))
                            
                            # Aktif sinyal olarak kaydet
                            kaydet_aktif_sinyal(active_signals, symbol, "SATIŞ", current_price, target_price, stop_loss, wait_info["signals"], wait_info["leverage"], str(datetime.now()))
                            
                            # İstatistikleri güncelle
                            update_stats(stats, active_signals, total_signals_delta=1)
                            
                            # Bekleyen sinyalden kaldır
                            del waiting_signals[symbol]
                            
                    except Exception as e:
                        print(f"Bekleyen sinyal kontrol hatası: {symbol} - {str(e)}")
                
                # Tüm bekleyen sinyalleri paralel kontrol et
                waiting_tasks = [check_waiting_signal(symbol, wait_info) for symbol, wait_info in waiting_items]
                await asyncio.gather(*waiting_tasks)
            
            # 2. Pozisyonları paralel kontrol et (hedef/stop)
            position_items = list(positions.items())
            if position_items:
                async def check_position(symbol, pos):
                    try:
                        df = await async_get_historical_data(symbol, '1h', 2)  # En güncel fiyatı çek
                        last_price = float(df['close'].iloc[-1])
                    
                        # Aktif sinyal bilgilerini güncelle
                        if symbol in active_signals:
                            active_signals[symbol]["current_price"] = format_price(last_price, pos["open_price"])
                            active_signals[symbol]["current_price_float"] = last_price
                            active_signals[symbol]["last_update"] = str(datetime.now())
                        
                        if pos["type"] == "ALIŞ":
                            if last_price >= pos["target"]:
                                msg = f"🎯 <b>HEDEF BAŞARIYLA GERÇEKLEŞTİ!</b> 🎯\n\n<b>{symbol}</b> işlemi için hedef fiyatına ulaşıldı!\nÇıkış Fiyatı: <b>{format_price(last_price)}</b>\n"
                                await send_telegram_message(msg)
                                cooldown_signals[(symbol, "ALIS")] = datetime.now()
                                # Kar/zarar sabit: %2 kar, 100$ yatırım, kaldıraçlı
                                profit_percent = 2.0
                                profit_usd = 100 * (profit_percent / 100) * pos.get("leverage", 1)
                                successful_signals[symbol] = {
                                    "symbol": symbol,
                                    "type": pos["type"],
                                    "entry_price": format_price(pos["open_price"], pos["open_price"]),
                                    "exit_price": format_price(pos["target"], pos["open_price"]),
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
                                # İstatistikleri güncelle (kar ekle)
                                update_stats(stats, active_signals, total_signals_delta=-1, successful_delta=1, profit_delta=profit_usd)
                                if symbol in active_signals:
                                    del active_signals[symbol]
                                del positions[symbol]
                            elif last_price <= pos["stop"]:
                                msg = f"❌ {symbol} işlemi stop oldu! Stop fiyatı: {pos['stop_str']}, Şu anki fiyat: {format_price(last_price, pos['stop'])}"
                                await send_telegram_message(msg)
                                cooldown_signals[(symbol, "ALIS")] = datetime.now()
                                stop_cooldown[symbol] = datetime.now()
                                stopped_coins[symbol] = {
                                    "symbol": symbol,
                                    "type": pos["type"],
                                    "entry_price": format_price(pos["open_price"], pos["open_price"]),
                                    "stop_time": str(datetime.now()),
                                    "target_price": format_price(pos["target"], pos["open_price"]),
                                    "stop_loss": format_price(pos["stop"], pos["open_price"]),
                                    "signals": pos["signals"],
                                    "min_price": format_price(last_price, pos["open_price"]),
                                    "max_price": None,
                                    "max_drawdown_percent": 0.0,
                                    "max_drawup_percent": None,
                                    "reached_target": False
                                }
                                info_to_save = {k: v for k, v in stopped_coins[symbol].items() if k in ["symbol", "type", "entry_price", "stop_time", "target_price", "stop_loss", "signals", "min_price", "max_drawdown_percent", "reached_target"]}
                                with open(f'stopped_{symbol}.json', 'w', encoding='utf-8') as f:
                                    json.dump(info_to_save, f, ensure_ascii=False, indent=2)
                                # Zarar sabit: -%1 zarar, 100$ yatırım, kaldıraçlı
                                loss_percent = -1.0
                                loss_usd = 100 * (loss_percent / 100) * pos.get("leverage", 1)
                                failed_signals[symbol] = {
                                    "symbol": symbol,
                                    "type": pos["type"],
                                    "entry_price": format_price(pos["open_price"], pos["open_price"]),
                                    "exit_price": format_price(pos["stop"], pos["open_price"]),
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
                                update_stats(stats, active_signals, total_signals_delta=-1, failed_delta=1, profit_delta=loss_usd)
                                if symbol in active_signals:
                                    del active_signals[symbol]
                                del positions[symbol]
                        elif pos["type"] == "SATIŞ":
                            if last_price <= pos["target"]:
                                msg = f"🎯 <b>HEDEF BAŞARIYLA GERÇEKLEŞTİ!</b> 🎯\n\n<b>{symbol}</b> işlemi için hedef fiyatına ulaşıldı!\nÇıkış Fiyatı: <b>{format_price(last_price)}</b>\n"
                                await send_telegram_message(msg)
                                cooldown_signals[(symbol, "SATIS")] = datetime.now()
                                # Kar/zarar sabit: %2 kar, 100$ yatırım, kaldıraçlı
                                profit_percent = 2.0
                                profit_usd = 100 * (profit_percent / 100) * pos.get("leverage", 1)
                                successful_signals[symbol] = {
                                    "symbol": symbol,
                                    "type": pos["type"],
                                    "entry_price": format_price(pos["open_price"], pos["open_price"]),
                                    "exit_price": format_price(pos["target"], pos["open_price"]),
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
                                update_stats(stats, active_signals, total_signals_delta=-1, successful_delta=1, profit_delta=profit_usd)
                                if symbol in active_signals:
                                    del active_signals[symbol]
                                del positions[symbol]
                            elif last_price >= pos["stop"]:
                                msg = f"❌ {symbol} işlemi stop oldu! Stop fiyatı: {pos['stop_str']}, Şu anki fiyat: {format_price(last_price, pos['stop'])}"
                                await send_telegram_message(msg)
                                cooldown_signals[(symbol, "SATIS")] = datetime.now()
                                stop_cooldown[symbol] = datetime.now()
                                stopped_coins[symbol] = {
                                    "symbol": symbol,
                                    "type": pos["type"],
                                    "entry_price": format_price(pos["open_price"], pos["open_price"]),
                                    "stop_time": str(datetime.now()),
                                    "target_price": format_price(pos["target"], pos["open_price"]),
                                    "stop_loss": format_price(pos["stop"], pos["open_price"]),
                                    "signals": pos["signals"],
                                    "min_price": None,
                                    "max_price": format_price(last_price, pos["open_price"]),
                                    "max_drawdown_percent": None,
                                    "max_drawup_percent": 0.0,
                                    "reached_target": False
                                }
                                info_to_save = {k: v for k, v in stopped_coins[symbol].items() if k in ["symbol", "type", "entry_price", "stop_time", "target_price", "stop_loss", "signals", "max_price", "max_drawup_percent", "reached_target"]}
                                with open(f'stopped_{symbol}.json', 'w', encoding='utf-8') as f:
                                    json.dump(info_to_save, f, ensure_ascii=False, indent=2)
                                # Zarar sabit: -%1 zarar, 100$ yatırım, kaldıraçlı
                                loss_percent = -1.0
                                loss_usd = 100 * (loss_percent / 100) * pos.get("leverage", 1)
                                failed_signals[symbol] = {
                                    "symbol": symbol,
                                    "type": pos["type"],
                                    "entry_price": format_price(pos["open_price"], pos["open_price"]),
                                    "exit_price": format_price(pos["stop"], pos["open_price"]),
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
                                update_stats(stats, active_signals, total_signals_delta=-1, failed_delta=1, profit_delta=loss_usd)
                                if symbol in active_signals:
                                    del active_signals[symbol]
                                del positions[symbol]
                    except Exception as e:
                        print(f"Pozisyon kontrol hatası: {symbol} - {str(e)}")
                
                # Tüm pozisyonları paralel kontrol et
                position_tasks = [check_position(symbol, pos) for symbol, pos in position_items]
                await asyncio.gather(*position_tasks)
            
            # STOP OLAN COINLERİ TAKİP ET
            for symbol, info in list(stopped_coins.items()):
                try:
                    df = await async_get_historical_data(symbol, '30m', 2)
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
            
            # 2. Sinyal arama
            async def process_symbol(symbol):
                # Eğer pozisyon açıksa, yeni sinyal arama
                if symbol in positions:
                    return
                # Stop sonrası 4 saatlik cooldown kontrolü
                if symbol in stop_cooldown:
                    last_stop = stop_cooldown[symbol]
                    if (datetime.now() - last_stop) < timedelta(hours=2):
                        return  # 2 saat dolmadıysa sinyal arama
                    else:
                        del stop_cooldown[symbol]  # 2 saat dolduysa tekrar sinyal aranabilir
                # 1 günlük veri kontrolü - KALDIRILDI (coin listesi çekerken zaten kontrol ediliyor)
                # Mevcut sinyalleri paralel al
                async def get_signal_for_timeframe(tf_name):
                    try:
                        df = await async_get_historical_data(symbol, timeframes[tf_name], 200)
                        df = calculate_full_pine_signals(df, tf_name)
                        return tf_name, int(df['signal'].iloc[-1])
                    except Exception as e:
                        print(f"Hata: {symbol} - {tf_name} - {str(e)}")
                        return tf_name, 0
                
                # Tüm zaman dilimlerini paralel çek
                signal_tasks = [get_signal_for_timeframe(tf_name) for tf_name in tf_names]
                signal_results = await asyncio.gather(*signal_tasks)
                current_signals = {tf_name: signal for tf_name, signal in signal_results}
                # İlk çalıştırmada sadece sinyalleri kaydet, sinyal verme
                if first_run:
                    previous_signals[symbol] = current_signals.copy()
                    print(f"İlk çalıştırma - {symbol} sinyalleri kaydedildi: {current_signals}")
                    return  # İlk çalıştırmada hiçbir sinyal gönderme
                # İlk çalıştırma değilse, değişiklik kontrolü yap
                if symbol in previous_signals:
                    prev_signals = previous_signals[symbol]
                    signal_changed = False
                    # Herhangi bir zaman diliminde değişiklik var mı kontrol et
                    for tf in tf_names:
                        if prev_signals[tf] != current_signals[tf]:
                            signal_changed = True
                            print(f"🔍 {symbol} - {tf} sinyali değişti: {prev_signals[tf]} -> {current_signals[tf]}")
                            break
                    if not signal_changed:
                        return  # Değişiklik yoksa devam et
                    
                    # Debug: Sinyal değişikliği bulundu
                    print(f"🔔 {symbol} sinyal değişikliği tespit edildi!")
                    
                    print(f"📊 {symbol} sinyal analizi başlıyor...")
                    print(f"   Önceki: {prev_signals}")
                    print(f"   Şu anki: {current_signals}")
                    
                    # Değişiklik varsa, yeni sinyal analizi yap
                    signal_values = [current_signals[tf] for tf in tf_names]
                    
                    # 1 saatlik sinyal diğerlerinden farklı mı kontrol et
                    tf_1h_diff = (signal_values[0] != signal_values[1] and signal_values[0] != signal_values[2])
                    
                    print(f"   Sinyal değerleri: {signal_values}")
                    print(f"   1h farklı mı: {tf_1h_diff}")
                    
                    # Sinyal koşullarını kontrol et
                    if all(s == 1 for s in signal_values):
                        sinyal_tipi = 'ALIS'
                        wait_for_better_entry = False
                        print(f"   ✅ Tüm zaman dilimleri ALIŞ - Anında işlem")
                    elif all(s == -1 for s in signal_values):
                        sinyal_tipi = 'SATIS'
                        wait_for_better_entry = False
                        print(f"   ✅ Tüm zaman dilimleri SATIŞ - Anında işlem")
                    elif (
                        (signal_values[0] == signal_values[1] != 0) or
                        (signal_values[1] == signal_values[2] != 0) or
                        (signal_values[0] == signal_values[2] != 0)
                    ):
                        sinyal_tipi = 'ALIS' if signal_values.count(1) >= 2 else 'SATIS'
                        wait_for_better_entry = False
                        print(f"   ✅ 2 zaman dilimi aynı - Anında işlem: {sinyal_tipi}")
                    else:
                        # Sinyal koşulu sağlanmıyorsa sadece güncelle ve devam et
                        print(f"   ❌ Sinyal koşulu sağlanmıyor - İşlem yok")
                        previous_signals[symbol] = current_signals.copy()
                        return
                    
                    # Bekleme mantığı sadece belirli durumlarda çalışacak
                    # SATIŞ-ALIŞ-ALIŞ (1h farklı) → %1 düşüş bekle VEYA 1h tekrar ALIŞ olması
                    # ALIŞ-SATIŞ-SATIŞ (1h farklı) → %1 yükseliş bekle VEYA 1h tekrar SATIŞ olması
                    # Diğer durumlar → Anında işlem
                    if tf_1h_diff:
                        if signal_values[0] == -1 and signal_values[1] == 1 and signal_values[2] == 1:
                            # SATIŞ-ALIŞ-ALIŞ → %1 düşüş bekle VEYA 1h tekrar ALIŞ olması
                            wait_for_better_entry = True
                            entry_strategy = "SATIŞ-ALIŞ-ALIŞ: Fiyatın %1 düşmesini bekleyip alım yapılacak VEYA 1h tekrar ALIŞ olması"
                            print(f"   ⏳ BEKLEME: SATIŞ-ALIŞ-ALIŞ")
                        elif signal_values[0] == 1 and signal_values[1] == -1 and signal_values[2] == -1:
                            # ALIŞ-SATIŞ-SATIŞ → %1 yükseliş bekle VEYA 1h tekrar SATIŞ olması
                            wait_for_better_entry = True
                            entry_strategy = "ALIŞ-SATIŞ-SATIŞ: Fiyatın %1 yükselmesini bekleyip satım yapılacak VEYA 1h tekrar SATIŞ olması"
                            print(f"   ⏳ BEKLEME: ALIŞ-SATIŞ-SATIŞ")
                        else:
                            # Diğer farklı durumlar → Anında işlem
                            wait_for_better_entry = False
                            entry_strategy = "Anında işlem (1h farklı ama özel durum değil)"
                            print(f"   ⚡ ANINDA İŞLEM: 1h farklı ama özel durum değil - {signal_values}")
                    else:
                        # Tüm zaman dilimleri aynı → Anında işlem
                        wait_for_better_entry = False
                        entry_strategy = "Anında işlem (tüm zaman dilimleri aynı)"
                        print(f"   ⚡ ANINDA İŞLEM: Tüm zaman dilimleri aynı - {signal_values}")
                    
                    # 4 saatlik cooldown kontrolü
                    cooldown_key = (symbol, sinyal_tipi)
                    if cooldown_key in cooldown_signals:
                        last_time = cooldown_signals[cooldown_key]
                        if (datetime.now() - last_time) < timedelta(hours=2):
                            # Cooldown süresi dolmadıysa sinyalleri güncelle ve devam et
                            print(f"   ⏸️ COOLDOWN: {symbol} için 2 saat dolmadı")
                            previous_signals[symbol] = current_signals.copy()
                            return  # 2 saat dolmadıysa sinyal arama
                        else:
                            del cooldown_signals[cooldown_key]  # 2 saat dolduysa tekrar sinyal aranabilir
                            print(f"   ✅ COOLDOWN: {symbol} için 2 saat doldu, tekrar sinyal aranabilir")
                    
                    # Aynı sinyal daha önce gönderilmiş mi kontrol et
                    signal_key = (symbol, sinyal_tipi)
                    if sent_signals.get(signal_key) == signal_values:
                        # Aynı sinyal daha önce gönderilmişse sinyalleri güncelle ve devam et
                        print(f"   🔄 AYNI SİNYAL: {symbol} için aynı sinyal daha önce gönderilmiş")
                        previous_signals[symbol] = current_signals.copy()
                        return
                    
                    print(f"   🎯 YENİ SİNYAL: {symbol} için yeni sinyal hazırlanıyor...")
                    
                    # Yeni sinyal gönder
                    sent_signals[signal_key] = signal_values.copy()
                    
                    # Son fiyatı al (1h verisinden)
                    try:
                        df_1h = await async_get_historical_data(symbol, timeframes['1h'], 2)
                        price = float(df_1h['close'].iloc[-1])
                    except Exception as e:
                        print(f"Fiyat çekme hatası: {symbol} - {str(e)}")
                        previous_signals[symbol] = current_signals.copy()
                        return
                    
                    message, dominant_signal, target_price, stop_loss, stop_loss_str = create_signal_message(symbol, price, current_signals)
                    if message and not wait_for_better_entry:  # Sadece beklemeyen sinyaller için mesaj gönder
                        print(f"   📤 TELEGRAM'A GÖNDERİLİYOR: {symbol} - {dominant_signal}")
                        print(f"   📊 Değişiklik: {prev_signals} -> {current_signals}")
                        await send_telegram_message(message)
                        
                        # Kaldıraç hesaplama
                        buy_count = sum(1 for s in current_signals.values() if s == 1)
                        sell_count = sum(1 for s in current_signals.values() if s == -1)
                        leverage = 10 if (buy_count == 3 or sell_count == 3) else 5
                        
                        print(f"   💰 Pozisyon kaydediliyor: {symbol} - {dominant_signal} - Kaldıraç: {leverage}x")
                        
                        # Anında pozisyonu kaydet (tüm sayısal değerler float!)
                        kaydet_pozisyon(positions, symbol, dominant_signal, price, target_price, stop_loss, stop_loss_str, {k: ("ALIŞ" if v == 1 else "SATIŞ") for k, v in current_signals.items()}, leverage, str(datetime.now()))
                        # Aktif sinyal olarak kaydet
                        kaydet_aktif_sinyal(active_signals, symbol, dominant_signal, price, target_price, stop_loss, {k: ("ALIŞ" if v == 1 else "SATIŞ") for k, v in current_signals.items()}, leverage, str(datetime.now()))
                        # İstatistikleri güncelle
                        update_stats(stats, active_signals, total_signals_delta=1)
                        print(f"   ✅ SİNYAL BAŞARILI: {symbol} için sinyal gönderildi ve pozisyon açıldı")
                    elif wait_for_better_entry:
                        # Bekleyen sinyal olarak kaydet (mesaj gönderme)
                        print(f"   ⏳ BEKLEYEN SİNYAL: {symbol} - {entry_strategy}")
                        
                        # Bekleyen sinyal olarak kaydet
                        waiting_signals[symbol] = {
                            "type": "ALIŞ" if sinyal_tipi == 'ALIS' else "SATIŞ",
                            "signal_price": price,
                            "signals": {k: ("ALIŞ" if v == 1 else "SATIŞ") for k, v in current_signals.items()},
                            "leverage": 10 if (signal_values.count(1) == 3 or signal_values.count(-1) == 3) else 5,
                            "wait_start_time": str(datetime.now()),
                            "entry_strategy": entry_strategy
                        }
                        
                        print(f"   📝 BEKLEYEN SİNYAL KAYDEDİLDİ: {symbol} - {entry_strategy}")
                        # Bekleme mantığı burada çalışacak ama mesaj gönderilmeyecek
                    else:
                        print(f"   ❌ MESAJ OLUŞTURULAMADI: {symbol} için mesaj oluşturulamadı")
                    # Sinyalleri güncelle (her durumda)
                    previous_signals[symbol] = current_signals.copy()
                await asyncio.sleep(0)  # Task'ler arası context switch için

            # Paralel task listesi oluştur
            tasks = [process_symbol(symbol) for symbol in symbols]
            await asyncio.gather(*tasks)
            
            # İlk çalıştırma tamamlandıysa
            if first_run:
                first_run = False
                print("✅ İlk çalıştırma tamamlandı!")
                print("🎯 Artık sinyal değişiklikleri takip ediliyor ve Telegram'a gönderilecek!")
                print("📊 Mevcut durum kaydedildi, değişiklikler aranıyor...")
            
            save_all_state(active_signals, waiting_signals, successful_signals, failed_signals)
            print_stats(stats, waiting_signals)
            await asyncio.sleep(5)
        except Exception as e:
            print(f"Genel hata: {e}")
            print("10 saniye bekleniyor ve tekrar deneniyor...")
            await asyncio.sleep(10)
            continue

async def run_bot():
    global _session
    loop_task = None
    try:
        # Kapanışta session ve dosya kaydı için signal handler
        def handle_exit(signum, frame):
            print("\nKapanış sinyali alındı, durum kaydediliyor ve session kapatılıyor...")
            # main_loop içindeki değişkenlere erişmek için closure veya global kullanılabilir
            # Burada sadece session'ı kapatıyoruz
            if _session and not _session.closed:
                asyncio.get_event_loop().run_until_complete(_session.close())
            exit(0)
        signal.signal(signal.SIGINT, handle_exit)
        signal.signal(signal.SIGTERM, handle_exit)
        loop_task = asyncio.create_task(main_loop())
        await loop_task
    finally:
        if _session and not _session.closed:
            await _session.close()

if __name__ == "__main__":
    asyncio.run(run_bot())
