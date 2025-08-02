import sys
import asyncio
if sys.platform.startswith("win"):
    asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())
import pandas as pd
import numpy as np
import ta
import time
from datetime import datetime, timedelta
import aiohttp
import json
from decimal import Decimal, ROUND_DOWN, getcontext

from collections import defaultdict

# Binance client oluştur (globalde)
from binance.client import Client
client = Client()

def get_tr_time():
    """TR saat dilimi için zaman alma fonksiyonu"""
    try:
        from zoneinfo import ZoneInfo
        return datetime.now(ZoneInfo("Europe/Istanbul"))
    except ImportError:
        import pytz
        return datetime.now(pytz.timezone("Europe/Istanbul"))

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

def calculate_full_pine_signals(df, timeframe, rsi_oversold=45, rsi_overbought=60, fib_filter_enabled=False):
    """
    Pine Script'e birebir uyumlu AL/SAT sinyal hesaplaması.
    Zaman dilimine göre özel parametreler içerir.
    RSI seviyeleri 45-60 olarak sabitlendi.
    """
    # --- Zaman dilimine göre sabit parametreler ---
    if timeframe == '4h':
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

    # RSI - sabit 45-60 seviyeleri
    df['rsi'] = ta.momentum.RSIIndicator(df['close'], window=rsi_length).rsi()
    # rsi_overbought = 60, rsi_oversold = 45 sabit

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

    # Sinyaller - sabit 45-60 RSI seviyeleri
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

async def get_active_high_volume_usdt_pairs(top_n=40):
    """
    Sadece Futures'da aktif, USDT bazlı coinlerden hacme göre sıralanmış ilk top_n kadar uygun coin döndürür.
    1 günlük verisi 30 mumdan az olan coin'ler elenir.
    """
    # Futures exchange info al
    futures_exchange_info = client.futures_exchange_info()
    futures_tickers = client.futures_ticker()
    
    futures_usdt_pairs = set()
    for symbol in futures_exchange_info['symbols']:
        if (
            symbol['quoteAsset'] == 'USDT' and
            symbol['status'] == 'TRADING' and
            symbol['contractType'] == 'PERPETUAL'
        ):
            futures_usdt_pairs.add(symbol['symbol'])

    high_volume_pairs = []
    for ticker in futures_tickers:
        symbol = ticker['symbol']
        if symbol in ['USDCUSDT', 'FDUSDUSDT', 'TUSDUSDT', 'BUSDUSDT', 'USDPUSDT', 'USDTUSDT']:
            continue
        if symbol in futures_usdt_pairs:
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
                idx += 1
                continue
            uygun_pairs.append(symbol)
        except Exception as e:
            idx += 1
            continue
        idx += 1

    return uygun_pairs

class BacktestEngine:
    def __init__(self, initial_balance=100, leverage=10, commission=0.001, profit_percent=3.0, stop_percent=2.5):
        self.initial_balance = initial_balance
        self.balance = initial_balance
        self.leverage = leverage
        self.commission = commission
        self.profit_percent = profit_percent
        self.stop_percent = stop_percent
        self.positions = {}
        self.trades = []
        self.equity_curve = []
        self.signals = []
        
        # Orijinal kod özellikleri
        self.sent_signals = dict()  # {(symbol, sinyal_tipi): signal_values}
        self.cooldown_signals = dict()  # {(symbol, sinyal_tipi): datetime}
        self.stop_cooldown = dict()  # {symbol: datetime}
        self.previous_signals = dict()  # {symbol: {tf: signal}}
        self.successful_signals = dict()  # {symbol: {...}}
        self.failed_signals = dict()  # {symbol: {...}}
        self.first_run = True  # İlk çalıştırma kontrolü
        
    def reset(self):
        """Backtest'i sıfırla"""
        self.balance = self.initial_balance
        self.positions = {}
        self.trades = []
        self.equity_curve = []
        self.signals = []
        self.sent_signals = dict()
        self.cooldown_signals = dict()
        self.stop_cooldown = dict()
        self.previous_signals = dict()
        self.successful_signals = dict()
        self.failed_signals = dict()
        self.first_run = True
    
    def open_position(self, symbol, signal_type, entry_price, timestamp, signals_dict):
        """Pozisyon aç"""
        if signal_type == "ALIŞ":
            target_price = entry_price * (1 + self.profit_percent / 100)  # %3 kar hedefi
            stop_loss = entry_price * (1 - self.stop_percent / 100)      # %2.5 stop loss
        else:  # SATIŞ
            target_price = entry_price * (1 - self.profit_percent / 100)  # %3 kar hedefi
            stop_loss = entry_price * (1 + self.stop_percent / 100)       # %2.5 stop loss
        
        position_size = self.initial_balance  # Sabit $100
        quantity = position_size / entry_price  # Normal quantity
        
        self.positions[symbol] = {
            "type": signal_type,
            "entry_price": entry_price,
            "target_price": target_price,
            "stop_loss": stop_loss,
            "quantity": quantity,
            "position_size": position_size,  # position_size'ı da kaydet
            "entry_time": timestamp,
            "signals": signals_dict
        }
        
        # Komisyon düş
        commission_cost = position_size * self.commission
        self.balance -= commission_cost
        
        print(f"📈 {timestamp.strftime('%Y-%m-%d %H:%M')} - {symbol} {signal_type} pozisyonu açıldı: {format_price(entry_price)} | Hedef: %{self.profit_percent} | Stop: %{self.stop_percent}")
    
    def close_position(self, symbol, exit_price, timestamp, reason):
        """Pozisyon kapat"""
        if symbol not in self.positions:
            return
        
        position = self.positions[symbol]
        entry_price = position["entry_price"]
        quantity = position["quantity"]
        position_size = position["position_size"]
        
        if position["type"] == "ALIŞ":
            pnl = (exit_price - entry_price) * quantity * self.leverage  # Kaldıraç PnL'ye uygulanır
        else:  # SATIŞ
            pnl = (entry_price - exit_price) * quantity * self.leverage  # Kaldıraç PnL'ye uygulanır
        
        # Komisyon düş
        position_value = quantity * exit_price
        commission_cost = position_value * self.commission
        pnl -= commission_cost
        
        self.balance += pnl
        
        # Trade kaydı
        trade = {
            "symbol": symbol,
            "type": position["type"],
            "entry_price": entry_price,
            "exit_price": exit_price,
            "entry_time": position["entry_time"],
            "exit_time": timestamp,
            "pnl": pnl,
            "pnl_percent": (pnl / (position_size)) * 100,  # Düzeltildi: position_size kullan
            "reason": reason,
            "signals": position["signals"]
        }
        self.trades.append(trade)
        
        # Cooldown ekle
        signal_type = "ALIS" if position["type"] == "ALIŞ" else "SATIS"
        self.cooldown_signals[(symbol, signal_type)] = timestamp
        
        # Başarılı/başarısız sinyal kaydı
        if reason == "HEDEF":
            self.successful_signals[symbol] = {
                "symbol": symbol,
                "type": position["type"],
                "entry_price": format_price(entry_price, entry_price),
                "exit_price": format_price(exit_price, entry_price),
                "target_price": format_price(position["target_price"], entry_price),
                "stop_loss": format_price(position["stop_loss"], entry_price),
                "signals": position["signals"],
                "completion_time": str(timestamp),
                "status": "SUCCESS",
                "profit_percent": round(trade['pnl_percent'], 2),
                "profit_usd": round(pnl, 2),
                "leverage": self.leverage,
                "entry_time": str(position["entry_time"]),
                "duration_hours": round((timestamp - position["entry_time"]).total_seconds() / 3600, 2)
            }
        else:  # STOP
            self.failed_signals[symbol] = {
                "symbol": symbol,
                "type": position["type"],
                "entry_price": format_price(entry_price, entry_price),
                "exit_price": format_price(exit_price, entry_price),
                "target_price": format_price(position["target_price"], entry_price),
                "stop_loss": format_price(position["stop_loss"], entry_price),
                "signals": position["signals"],
                "completion_time": str(timestamp),
                "status": "FAILED",
                "loss_percent": round(trade['pnl_percent'], 2),
                "loss_usd": round(pnl, 2),
                "leverage": self.leverage,
                "entry_time": str(position["entry_time"]),
                "duration_hours": round((timestamp - position["entry_time"]).total_seconds() / 3600, 2)
            }
            # Stop sonrası 8 saatlik cooldown
            self.stop_cooldown[symbol] = timestamp
        
        print(f"📉 {timestamp.strftime('%Y-%m-%d %H:%M')} - {symbol} pozisyonu kapatıldı: {format_price(exit_price)} | PnL: ${pnl:.2f} ({trade['pnl_percent']:.2f}%) | Sebep: {reason}")
        
        del self.positions[symbol]
    
    def check_positions(self, symbol, high, low, close, timestamp):
        """Pozisyonları kontrol et"""
        if symbol not in self.positions:
            return
        
        position = self.positions[symbol]
        
        if position["type"] == "ALIŞ":
            # Hedef kontrolü
            if high >= position["target_price"]:
                self.close_position(symbol, position["target_price"], timestamp, "HEDEF")
            # Stop kontrolü
            elif low <= position["stop_loss"]:
                self.close_position(symbol, position["stop_loss"], timestamp, "STOP")
        else:  # SATIŞ
            # Hedef kontrolü
            if low <= position["target_price"]:
                self.close_position(symbol, position["target_price"], timestamp, "HEDEF")
            # Stop kontrolü
            elif high >= position["stop_loss"]:
                self.close_position(symbol, position["stop_loss"], timestamp, "STOP")
    
    def record_equity(self, timestamp):
        """Equity kaydı"""
        current_equity = self.balance
        for symbol, position in self.positions.items():
            # Pozisyon değerini hesapla (basit yaklaşım)
            current_equity += position["quantity"] * position["entry_price"]
        
        self.equity_curve.append({
            "timestamp": timestamp,
            "equity": current_equity,
            "balance": self.balance,
            "open_positions": len(self.positions)
        })
    
    def get_statistics(self):
        """Backtest istatistiklerini hesapla"""
        if not self.trades:
            return {
                "total_trades": 0,
                "winning_trades": 0,
                "losing_trades": 0,
                "win_rate": 0,
                "total_pnl": 0,
                "avg_win": 0,
                "avg_loss": 0,
                "profit_factor": 0,
                "max_drawdown": 0,
                "sharpe_ratio": 0,
                "final_balance": self.balance,
                "total_return": 0
            }
        
        winning_trades = [t for t in self.trades if t["pnl"] > 0]
        losing_trades = [t for t in self.trades if t["pnl"] < 0]
        
        total_pnl = sum(t["pnl"] for t in self.trades)
        avg_win = np.mean([t["pnl"] for t in winning_trades]) if winning_trades else 0
        avg_loss = np.mean([t["pnl"] for t in losing_trades]) if losing_trades else 0
        
        # Profit factor
        gross_profit = sum(t["pnl"] for t in winning_trades)
        gross_loss = abs(sum(t["pnl"] for t in losing_trades))
        profit_factor = gross_profit / gross_loss if gross_loss > 0 else float('inf')
        
        # Max drawdown
        equity_values = [e["equity"] for e in self.equity_curve]
        peak = equity_values[0]
        max_dd = 0
        for equity in equity_values:
            if equity > peak:
                peak = equity
            dd = (peak - equity) / peak * 100
            if dd > max_dd:
                max_dd = dd
        
        # Sharpe ratio (basit hesaplama)
        returns = []
        for i in range(1, len(self.equity_curve)):
            prev_equity = self.equity_curve[i-1]["equity"]
            curr_equity = self.equity_curve[i]["equity"]
            ret = (curr_equity - prev_equity) / prev_equity
            returns.append(ret)
        
        sharpe_ratio = np.mean(returns) / np.std(returns) * np.sqrt(252) if len(returns) > 1 and np.std(returns) > 0 else 0
        
        return {
            "total_trades": len(self.trades),
            "winning_trades": len(winning_trades),
            "losing_trades": len(losing_trades),
            "win_rate": len(winning_trades) / len(self.trades) * 100,
            "total_pnl": total_pnl,
            "avg_win": avg_win,
            "avg_loss": avg_loss,
            "profit_factor": profit_factor,
            "max_drawdown": max_dd,
            "sharpe_ratio": sharpe_ratio,
            "final_balance": self.balance,
            "total_return": (self.balance - self.initial_balance) / self.initial_balance * 100
        }

async def run_backtest_with_interval_optimization(symbols, start_date, end_date, timeframe='4h'):
    """Sinyal arama aralıklarını optimize eden backtest çalıştır"""
    print(f"🚀 Sinyal Arama Aralığı Optimizasyonlu Futures Backtest başlatılıyor...")
    print(f"📅 SABİT TARİH ARALIĞI: {start_date.strftime('%Y-%m-%d')} - {end_date.strftime('%Y-%m-%d')}")
    print(f"⏰ Zaman dilimi: 4h-1d kombinasyonu")
    print(f"💰 Başlangıç bakiyesi: $100")
    print(f"📊 Kaldıraç: 10x")
    print(f"💸 Komisyon: %0.1")
    print(f"🎯 Hedef: %3 | 🛑 Stop: %2.5")
    print(f"📈 Veri Kaynağı: Binance Futures")
    print(f"🎯 RSI Seviyeleri: 45-60 (Sabit)")
    print(f"📊 SABİT COİN SAYISI: {len(symbols)}")
    print("=" * 60)
    
    # Sinyal arama aralıkları (dakika cinsinden)
    search_intervals = [
        (5, "5 dakika"),
        (10, "10 dakika"),
        (15, "15 dakika")
    ]
    
    best_result = None
    best_stats = None
    best_interval = None
    all_results_data = []  # Tüm sonuçları sakla
    
    # Her arama aralığı için backtest yap
    for interval_minutes, interval_name in search_intervals:
        print(f"\n🔍 Sinyal Arama Aralığı Test Ediliyor: {interval_name}")
        print(f"📊 Test edilen coin sayısı: {len(symbols)}")
        print("-" * 50)
        
        # Backtest engine oluştur
        engine = BacktestEngine(initial_balance=100, leverage=10, commission=0.001, profit_percent=3.0, stop_percent=2.5)
        
        # Zaman dilimi mapping
        timeframes = {
            '4h': '4h',
            '1d': '1d'
        }
        tf_names = ['4h', '1d']
        
        # Her coin için backtest yap
        for symbol in symbols:
            try:
                # Tüm zaman dilimleri için veri çek
                all_data = {}
                for tf in tf_names:
                    try:
                        df = await async_get_historical_data(symbol, timeframes[tf], 1000)
                        df = calculate_full_pine_signals(df, tf, 45, 60)  # Sabit 45-60 RSI
                        all_data[tf] = df
                    except Exception as e:
                        print(f"⚠️ {symbol} {tf} veri çekme hatası: {e}")
                        continue
                
                if not all_data:
                    print(f"❌ {symbol} için hiç veri bulunamadı")
                    continue
                
                # En kısa zaman diliminin verilerini kullan
                main_tf = '4h' if '4h' in all_data else list(all_data.keys())[0]
                main_df = all_data[main_tf]
                
                # Tarih filtreleme
                main_df = main_df[(main_df['timestamp'] >= start_date) & (main_df['timestamp'] <= end_date)]
                
                if len(main_df) < 50:
                    continue
                
                # İlk çalıştırma için sinyalleri kaydet
                if engine.first_run:
                    first_time = main_df.iloc[50]['timestamp']
                    first_signals = {}
                    for tf in tf_names:
                        if tf in all_data:
                            tf_df = all_data[tf]
                            closest_idx = tf_df['timestamp'].searchsorted(first_time)
                            if closest_idx < len(tf_df):
                                signal = int(tf_df.iloc[closest_idx]['signal'])
                                if signal == 0:
                                    if tf_df['macd'].iloc[closest_idx] > tf_df['macd_signal'].iloc[closest_idx]:
                                        signal = 1
                                    else:
                                        signal = -1
                                first_signals[tf] = signal
                            else:
                                first_signals[tf] = 0
                        else:
                            first_signals[tf] = 0
                    engine.previous_signals[symbol] = first_signals.copy()
                
                # Her mum için sinyal kontrolü
                for i in range(50, len(main_df)):
                    current_time = main_df.iloc[i]['timestamp']
                    current_price = main_df.iloc[i]['close']
                    current_high = main_df.iloc[i]['high']
                    current_low = main_df.iloc[i]['low']
                    
                    # Equity kaydı
                    engine.record_equity(current_time)
                    
                    # Mevcut pozisyonları kontrol et
                    engine.check_positions(symbol, current_high, current_low, current_price, current_time)
                    
                    # Eğer pozisyon açıksa yeni sinyal arama
                    if symbol in engine.positions:
                        continue
                    
                    # Sinyal arama aralığı kontrolü - sadece belirli aralıklarda sinyal ara
                    if i % (interval_minutes // 5) != 0:  # 5 dakikalık mumlar için
                        continue
                    
                    # Stop sonrası 1 saatlik cooldown kontrolü
                    if symbol in engine.stop_cooldown:
                        last_stop = engine.stop_cooldown[symbol]
                        if (current_time - last_stop) < timedelta(hours=1):
                            continue
                        else:
                            del engine.stop_cooldown[symbol]
                    
                    # Başarılı/başarısız sinyal sonrası 1 saatlik cooldown kontrolü
                    for sdict in [engine.successful_signals, engine.failed_signals]:
                        if symbol in sdict:
                            last_time = sdict[symbol].get("completion_time")
                            if last_time:
                                last_time_dt = datetime.fromisoformat(last_time)
                                if (current_time - last_time_dt) < timedelta(hours=1):
                                    continue
                                else:
                                    del sdict[symbol]
                    
                    # Tüm zaman dilimlerinin sinyallerini al
                    current_signals = {}
                    for tf in tf_names:
                        if tf in all_data:
                            tf_df = all_data[tf]
                            closest_idx = tf_df['timestamp'].searchsorted(current_time)
                            if closest_idx < len(tf_df):
                                signal = int(tf_df.iloc[closest_idx]['signal'])
                                if signal == 0:
                                    if tf_df['macd'].iloc[closest_idx] > tf_df['macd_signal'].iloc[closest_idx]:
                                        signal = 1
                                    else:
                                        signal = -1
                                current_signals[tf] = signal
                            else:
                                current_signals[tf] = 0
                        else:
                            current_signals[tf] = 0
                    
                    # İlk çalıştırma değilse, değişiklik kontrolü yap
                    if not engine.first_run and symbol in engine.previous_signals:
                        prev_signals = engine.previous_signals[symbol]
                        signal_changed = False
                        for tf in tf_names:
                            if prev_signals[tf] != current_signals[tf]:
                                signal_changed = True
                                break
                        if not signal_changed:
                            continue
                    
                    # 2 zaman dilimi şartı kontrolü (4h-1d)
                    signal_values = [current_signals.get(tf, 0) for tf in tf_names]
                    
                    buy_count = sum(1 for s in signal_values if s == 1)
                    sell_count = sum(1 for s in signal_values if s == -1)
                    
                    if buy_count == 2:  # 2/2 ALIŞ
                        sinyal_tipi = 'ALIS'
                        leverage = 10
                    elif sell_count == 2:  # 2/2 SATIŞ
                        sinyal_tipi = 'SATIS'
                        leverage = 10
                    else:
                        if not engine.first_run:
                            engine.previous_signals[symbol] = current_signals.copy()
                        continue
                    
                    # 1 saatlik cooldown kontrolü
                    cooldown_key = (symbol, sinyal_tipi)
                    if cooldown_key in engine.cooldown_signals:
                        last_time = engine.cooldown_signals[cooldown_key]
                        if (current_time - last_time) < timedelta(hours=1):
                            if not engine.first_run:
                                engine.previous_signals[symbol] = current_signals.copy()
                            continue
                        else:
                            del engine.cooldown_signals[cooldown_key]
                    
                    # Aynı sinyal daha önce gönderilmiş mi kontrol et
                    signal_key = (symbol, sinyal_tipi)
                    if engine.sent_signals.get(signal_key) == signal_values:
                        if not engine.first_run:
                            engine.previous_signals[symbol] = current_signals.copy()
                        continue
                    
                    # Yeni sinyal gönder
                    engine.sent_signals[signal_key] = signal_values.copy()
                    
                    # Sinyal türünü belirle
                    if sinyal_tipi == 'ALIS':
                        signal_type = "ALIŞ"
                    else:
                        signal_type = "SATIŞ"
                    
                    engine.open_position(symbol, signal_type, current_price, current_time, current_signals)
                    engine.signals.append({
                        "timestamp": current_time,
                        "symbol": symbol,
                        "type": signal_type,
                        "price": current_price,
                        "signals": current_signals
                    })
                    
                    if not engine.first_run:
                        engine.previous_signals[symbol] = current_signals.copy()
                
                # Son pozisyonları kapat
                if symbol in engine.positions:
                    last_price = main_df.iloc[-1]['close']
                    last_time = main_df.iloc[-1]['timestamp']
                    engine.close_position(symbol, last_price, last_time, "BACKTEST SONU")
                    
            except Exception as e:
                continue
        
        # İlk çalıştırma tamamlandıysa
        if engine.first_run:
            engine.first_run = False
        
        # Sonuçları hesapla
        stats = engine.get_statistics()
        
        print(f"📊 {interval_name} Arama Aralığı Sonuçları:")
        print(f"   Toplam İşlem: {stats['total_trades']}")
        print(f"   Kazanma Oranı: %{stats['win_rate']:.2f}")
        print(f"   Toplam Getiri: %{stats['total_return']:.2f}")
        print(f"   Profit Factor: {stats['profit_factor']:.2f}")
        print(f"   Son Bakiye: ${stats['final_balance']:.2f}")
        
        # Sonucu sakla
        all_results_data.append({
            "interval": interval_name,
            "interval_minutes": interval_minutes,
            "total_trades": stats['total_trades'],
            "win_rate": stats['win_rate'],
            "total_return": stats['total_return'],
            "profit_factor": stats['profit_factor'],
            "final_balance": stats['final_balance'],
            "avg_win": stats['avg_win'],
            "avg_loss": stats['avg_loss'],
            "max_drawdown": stats['max_drawdown'],
            "sharpe_ratio": stats['sharpe_ratio']
        })
        
        # En iyi sonucu güncelle
        if best_result is None or stats['total_return'] > best_stats['total_return']:
            best_result = engine
            best_stats = stats
            best_interval = (interval_minutes, interval_name)
    
    # En iyi sonucu göster
    print("\n" + "=" * 100)
    print("🏆 EN İYİ SİNYAL ARAMA ARALIĞI BULUNDU!")
    print("=" * 100)
    print(f"🎯 En İyi Arama Aralığı: {best_interval[1]}")
    print(f"💰 Başlangıç Bakiyesi: ${best_result.initial_balance:,.2f}")
    print(f"💰 Son Bakiye: ${best_stats['final_balance']:,.2f}")
    print(f"📈 Toplam Getiri: %{best_stats['total_return']:.2f}")
    
    total_days = (end_date - start_date).days + 1
    daily_avg_signals = best_stats['total_trades'] / total_days if total_days > 0 else 0
    
    print(f"📊 Toplam İşlem: {best_stats['total_trades']}")
    print(f"📅 Toplam Gün: {total_days}")
    print(f"📈 Günlük Ortalama Sinyal: {daily_avg_signals:.2f}")
    print(f"✅ Kazanan İşlem: {best_stats['winning_trades']}")
    print(f"❌ Kaybeden İşlem: {best_stats['losing_trades']}")
    print(f"🎯 Kazanma Oranı: %{best_stats['win_rate']:.2f}")
    print(f"📈 Ortalama Kazanç: ${best_stats['avg_win']:.2f}")
    print(f"📉 Ortalama Kayıp: ${best_stats['avg_loss']:.2f}")
    print(f"💰 Profit Factor: {best_stats['profit_factor']:.2f}")
    print(f"📉 Maksimum Drawdown: %{best_stats['max_drawdown']:.2f}")
    print(f"📊 Sharpe Ratio: {best_stats['sharpe_ratio']:.2f}")
    
    # Tablo şeklinde karşılaştırma
    print("\n" + "=" * 120)
    print("📊 TÜM ARAMA ARALIKLARI KARŞILAŞTIRMA TABLOSU")
    print("=" * 120)
    print(f"{'Arama Aralığı':<15} {'Toplam İşlem':<12} {'Kazanma Oranı':<12} {'Toplam Getiri':<12} {'Profit Factor':<12} {'Son Bakiye':<12} {'Ort. Kazanç':<12} {'Ort. Kayıp':<12} {'Max DD':<10}")
    print("-" * 120)
    
    # Tüm sonuçları tablo halinde göster
    for result in all_results_data:
        # En iyi sonucu işaretle
        if result['interval'] == best_interval[1]:
            interval_display = f"🏆 {result['interval']}"
        else:
            interval_display = result['interval']
        
        print(f"{interval_display:<15} {result['total_trades']:<12} {result['win_rate']:<12.1f}% {result['total_return']:<12.1f}% {result['profit_factor']:<12.2f} ${result['final_balance']:<11.2f} ${result['avg_win']:<11.2f} ${result['avg_loss']:<11.2f} {result['max_drawdown']:<10.1f}%")
    
    print("-" * 120)
    print("📈 En iyi performans gösteren aralık işaretlenmiştir (🏆)")
    print("📊 DD = Drawdown (Maksimum Düşüş)")
    
    # Tüm sonuçları dosyaya kaydet
    all_results = {
        "best_interval": best_interval,
        "best_stats": best_stats,
        "all_intervals": all_results_data
    }
    
    with open(f'interval_optimization_results_{start_date.strftime("%Y%m%d")}_{end_date.strftime("%Y%m%d")}.json', 'w', encoding='utf-8') as f:
        json.dump(all_results, f, ensure_ascii=False, indent=2, default=str)
    
    print(f"\n💾 Sonuçlar 'interval_optimization_results_{start_date.strftime('%Y%m%d')}_{end_date.strftime('%Y%m%d')}.json' dosyasına kaydedildi.")
    
    return best_result, best_stats, best_interval

def plot_results(engine, stats):
    """Backtest sonuçlarını görselleştir - KALDIRILDI"""
    print("📊 Grafik çizimi kaldırıldı.")

async def main():
    """Ana fonksiyon"""
    print("🤖 Sinyal Arama Aralığı Optimizasyonlu Kripto Sinyal Futures Backtest Sistemi")
    print("=" * 80)
    
    # Test parametreleri - SABİT TARİH ARALIĞI
    start_date = datetime(2025, 1, 1)  # 1 Ocak 2025
    end_date = datetime(2025, 1, 31)   # 31 Ocak 2025 (SABİT 1 AY)
    timeframe = '4h'  # 4h-1d kombinasyonu
    
    # Test edilecek coinler - SABİT LİSTE
    print("📊 En yüksek hacimli Futures coinler alınıyor...")
    symbols = await get_active_high_volume_usdt_pairs(100)  # 100 coin ile test
    
    if not symbols:
        print("❌ Hiç coin bulunamadı!")
        return
    
    # Coin listesini sabitle ve yazdır
    print(f"✅ {len(symbols)} coin bulundu ve sabitlendi:")
    for i in range(0, len(symbols), 10):
        group = symbols[i:i+10]
        print(f"   {', '.join(group)}")
    
    print(f"\n📅 SABİT TARİH ARALIĞI: {start_date.strftime('%Y-%m-%d')} - {end_date.strftime('%Y-%m-%d')}")
    print(f"🎯 SABİT COİN SAYISI: {len(symbols)}")
    print("=" * 60)
    
    # Sinyal arama aralığı optimizasyonlu backtest çalıştır
    engine, stats, best_interval = await run_backtest_with_interval_optimization(symbols, start_date, end_date, timeframe)
    
    # Grafikleri çiz
    if engine.trades:
        plot_results(engine, stats)
    else:
        print("❌ Hiç işlem yapılmadı, grafik çizilemedi.")

if __name__ == "__main__":
    asyncio.run(main()) 