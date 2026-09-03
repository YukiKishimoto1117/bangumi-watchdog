"""実データ（EPG / corners.csv）でのEPG解析・CSV検査の確認。

リポジトリ相対で動く。AWSには接続しない。
    python tests/local_test.py
"""
import os
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(_HERE)
FIXTURES = os.path.join(_ROOT, "tests", "fixtures")
sys.path.insert(0, os.path.join(_ROOT, "src"))
import datetime as dt
import json

import config as C

C.CHANNELS = ["ch1", "ch4", "ch6"]

import epg
import csv_check


doc = json.load(open(f"{FIXTURES}/bangumi_20260831.json"))
progs = epg.programs_from(doc)
print(f"監視対象3局の番組数: {len(progs)}")

by_ch = {}
for p in progs:
    by_ch.setdefault(p.ch, []).append(p)
for ch, pl in sorted(by_ch.items()):
    print(f"  {ch} {C.CHANNEL_MAP[ch]['name']}: {len(pl)}件")

print("\n--- 時間軸の連続性 ---")
issues = epg.check_continuity(progs)
print("OK" if not issues else "\n".join(issues))

print("\n--- キー生成（メ～テレ 朝〜深夜）---")
for p in sorted(by_ch["ch6"], key=lambda x: x.start):
    if p.start.hour in (6, 8) or p.start.date() != p.end.date() or p.start.hour == 0:
        tgt = "★分析対象" if epg.is_analysis_target(p) else ""
        print(f"  {p.start:%m/%d %H:%M}-{p.end:%m/%d %H:%M} {tgt}")
        print(f"    {p.video_key}")

print("\n--- 分析対象と判定された番組 ---")
for ch, pl in sorted(by_ch.items()):
    tg = [p for p in sorted(pl, key=lambda x: x.start) if epg.is_analysis_target(p)]
    print(f"  {ch}: {len(tg)}件")
    for p in tg:
        print(f"      {p.start:%H:%M}-{p.end:%H:%M} {p.title[:32]}")

print("\n--- ⑤ CSV検査（正常データ）---")
text = open(f"{FIXTURES}/CH6_20260828_060000_corners.csv", encoding="utf-8-sig").read()


class FakeProg:
    ch = "ch6"
    start = dt.datetime(2026, 8, 28, 6, 0, tzinfo=C.JST)
    end = dt.datetime(2026, 8, 28, 8, 0, tzinfo=C.JST)
    duration_sec = 7200.0


print("検出された問題:", csv_check.validate(text, FakeProg) or "なし（正常）")

print("\n--- ⑤ CSV検査（壊れたデータを注入）---")
lines = text.splitlines()
broken = "\n".join(lines[:40])  # 途中で切れたCSV
print("途中切れ:", csv_check.validate(broken, FakeProg))

rows = lines[0:1] + [l.replace("CH6_20260828_060000.mp4", "CH6_20260828_080000.mp4") for l in lines[1:]]
print("別番組混入:", csv_check.validate("\n".join(rows), FakeProg)[:2])
