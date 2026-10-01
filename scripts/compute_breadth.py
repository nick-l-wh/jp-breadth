"""步驟 3：計算日本市場廣度指標（Stockbee Market Monitor 日本版）。

用法（在專案根目錄執行）：
    python scripts/compute_breadth.py
    python scripts/compute_breadth.py --min-avg-volume 10000000     # 20 日均量（股）下限
    python scripts/compute_breadth.py --min-avg-turnover 50000000   # 20 日均成交金額（日圓）下限
    python scripts/compute_breadth.py --diagnose                    # 只列出各門檻下的股票數，不輸出

輸入：data/universe.csv、data/prices/*.csv（步驟 2 產生，使用 AdjClose 計算報酬）
輸出：data/breadth.csv（每個交易日一列）

指標定義（全部只計入「當日通過流動性過濾」的股票）：
    up4 / down4        當日漲／跌 >= 4%
    ratio5 / ratio10   近 5／10 日 up4 合計 ÷ down4 合計
    q_up25 / q_dn25    65 個交易日報酬 >= +25% / <= -25%
    m_up25 / m_dn25    21 個交易日報酬 >= +25% / <= -25%
    m_up50 / m_dn50    21 個交易日報酬 >= +50% / <= -50%
    d34_up13 / d34_dn13  34 個交易日報酬 >= +13% / <= -13%
    t2108              收盤高於 40 日均線的股票比例（%）
"""
import argparse
import sys
from pathlib import Path

import numpy as np
import pandas as pd

from jpmarket import strip_tainted_bar

UNIVERSE = Path("data/universe.csv")
PRICE_DIR = Path("data/prices")
OUT = Path("data/breadth.csv")

QUARTER, MONTH, D34, MA = 65, 21, 34, 40
MAX_ABS_DAILY_RETURN = 0.60      # 單日報酬超過 ±60% 視為資料錯誤，剔除
MIN_COVERAGE = 0.5               # 當日至少 50% 股票有價格才算有效交易日
INDEX_TICKERS = {"^N225": "nikkei225", "1306.T": "topix_etf"}


def load_panel(tickers):
    adj, vol, close = {}, {}, {}
    missing = 0
    for t in tickers:
        p = PRICE_DIR / f"{t}.csv"
        if not p.exists():
            missing += 1
            continue
        try:
            d = pd.read_csv(p, index_col="Date", parse_dates=True,
                            usecols=["Date", "Close", "AdjClose", "Volume"])
        except Exception as e:  # noqa: BLE001
            print(f"  讀取失敗 {t}: {e}")
            missing += 1
            continue
        d = strip_tainted_bar(d, p.stat().st_mtime)   # 盤中寫入的檔案，去掉當日不完整 K 線
        adj[t], vol[t], close[t] = d["AdjClose"], d["Volume"], d["Close"]
    if missing:
        print(f"  {missing} 隻沒有價格檔，已略過")
    return pd.DataFrame(adj).sort_index(), pd.DataFrame(vol).sort_index(), pd.DataFrame(close).sort_index()


def eligibility(vol, close, min_vol, min_turnover):
    avg_vol = vol.rolling(20, min_periods=20).mean()
    avg_turn = (vol * close).rolling(20, min_periods=20).mean()
    return (avg_vol >= min_vol) & (avg_turn >= min_turnover), avg_vol, avg_turn


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--min-avg-volume", type=float, default=0,
                    help="20 日平均成交量（股）下限，預設 0 = 不限")
    ap.add_argument("--min-avg-turnover", type=float, default=10_000_000,
                    help="20 日平均成交金額（日圓）下限，預設 1000 萬日圓")
    ap.add_argument("--prime-only", action="store_true", help="只計算東證 Prime")
    ap.add_argument("--diagnose", action="store_true", help="只顯示各門檻股票數")
    args = ap.parse_args()

    if not UNIVERSE.exists():
        sys.exit("找不到 data/universe.csv")
    uni = pd.read_csv(UNIVERSE)
    if args.prime_only:
        uni = uni[uni["is_prime"]]
    tickers = uni["ticker"].tolist()

    print(f"讀取 {len(tickers)} 隻股票價格…")
    adj, vol, close = load_panel(tickers)
    idx_adj, _, idx_close = load_panel(list(INDEX_TICKERS))

    # 有效交易日：至少一半股票有價格
    coverage = adj.notna().mean(axis=1)
    valid_days = coverage[coverage >= MIN_COVERAGE].index
    dropped = len(adj) - len(valid_days)
    if dropped:
        print(f"  剔除 {dropped} 個覆蓋率不足的日期（例如收盤前的不完整資料）")
    adj, vol, close = adj.loc[valid_days], vol.loc[valid_days], close.loc[valid_days]
    print(f"  期間 {adj.index[0]:%Y-%m-%d} ~ {adj.index[-1]:%Y-%m-%d}，共 {len(adj)} 個交易日")

    elig, avg_vol, avg_turn = eligibility(vol, close, args.min_avg_volume, args.min_avg_turnover)

    if args.diagnose:
        last_vol, last_turn = avg_vol.iloc[-1].dropna(), avg_turn.iloc[-1].dropna()
        print(f"\n最新一日有資料的股票：{len(last_vol)} 隻")
        print("20 日平均成交量（股）≥ 門檻的股票數：")
        for th in (100_000, 500_000, 1_000_000, 3_000_000, 5_000_000, 10_000_000):
            print(f"  ≥ {th:>12,}：{(last_vol >= th).sum():>5}")
        print("20 日平均成交金額（日圓）≥ 門檻的股票數：")
        for th in (10_000_000, 30_000_000, 50_000_000, 100_000_000, 300_000_000, 1_000_000_000):
            print(f"  ≥ {th:>14,}：{(last_turn >= th).sum():>5}")
        return

    # ---- 報酬 ----
    ret1 = adj.pct_change(fill_method=None)
    bad = ret1.abs() > MAX_ABS_DAILY_RETURN
    n_bad = int(bad.sum().sum())
    if n_bad:
        print(f"  {n_bad} 筆單日報酬超過 ±{MAX_ABS_DAILY_RETURN:.0%}，視為資料錯誤，已排除")
    ret1 = ret1.mask(bad)

    retQ = adj / adj.shift(QUARTER) - 1
    retM = adj / adj.shift(MONTH) - 1
    ret34 = adj / adj.shift(D34) - 1
    ma = adj.rolling(MA, min_periods=MA).mean()

    def count(mask):
        return (mask & elig).sum(axis=1)

    out = pd.DataFrame(index=adj.index)
    out["universe_size"] = elig.sum(axis=1)
    out["up4"] = count(ret1 >= 0.04)
    out["down4"] = count(ret1 <= -0.04)
    out["ratio5"] = out["up4"].rolling(5).sum() / out["down4"].rolling(5).sum().replace(0, np.nan)
    out["ratio10"] = out["up4"].rolling(10).sum() / out["down4"].rolling(10).sum().replace(0, np.nan)
    out["q_up25"] = count(retQ >= 0.25)
    out["q_dn25"] = count(retQ <= -0.25)
    out["m_up25"] = count(retM >= 0.25)
    out["m_dn25"] = count(retM <= -0.25)
    out["m_up50"] = count(retM >= 0.50)
    out["m_dn50"] = count(retM <= -0.50)
    out["d34_up13"] = count(ret34 >= 0.13)
    out["d34_dn13"] = count(ret34 <= -0.13)
    above = ((adj > ma) & elig).sum(axis=1)
    has_ma = (ma.notna() & elig).sum(axis=1)
    out["t2108"] = (above / has_ma.replace(0, np.nan) * 100)

    # 指數對照（Yahoo 偶爾有單日錯價，例如 ETF 突然縮小 10 倍，用滾動中位數剔除）
    for tk, name in INDEX_TICKERS.items():
        if tk in idx_close:
            s = idx_close[tk].reindex(out.index)
            med = s.rolling(7, center=True, min_periods=3).median()
            bad_pts = (s / med - 1).abs() > 0.30
            if bad_pts.any():
                print(f"  {tk} 有 {int(bad_pts.sum())} 個異常價格已剔除並以前值填補：{[d.strftime('%Y-%m-%d') for d in s.index[bad_pts]]}")
                s = s.mask(bad_pts).ffill()
            out[name] = s
            out[name + "_chg_pct"] = s.pct_change(fill_method=None) * 100

    # 季度指標需要 65 日歷史，之前的列沒有意義
    out = out.iloc[QUARTER:]
    out = out.round(3)
    OUT.parent.mkdir(exist_ok=True)
    saved = OUT
    try:
        out.to_csv(OUT, index_label="Date")
    except PermissionError:
        # 檔案被 Excel 或 OneDrive 鎖住 → 改存到帶時間戳的新檔，計算結果不會浪費
        from datetime import datetime
        saved = OUT.with_name(f"breadth_{datetime.now():%Y%m%d_%H%M%S}.csv")
        out.to_csv(saved, index_label="Date")
        print(f"\n注意：{OUT} 正被其他程式（可能是 Excel）開啟，無法覆寫。"
              f"已改存為 {saved}。請關閉 Excel 後再執行一次。")

    print(f"\n已輸出 {saved}（{len(out)} 列）。最新 3 日：")
    cols = ["universe_size", "up4", "down4", "ratio5", "ratio10", "q_up25", "q_dn25", "t2108"]
    print(out[cols].tail(3).to_string())
    print(f"\n通過流動性過濾的股票：最新 {int(out['universe_size'].iloc[-1])} 隻，"
          f"期間最少 {int(out['universe_size'].min())} 隻")
    if out["universe_size"].iloc[-1] < 800:
        print("警告：通過過濾的股票不到 800 隻，廣度指標會偏窄。"
              "可先執行 --diagnose 查看各門檻的股票數，再調整 --min-avg-turnover。")


if __name__ == "__main__":
    main()
