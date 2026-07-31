from __future__ import annotations

import hashlib
import mimetypes
import shutil
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import BinaryIO, Protocol

from fastapi import HTTPException, UploadFile

from .models import (
    FileArtifactInfo,
    FileArtifactKind,
    ImageInfo,
    JobRecord,
    ReportLinks,
    ResultFileInfo,
    UploadedFileInfo,
)
from .settings import AppSettings


CSV_MAX_BYTES = 50 * 1024 * 1024
AGENT_BUNDLE_MAX_BYTES = 150 * 1024 * 1024


@dataclass(frozen=True)
class RunPaths:
    run_dir: Path
    input_dir: Path
    output_dir: Path


class StorageBackend(Protocol):
    def put_file(self, source: Path, key: str, *, content_type: str | None = None, metadata: dict[str, str] | None = None) -> None:
        ...

    def put_bytes(self, data: bytes, key: str, *, content_type: str | None = None, metadata: dict[str, str] | None = None) -> None:
        ...

    def open(self, key: str) -> BinaryIO:
        ...

    def exists(self, key: str) -> bool:
        ...

    def delete(self, key: str) -> None:
        ...

    def list_keys(self, prefix: str) -> list[str]:
        ...

    def head(self, key: str) -> dict[str, object] | None:
        ...


class LocalDiskBackend:
    def __init__(self, root: Path) -> None:
        self.root = root
        self.root.mkdir(parents=True, exist_ok=True)

    def put_file(self, source: Path, key: str, *, content_type: str | None = None, metadata: dict[str, str] | None = None) -> None:
        target = self._resolve(key)
        if source.resolve() == target.resolve():
            return
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, target)

    def put_bytes(self, data: bytes, key: str, *, content_type: str | None = None, metadata: dict[str, str] | None = None) -> None:
        target = self._resolve(key)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(data)

    def open(self, key: str) -> BinaryIO:
        return self._resolve(key).open("rb")

    def exists(self, key: str) -> bool:
        return self._resolve(key).exists()

    def delete(self, key: str) -> None:
        path = self._resolve(key)
        if path.exists():
            path.unlink()

    def list_keys(self, prefix: str) -> list[str]:
        root = self._resolve(prefix)
        if not root.exists():
            return []
        keys: list[str] = []
        if root.is_file():
            return [Path(prefix).as_posix()]
        for path in sorted(root.rglob("*")):
            if path.is_file():
                keys.append(path.relative_to(self.root).as_posix())
        return keys

    def head(self, key: str) -> dict[str, object] | None:
        path = self._resolve(key)
        if not path.exists():
            return None
        data = path.stat()
        return {
            "content_length": data.st_size,
            "content_type": mimetypes.guess_type(path.name)[0],
            "last_modified": datetime.fromtimestamp(data.st_mtime, timezone.utc),
        }

    def _resolve(self, key: str) -> Path:
        clean = key.strip().lstrip("/").replace("\x00", "")
        target = (self.root / clean).resolve()
        if not target.is_relative_to(self.root.resolve()):
            raise HTTPException(status_code=400, detail="Invalid storage key")
        return target


class S3Backend:
    def __init__(
        self,
        *,
        bucket: str,
        region: str,
        endpoint_url: str | None = None,
        access_key_id: str | None = None,
        secret_access_key: str | None = None,
    ) -> None:
        try:
            import boto3
        except ImportError as exc:  # pragma: no cover - dependency guard
            raise RuntimeError("Install boto3>=1.34 to use S3/MinIO storage") from exc

        self.bucket = bucket
        self.client = boto3.client(
            "s3",
            region_name=region,
            endpoint_url=endpoint_url,
            aws_access_key_id=access_key_id,
            aws_secret_access_key=secret_access_key,
        )

    def put_file(self, source: Path, key: str, *, content_type: str | None = None, metadata: dict[str, str] | None = None) -> None:
        extra_args = _upload_args(content_type=content_type, metadata=metadata)
        with source.open("rb") as handle:
            self.client.upload_fileobj(handle, self.bucket, key, ExtraArgs=extra_args or None)

    def put_bytes(self, data: bytes, key: str, *, content_type: str | None = None, metadata: dict[str, str] | None = None) -> None:
        extra_args = _upload_args(content_type=content_type, metadata=metadata)
        self.client.put_object(Bucket=self.bucket, Key=key, Body=data, **(extra_args or {}))

    def open(self, key: str) -> BinaryIO:
        response = self.client.get_object(Bucket=self.bucket, Key=key)
        return response["Body"]

    def exists(self, key: str) -> bool:
        try:
            self.client.head_object(Bucket=self.bucket, Key=key)
            return True
        except Exception:
            return False

    def delete(self, key: str) -> None:
        self.client.delete_object(Bucket=self.bucket, Key=key)

    def list_keys(self, prefix: str) -> list[str]:
        keys: list[str] = []
        token: str | None = None
        while True:
            kwargs = {"Bucket": self.bucket, "Prefix": prefix}
            if token:
                kwargs["ContinuationToken"] = token
            response = self.client.list_objects_v2(**kwargs)
            for item in response.get("Contents", []):
                keys.append(item["Key"])
            if not response.get("IsTruncated"):
                break
            token = response.get("NextContinuationToken")
            if not token:
                break
        return keys

    def head(self, key: str) -> dict[str, object] | None:
        try:
            response = self.client.head_object(Bucket=self.bucket, Key=key)
        except Exception:
            return None
        return {
            "content_length": response.get("ContentLength"),
            "content_type": response.get("ContentType"),
            "etag": response.get("ETag"),
            "last_modified": response.get("LastModified"),
            "metadata": response.get("Metadata", {}),
        }

    def presigned_get_url(self, key: str, expires_in: int) -> str:
        return self.client.generate_presigned_url(
            "get_object",
            Params={"Bucket": self.bucket, "Key": key},
            ExpiresIn=expires_in,
        )


class FileStorageService:
    def __init__(self, settings: AppSettings) -> None:
        self.settings = settings
        self.workspace_root = settings.runs_dir
        self.workspace_root.mkdir(parents=True, exist_ok=True)
        self.backend_name = settings.file_storage_backend
        self.storage_prefix = "" if self.backend_name == "local" else settings.file_storage_prefix.strip("/")
        if self.backend_name == "s3":
            self.backend: StorageBackend = S3Backend(
                bucket=settings.file_storage_bucket,
                region=settings.file_storage_region,
                endpoint_url=settings.file_storage_endpoint_url,
                access_key_id=settings.file_storage_access_key_id,
                secret_access_key=settings.file_storage_secret_access_key,
            )
        else:
            self.backend = LocalDiskBackend(self.workspace_root)
        self._job_store = None

    def attach_job_store(self, job_store) -> None:
        self._job_store = job_store

    def paths(self, job_id: str) -> RunPaths:
        run_dir = self._run_dir(job_id)
        return RunPaths(run_dir=run_dir, input_dir=run_dir / "inputs", output_dir=run_dir / "outputs")

    def run_dir(self, job_id: str) -> Path:
        return self.paths(job_id).run_dir

    def input_dir(self, job_id: str) -> Path:
        return self.paths(job_id).input_dir

    def output_dir(self, job_id: str) -> Path:
        return self.paths(job_id).output_dir

    def resolve_run_file(self, job_id: str, relative_path: str) -> Path:
        path = self._workspace_path(job_id, relative_path)
        if path.exists():
            return path
        artifact = self._artifact_for_path(job_id, relative_path)
        if artifact is not None:
            return self.ensure_local_copy(job_id, artifact)
        raise HTTPException(status_code=404, detail="File not found")

    def output_path(self, job_id: str, relative_path: str) -> Path:
        return self._workspace_path(job_id, f"outputs/{relative_path}" if not relative_path.startswith("outputs/") else relative_path)

    def input_path(self, job_id: str, relative_path: str) -> Path:
        return self._workspace_path(job_id, f"inputs/{relative_path}" if not relative_path.startswith("inputs/") else relative_path)

    def figure_spec_path(self, job_id: str, figure_id: str) -> Path:
        safe_id = _safe_figure_id(figure_id)
        return self._workspace_path(job_id, f"outputs/figures/{safe_id}.json")

    def prepare_run_dirs(self, job_id: str) -> RunPaths:
        paths = self.paths(job_id)
        paths.input_dir.mkdir(parents=True, exist_ok=True)
        paths.output_dir.mkdir(parents=True, exist_ok=True)
        return paths

    async def save_upload(self, job_id: str, field: str, upload: UploadFile, fallback_name: str) -> UploadedFileInfo:
        original_filename = _safe_filename(upload.filename, fallback_name)
        if Path(original_filename).suffix.lower() != ".csv":
            raise HTTPException(status_code=400, detail=f"{field} must be a CSV file")

        paths = self.paths(job_id)
        relative_path = f"inputs/{fallback_name}"
        workspace_path = paths.run_dir / relative_path
        workspace_path.parent.mkdir(parents=True, exist_ok=True)

        await upload.seek(0)
        hasher = hashlib.sha256()
        size = 0
        with workspace_path.open("wb") as handle:
            while chunk := await upload.read(1024 * 1024):
                size += len(chunk)
                hasher.update(chunk)
                handle.write(chunk)

        if size == 0:
            workspace_path.unlink(missing_ok=True)
            raise HTTPException(status_code=400, detail=f"{field} file is empty")
        if size > CSV_MAX_BYTES:
            workspace_path.unlink(missing_ok=True)
            raise HTTPException(status_code=413, detail=f"{field} exceeds 50 MB limit ({size / 1024 / 1024:.1f} MB)")

        checksum = hasher.hexdigest()
        content_type = upload.content_type or _guess_content_type(workspace_path.name)
        storage_key = self._storage_key(job_id, relative_path)
        self.backend.put_file(
            workspace_path,
            storage_key,
            content_type=content_type,
            metadata={
                "checksum": checksum,
                "created_at": datetime.now(timezone.utc).isoformat(),
                "kind": FileArtifactKind.INPUT.value,
                "field": field,
                "filename": original_filename,
                "path": relative_path,
            },
        )
        return UploadedFileInfo(
            kind=FileArtifactKind.INPUT,
            field=field,
            filename=original_filename,
            path=relative_path,
            storage_key=storage_key,
            checksum=checksum,
            content_type=content_type,
            size_bytes=size,
            created_at=datetime.now(timezone.utc),
        )

    async def save_staged_upload(self, bundle_id: str, field: str, upload: UploadFile) -> UploadedFileInfo:
        """保存审批前输入；调用方只能向用户返回脱敏后的文件 DTO。"""
        original_filename = _safe_filename(upload.filename, f"{field}.csv")
        if Path(original_filename).suffix.lower() != ".csv":
            raise HTTPException(status_code=400, detail=f"{field} must be a CSV file")

        relative_path = f"agent-inputs/{bundle_id}/{field}.csv"
        workspace_path = self.workspace_root / relative_path
        workspace_path.parent.mkdir(parents=True, exist_ok=True)
        await upload.seek(0)
        hasher = hashlib.sha256()
        size = 0
        too_large = False
        with workspace_path.open("wb") as handle:
            while chunk := await upload.read(1024 * 1024):
                size += len(chunk)
                if size > CSV_MAX_BYTES:
                    too_large = True
                    break
                hasher.update(chunk)
                handle.write(chunk)
        if too_large:
            workspace_path.unlink(missing_ok=True)
            raise HTTPException(status_code=413, detail=f"{field} exceeds 50 MB limit")
        if size == 0:
            workspace_path.unlink(missing_ok=True)
            raise HTTPException(status_code=400, detail=f"{field} file is empty")

        checksum = "sha256:" + hasher.hexdigest()
        content_type = upload.content_type or "text/csv"
        storage_key = relative_path
        if self.storage_prefix:
            storage_key = f"{self.storage_prefix}/{relative_path}"
        self.backend.put_file(
            workspace_path,
            storage_key,
            content_type=content_type,
            metadata={
                "checksum": checksum,
                "created_at": datetime.now(timezone.utc).isoformat(),
                "kind": FileArtifactKind.INPUT.value,
                "field": field,
                "filename": original_filename,
                "bundle_id": bundle_id,
            },
        )
        return UploadedFileInfo(
            kind=FileArtifactKind.INPUT,
            field=field,
            filename=original_filename,
            path=f"{field}.csv",
            storage_key=storage_key,
            checksum=checksum,
            content_type=content_type,
            size_bytes=size,
            created_at=datetime.now(timezone.utc),
        )

    def delete_staged_upload(self, storage_key: str) -> None:
        prefix = "agent-inputs/"
        if self.storage_prefix:
            prefix = f"{self.storage_prefix}/{prefix}"
        if not storage_key.startswith(prefix):
            raise ValueError("storage key is not an agent staged input")
        self.backend.delete(storage_key)

    def materialize_inputs(self, job: JobRecord) -> dict[str, Path]:
        return {item.field: self.ensure_local_copy(job.id, item) for item in job.inputs}

    def copy_input_artifact(self, source_job_id: str, target_job_id: str, source: UploadedFileInfo) -> UploadedFileInfo:
        target_relative_path = f"inputs/{Path(source.path).name or source.field + '.csv'}"
        source_path = self.ensure_local_copy(source_job_id, source)
        target_path = self._workspace_path(target_job_id, target_relative_path)
        target_path.parent.mkdir(parents=True, exist_ok=True)
        if source_path.resolve() != target_path.resolve():
            shutil.copy2(source_path, target_path)
        storage_key = self._storage_key(target_job_id, target_relative_path)
        self.backend.put_file(
            target_path,
            storage_key,
            content_type=source.content_type or _guess_content_type(target_path.name),
            metadata={
                "checksum": source.checksum or _file_checksum(target_path),
                "created_at": datetime.now(timezone.utc).isoformat(),
                "kind": FileArtifactKind.INPUT.value,
                "field": source.field,
                "filename": source.filename,
                "path": target_relative_path,
                "source_job_id": source_job_id,
            },
        )
        return UploadedFileInfo(
            kind=FileArtifactKind.INPUT,
            field=source.field,
            filename=source.filename,
            path=target_relative_path,
            storage_key=storage_key,
            checksum=source.checksum or _file_checksum(target_path),
            content_type=source.content_type or _guess_content_type(target_path.name),
            size_bytes=target_path.stat().st_size,
            created_at=datetime.now(timezone.utc),
        )

    def open_storage_key(self, storage_key: str) -> BinaryIO:
        """仅供已经过 ownership 校验的内部输入源读取暂存对象。"""
        return self.backend.open(storage_key)

    def copy_staged_input(self, target_job_id: str, item) -> UploadedFileInfo:
        filename = _safe_filename(item.filename, f"{item.field}.csv")
        relative_path = f"inputs/{item.field}.csv"
        storage_key = self._storage_key(target_job_id, relative_path)
        with self.backend.open(item.storage_key) as handle:
            content = handle.read(CSV_MAX_BYTES + 1)
        if not content or len(content) > CSV_MAX_BYTES:
            raise HTTPException(status_code=400, detail=f"{item.field} staged input is invalid")
        checksum = hashlib.sha256(content).hexdigest()
        expected = str(item.checksum).removeprefix("sha256:")
        if expected and checksum != expected:
            raise HTTPException(status_code=409, detail=f"{item.field} staged input checksum changed")
        self.backend.put_bytes(
            content,
            storage_key,
            content_type=item.content_type or "text/csv",
            metadata={
                "checksum": checksum,
                "created_at": datetime.now(timezone.utc).isoformat(),
                "kind": FileArtifactKind.INPUT.value,
                "field": item.field,
                "filename": filename,
                "path": relative_path,
                "source_bundle_id": item.bundle_id,
            },
        )
        return UploadedFileInfo(
            kind=FileArtifactKind.INPUT,
            field=item.field,
            filename=filename,
            path=relative_path,
            storage_key=storage_key,
            checksum=checksum,
            content_type=item.content_type or "text/csv",
            size_bytes=len(content),
            created_at=datetime.now(timezone.utc),
        )

    def ensure_local_copy(self, job_id: str, artifact: FileArtifactInfo) -> Path:
        relative_path = artifact.path
        workspace_path = self._workspace_path(job_id, relative_path)
        if workspace_path.exists():
            return workspace_path
        workspace_path.parent.mkdir(parents=True, exist_ok=True)
        with self.backend.open(artifact.storage_key) as handle, workspace_path.open("wb") as output:
            shutil.copyfileobj(handle, output)
        return workspace_path

    def resolve_inputs_by_field(self, job: JobRecord) -> dict[str, Path]:
        return self.materialize_inputs(job)

    def sync_workspace_artifacts(self, job: JobRecord) -> list[FileArtifactInfo]:
        paths = self.paths(job.id)
        if not paths.run_dir.exists():
            return []

        artifacts: list[FileArtifactInfo] = []
        seen_checksums: dict[str, str] = {}
        for path in sorted(paths.run_dir.rglob("*")):
            if not path.is_file():
                continue
            relative_path = path.relative_to(paths.run_dir).as_posix()
            if relative_path == "job.json" or relative_path.endswith(".tmp"):
                continue
            kind = _infer_artifact_kind(relative_path)
            checksum = _file_checksum(path)
            if self.settings.file_storage_dedupe_enabled and checksum in seen_checksums:
                storage_key = seen_checksums[checksum]
            else:
                storage_key = self._storage_key(job.id, relative_path)
                self.backend.put_file(
                    path,
                    storage_key,
                    content_type=_guess_content_type(path.name),
                    metadata={
                        "checksum": checksum,
                        "created_at": datetime.now(timezone.utc).isoformat(),
                        "kind": kind.value,
                        "path": relative_path,
                        "filename": path.name,
                    },
                )
                seen_checksums[checksum] = storage_key
            artifacts.append(
                FileArtifactInfo(
                    kind=kind,
                    filename=path.name,
                    path=relative_path,
                    storage_key=storage_key,
                    checksum=checksum,
                    content_type=_guess_content_type(path.name),
                    size_bytes=path.stat().st_size,
                    created_at=datetime.fromtimestamp(path.stat().st_mtime, timezone.utc),
                )
            )
        return artifacts

    def list_artifacts(self, job_id: str) -> list[FileArtifactInfo]:
        return self._artifacts_for_job(job_id)

    def collect_result_files(self, job_id: str) -> list[ResultFileInfo]:
        job = self._get_job(job_id)
        artifacts = self._artifacts_for_job(job_id, job)
        files: list[ResultFileInfo] = []
        for artifact in artifacts:
            if artifact.kind not in {FileArtifactKind.OUTPUT, FileArtifactKind.REPORT, FileArtifactKind.FIGURE}:
                continue
            files.append(
                ResultFileInfo(
                    kind=artifact.kind,
                    field=None,
                    filename=artifact.filename,
                    path=artifact.path,
                    storage_key=artifact.storage_key,
                    checksum=artifact.checksum,
                    content_type=artifact.content_type,
                    size_bytes=artifact.size_bytes,
                    created_at=artifact.created_at,
                    name=artifact.filename,
                    download_url=f"/api/jobs/{job_id}/download/{artifact.path}",
                )
            )
        return files

    def collect_report_links(self, job_id: str) -> ReportLinks:
        job = self._get_job(job_id)
        artifacts = self._artifacts_for_job(job_id, job)
        summary = None
        interactive = None
        for artifact in artifacts:
            if artifact.path.endswith("OmicsPrism_Report.html"):
                summary = f"/api/jobs/{job_id}/reports/summary"
            elif artifact.path.endswith("OmicsPrism_Interactive_Report.html"):
                interactive = f"/api/jobs/{job_id}/reports/interactive"
        return ReportLinks(summary=summary, interactive=interactive)

    def list_images(self, job_id: str) -> list[ImageInfo]:
        job = self._get_job(job_id)
        artifacts = self._artifacts_for_job(job_id, job)
        artifact_paths = {artifact.path for artifact in artifacts}
        images: list[ImageInfo] = []
        for artifact in artifacts:
            if artifact.kind != FileArtifactKind.IMAGE:
                continue
            interactive_path = _interactive_html_path_for_image(artifact.path)
            interactive_url = (
                f"/api/jobs/{job_id}/download/{interactive_path}"
                if interactive_path in artifact_paths
                else None
            )
            images.append(
                ImageInfo(
                    kind=FileArtifactKind.IMAGE,
                    field=None,
                    filename=artifact.filename,
                    path=artifact.path,
                    storage_key=artifact.storage_key,
                    checksum=artifact.checksum,
                    content_type=artifact.content_type,
                    size_bytes=artifact.size_bytes,
                    created_at=artifact.created_at,
                    name=artifact.filename,
                    thumbnail_url=f"/api/jobs/{job_id}/download/{artifact.path}",
                    full_url=f"/api/jobs/{job_id}/download/{artifact.path}",
                    interactive_url=interactive_url,
                )
            )
        return images

    def discover_static_image_stems(self, job_id: str) -> list[str]:
        job = self._get_job(job_id)
        artifacts = self._artifacts_for_job(job_id, job)
        stems = {Path(artifact.path).stem for artifact in artifacts if artifact.kind == FileArtifactKind.IMAGE}
        return sorted(stems)

    def source_static_paths(self, job_id: str, stem: str) -> dict[str, str | None]:
        job = self._get_job(job_id)
        artifacts = self._artifacts_for_job(job_id, job)
        result: dict[str, str | None] = {"png": None, "svg": None, "pdf": None, "jpg": None, "jpeg": None}
        for ext in result:
            match = next((artifact for artifact in artifacts if Path(artifact.path).name == f"{stem}.{ext}"), None)
            if match:
                result[ext] = match.path
        return result

    def read_log(self, job_id: str, max_chars: int = 50000) -> tuple[str | None, str]:
        job = self._get_job(job_id)
        artifacts = self._artifacts_for_job(job_id, job)
        for name in ("error.log", "omicsprism.log"):
            artifact = next((item for item in artifacts if item.path.endswith(name)), None)
            if artifact is None:
                continue
            content = self._read_text_artifact(job_id, artifact.path)
            return artifact.filename, content[-max_chars:]
        for name in ("error.log", "omicsprism.log"):
            local = self._workspace_path(job_id, f"outputs/{name}")
            if local.exists():
                return local.name, local.read_text(encoding="utf-8", errors="replace")[-max_chars:]
        return None, ""

    def recent_log(self, job_id: str) -> tuple[str | None, str | None]:
        log_name, content = self.read_log(job_id, max_chars=12000)
        return log_name, content or None

    def write_error_log(self, job_id: str, content: str) -> Path:
        return self.write_text_artifact(job_id, "outputs/error.log", content, kind=FileArtifactKind.LOG)

    def write_figure_manifest(self, job_id: str, manifest: dict[str, object]) -> Path:
        return self.write_json_artifact(job_id, "outputs/figures/manifest.json", manifest, kind=FileArtifactKind.FIGURE)

    def write_figure_spec(self, job_id: str, figure_id: str, spec: dict[str, object]) -> Path:
        safe_id = _safe_figure_id(figure_id)
        return self.write_json_artifact(job_id, f"outputs/figures/{safe_id}.json", spec, kind=FileArtifactKind.FIGURE)

    def write_text_artifact(self, job_id: str, relative_path: str, content: str, *, kind: FileArtifactKind) -> Path:
        path = self._workspace_path(job_id, relative_path)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")
        self._sync_artifact_file(job_id, path, kind=kind)
        return path

    def write_json_artifact(self, job_id: str, relative_path: str, payload: dict[str, object], *, kind: FileArtifactKind) -> Path:
        import json

        path = self._workspace_path(job_id, relative_path)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")
        self._sync_artifact_file(job_id, path, kind=kind)
        return path

    def sync_path(self, job_id: str, relative_path: str, *, kind: FileArtifactKind | None = None) -> Path:
        path = self._workspace_path(job_id, relative_path)
        if not path.exists():
            raise HTTPException(status_code=404, detail="File not found")
        self._sync_artifact_file(job_id, path, kind=kind or _infer_artifact_kind(relative_path))
        return path

    def open_artifact(self, job_id: str, relative_path: str) -> BinaryIO:
        artifact = self._artifact_for_path(job_id, relative_path)
        if artifact is None:
            return self._workspace_path(job_id, relative_path).open("rb")
        return self.backend.open(artifact.storage_key)

    def read_artifact_text(self, job_id: str, relative_path: str, *, max_chars: int | None = None) -> str:
        with self.open_artifact(job_id, relative_path) as handle:
            content = handle.read().decode("utf-8", errors="replace")
        return content[-max_chars:] if max_chars is not None else content

    def public_url(self, job_id: str, relative_path: str) -> str:
        if self.settings.file_storage_public_base_url:
            base = self.settings.file_storage_public_base_url.rstrip("/")
            return f"{base}/{self._storage_key(job_id, relative_path)}"
        return f"/api/jobs/{job_id}/download/{relative_path}"

    def read_json_artifact(self, job_id: str, relative_path: str) -> dict[str, object]:
        import json

        with self.open_artifact(job_id, relative_path) as handle:
            return json.loads(handle.read().decode("utf-8"))

    def get_artifact_download_name(self, job_id: str, relative_path: str) -> str:
        artifact = self._artifact_for_path(job_id, relative_path)
        if artifact is not None:
            return artifact.filename
        return Path(relative_path).name

    def storage_key(self, job_id: str, relative_path: str) -> str:
        return self._storage_key(job_id, relative_path)

    def update_job_artifacts(self, job: JobRecord, artifacts: list[FileArtifactInfo]) -> JobRecord:
        job.artifacts = artifacts
        existing_input_fields = {item.path: item.field for item in job.inputs if item.field}
        job.inputs = [
            UploadedFileInfo(
                kind=FileArtifactKind.INPUT,
                field=existing_input_fields.get(item.path) or item.field or "",
                filename=item.filename,
                path=item.path,
                storage_key=item.storage_key,
                checksum=item.checksum,
                content_type=item.content_type,
                size_bytes=item.size_bytes,
                created_at=item.created_at,
            )
            for item in artifacts
            if item.kind == FileArtifactKind.INPUT
        ]
        job.result_files = [
            ResultFileInfo(
                kind=item.kind,
                field=item.field,
                filename=item.filename,
                path=item.path,
                storage_key=item.storage_key,
                checksum=item.checksum,
                content_type=item.content_type,
                size_bytes=item.size_bytes,
                created_at=item.created_at,
                name=item.filename,
                download_url=f"/api/jobs/{job.id}/download/{item.path}",
            )
            for item in artifacts
            if item.kind in {FileArtifactKind.OUTPUT, FileArtifactKind.REPORT, FileArtifactKind.FIGURE}
            and item.path not in {"outputs/figures/manifest.json"}
        ]
        job.report_links = self.collect_report_links(job.id)
        return job

    def cleanup_job_storage(self, job: JobRecord) -> None:
        artifacts = self._artifacts_for_job(job.id, job)
        for artifact in artifacts:
            self.backend.delete(artifact.storage_key)
        shutil.rmtree(self._run_dir(job.id), ignore_errors=True)

    def has_artifact(self, job_id: str, relative_path: str) -> bool:
        artifact = self._artifact_for_path(job_id, relative_path)
        if artifact is not None:
            return True
        return self._workspace_path(job_id, relative_path).exists()

    def cleanup_artifact_path(self, relative_path: str) -> None:
        self.backend.delete(relative_path)

    def _sync_artifact_file(self, job_id: str, path: Path, *, kind: FileArtifactKind) -> None:
        relative_path = path.relative_to(self._run_dir(job_id)).as_posix()
        checksum = _file_checksum(path)
        storage_key = self._storage_key(job_id, relative_path)
        self.backend.put_file(
            path,
            storage_key,
            content_type=_guess_content_type(path.name),
            metadata={
                "checksum": checksum,
                "created_at": datetime.now(timezone.utc).isoformat(),
                "kind": kind.value,
                "path": relative_path,
                "filename": path.name,
            },
        )

    def _artifact_for_path(self, job_id: str, relative_path: str) -> FileArtifactInfo | None:
        job = self._get_job(job_id)
        for artifact in self._artifacts_for_job(job_id, job):
            if artifact.path == relative_path:
                return artifact
        return None

    def _artifacts_for_job(self, job_id: str, job: JobRecord | None = None) -> list[FileArtifactInfo]:
        current = job or self._get_job(job_id)
        if current and current.artifacts:
            return current.artifacts
        artifacts: list[FileArtifactInfo] = []
        workspace = self._workspace_path(job_id, ".")
        if workspace.exists():
            for path in sorted(workspace.rglob("*")):
                if path.is_file():
                    relative_path = path.relative_to(workspace).as_posix()
                    artifacts.append(
                        FileArtifactInfo(
                            kind=_infer_artifact_kind(relative_path),
                            field=None,
                            filename=path.name,
                            path=relative_path,
                            storage_key=self._storage_key(job_id, relative_path),
                            checksum=_file_checksum(path),
                            content_type=_guess_content_type(path.name),
                            size_bytes=path.stat().st_size,
                            created_at=datetime.fromtimestamp(path.stat().st_mtime, timezone.utc),
                        )
                    )
            return artifacts
        prefix = self._job_prefix(job_id)
        for key in self.backend.list_keys(prefix):
            if key.endswith("/"):
                continue
            relative_path = self._relative_path_from_key(job_id, key)
            head = self.backend.head(key) or {}
            metadata = head.get("metadata", {}) if isinstance(head.get("metadata", {}), dict) else {}
            artifacts.append(
                FileArtifactInfo(
                    kind=_infer_artifact_kind(relative_path),
                    field=metadata.get("field") if isinstance(metadata, dict) else None,
                    filename=metadata.get("filename") if isinstance(metadata, dict) else Path(relative_path).name,
                    path=relative_path,
                    storage_key=key,
                    checksum=metadata.get("checksum") if isinstance(metadata, dict) else None,
                    content_type=head.get("content_type") if isinstance(head.get("content_type"), str) else None,
                    size_bytes=int(head.get("content_length") or 0),
                    created_at=head.get("last_modified") if isinstance(head.get("last_modified"), datetime) else datetime.now(timezone.utc),
                )
            )
        return artifacts

    def _get_job(self, job_id: str) -> JobRecord | None:
        if self._job_store is None:
            return None
        try:
            return self._job_store.get_internal(job_id)
        except Exception:
            return None

    def _run_dir(self, job_id: str) -> Path:
        cleaned = _safe_job_id(job_id)
        run_dir = (self.workspace_root / cleaned).resolve()
        base_dir = self.workspace_root.resolve()
        if not run_dir.is_relative_to(base_dir):
            raise HTTPException(status_code=400, detail="Invalid job id")
        return run_dir

    def _workspace_path(self, job_id: str, relative_path: str) -> Path:
        base_dir = self._run_dir(job_id)
        target = (base_dir / relative_path).resolve()
        if not target.is_relative_to(base_dir):
            raise HTTPException(status_code=400, detail="Invalid file path")
        return target

    def _job_prefix(self, job_id: str) -> str:
        job_part = _safe_job_id(job_id)
        if self.storage_prefix:
            return f"{self.storage_prefix}/{job_part}"
        return job_part

    def _storage_key(self, job_id: str, relative_path: str) -> str:
        return f"{self._job_prefix(job_id)}/{relative_path}".replace("\\", "/").lstrip("/")

    def _relative_path_from_key(self, job_id: str, key: str) -> str:
        prefix = self._job_prefix(job_id).rstrip("/")
        key = key.replace("\\", "/")
        if key.startswith(prefix + "/"):
            return key[len(prefix) + 1 :]
        return key.split("/", 1)[-1]

    def _read_text_artifact(self, job_id: str, relative_path: str) -> str:
        with self.open_artifact(job_id, relative_path) as handle:
            return handle.read().decode("utf-8", errors="replace")


def _upload_args(*, content_type: str | None, metadata: dict[str, str] | None) -> dict[str, object]:
    result: dict[str, object] = {}
    if content_type:
        result["ContentType"] = content_type
    if metadata:
        result["Metadata"] = metadata
    return result


def _file_checksum(path: Path) -> str:
    hasher = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            hasher.update(chunk)
    return hasher.hexdigest()


def _guess_content_type(name: str) -> str | None:
    content_type, _ = mimetypes.guess_type(name)
    return content_type


def _infer_artifact_kind(relative_path: str) -> FileArtifactKind:
    normalized = relative_path.replace("\\", "/").lower()
    if normalized.endswith(".log"):
        return FileArtifactKind.LOG
    if normalized.endswith(".html"):
        return FileArtifactKind.REPORT
    if "/figures/" in normalized or normalized.startswith("outputs/figures/"):
        return FileArtifactKind.FIGURE
    if normalized.endswith((".png", ".svg", ".jpg", ".jpeg")):
        return FileArtifactKind.IMAGE
    if normalized.startswith("inputs/"):
        return FileArtifactKind.INPUT
    if normalized.endswith(".tmp"):
        return FileArtifactKind.TEMP
    return FileArtifactKind.OUTPUT


def _interactive_html_path_for_image(relative_path: str) -> str | None:
    normalized = relative_path.replace("\\", "/")
    suffix = Path(normalized).suffix.lower()
    if suffix not in {".png", ".svg", ".jpg", ".jpeg"}:
        return None
    path = Path(normalized)
    if "plots" not in path.parts:
        return None
    return f"outputs/plots/interactive/{path.stem}.html"


def _safe_job_id(job_id: str) -> str:
    cleaned = Path(str(job_id)).name.strip().replace("\x00", "")
    if cleaned != job_id or not cleaned:
        raise HTTPException(status_code=400, detail="Invalid job id")
    return cleaned


def _safe_filename(name: str | None, fallback: str) -> str:
    cleaned = Path(str(name or fallback)).name.strip().replace("\x00", "")
    return cleaned or fallback


def _safe_figure_id(value: str) -> str:
    cleaned = "".join(char if char.isalnum() or char in {"-", "_", "."} else "_" for char in value.strip())
    return cleaned.strip("._") or "figure"
