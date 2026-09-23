"""Pluggable object storage for firmware artifacts (issue #168). Artifacts
are NEVER written to Railway's ephemeral filesystem in production -- see
`Settings.firmware_storage_backend` and its `model_post_init` guard.

- `S3ReleaseStorage`: the production backend, any S3-compatible provider
  (set `firmware_s3_endpoint_url` for non-AWS providers). NOT exercised
  against live infra in this sandbox -- no cloud credentials are available
  and outbound network access to an arbitrary endpoint isn't guaranteed
  here either. Covered by unit tests against a fake boto3-shaped client
  instead; documented honestly rather than worked around (see
  docs/LIMITATIONS.md).
- `LocalDevReleaseStorage`: local-disk fallback for development only,
  refused at startup in production, mirroring `email_backend="console"`.
"""
from __future__ import annotations

import hashlib
from abc import ABC, abstractmethod
from pathlib import Path


class ReleaseStorageError(Exception):
    pass


class ReleaseStorage(ABC):
    @abstractmethod
    def put(self, key: str, data: bytes, *, content_type: str = "application/octet-stream") -> None: ...

    @abstractmethod
    def generate_download_url(self, key: str, *, ttl_seconds: int) -> str: ...

    @abstractmethod
    def delete(self, key: str) -> None: ...


class S3ReleaseStorage(ReleaseStorage):
    def __init__(
        self, *, bucket: str | None, region: str | None = None, endpoint_url: str | None = None,
        access_key_id: str | None = None, secret_access_key: str | None = None,
    ):
        if not bucket:
            raise ReleaseStorageError("FIRMWARE_S3_BUCKET este necesar pentru backend-ul 's3'.")
        import boto3  # imported lazily: most tests never touch this backend

        self._bucket = bucket
        self._client = boto3.client(
            "s3", region_name=region, endpoint_url=endpoint_url,
            aws_access_key_id=access_key_id, aws_secret_access_key=secret_access_key,
        )

    def put(self, key: str, data: bytes, *, content_type: str = "application/octet-stream") -> None:
        self._client.put_object(Bucket=self._bucket, Key=key, Body=data, ContentType=content_type)

    def generate_download_url(self, key: str, *, ttl_seconds: int) -> str:
        # Time-limited presigned URL -- never a permanent/public link (issue
        # #168's "Download URL este allowlisted și cu durată limitată").
        return self._client.generate_presigned_url(
            "get_object", Params={"Bucket": self._bucket, "Key": key}, ExpiresIn=ttl_seconds,
        )

    def delete(self, key: str) -> None:
        self._client.delete_object(Bucket=self._bucket, Key=key)


class LocalDevReleaseStorage(ReleaseStorage):
    """Development-only fallback -- refused in production (Settings.
    model_post_init). Not durable/shared across processes, and its "URL" is
    just an opaque local marker, never dereferenced by a real device."""

    def __init__(self, base_dir: str):
        self._base = Path(base_dir)
        self._base.mkdir(parents=True, exist_ok=True, mode=0o700)

    def _path(self, key: str) -> Path:
        # `key` is always server-generated (see firmware_service.build_storage_key),
        # never taken directly from a request path -- this strip is defense in depth.
        safe = key.replace("..", "").lstrip("/")
        return self._base / safe

    def put(self, key: str, data: bytes, *, content_type: str = "application/octet-stream") -> None:
        path = self._path(key)
        path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        path.write_bytes(data)

    def generate_download_url(self, key: str, *, ttl_seconds: int) -> str:
        return f"local-dev-storage://{key}"

    def delete(self, key: str) -> None:
        self._path(key).unlink(missing_ok=True)


def get_release_storage(settings) -> ReleaseStorage:
    if settings.firmware_storage_backend == "s3":
        return S3ReleaseStorage(
            bucket=settings.firmware_s3_bucket, region=settings.firmware_s3_region,
            endpoint_url=settings.firmware_s3_endpoint_url,
            access_key_id=settings.firmware_s3_access_key_id,
            secret_access_key=settings.firmware_s3_secret_access_key,
        )
    return LocalDevReleaseStorage(settings.firmware_local_storage_dir)


def sha256_hex(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()
