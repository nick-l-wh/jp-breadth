"""建立日本股票池（東證內國普通股）。可單獨執行，也會被 download_prices.py 自動呼叫。

輸入：JPX 上市公司一覽（英文版 data_e.xlsx 或日文版 data_j.xls 皆可）
輸出：data/universe.csv
用法：python scripts/build_universe.py [檔案路徑]
"""
import sys
from pathlib import Path
import pandas as pd

OUT = Path("data/universe.csv")

COLS = {
    "code":    ["Local Code", "コード"],
    "name":    ["Name (English)", "銘柄名"],
    "section": ["Section/Products", "市場・商品区分"],
    "sector33": ["33 Sector(name)", "33業種区分"],
    "size":    ["Size (New Index Series)", "規模区分"],
}
SEGMENTS = {
    "Prime Market (Domestic)": "Prime", "プライム（内国株式）": "Prime",
    "Standard Market(Domestic)": "Standard", "スタンダード（内国株式）": "Standard",
    "Growth Market(Domestic)": "Growth", "グロース（内国株式）": "Growth",
}


def build(src):
    df = pd.read_excel(src, dtype=str)
    df.columns = [c.strip() for c in df.columns]

    def pick(key, required=True):
        for c in COLS[key]:
            if c in df.columns:
                return df[c].str.strip()
        if required:
            raise ValueError(f"找不到欄位 {COLS[key]}，實際欄位：{list(df.columns)}")
        return pd.Series("-", index=df.index)

    d = pd.DataFrame({
        "code": pick("code").str.upper(),
        "name": pick("name"),
        "segment": pick("section").map(SEGMENTS),
        "sector33": pick("sector33"),
        "topix_size": pick("size", required=False),
    })
    d = d[d["segment"].notna()]
    d = d[d["code"].str.fullmatch(r"\d{3}[0-9A-Z]")]
    d["ticker"] = d["code"] + ".T"
    d["is_prime"] = d["segment"].eq("Prime")
    d = d.drop_duplicates("ticker").sort_values("code")
    d = d[["ticker", "code", "name", "segment", "sector33", "topix_size", "is_prime"]]
    OUT.parent.mkdir(exist_ok=True)
    d.to_csv(OUT, index=False, encoding="utf-8-sig")
    print(f"內國普通股合計 {len(d)} 隻")
    print(d["segment"].value_counts().to_string())
    return d


if __name__ == "__main__":
    build(sys.argv[1] if len(sys.argv) > 1 else "data/data_e.xlsx")
