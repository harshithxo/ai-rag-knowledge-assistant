"""Private PDF storage backends. Local storage is the development default; S3 is optional."""
from pathlib import Path, PurePosixPath
import os
import tempfile

from app.config import settings


class LocalPDFStorage:
    def _path(self, key: str) -> Path:
        root = Path(settings.upload_dir).resolve()
        path = (root / Path(*PurePosixPath(key).parts)).resolve()
        if root not in path.parents:
            raise ValueError("Invalid PDF storage key")
        return path

    def put(self, key: str, content: bytes) -> None:
        path = self._path(key)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(content)

    def exists(self, key: str) -> bool:
        path = self._path(key)
        if path.is_file():
            return True
        return PurePosixPath(key).parts[0] == "legacy" and (Path(settings.upload_dir).resolve() / PurePosixPath(key).name).is_file()

    def put_if_absent(self, key: str, content: bytes) -> bool:
        """Atomically create a PDF without replacing an existing path; return True if created."""
        path = self._path(key)
        path.parent.mkdir(parents=True, exist_ok=True)
        fd, temporary = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
        try:
            with os.fdopen(fd, "wb") as stream:
                stream.write(content)
                stream.flush()
                os.fsync(stream.fileno())
            try:
                os.link(temporary, path)
                return True
            except FileExistsError:
                return False
        finally:
            Path(temporary).unlink(missing_ok=True)

    def get(self, key: str) -> bytes:
        path = self._path(key)
        if not path.is_file() and PurePosixPath(key).parts[0] == "legacy":
            legacy = Path(settings.upload_dir).resolve() / PurePosixPath(key).name
            if legacy.is_file():
                path = legacy
        return path.read_bytes()

    def delete(self, key: str) -> None:
        path = self._path(key)
        if not path.exists() and PurePosixPath(key).parts[0] == "legacy":
            path = Path(settings.upload_dir).resolve() / PurePosixPath(key).name
        path.unlink(missing_ok=True)

    def path(self, key: str) -> Path:
        return self._path(key)


class S3PDFStorage:
    def __init__(self):
        import boto3
        # Supabase Storage's S3-compatible endpoint expects path-style addressing.
        # Do not send AWS ServerSideEncryption headers: Supabase manages encryption itself.
        from botocore.config import Config
        self.client = boto3.client(
            "s3", region_name=settings.s3_region or None,
            endpoint_url=settings.s3_endpoint_url or None,
            aws_access_key_id=settings.s3_access_key_id or None,
            aws_secret_access_key=settings.s3_secret_access_key or None,
            config=Config(s3={"addressing_style": "path"}),
        )

    def put(self, key: str, content: bytes) -> None:
        self.client.put_object(Bucket=settings.s3_bucket, Key=key, Body=content,
                               ContentType="application/pdf")

    def exists(self, key: str) -> bool:
        from botocore.exceptions import ClientError
        try:
            self.client.head_object(Bucket=settings.s3_bucket, Key=key)
            return True
        except ClientError as exc:
            code = str(exc.response.get("Error", {}).get("Code", ""))
            if code in {"404", "NoSuchKey", "NotFound"}:
                return False
            raise

    def put_if_absent(self, key: str, content: bytes) -> bool:
        """Use S3 conditional creation; unsupported providers fail closed rather than overwrite."""
        from botocore.exceptions import ClientError
        try:
            self.client.put_object(Bucket=settings.s3_bucket, Key=key, Body=content,
                                   ContentType="application/pdf", IfNoneMatch="*")
            return True
        except ClientError as exc:
            code = str(exc.response.get("Error", {}).get("Code", ""))
            status = exc.response.get("ResponseMetadata", {}).get("HTTPStatusCode")
            if code in {"PreconditionFailed", "ConditionalRequestConflict"} or status in {409, 412}:
                return False
            raise

    def get(self, key: str) -> bytes:
        return self.client.get_object(Bucket=settings.s3_bucket, Key=key)["Body"].read()

    def delete(self, key: str) -> None:
        self.client.delete_object(Bucket=settings.s3_bucket, Key=key)


def get_pdf_storage():
    if settings.upload_storage.lower() == "s3":
        return S3PDFStorage()
    if settings.upload_storage.lower() != "local":
        raise RuntimeError("UPLOAD_STORAGE must be local or s3")
    return LocalPDFStorage()


def document_key(user_id: str | None, stored_name: str) -> str:
    if not user_id and settings.upload_storage.lower() == "local":
        return stored_name
    return f"{user_id or 'legacy'}/{stored_name}"
