"""設定値。環境変数で上書き可能。"""
import os
from zoneinfo import ZoneInfo

JST = ZoneInfo("Asia/Tokyo")

BUCKET = os.environ.get("BUCKET", "bangumi-info")
EPG_PREFIX = os.environ.get("EPG_PREFIX", "epg-all")
MOVIE_PREFIX = os.environ.get("MOVIE_PREFIX", "movie")
RESULT_PREFIX = os.environ.get("RESULT_PREFIX", "results")

SNS_TOPIC_ARN = os.environ.get("SNS_TOPIC_ARN", "")
STATE_TABLE = os.environ.get("STATE_TABLE", "pipeline_status")

# 収録・分析の対象チャンネル。5局化するときはここに "ch5","ch10" を足すだけ。
CHANNELS = [c.strip() for c in os.environ.get("CHANNELS", "ch1,ch4,ch6").split(",") if c.strip()]

# ch番号 -> EPGの ChannelId / 表示名
CHANNEL_MAP = {
    "ch1":  {"channel_id": "0x0C10", "name": "東海テレビ"},
    "ch4":  {"channel_id": "0x0C28", "name": "中京テレビ"},
    "ch5":  {"channel_id": "0x0C18", "name": "CBC"},
    "ch6":  {"channel_id": "0x0C20", "name": "メ～テレ"},
    "ch10": {"channel_id": "0x8430", "name": "テレビ愛知"},
}

# 動画の到達判定：番組終了から何分後に「未達」とみなすか
VIDEO_GRACE_MINUTES = int(os.environ.get("VIDEO_GRACE_MINUTES", "30"))

# 未達を遡って探す範囲（時間）。これより古いものは日次サマリで扱い、再通知しない。
LOOKBACK_HOURS = int(os.environ.get("LOOKBACK_HOURS", "36"))

# EPGの時間軸のズレをどこから問題とみなすか（秒）。
# 実データで1分・5分程度のフィラー由来のズレが常時存在するため、
# 短いものは無視しないと毎日誤検知する。
EPG_GAP_TOLERANCE_SEC = int(os.environ.get("EPG_GAP_TOLERANCE_SEC", "300"))

# AI分析の対象時間帯（JST, HH:MM)。番組が一部でも重なれば対象。
ANALYSIS_WINDOWS = [("05:30", "08:30"), ("15:30", "19:00")]

# ---- ③ 動画サイズの妥当性（ffmpeg不要）----
# 実測: H.264 1280x720 / 総ビットレート約944kbps
#       810MB ÷ 7194秒 = 約118,000 バイト/秒
# ほぼ固定ビットレートなのでサイズは尺に比例する。
VIDEO_BYTES_PER_SEC = int(os.environ.get("VIDEO_BYTES_PER_SEC", "118000"))
# 許容する乖離（±）。最初は広めに取り、数日運用してから絞る。
VIDEO_SIZE_TOLERANCE = float(os.environ.get("VIDEO_SIZE_TOLERANCE", "0.35"))
# 尺が短い番組は誤差が大きいので、この秒数未満はサイズ検査しない
VIDEO_SIZE_MIN_DURATION_SEC = 300

# ---- 分析結果CSVの検査閾値 ----
EXPECTED_CSV_COLUMNS = [
    "broadcast_date", "channel", "filename",
    "start_sec", "end_sec", "title", "summary", "tags", "segment",
]
KNOWN_SEGMENTS = {
    "opening", "weather", "news", "sports", "ent",
    "feature", "live", "cm", "sponsor", "other",
}
# 区間の連続性を許容する誤差（秒）
CONTIGUITY_TOLERANCE_SEC = 1.0
# 最終 end_sec が番組尺の何割以上をカバーしていれば正常とみなすか
MIN_COVERAGE_RATIO = 0.98
# 尺10分あたりの最低行数（2時間62行 ≒ 5.2行/10分 なので余裕を持って3）
MIN_ROWS_PER_10MIN = 3.0
# 同一タイトルが何回連続したら固着とみなすか（CMは除外）
MAX_SAME_TITLE_RUN = 3
# CM区間が総尺に占める比率の許容レンジ
CM_RATIO_RANGE = (0.02, 0.45)
