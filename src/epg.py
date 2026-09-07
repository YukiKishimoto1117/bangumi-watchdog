"""EPGの読み込みと期待キューの生成。

EPGファイル bangumi_{date}.json は「その日の04:30頃 〜 翌日05:20頃」を収録している。
そのため終了時刻が date に属する番組は date と date-1 の2ファイルに分散する。
両方を読んでマージする。
"""
import json
import re
import datetime as dt
from dataclasses import dataclass

import config as C


@dataclass(frozen=True)
class Program:
    ch: str
    channel_name: str
    start: dt.datetime
    end: dt.datetime
    se_id: str
    title: str

    @property
    def duration_sec(self) -> float:
        return (self.end - self.start).total_seconds()

    @property
    def video_key(self) -> str:
        """movie/{ch}/{終了日}/{CH}_{開始日}_{HHMM}00.mp4"""
        return (
            f"{C.MOVIE_PREFIX}/{self.ch}/{self.end:%Y%m%d}/"
            f"{self.ch.upper()}_{self.start:%Y%m%d_%H%M}00.mp4"
        )

    @property
    def result_key(self) -> str:
        """results/{ch}/{終了日}/{CH}_{開始日}_{HHMM}00_corners.csv"""
        return (
            f"{C.RESULT_PREFIX}/{self.ch}/{self.end:%Y%m%d}/"
            f"{self.ch.upper()}_{self.start:%Y%m%d_%H%M}00_corners.csv"
        )

    @property
    def label(self) -> str:
        return f"{self.start:%H:%M}-{self.end:%H:%M} {self.title}"


def _parse_ts(s: str) -> dt.datetime:
    """'202608310530' -> JSTのdatetime"""
    return dt.datetime.strptime(s, "%Y%m%d%H%M").replace(tzinfo=C.JST)


def epg_key(date: dt.date) -> str:
    return f"{C.EPG_PREFIX}/bangumi_{date:%Y%m%d}.json"


def load_epg(s3, date: dt.date) -> dict | None:
    """1日分のEPGを読む。無ければ None。"""
    key = epg_key(date)
    try:
        body = s3.get_object(Bucket=C.BUCKET, Key=key)["Body"].read()
    except s3.exceptions.NoSuchKey:
        return None
    except Exception as e:  # ClientError(404)含む
        if "NoSuchKey" in str(e) or "404" in str(e):
            return None
        raise
    return json.loads(body)


def programs_from(doc: dict) -> list[Program]:
    """EPGのJSONから、監視対象チャンネル分のProgramを取り出す。"""
    id_to_ch = {
        C.CHANNEL_MAP[ch]["channel_id"]: ch
        for ch in C.CHANNELS
        if ch in C.CHANNEL_MAP
    }
    out = []
    for p in doc.get("programs", []):
        ch = id_to_ch.get(p.get("ChannelId"))
        if ch is None:
            continue
        try:
            start = _parse_ts(p["StartTime"])
            end = _parse_ts(p["EndTime"])
        except (KeyError, ValueError):
            continue
        if end <= start:
            continue
        out.append(
            Program(
                ch=ch,
                channel_name=C.CHANNEL_MAP[ch]["name"],
                start=start,
                end=end,
                se_id=p.get("SeId", ""),
                title=p.get("ProgramTitle", ""),
            )
        )
    return out


def collect_programs(s3, dates: list[dt.date]) -> tuple[list[Program], list[dt.date]]:
    """複数日のEPGをマージ。(番組リスト, 読めなかった日) を返す。

    (ch, start) で重複排除する。EPGは1日7回上書きされるため、
    起動のたびに読み直すことで編成変更に自動追従する。
    """
    seen: dict[tuple[str, dt.datetime], Program] = {}
    missing: list[dt.date] = []
    for d in dates:
        doc = load_epg(s3, d)
        if doc is None:
            missing.append(d)
            continue
        for prog in programs_from(doc):
            seen[(prog.ch, prog.start)] = prog
    return sorted(seen.values(), key=lambda p: (p.ch, p.start)), missing


def check_continuity(programs: list[Program]) -> list[str]:
    """局ごとに EndTime == 次のStartTime かを確認。ズレを文字列で返す。"""
    issues = []
    by_ch: dict[str, list[Program]] = {}
    for p in programs:
        by_ch.setdefault(p.ch, []).append(p)

    for ch, plist in sorted(by_ch.items()):
        plist = sorted(plist, key=lambda x: x.start)
        name = C.CHANNEL_MAP.get(ch, {}).get("name", ch)
        for prev, nxt in zip(plist, plist[1:]):
            diff = (nxt.start - prev.end).total_seconds()
            if abs(diff) <= C.EPG_GAP_TOLERANCE_SEC:
                continue
            kind = "重複" if diff < 0 else "欠落"
            issues.append(
                f"{ch}({name}) 時間軸{kind} {abs(diff) / 60:.0f}分: "
                f"{prev.end:%m/%d %H:%M} -> {nxt.start:%m/%d %H:%M} "
                f"（{prev.title[:20]} / {nxt.title[:20]}）"
            )
    return issues


def _window_bounds(day: dt.date, hhmm_from: str, hhmm_to: str):
    def at(hhmm: str) -> dt.datetime:
        h, m = hhmm.split(":")
        return dt.datetime.combine(day, dt.time(int(h), int(m)), tzinfo=C.JST)

    return at(hhmm_from), at(hhmm_to)


def is_analysis_target(prog: Program) -> bool:
    """番組が分析対象時間帯に一部でも重なるか。"""
    for hhmm_from, hhmm_to in C.ANALYSIS_WINDOWS:
        w_start, w_end = _window_bounds(prog.start.date(), hhmm_from, hhmm_to)
        if prog.start < w_end and prog.end > w_start:
            return True
    return False


_VIDEO_KEY_RE = re.compile(r"^[^/]+/[^/]+/\d{8}/[A-Za-z0-9]+_(\d{8})_(\d{4})00\.mp4$")


def build_recording_spans(inventory: dict[str, int], ch: str) -> list[tuple[dt.datetime, dt.datetime]]:
    """収録システムはEPGの番組境界でファイルを切ろうとするが、実データでは
    たまに区切りに失敗し、複数番組が1本のファイルに連結される
    （例: 03:17開始の番組のファイルが、後続の3番組分まで飲み込んで1本になっていた）。

    ファイル名に埋め込まれた開始時刻と、サイズから逆算した長さで、
    その局が実際にカバーしている時間帯（録画区間）の一覧を作る。
    専用ファイルが見つからない番組が、連結録画に含まれていないか確認するために使う。
    """
    needle = f"/{ch}/"
    spans = []
    for key, size in inventory.items():
        if needle not in key:
            continue
        m = _VIDEO_KEY_RE.match(key)
        if not m:
            continue
        start = dt.datetime.strptime(m.group(1) + m.group(2), "%Y%m%d%H%M").replace(tzinfo=C.JST)
        end = start + dt.timedelta(seconds=size / C.VIDEO_BYTES_PER_SEC)
        spans.append((start, end))
    spans.sort()
    return spans


def covered_by(spans: list[tuple[dt.datetime, dt.datetime]], moment: dt.datetime) -> bool:
    """momentがどれかの録画区間に含まれるか（開始時刻でソート済みのspans前提）。"""
    for start, end in spans:
        if start > moment:
            break
        if start <= moment < end:
            return True
    return False
