#!/usr/bin/env python3
"""AWS Secrets Manager에서 비밀값을 가져와 .env 파일을 생성합니다.

Usage:
    python3 scripts/secrets.py [--secret moppu/prod] [--region ap-northeast-2] [--out .env]
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path


def fetch_and_write(secret_name: str, region: str, out_path: str) -> None:
    try:
        import boto3
    except ImportError:
        print("ERROR: boto3가 없습니다. 'pip install boto3' 후 재시도하세요.", file=sys.stderr)
        sys.exit(1)

    client = boto3.client("secretsmanager", region_name=region)
    try:
        resp = client.get_secret_value(SecretId=secret_name)
    except client.exceptions.ResourceNotFoundException:
        print(f"ERROR: Secret '{secret_name}' 을 찾을 수 없습니다.", file=sys.stderr)
        sys.exit(1)
    except Exception as e:
        print(f"ERROR: Secrets Manager 조회 실패: {e}", file=sys.stderr)
        sys.exit(1)

    raw = resp.get("SecretString") or ""
    try:
        data: dict = json.loads(raw)
    except json.JSONDecodeError:
        print("ERROR: Secret 값이 JSON 형식이 아닙니다.", file=sys.stderr)
        sys.exit(1)

    lines = [
        "# Generated from AWS Secrets Manager — DO NOT COMMIT",
        f"# Source: {secret_name} ({region})",
        "",
    ]
    for k, v in data.items():
        lines.append(f"{k}={v}")

    Path(out_path).write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(f"✓ {len(data)}개 항목을 {out_path} 에 저장했습니다.")


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--secret", default="moppu/prod")
    parser.add_argument("--region", default="ap-northeast-2")
    parser.add_argument("--out", default=".env")
    args = parser.parse_args()
    fetch_and_write(args.secret, args.region, args.out)
