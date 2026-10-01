"""每日一鍵更新：下載增量價格 → 計算廣度 → 提交並推送到 GitHub。

用法：  py scripts/update_daily.py            （建議東京時間 16:30 之後執行）
        py scripts/update_daily.py --no-push  （只更新資料，不推送）
        py scripts/update_daily.py --yes      （盤中也不詢問，直接執行）
"""
import argparse
import os
import subprocess
import sys
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(Path(__file__).resolve().parent))
from jpmarket import now_jst, _in_session  # noqa: E402


def run(cmd, check=True):
    print(f"\n$ {' '.join(cmd)}", flush=True)
    r = subprocess.run(cmd, cwd=ROOT)
    if check and r.returncode != 0:
        sys.exit(f"失敗（結束碼 {r.returncode}），已停止。")
    return r.returncode


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--no-push", action="store_true")
    ap.add_argument("--yes", action="store_true")
    args = ap.parse_args()
    os.chdir(ROOT)

    now = now_jst()
    if _in_session(now) and not args.yes:
        print(f"現在東京時間 {now:%H:%M}，東證尚未收盤資料穩定（建議 16:30 之後）。")
        print("盤中執行不會存入今天未完成的 K 線，但最新一日會是昨天。")
        if input("仍要繼續？(y/N) ").strip().lower() != "y":
            sys.exit("已取消。")

    py = sys.executable
    run([py, "scripts/download_prices.py"])
    run([py, "scripts/compute_breadth.py"])

    if args.no_push:
        print("\n已略過推送（--no-push）。")
        return
    if run(["git", "--version"], check=False) != 0:
        sys.exit("找不到 git，無法推送。資料已更新完成。")
    run(["git", "add", "index.html", "data/breadth.csv", "data/universe.csv"])
    if run(["git", "diff", "--cached", "--quiet"], check=False) != 0:
        run(["git", "commit", "-m", f"update {datetime.now():%Y-%m-%d %H:%M}"])
    else:
        print("\n資料沒有新的變更。")
    run(["git", "push", "-u", "origin", "HEAD"])
    print("\n完成，網頁約 1–2 分鐘後更新。")


if __name__ == "__main__":
    main()
