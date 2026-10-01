"""東京市場時間工具：避免把「盤中未收盤」的當日 K 線當成完整資料存下來或拿去計算。

東證交易時間 9:00–15:30（JST），Yahoo 收盤資料穩定約在 16:00 之後。
規則：平日 JST 09:00–15:59 之間下載的資料，當天那根 K 線視為不完整。
"""
from datetime import datetime, timedelta, timezone

import pandas as pd

JST = timezone(timedelta(hours=9))
SESSION_START_H = 9
SESSION_END_H = 16     # 16:00 JST 起視為收盤資料已完整


def now_jst():
    return datetime.now(JST)


def _in_session(dt):
    return dt.weekday() < 5 and SESSION_START_H <= dt.hour < SESSION_END_H


def drop_partial_today(df):
    """下載當下若在盤中，丟掉「今天」那一列（盤中價格不是收盤價）。"""
    now = now_jst()
    if _in_session(now):
        return df[df.index < pd.Timestamp(now.date())]
    return df


def is_tainted(mtime):
    """檔案是否在盤中寫入（可能含當日不完整 K 線；舊版腳本留下的檔案也適用）。"""
    return _in_session(datetime.fromtimestamp(mtime, JST))


def strip_tainted_bar(df, mtime):
    """若檔案在盤中寫入，移除檔案寫入當天那一列。"""
    if is_tainted(mtime):
        day = pd.Timestamp(datetime.fromtimestamp(mtime, JST).date())
        return df[df.index < day]
    return df
