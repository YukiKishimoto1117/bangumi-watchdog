"""AWSをスタブ化してhandlerを通し実行する。動画は「全部未達」を再現。

リポジトリ相対で動く。AWSには接続しない。
    python tests/dryrun.py
"""
import os
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(_HERE)
FIXTURES = os.path.join(_ROOT, "tests", "fixtures")
sys.path.insert(0, os.path.join(_ROOT, "src"))
import json, datetime as dt

import config as C
C.CHANNELS = ["ch1", "ch4", "ch6"]
C.SNS_TOPIC_ARN = ""  # 未設定 -> 標準出力に印字

import aws_util, epg, handler

DOC = json.load(open(f"{FIXTURES}/bangumi_20260831.json", encoding="utf-8"))
CSV = open(f"{FIXTURES}/CH6_20260828_060000_corners.csv", encoding="utf-8-sig").read()

INVENTORY = {}          # 空 = 動画も分析結果も1件も無い
FOUND_KEYS = set()


class FakeS3:
    class exceptions:
        class NoSuchKey(Exception):
            pass

    def get_object(self, Bucket, Key):
        if Key == f"{C.EPG_PREFIX}/bangumi_20260831.json":
            body = json.dumps(DOC).encode()
        elif Key.endswith("_corners.csv"):
            body = CSV.encode()
        else:
            raise FakeS3.exceptions.NoSuchKey()
        return {"Body": type("B", (), {"read": lambda s: body})()}


aws_util._s3 = FakeS3()
aws_util.list_keys = lambda prefix: {k: v for k, v in INVENTORY.items() if k.startswith(prefix)}
aws_util.already_notified = lambda *a, **k: False
aws_util.mark_notified = lambda *a, **k: None
handler.list_keys = aws_util.list_keys
handler.already_notified = aws_util.already_notified
handler.mark_notified = aws_util.mark_notified
handler.build_inventory = lambda root, chs, dates: {
    k: v for k, v in INVENTORY.items() if k.startswith(root)
}
handler.get_text = lambda key: CSV
handler._now = lambda: dt.datetime(2026, 8, 31, 9, 0, tzinfo=C.JST)

print("=" * 70)
print("CASE 1: watch モード / 動画が1件も上がっていない")
print("=" * 70)
print(handler.lambda_handler({"mode": "watch"}, None))

print()
print("=" * 70)
print("CASE 2: daily モード / 分析結果は存在するが動画は未達")
print("=" * 70)
handler._now = lambda: dt.datetime(2026, 9, 1, 9, 0, tzinfo=C.JST)


def fake_collect(s3, dates):
    return epg.programs_from(DOC), []


epg.collect_programs = fake_collect
handler.epg.collect_programs = fake_collect
INVENTORY.update({
    p.result_key: 32251
    for p in epg.programs_from(DOC)
    if epg.is_analysis_target(p) and p.start.date() == dt.date(2026, 8, 31)
})
handler.lambda_handler({"mode": "daily"}, None)
