"""Object storage abstraction: local directory for development, S3 on AWS."""

from __future__ import annotations

import hashlib
from pathlib import Path
from typing import Protocol

from resilience_map.core.config import Settings, get_settings


class ObjectStore(Protocol):
    def put(self, key: str, data: bytes, content_type: str = "application/octet-stream") -> str:
        """Store immutable bytes; returns the sha256 hex digest."""

    def get(self, key: str) -> bytes: ...

    def exists(self, key: str) -> bool: ...


class LocalObjectStore:
    def __init__(self, root: str) -> None:
        self._root = Path(root).resolve()

    def _path(self, key: str) -> Path:
        path = (self._root / key).resolve()
        if self._root not in path.parents:
            raise ValueError("key escapes object-store root")
        return path

    def put(self, key: str, data: bytes, content_type: str = "application/octet-stream") -> str:
        path = self._path(key)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
        return hashlib.sha256(data).hexdigest()

    def get(self, key: str) -> bytes:
        return self._path(key).read_bytes()

    def exists(self, key: str) -> bool:
        return self._path(key).exists()


class S3ObjectStore:
    def __init__(self, bucket: str, region: str) -> None:
        import boto3

        self._bucket = bucket
        self._client = boto3.client("s3", region_name=region)

    def put(self, key: str, data: bytes, content_type: str = "application/octet-stream") -> str:
        digest = hashlib.sha256(data).hexdigest()
        self._client.put_object(
            Bucket=self._bucket,
            Key=key,
            Body=data,
            ContentType=content_type,
            ServerSideEncryption="AES256",
            Metadata={"sha256": digest},
        )
        return digest

    def get(self, key: str) -> bytes:
        return bytes(self._client.get_object(Bucket=self._bucket, Key=key)["Body"].read())

    def exists(self, key: str) -> bool:
        from botocore.exceptions import ClientError

        try:
            self._client.head_object(Bucket=self._bucket, Key=key)
        except ClientError:
            return False
        return True


def get_object_store(settings: Settings | None = None) -> ObjectStore:
    s = settings or get_settings()
    if s.object_store_backend == "s3":
        return S3ObjectStore(s.s3_bucket, s.aws_region)
    return LocalObjectStore(s.object_store_path)
