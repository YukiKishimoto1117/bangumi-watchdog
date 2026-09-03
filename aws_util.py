"""S3の一覧取得、DynamoDBによる通知抑止、SNS送信。"""
import datetime as dt
import re

import boto3

import config as C

_s3 = None
_sns = None
_ddb = None


def s3():
    global _s3
    if _s3 is None:
        _s3 = boto3.client("s3")
    return _s3


def sns():
    global _sns
    if _sns is None:
        _sns = boto3.client("sns")
    return _sns


def table():
    global _ddb
    if _ddb is None:
        _ddb = boto3.resource("dynamodb").Table(C.STATE_TABLE)
    return _ddb


# ---------- S3 ----------

def list_keys(prefix: str) -> dict[str, int]:
    """プレフィックス配下のキー -> サイズ。存在確認をHEADの連打ではなく
    LIST一発で済ませるため。"""
    out: dict[str, int] = {}
    paginator = s3().get_paginator("list_objects_v2")
    for page in paginator.paginate(Bucket=C.BUCKET, Prefix=prefix):
        for obj in page.get("Contents", []):
            out[obj["Key"]] = obj["Size"]
    return out


def build_inventory(prefix_root: str, channels: list[str], dates: list[dt.date]) -> dict[str, int]:
    """movie/ または results/ の該当日フォルダを一括で読み込む。"""
    inv: dict[str, int] = {}
    for ch in channels:
        for d in dates:
            inv.update(list_keys(f"{prefix_root}/{ch}/{d:%Y%m%d}/"))
    return inv


def get_text(key: str) -> str | None:
    try:
        return s3().get_object(Bucket=C.BUCKET, Key=key)["Body"].read().decode("utf-8-sig")
    except Exception:
        return None


# ---------- DynamoDB（通知の重複抑止のみ） ----------

def already_notified(date_str: str, ch: str, sk: str) -> bool:
    try:
        r = table().get_item(Key={"pk": f"{date_str}#{ch}", "sk": sk})
    except Exception:
        return False  # 状態が読めなくても通知は止めない
    return "Item" in r


def mark_notified(date_str: str, ch: str, sk: str, error_code: str = "") -> None:
    ttl = int((dt.datetime.now(tz=dt.timezone.utc) + dt.timedelta(days=14)).timestamp())
    try:
        table().put_item(
            Item={
                "pk": f"{date_str}#{ch}",
                "sk": sk,
                "notified_at": dt.datetime.now(tz=C.JST).isoformat(),
                "error_code": error_code,
                "ttl": ttl,
            }
        )
    except Exception:
        pass


# ---------- SNS ----------

_ASCII_SAFE = re.compile(r"[^\x20-\x7E]")


def publish(subject: str, body: str) -> None:
    """SNSのSubjectはASCIIかつ100文字未満という制約があるため、
    件名は英数字で組み立て、日本語は本文に入れる。"""
    if not C.SNS_TOPIC_ARN:
        print("[SNS未設定] " + subject + "\n" + body)
        return
    safe = _ASCII_SAFE.sub("?", subject)[:99]
    sns().publish(TopicArn=C.SNS_TOPIC_ARN, Subject=safe, Message=body)
    print(f"published: {safe}")
