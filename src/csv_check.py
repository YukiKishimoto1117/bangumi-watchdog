"""⑤ 分析結果CSVの中身検査。

実データ（CH6_20260828_060000_corners.csv, 62行 / 0.000-7194.160秒）で
確認した構造を前提にしている。区間は隙間・重複なく連続する。
"""
import csv
import io

import config as C


def validate(text: str, prog, video_size: int | None = None) -> list[str]:
    """問題があればメッセージのリストを返す。空リスト = 正常。"""
    issues: list[str] = []

    try:
        rows = list(csv.DictReader(io.StringIO(text)))
    except Exception as e:
        return [f"CSVとして読めない: {e}"]

    if not rows:
        return ["行が0件"]

    # --- 列構成 ---
    cols = list(rows[0].keys())
    missing = [c for c in C.EXPECTED_CSV_COLUMNS if c not in cols]
    if missing:
        issues.append(f"列が不足: {', '.join(missing)}")
        return issues  # 以降の検査ができない

    # --- パスとの整合（別番組の結果を書き込む事故の検知）---
    expected_file = f"{prog.ch.upper()}_{prog.start:%Y%m%d_%H%M}00.mp4"
    bad_file = {r["filename"] for r in rows if r["filename"] != expected_file}
    if bad_file:
        issues.append(f"filename列が不一致: 期待={expected_file} 実際={sorted(bad_file)[:3]}")

    bad_ch = {r["channel"] for r in rows if r["channel"] != prog.ch}
    if bad_ch:
        issues.append(f"channel列が不一致: 期待={prog.ch} 実際={sorted(bad_ch)[:3]}")

    # --- 秒数のパース ---
    try:
        spans = [(float(r["start_sec"]), float(r["end_sec"])) for r in rows]
    except ValueError as e:
        issues.append(f"start_sec/end_secが数値でない行がある: {e}")
        return issues

    # --- 先頭が0 ---
    if abs(spans[0][0]) > C.CONTIGUITY_TOLERANCE_SEC:
        issues.append(f"先頭のstart_secが0でない: {spans[0][0]}")

    # --- 逆転 ---
    reversed_rows = [i for i, (s, e) in enumerate(spans) if e <= s]
    if reversed_rows:
        issues.append(f"end_sec <= start_sec の行: {len(reversed_rows)}件（行{reversed_rows[:3]}）")

    # --- 連続性 ---
    gaps = []
    for i in range(1, len(spans)):
        diff = spans[i][0] - spans[i - 1][1]
        if abs(diff) > C.CONTIGUITY_TOLERANCE_SEC:
            gaps.append((i, round(diff, 1)))
    if gaps:
        kind = "重複" if any(d < 0 for _, d in gaps) else "欠落"
        issues.append(f"区間の不連続({kind}) {len(gaps)}箇所: {gaps[:3]}")

    # --- カバレッジ（途中切れ / MAX_TOKENS の検知）---
    total = spans[-1][1]
    expect = prog.duration_sec
    if expect > 0:
        ratio = total / expect
        if ratio < C.MIN_COVERAGE_RATIO:
            issues.append(
                f"番組尺のカバー不足: {total:.0f}秒 / EPG {expect:.0f}秒 ({ratio:.1%})"
            )

    # --- 行数 ---
    min_rows = max(3, int(expect / 600 * C.MIN_ROWS_PER_10MIN))
    if len(rows) < min_rows:
        issues.append(f"行数が少なすぎる: {len(rows)}行（最低{min_rows}行想定）")

    # --- segment語彙 ---
    unknown = {r["segment"] for r in rows} - C.KNOWN_SEGMENTS
    if unknown:
        issues.append(f"未知のsegment値: {sorted(unknown)}")

    # --- 空文字 ---
    empty_title = sum(1 for r in rows if not r["title"].strip())
    empty_summary = sum(1 for r in rows if not r["summary"].strip())
    if empty_title:
        issues.append(f"titleが空の行: {empty_title}件")
    if empty_summary:
        issues.append(f"summaryが空の行: {empty_summary}件")

    # --- 同一タイトルの連続（固着・ハルシネーションの兆候）---
    run, run_title = 1, None
    worst = (1, None)
    for r in rows:
        t = r["title"].strip()
        if r["segment"] == "cm":
            run, run_title = 1, None
            continue
        if t == run_title:
            run += 1
        else:
            run, run_title = 1, t
        if run > worst[0]:
            worst = (run, t)
    if worst[0] >= C.MAX_SAME_TITLE_RUN:
        issues.append(f"同一titleが{worst[0]}回連続: 「{worst[1][:30]}」")

    # --- CM比率 ---
    cm_sec = sum(e - s for (s, e), r in zip(spans, rows) if r["segment"] == "cm")
    if total > 0:
        cm_ratio = cm_sec / total
        lo, hi = C.CM_RATIO_RANGE
        if not (lo <= cm_ratio <= hi):
            issues.append(f"CM比率が想定外: {cm_ratio:.1%}（想定 {lo:.0%}〜{hi:.0%}）")

    return issues
