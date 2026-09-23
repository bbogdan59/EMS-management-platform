import pytest

from app.services import firmware_storage


def test_sha256_hex_matches_known_value():
    assert firmware_storage.sha256_hex(b"") == "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855"


def test_local_dev_storage_put_and_get_download_url(tmp_path):
    storage = firmware_storage.LocalDevReleaseStorage(str(tmp_path))
    storage.put("releases/1.0.0/abc.tar.gz", b"artifact bytes")
    assert (tmp_path / "releases" / "1.0.0" / "abc.tar.gz").read_bytes() == b"artifact bytes"
    url = storage.generate_download_url("releases/1.0.0/abc.tar.gz", ttl_seconds=300)
    assert "releases/1.0.0/abc.tar.gz" in url


def test_local_dev_storage_delete(tmp_path):
    storage = firmware_storage.LocalDevReleaseStorage(str(tmp_path))
    storage.put("x.tar.gz", b"data")
    storage.delete("x.tar.gz")
    assert not (tmp_path / "x.tar.gz").exists()


def test_local_dev_storage_delete_missing_is_a_noop(tmp_path):
    storage = firmware_storage.LocalDevReleaseStorage(str(tmp_path))
    storage.delete("never-existed.tar.gz")  # must not raise


def test_local_dev_storage_rejects_path_traversal(tmp_path):
    storage = firmware_storage.LocalDevReleaseStorage(str(tmp_path))
    storage.put("../../escape.tar.gz", b"data")
    # The ".." is stripped, not followed -- the file lands inside base_dir.
    assert not (tmp_path.parent.parent / "escape.tar.gz").exists()
    assert (tmp_path / "escape.tar.gz").exists()


def test_s3_storage_requires_bucket():
    with pytest.raises(firmware_storage.ReleaseStorageError, match="FIRMWARE_S3_BUCKET"):
        firmware_storage.S3ReleaseStorage(bucket=None)


class _FakeS3Client:
    def __init__(self):
        self.put_calls = []
        self.presign_calls = []
        self.delete_calls = []

    def put_object(self, **kwargs):
        self.put_calls.append(kwargs)

    def generate_presigned_url(self, operation, Params, ExpiresIn):
        self.presign_calls.append((operation, Params, ExpiresIn))
        return f"https://fake-s3.example/{Params['Bucket']}/{Params['Key']}?ttl={ExpiresIn}"

    def delete_object(self, **kwargs):
        self.delete_calls.append(kwargs)


def test_s3_storage_put_generate_url_delete(monkeypatch):
    fake_client = _FakeS3Client()
    monkeypatch.setattr(
        "boto3.client", lambda service, **kwargs: fake_client if service == "s3" else None
    )
    storage = firmware_storage.S3ReleaseStorage(bucket="ems-firmware", region="eu-west-1")
    storage.put("releases/1.0.0/x.tar.gz", b"bytes", content_type="application/gzip")
    assert fake_client.put_calls[0]["Bucket"] == "ems-firmware"
    assert fake_client.put_calls[0]["Key"] == "releases/1.0.0/x.tar.gz"
    assert fake_client.put_calls[0]["Body"] == b"bytes"

    url = storage.generate_download_url("releases/1.0.0/x.tar.gz", ttl_seconds=300)
    assert "ttl=300" in url
    assert fake_client.presign_calls[0][2] == 300

    storage.delete("releases/1.0.0/x.tar.gz")
    assert fake_client.delete_calls[0]["Key"] == "releases/1.0.0/x.tar.gz"


def test_get_release_storage_selects_backend_from_settings(tmp_path):
    from types import SimpleNamespace

    settings = SimpleNamespace(firmware_storage_backend="local_dev_only", firmware_local_storage_dir=str(tmp_path))
    storage = firmware_storage.get_release_storage(settings)
    assert isinstance(storage, firmware_storage.LocalDevReleaseStorage)
