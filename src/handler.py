"""番人Lambda。

起動モード:
  mode=watch  … 15分ごと。EPGチェック(①)と動画の到達チェック(②)。
  mode=daily  … 毎朝9:00。前日分の分析結果チェック(④⑤)と日次サマリ。

期待キューは保存せず、起動のたびにEPGを読み直して生成する。
EPGは1日7回上書きされるため、これで編成変更に自動追従する。
"""
import datetime as dt

import config as C
import epg
import csv_check
from aws_util import (
    s3, list_keys, build_inventory, get_text,
    already_notified, mark_notified, publish,
)


def _now() -> dt.datetime:
    return dt.datetime.now(tz=C.JST)


def _ch_label(ch: str) -> str:
    return f"{ch}({C.CHANNEL_MAP.get(ch, {}).get('name', '?')})"


# --------------------------------------------------------------------------
# ① EPG
# --------------------------------------------------------------------------
def check_epg(now: dt.datetime):
    """EPGの存在を確認し、(致命的な問題, 番組リスト) を返す。

    時間軸のズレは即時通知しない。実データでは1〜5分のフィラー由来のズレが
    常時存在し、P1にすると毎日誤検知するため、日次サマリでのみ扱う。
    """
    problems: list[str] = []
    today = now.date()
    # 終了時刻が今日/昨日に属する番組を漏れなく拾うため複数日読む
    dates = [today - dt.timedelta(days=2), today - dt.timedelta(days=1), today]
    programs, missing = epg.collect_programs(s3(), dates)

    # 当日ファイルが無いのは致命的（04:00の生成バッチ後に判定）
    if today in missing and now.hour >= 5:
        problems.append(f"当日のEPGが存在しない: s3://{C.BUCKET}/{epg.epg_key(today)}")

    present_ch = {p.ch for p in programs}
    for ch in C.CHANNELS:
        if ch not in present_ch:
            problems.append(f"EPGに{_ch_label(ch)}の番組が1件も無い")

    return problems, programs


# --------------------------------------------------------------------------
# ② 動画の到達
# --------------------------------------------------------------------------
def check_videos(now: dt.datetime, programs: list[epg.Program]):
    """締切を過ぎた番組のうち、動画が無いものを返す。"""
    deadline = now - dt.timedelta(minutes=C.VIDEO_GRACE_MINUTES)
    horizon = now - dt.timedelta(hours=C.LOOKBACK_HOURS)

    due = [p for p in programs if horizon <= p.end <= deadline]
    if not due:
        return [], []

    dates = sorted({p.end.date() for p in due})
    inv = build_inventory(C.MOVIE_PREFIX, C.CHANNELS, dates)

    missing, zero_size, size_odd = [], [], []
    for p in due:
        size = inv.get(p.video_key)
        if size is None:
            missing.append(p)
            continue
        if size == 0:
            zero_size.append(p)
            continue
        note = size_anomaly(p, size)
        if note:
            size_odd.append((p, note))
    return missing, zero_size, size_odd


def size_anomaly(p: epg.Program, size: int) -> str | None:
    """③の一部。ほぼ固定ビットレートなので、サイズと尺の比で
    途中切れ・異常な短さを ffmpeg なしで検知する。"""
    if p.duration_sec < C.VIDEO_SIZE_MIN_DURATION_SEC:
        return None
    expect = p.duration_sec * C.VIDEO_BYTES_PER_SEC
    if expect <= 0:
        return None
    ratio = size / expect
    lo, hi = 1 - C.VIDEO_SIZE_TOLERANCE, 1 + C.VIDEO_SIZE_TOLERANCE
    if lo <= ratio <= hi:
        return None
    kind = "小さすぎる（途中切れ・映像喪失の疑い）" if ratio < lo else "大きすぎる"
    return (
        f"サイズが{kind}: {size / 1024 / 1024:.0f}MB "
        f"（尺{p.duration_sec / 60:.0f}分の想定 {expect / 1024 / 1024:.0f}MB / {ratio:.0%}）"
    )


def notify_missing_videos(now: dt.datetime, missing, zero_size, epg_problems):
    """局単位でロールアップして通知。1回の障害で1通に集約する。"""
    sent = []

    if epg_problems:
        key = f"epg#{now:%Y%m%d%H}"
        if not already_notified(f"{now:%Y%m%d}", "_system", key):
            body = (
                f"【P1】EPGに異常があります（{now:%Y-%m-%d %H:%M} JST）\n\n"
                + "\n".join(f"  - {x}" for x in epg_problems)
                + "\n\nEPGは監視の期待値マスタです。ここが壊れると"
                "\n未達の検知そのものが機能しなくなります。\n"
                f"確認先: s3://{C.BUCKET}/{C.EPG_PREFIX}/\n"
            )
            publish(f"[P1] EPG error ({now:%Y-%m-%d %H:%M} JST)", body)
            mark_notified(f"{now:%Y%m%d}", "_system", key, "epg")
            sent.append("epg")

    by_ch: dict[str, list] = {}
    for p in missing:
        by_ch.setdefault(p.ch, []).append(p)
    for p in zero_size:
        by_ch.setdefault(p.ch, []).append(p)

    # 新規（未通知）のものだけを対象にする
    fresh: dict[str, list] = {}
    for ch, plist in by_ch.items():
        new = []
        for p in plist:
            sk = f"{p.start:%H%M}#video"
            if not already_notified(f"{p.end:%Y%m%d}", ch, sk):
                new.append(p)
        if new:
            fresh[ch] = sorted(new, key=lambda x: x.start)

    if not fresh:
        return sent

    multi = len(fresh) >= max(2, len(C.CHANNELS) - 1)
    level = "P1" if multi else "P2"

    lines = []
    for ch, plist in sorted(fresh.items()):
        lines.append(f"■ {_ch_label(ch)} … {len(plist)}件")
        for p in plist[:8]:
            lines.append(f"    {p.label[:60]}")
            lines.append(f"      s3://{C.BUCKET}/{p.video_key}")
        if len(plist) > 8:
            lines.append(f"    ... 他{len(plist) - 8}件")

    ok_ch = [c for c in C.CHANNELS if c not in fresh]
    if multi:
        judgement = (
            "複数局が同時に未達です。個別のチューナーではなく、\n"
            "収録PC本体・アップロード処理・回線の障害である可能性が高いです。"
        )
    else:
        judgement = (
            "1局のみの未達です。該当チャンネルのチューナーまたは\n"
            "その番組固有の問題である可能性が高いです。"
        )

    body = (
        f"【{level}】動画がS3に上がっていません（{now:%Y-%m-%d %H:%M} JST）\n\n"
        + "\n".join(lines)
        + f"\n\n正常だった局: {', '.join(_ch_label(c) for c in ok_ch) or 'なし'}\n\n"
        + f"判定: {judgement}\n\n"
        + "確認先:\n"
        + "  1. 収録PCのログ（収録が失敗したのか、アップロードが失敗したのか）\n"
        + "  2. 収録PCのディスク空き容量\n"
        + "  3. 収録プロセスの生存\n"
    )
    publish(f"[{level}] Video missing: {len(fresh)} ch ({now:%Y-%m-%d %H:%M} JST)", body)

    for ch, plist in fresh.items():
        for p in plist:
            mark_notified(f"{p.end:%Y%m%d}", ch, f"{p.start:%H%M}#video", "video_missing")
    sent.append("video")
    return sent


# --------------------------------------------------------------------------
# ④⑤ 分析結果
# --------------------------------------------------------------------------
def check_analysis(target_date: dt.date, programs: list[epg.Program]):
    """対象日の分析対象番組について、到達(④)と中身(⑤)を確認。"""
    targets = [
        p for p in programs
        if p.start.date() == target_date and epg.is_analysis_target(p)
    ]
    if not targets:
        return [], [], []

    dates = sorted({p.end.date() for p in targets})
    inv = build_inventory(C.RESULT_PREFIX, C.CHANNELS, dates)

    missing, invalid, ok = [], [], []
    for p in targets:
        if p.result_key not in inv:
            missing.append(p)
            continue
        text = get_text(p.result_key)
        if text is None:
            invalid.append((p, ["CSVを取得できない"]))
            continue
        issues = csv_check.validate(text, p)
        (invalid if issues else ok).append((p, issues) if issues else p)
    return missing, invalid, ok


# --------------------------------------------------------------------------
# 日次サマリ
# --------------------------------------------------------------------------
def daily_summary(now: dt.datetime):
    target_date = (now - dt.timedelta(days=1)).date()
    dates = [target_date - dt.timedelta(days=1), target_date, now.date()]
    programs, epg_missing = epg.collect_programs(s3(), dates)

    day_progs = [p for p in programs if p.start.date() == target_date]
    video_dates = sorted({p.end.date() for p in day_progs}) or [target_date]
    inv = build_inventory(C.MOVIE_PREFIX, C.CHANNELS, video_dates)

    lines = [
        f"視聴率分析ダッシュボード 日次サマリ",
        f"対象放送日: {target_date:%Y-%m-%d}",
        f"生成: {now:%Y-%m-%d %H:%M} JST",
        "",
        "── ① EPG ──",
    ]
    if epg_missing:
        lines.append(f"  ✗ 取得できなかった日: {[f'{d:%m/%d}' for d in epg_missing]}")
    else:
        lines.append(f"  ✓ 取得OK（対象3日分、番組 {len(programs)}件）")
    cont = epg.check_continuity(day_progs)
    lines.append("  ✓ 時間軸の連続性OK" if not cont else f"  ✗ 不連続 {len(cont)}件")
    for c in cont[:5]:
        lines.append(f"      {c}")

    lines += ["", "── ② 動画の到達 ──"]
    total_missing = []
    size_notes: list[str] = []
    for ch in C.CHANNELS:
        plist = [p for p in day_progs if p.ch == ch]
        miss = [p for p in plist if p.video_key not in inv]
        total_missing += miss
        mark = "✓" if not miss else "✗"
        lines.append(f"  {mark} {_ch_label(ch)}: {len(plist) - len(miss)}/{len(plist)}")
        for p in miss[:5]:
            lines.append(f"      未達 {p.label[:55]}")
        if len(miss) > 5:
            lines.append(f"      ... 他{len(miss) - 5}件")
        for p in plist:
            size = inv.get(p.video_key)
            if size is None:
                continue
            note = size_anomaly(p, size)
            if note:
                size_notes.append(f"  ! {_ch_label(ch)} {p.label[:45]}")
                size_notes.append(f"      {note}")

    if size_notes:
        lines += ["", "── ③ 動画サイズの妥当性 ──"] + size_notes

    lines += ["", "── ④⑤ AI分析結果 ──"]
    a_missing, a_invalid, a_ok = check_analysis(target_date, programs)
    n_target = len(a_missing) + len(a_invalid) + len(a_ok)
    lines.append(f"  対象 {n_target}件 / 正常 {len(a_ok)}件 / 未達 {len(a_missing)}件 / 内容異常 {len(a_invalid)}件")
    for p in a_missing:
        lines.append(f"  ✗ 未達 {_ch_label(p.ch)} {p.label[:50]}")
    for p, issues in a_invalid:
        lines.append(f"  ! 内容異常 {_ch_label(p.ch)} {p.label[:50]}")
        for i in issues[:4]:
            lines.append(f"      - {i}")

    has_problem = bool(epg_missing or cont or total_missing or a_missing or a_invalid)
    lines += [
        "",
        "──────────",
        "異常なし。" if not has_problem else "上記の異常を確認してください。",
        "",
        "※ このサマリは異常が無い日も毎日届きます。",
        "   届かない場合は監視Lambda自体が停止している可能性があります。",
    ]

    level = "WARN" if has_problem else "OK"
    publish(
        f"[{level}] Daily summary {target_date:%Y-%m-%d}",
        "\n".join(lines),
    )

    # 分析未達・内容異常はサマリとは別にP2で通知（重複抑止つき）
    if a_missing or a_invalid:
        key = f"analysis#{target_date:%Y%m%d}"
        if not already_notified(f"{target_date:%Y%m%d}", "_system", key):
            body = [f"【P2】AI分析結果に問題があります（放送日 {target_date:%Y-%m-%d}）", ""]
            for p in a_missing:
                body.append(f"■ 未達 {_ch_label(p.ch)} {p.label}")
                body.append(f"    s3://{C.BUCKET}/{p.result_key}")
            for p, issues in a_invalid:
                body.append(f"■ 内容異常 {_ch_label(p.ch)} {p.label}")
                for i in issues:
                    body.append(f"    - {i}")
            body += [
                "",
                "動画はS3に残っているため、分析の再実行が可能です。",
                "Gemini側の finishReason（SAFETY / MAX_TOKENS）と",
                "429（レート制限）のログを確認してください。",
            ]
            publish(f"[P2] Analysis issue {target_date:%Y-%m-%d}", "\n".join(body))
            mark_notified(f"{target_date:%Y%m%d}", "_system", key, "analysis")


# --------------------------------------------------------------------------
def lambda_handler(event, context):
    now = _now()
    mode = (event or {}).get("mode", "watch")
    print(f"mode={mode} now={now:%Y-%m-%d %H:%M} JST channels={C.CHANNELS}")

    if mode == "daily":
        daily_summary(now)
        return {"mode": "daily", "ok": True}

    epg_problems, programs = check_epg(now)
    missing, zero_size, size_odd = check_videos(now, programs)
    sent = notify_missing_videos(now, missing, zero_size, epg_problems)
    for p, note in size_odd:
        print(f"[P3] size anomaly {p.ch} {p.label[:40]} :: {note}")

    result = {
        "mode": "watch",
        "programs": len(programs),
        "epg_problems": len(epg_problems),
        "video_missing": len(missing),
        "video_zero_size": len(zero_size),
        "video_size_odd": len(size_odd),
        "notified": sent,
    }
    print(result)
    return result
