"""步驟 2：下載日本股票日線（含調整後價格），支援增量更新。

用法（在專案根目錄執行）：
    pip install pandas yfinance
    python scripts/download_prices.py              # 第一次：全量；之後：增量
    python scripts/download_prices.py --limit 100  # 只測試前 100 隻
    python scripts/download_prices.py --rebuild    # 強制全部重抓

輸出：data/prices/<ticker>.csv，欄位：
    Date, Open, High, Low, Close, AdjClose, Volume, AdjOpen, AdjHigh, AdjLow
  Close    = Yahoo 的 Close（已含分割調整，不含股息調整），用來偵測公司行動
  AdjClose = 分割 + 股息調整後收盤價，用來計算漲跌幅
  Adj*     = 以 AdjClose/Close 的比例換算的調整後開高低

運作方式：
  * 沒有 CSV 的股票 → 全量下載 2 年（每批 50 隻，批次間隨機等待）
  * 已有 CSV 的股票 → 只抓最近 10 天，與本地重疊日期比對：
        重疊日的 Close 或 AdjClose 被 Yahoo 改過（除權息、分割）→ 該股整檔重抓
        否則只追加新日期
  * 中途中斷後重跑，已完成的股票自動走增量，等於斷點續傳
"""
import argparse
import random
import sys
import time
from datetime import datetime
from pathlib import Path

import pandas as pd
import yfinance as yf

from build_universe import build as build_universe
from jpmarket import drop_partial_today, is_tainted, strip_tainted_bar

UNIVERSE = Path("data/universe.csv")
PRICE_DIR = Path("data/prices")
LOG_FILE = Path("data/download_log.csv")
BENCHMARKS = ["^N225", "1306.T"]   # 日經225、TOPIX 連動 ETF（Yahoo 已無 ^TOPX 資料）

BATCH_SIZE = 50
FULL_PERIOD = "2y"
INCR_PERIOD = "10d"
OVERLAP_CHECK_DAYS = 5          # 比對最近幾個重疊交易日
TOLERANCE = 5e-4                # 相對誤差容許（0.05%），避免四捨五入誤報
SLEEP_RANGE = (3, 8)            # 批次之間隨機等待秒數
MAX_RETRY = 3
KEEP = ["Open", "High", "Low", "Close", "AdjClose", "Volume"]


JPX_PAGE = "https://www.jpx.co.jp/english/markets/statistics-equities/misc/01.html"
JPX_DIRECT = "https://www.jpx.co.jp/markets/statistics-equities/misc/tvdivq0000001vg2-att/data_j.xls"
UNIVERSE_MAX_AGE_DAYS = 30


def find_candidates():
    """在 data/ 與「下載」資料夾找最新的 JPX 名單檔。"""
    dirs = [Path("data"), Path.home() / "Downloads"]
    files = []
    for d in dirs:
        for pat in ("data_e*.xlsx", "data_j*.xls", "data_j*.xlsx", "data_e*.xls"):
            files += list(d.glob(pat))
    return sorted(files, key=lambda f: f.stat().st_mtime, reverse=True)


def ensure_universe(force=False):
    """股票池不存在或超過 30 天 → 更新。先嘗試自動下載，失敗則請你放入檔案。"""
    if UNIVERSE.exists() and not force:
        age = (time.time() - UNIVERSE.stat().st_mtime) / 86400
        if age <= UNIVERSE_MAX_AGE_DAYS:
            return
        log(f"股票池已 {age:.0f} 天未更新，需要更新（JPX 每月公布一次）")
    else:
        log("需要建立股票池")

    # 1) 嘗試自動下載（需要 pip install xlrd）
    try:
        import urllib.request
        dst = Path("data/data_j.xls")
        dst.parent.mkdir(exist_ok=True)
        urllib.request.urlretrieve(JPX_DIRECT, dst)
        build_universe(dst)
        log("已自動下載並更新股票池")
        return
    except Exception as e:  # noqa: BLE001
        log(f"自動下載失敗（{type(e).__name__}），改為手動")

    # 2) 手動：開啟 JPX 頁面，請你下載後放入 data/ 或「下載」資料夾
    try:
        import webbrowser
        webbrowser.open(JPX_PAGE)
    except Exception:  # noqa: BLE001
        pass
    print(f"\n請到 JPX 頁面下載「List of TSE-listed Issues」的 Excel：\n  {JPX_PAGE}")
    while True:
        cands = find_candidates()
        hint = f"（偵測到最新檔案：{cands[0]}，直接按 Enter 使用）" if cands else ""
        ans = input(f"下載後請輸入檔案路徑，或把檔案放進 data/ 資料夾後按 Enter {hint}\n> ").strip().strip('"')
        path = Path(ans) if ans else (cands[0] if cands else None)
        if path and path.exists():
            try:
                build_universe(path)
                return
            except Exception as e:  # noqa: BLE001
                print(f"讀取失敗：{e}")
        else:
            print("找不到檔案，請再試一次。")


def log(msg):
    print(f"[{datetime.now():%H:%M:%S}] {msg}", flush=True)


def fetch_batch(tickers, period):
    """下載一批，回傳 {ticker: DataFrame}。失敗自動重試（指數退避）。"""
    for attempt in range(1, MAX_RETRY + 1):
        try:
            raw = yf.download(
                tickers, period=period, interval="1d",
                auto_adjust=False, actions=False, group_by="ticker",
                threads=False, progress=False, timeout=30,
            )
            if raw is not None and not raw.empty:
                return split_batch(raw, tickers)
            raise RuntimeError("empty response")
        except Exception as e:  # noqa: BLE001
            wait = 10 * 2 ** (attempt - 1) + random.uniform(0, 5)
            log(f"  批次失敗（第 {attempt}/{MAX_RETRY} 次）：{e}；{wait:.0f}s 後重試")
            time.sleep(wait)
    return {}


def split_batch(raw, tickers):
    out = {}
    multi = isinstance(raw.columns, pd.MultiIndex)
    for t in tickers:
        try:
            df = raw[t] if multi else raw
        except KeyError:
            continue
        df = clean(df)
        if df is not None and not df.empty:
            out[t] = df
    return out


def clean(df):
    """標準化欄位、移除異常列、補上調整後開高低。"""
    df = df.rename(columns={"Adj Close": "AdjClose"})
    if not set(["Open", "High", "Low", "Close", "AdjClose", "Volume"]).issubset(df.columns):
        return None
    df = df[["Open", "High", "Low", "Close", "AdjClose", "Volume"]].copy()
    idx = pd.to_datetime(df.index)
    if getattr(idx, "tz", None) is not None:
        idx = idx.tz_localize(None)
    df.index = idx.normalize()
    df.index.name = "Date"
    df = df.dropna(subset=["Close", "AdjClose"])
    df = df[(df["Close"] > 0) & (df["AdjClose"] > 0)]
    df = df[~df.index.duplicated(keep="last")].sort_index()
    df = drop_partial_today(df)   # 盤中下載時，捨棄今天未收盤的 K 線
    f = df["AdjClose"] / df["Close"]
    for c in ("Open", "High", "Low"):
        df["Adj" + c] = df[c] * f
    return df


FRESH_HOURS = 8


def is_fresh(t):
    p = PRICE_DIR / f"{t}.csv"
    return (p.exists() and not is_tainted(p.stat().st_mtime)
            and (time.time() - p.stat().st_mtime) < FRESH_HOURS * 3600)


def keep_awake(on):
    """Windows：執行期間防止電腦進入休眠（螢幕可關閉）。其他系統無作用。"""
    try:
        import ctypes
        ES_CONTINUOUS, ES_SYSTEM_REQUIRED = 0x80000000, 0x00000001
        ctypes.windll.kernel32.SetThreadExecutionState(
            ES_CONTINUOUS | ES_SYSTEM_REQUIRED if on else ES_CONTINUOUS)
    except Exception:  # noqa: BLE001
        pass


def read_local(t):
    p = PRICE_DIR / f"{t}.csv"
    if not p.exists():
        return None
    try:
        df = pd.read_csv(p, index_col="Date", parse_dates=True)
        return strip_tainted_bar(df, p.stat().st_mtime)   # 清掉舊檔中盤中寫入的不完整 K 線
    except Exception:  # 檔案損毀 → 視同不存在，重抓
        return None


def write_local(t, df):
    PRICE_DIR.mkdir(parents=True, exist_ok=True)
    df.round(6).to_csv(PRICE_DIR / f"{t}.csv")


def corp_action_changed(old, new):
    """重疊日期上 Close / AdjClose 是否被 Yahoo 改過 → 代表有分割或除息。"""
    common = old.index.intersection(new.index)
    if len(common) == 0:
        return True   # 完全無重疊（停牌過久），保守起見整檔重抓
    common = common[-OVERLAP_CHECK_DAYS:]
    for col in ("Close", "AdjClose"):
        a, b = old.loc[common, col], new.loc[common, col]
        if (((a - b).abs() / a) > TOLERANCE).any():
            return True
    return False


def run_full(tickers, results):
    for i in range(0, len(tickers), BATCH_SIZE):
        batch = tickers[i:i + BATCH_SIZE]
        log(f"全量下載 {i + 1}-{i + len(batch)} / {len(tickers)}")
        got = fetch_batch(batch, FULL_PERIOD)
        for t in batch:
            if t in got and len(got[t]) > 0:
                write_local(t, got[t])
                results[t] = ("full", len(got[t]), "ok")
            else:
                results[t] = ("full", 0, "no_data")
        time.sleep(random.uniform(*SLEEP_RANGE))


def run_incremental(tickers, results):
    refetch = []
    for i in range(0, len(tickers), BATCH_SIZE):
        batch = tickers[i:i + BATCH_SIZE]
        log(f"增量更新 {i + 1}-{i + len(batch)} / {len(tickers)}")
        got = fetch_batch(batch, INCR_PERIOD)
        for t in batch:
            old = read_local(t)
            new = got.get(t)
            if old is None:
                refetch.append(t)
            elif new is None or new.empty:
                results[t] = ("incr", 0, "no_new_data")   # 停牌 / 下市
            elif corp_action_changed(old, new):
                refetch.append(t)
                results[t] = ("incr", 0, "corp_action→refetch")
            else:
                merged = pd.concat([old[~old.index.isin(new.index)], new]).sort_index()
                added = len(merged) - len(old)
                write_local(t, merged)
                results[t] = ("incr", added, "ok")
        time.sleep(random.uniform(*SLEEP_RANGE))
    if refetch:
        log(f"{len(refetch)} 隻偵測到公司行動或本地缺檔，整檔重抓")
        run_full(refetch, results)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--limit", type=int, help="只處理前 N 隻（測試用）")
    ap.add_argument("--rebuild", action="store_true", help="強制全部重抓")
    ap.add_argument("--update-universe", action="store_true", help="強制更新股票池")
    ap.add_argument("--force", action="store_true", help="忽略「近期已更新」判斷，全部重做")
    args = ap.parse_args()

    ensure_universe(force=args.update_universe)
    tickers = pd.read_csv(UNIVERSE)["ticker"].tolist() + BENCHMARKS
    if args.limit:
        tickers = tickers[:args.limit] + BENCHMARKS
    tickers = list(dict.fromkeys(tickers))

    PRICE_DIR.mkdir(parents=True, exist_ok=True)
    keep_awake(True)

    # 斷點續傳：近期（FRESH_HOURS 小時內）已寫入的檔案直接跳過，不再重抓或比對
    fresh = set() if args.force else {t for t in tickers if is_fresh(t)}
    if fresh:
        log(f"跳過 {len(fresh)} 隻（{FRESH_HOURS} 小時內已更新，用 --force 可強制重做）")
    pending = [t for t in tickers if t not in fresh]

    have = {t for t in pending if (PRICE_DIR / f"{t}.csv").exists()}
    todo_full = pending if args.rebuild else [t for t in pending if t not in have]
    todo_incr = [] if args.rebuild else [t for t in pending if t in have]
    log(f"共 {len(tickers)} 隻：全量 {len(todo_full)}，增量 {len(todo_incr)}")

    results = {}
    try:
        if todo_full:
            run_full(todo_full, results)
        if todo_incr:
            run_incremental(todo_incr, results)
    except KeyboardInterrupt:
        log("手動中斷。已完成的股票已存檔，重新執行即可接續。")
    finally:
        keep_awake(False)
        if results:
            log_df = pd.DataFrame(
                [(t, *v) for t, v in results.items()],
                columns=["ticker", "mode", "rows", "status"],
            )
            log_df["run_time"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            log_df.to_csv(LOG_FILE, index=False)
            log("狀態統計：")
            print(log_df["status"].value_counts().to_string())
            bad = log_df[log_df["status"] == "no_data"]
            if len(bad):
                log(f"{len(bad)} 隻下載不到資料（可能下市或 Yahoo 無資料），見 {LOG_FILE}")


if __name__ == "__main__":
    main()
