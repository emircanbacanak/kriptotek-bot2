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
TELEGRAM_TOKEN = "7872345042:AAE6Om2LGtz1QjqfZz8ge0em6Gw29llzFno"
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
    # Hedef ve stop fiyatlarını, fiyatın ondalık basamağı kadar formatla
    target_price_str = format_price(target_price, price)
    stop_loss_str = format_price(stop_loss, price)
    message = f"""
🚨 {sinyal_tipi} \n\nKripto Çifti: {symbol}\nFiyat: {price_str}\n\n⏰ Zaman Dilimleri:\n1 Saat: {signal_1h}\n4 Saat: {signal_4h}\n1 Gün: {signal_1d}\n\nKaldıraç Önerisi: 5x - 10x\n\n💰 Hedef Fiyat: {target_price_str}\n🛑 Stop Loss: {stop_loss_str}\n\n⚠️ YATIRIM TAVSİYESİ DEĞİLDİR ⚠️\n\n📋 DİKKAT:\n• Portföyünüzün max %5-10'unu kullanın\n• Stop loss'u mutlaka uygulayın\n• FOMO ile acele karar vermeyin\n• Hedef fiyata ulaşınca kar alın\n• Kendi araştırmanızı yapın\n"""
    return message, dominant_signal, target_price, stop_loss, stop_loss_str

async def async_get_historical_data(symbol, interval, lookback):
    """Binance'den geçmiş verileri asenkron çek"""
    url = f"https://api.binance.com/api/v3/klines?symbol={symbol}&interval={interval}&limit={lookback}"
    async with aiohttp.ClientSession() as session:
        async with session.get(url, ssl=False) as resp:
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

def calculate_full_pine_signals(df, timeframe, fib_filter_enabled=False):
    """
    Pine Script algo.pine mantığını eksiksiz şekilde Python'a taşır.
    df: pandas DataFrame (timestamp, open, high, low, close, volume)
    timeframe: '15m', '2h', '1d', '1w' gibi string
    fib_filter_enabled: Fibonacci filtresi aktif mi?
    Dönüş: df (ekstra sütunlarla, en sonda 'signal')
    """
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

    # Ichimoku Bulutu (isteğe bağlı, sinyalde kullanılmıyor ama eklenebilir)
    def ichimoku(df, conv_periods=9, base_periods=26, span_b_periods=52, displacement=26):
        high = df['high']
        low = df['low']
        close = df['close']
        conv_line = (high.rolling(window=conv_periods).max() + low.rolling(window=conv_periods).min()) / 2
        base_line = (high.rolling(window=base_periods).max() + low.rolling(window=base_periods).min()) / 2
        leading_span_a = ((conv_line + base_line) / 2).shift(displacement)
        leading_span_b = ((high.rolling(window=span_b_periods).max() + low.rolling(window=span_b_periods).min()) / 2).shift(displacement)
        lagging_span = close.shift(-displacement)
        return conv_line, base_line, leading_span_a, leading_span_b, lagging_span
    # conv_line, base_line, leading_span_a, leading_span_b, lagging_span = ichimoku(df)

    # Pivot Noktaları (isteğe bağlı, sinyalde kullanılmıyor ama eklenebilir)
    def pivot_points(df):
        high = df['high'].shift(1)
        low = df['low'].shift(1)
        close = df['close'].shift(1)
        pivot = (high + low + close) / 3
        r1 = 2 * pivot - low
        s1 = 2 * pivot - high
        r2 = pivot + (high - low)
        s2 = pivot - (high - low)
        return pivot, r1, s1, r2, s2
    # df['pivot'], df['r1'], df['s1'], df['r2'], df['s2'] = pivot_points(df)

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

# --- YENİ ANA DÖNGÜ VE MANTIK ---
async def get_active_high_volume_usdt_pairs(min_volume=30000000):
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
        # Stablecoin çiftlerini hariç tut
        if symbol in ['USDCUSDT', 'FDUSDUSDT', 'TUSDUSDT', 'BUSDUSDT', 'USDPUSDT', 'USDTUSDT']:
            continue
        if symbol in spot_usdt_pairs:
            try:
                quote_volume = float(ticker['quoteVolume'])
                if quote_volume >= min_volume:
                    high_volume_pairs.append((symbol, quote_volume))
            except Exception:
                continue
    # Hacme göre sırala
    high_volume_pairs.sort(key=lambda x: x[1], reverse=True)
    # 1d verisi 30'dan az olanları atla, uygun tüm coinleri döndür
    uygun_pairs = []
    for symbol, volume in high_volume_pairs:
        try:
            df_1d = await async_get_historical_data(symbol, '1d', 40)
            if len(df_1d) < 30:
                print(f"{symbol}: 1d veri yetersiz ({len(df_1d)})")
                continue  # yeni coin, atla
            uygun_pairs.append(symbol)
        except Exception as e:
            print(f"{symbol}: 1d veri çekilemedi: {e}")
            continue
    return uygun_pairs

async def main():
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
        '4h': '4h',
        '1d': '1d'
    }
    tf_names = ['1h', '4h', '1d']
    
    print("Sinyal botu başlatıldı!")
    print("İlk çalıştırma: Mevcut sinyaller kaydediliyor, değişiklik bekleniyor...")
    
    while True:
        try:
            symbols = await get_active_high_volume_usdt_pairs(min_volume=30000000)
            tracked_coins.update(symbols)  # Takip edilen coinleri güncelle
            print(f"Takip edilen coin sayısı: {len(symbols)}")
            
            # 1. Pozisyonları kontrol et (hedef/stop)
            for symbol, pos in list(positions.items()):
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
                        elif last_price <= pos["stop"]:
                            msg = f"❌ {symbol} işlemi stop oldu! Stop fiyatı: {pos['stop_str']}, Şu anki fiyat: {format_price(last_price, pos['stop'])}"
                            await send_telegram_message(msg)
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
                        if last_price <= pos["target"]:
                            msg = f"🎯 <b>HEDEF BAŞARIYLA GERÇEKLEŞTİ!</b> 🎯\n\n<b>{symbol}</b> işlemi için hedef fiyatına ulaşıldı!\nÇıkış Fiyatı: <b>{format_price(last_price)}</b>\n"
                            await send_telegram_message(msg)
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
                except Exception as e:
                    print(f"Pozisyon kontrol hatası: {symbol} - {str(e)}")
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
                # 1 günlük veri kontrolü
                try:
                    df_1d = await async_get_historical_data(symbol, timeframes['1d'], 40)
                    if len(df_1d) < 30:
                        print(f"UYARI: {symbol} için 1 günlük veri 30'dan az, sinyal aranmıyor.")
                        return
                except Exception as e:
                    print(f"UYARI: {symbol} için 1 günlük veri çekilemedi: {str(e)}")
                    return
                # Mevcut sinyalleri al
                current_signals = dict()
                for tf_name in tf_names:
                    try:
                        df = await async_get_historical_data(symbol, timeframes[tf_name], 200)
                        df = calculate_full_pine_signals(df, tf_name)
                        current_signals[tf_name] = int(df['signal'].iloc[-1])
                    except Exception as e:
                        print(f"Hata: {symbol} - {tf_name} - {str(e)}")
                        current_signals[tf_name] = 0
                # İlk çalıştırmada sadece sinyalleri kaydet
                if first_run:
                    previous_signals[symbol] = current_signals.copy()
                    print(f"İlk çalıştırma - {symbol} sinyalleri kaydedildi: {current_signals}")
                    return
                # İlk çalıştırma değilse, değişiklik kontrolü yap
                if symbol in previous_signals:
                    prev_signals = previous_signals[symbol]
                    signal_changed = False
                    # Herhangi bir zaman diliminde değişiklik var mı kontrol et
                    for tf in tf_names:
                        if prev_signals[tf] != current_signals[tf]:
                            signal_changed = True
                            print(f"{symbol} - {tf} sinyali değişti: {prev_signals[tf]} -> {current_signals[tf]}")
                            break
                    if not signal_changed:
                        return  # Değişiklik yoksa devam et
                    # Değişiklik varsa, yeni sinyal analizi yap
                    signal_values = [current_signals[tf] for tf in tf_names]
                    # Sinyal koşullarını kontrol et
                    # Sinyal koşulu: sadece 3 zaman dilimi de aynıysa
                    if all(s == 1 for s in signal_values):
                        sinyal_tipi = 'ALIS'
                    elif all(s == -1 for s in signal_values):
                        sinyal_tipi = 'SATIS'
                    else:
                        previous_signals[symbol] = current_signals.copy()
                        return
                    # 4 saatlik cooldown kontrolü
                    cooldown_key = (symbol, sinyal_tipi)
                    if cooldown_key in cooldown_signals:
                        last_time = cooldown_signals[cooldown_key]
                        if (datetime.now() - last_time) < timedelta(hours=2):
                            # Cooldown süresi dolmadıysa sinyalleri güncelle ve devam et
                            previous_signals[symbol] = current_signals.copy()
                            return  # 2 saat dolmadıysa sinyal arama
                        else:
                            del cooldown_signals[cooldown_key]  # 2 saat dolduysa tekrar sinyal aranabilir
                    # Aynı sinyal daha önce gönderilmiş mi kontrol et
                    signal_key = (symbol, sinyal_tipi)
                    if sent_signals.get(signal_key) == signal_values:
                        # Aynı sinyal daha önce gönderilmişse sinyalleri güncelle ve devam et
                        previous_signals[symbol] = current_signals.copy()
                        return
                    # Yeni sinyal gönder
                    sent_signals[signal_key] = signal_values.copy()
                    price = float(df['close'].iloc[-1])
                    message, dominant_signal, target_price, stop_loss, stop_loss_str = create_signal_message(symbol, price, current_signals)
                    if message:
                        message = message.replace('Kaldıraç Önerisi: 10x', 'Kaldıraç Önerisi: 5x - 10x')
                        print(f"Telegram'a gönderiliyor: {symbol} - {dominant_signal}")
                        print(f"Değişiklik: {prev_signals} -> {current_signals}")
                        await send_telegram_message(message)
                        # Kaldıraç hesaplama
                        buy_count = sum(1 for s in current_signals.values() if s == 1)
                        sell_count = sum(1 for s in current_signals.values() if s == -1)
                        leverage = 10 if (buy_count == 3 or sell_count == 3) else 5
                        # Pozisyonu kaydet (tüm sayısal değerler float!)
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
                        # İstatistikleri güncelle
                        stats["total_signals"] += 1
                        stats["active_signals_count"] = len(active_signals)
                    # Sinyalleri güncelle (her durumda)
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
                    df = await async_get_historical_data(symbol, '1h', 2)
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
            
            # Takip edilen coinlerin listesi
            with open('tracked_coins.json', 'w', encoding='utf-8') as f:
                json.dump({
                    "tracked_coins": list(tracked_coins),
                    "count": len(tracked_coins),
                    "last_update": str(datetime.now())
                }, f, ensure_ascii=False, indent=2)
            
            # Başarılı sinyaller dosyası
            with open('successful_signals.json', 'w', encoding='utf-8') as f:
                json.dump({
                    "successful_signals": successful_signals,
                    "count": len(successful_signals),
                    "total_profit_usd": sum(signal.get("profit_usd", 0) for signal in successful_signals.values()),
                    "total_profit_percent": sum(signal.get("profit_percent", 0) for signal in successful_signals.values()),
                    "average_profit_per_signal": round(sum(signal.get("profit_usd", 0) for signal in successful_signals.values()) / max(len(successful_signals), 1), 2),
                    "average_duration_hours": round(sum(signal.get("duration_hours", 0) for signal in successful_signals.values()) / max(len(successful_signals), 1), 2),
                    "last_update": str(datetime.now())
                }, f, ensure_ascii=False, indent=2)
            
            # Başarısız sinyaller dosyası
            with open('failed_signals.json', 'w', encoding='utf-8') as f:
                json.dump({
                    "failed_signals": failed_signals,
                    "count": len(failed_signals),
                    "total_loss_usd": sum(signal.get("loss_usd", 0) for signal in failed_signals.values()),
                    "total_loss_percent": sum(signal.get("loss_percent", 0) for signal in failed_signals.values()),
                    "average_loss_per_signal": round(sum(signal.get("loss_usd", 0) for signal in failed_signals.values()) / max(len(failed_signals), 1), 2),
                    "average_duration_hours": round(sum(signal.get("duration_hours", 0) for signal in failed_signals.values()) / max(len(failed_signals), 1), 2),
                    "last_update": str(datetime.now())
                }, f, ensure_ascii=False, indent=2)
            
            # Genel istatistikler dosyası
            with open('general_stats.json', 'w', encoding='utf-8') as f:
                json.dump({
                    "total_signals": stats["total_signals"],
                    "successful_signals": stats["successful_signals"],
                    "failed_signals": stats["failed_signals"],
                    "total_profit_loss_usd": stats["total_profit_loss"],
                    "success_rate_percent": round((stats["successful_signals"] / max(stats["total_signals"], 1)) * 100, 2),
                    "average_profit_per_signal": round(stats["total_profit_loss"] / max(stats["total_signals"], 1), 2),
                    "last_update": str(datetime.now())
                }, f, ensure_ascii=False, indent=2)
            
            # STOP OLAN COINLERİ TAKİP ET
            for symbol, info in list(stopped_coins.items()):
                try:
                    df = await async_get_historical_data(symbol, '1h', 2)
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
            print("Tüm coinler kontrol edildi. 30 saniye bekleniyor...")
            await asyncio.sleep(30)
            
            # Aktif sinyalleri dosyaya kaydet
            with open('active_signals.json', 'w', encoding='utf-8') as f:
                json.dump({
                    "active_signals": active_signals,
                    "count": len(active_signals),
                    "last_update": str(datetime.now())
                }, f, ensure_ascii=False, indent=2)
            
        except Exception as e:
            print(f"Genel hata: {e}")
            await asyncio.sleep(10)

if __name__ == "__main__":
    asyncio.run(main())
