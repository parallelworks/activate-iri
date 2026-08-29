"""filesystem domain: FirecREST-style POSIX operations executed as the caller on the cluster."""
from __future__ import annotations

from app.routers.filesystem import facility_adapter
from app.routers.filesystem import models as fs
from app.routers.status import models as status_models
from app.types.user import User
from fastapi import HTTPException

from . import posix
from .auth import ActivateAuthMixin
from .runtime import get_runtime


class FilesystemAdapter(ActivateAuthMixin, facility_adapter.FacilityAdapter):
    async def _run(self, resource: status_models.Resource, user: User, script: str, what: str, stdin: bytes | None = None) -> str:
        rt = get_runtime()
        inv = await rt.inventory()
        cluster = inv.cluster_for(resource.id)
        if cluster is None:
            raise HTTPException(status_code=400, detail="Resource has no filesystem endpoint")
        identity = rt.identities.resolve(user, cluster)
        result = await rt.executor.run(identity, script, stdin=stdin, timeout=rt.settings.command_timeout)
        if result.returncode != 0:
            detail = (result.stderr or result.stdout).strip().splitlines()
            raise HTTPException(status_code=400, detail=f"{what} failed: {detail[-1] if detail else 'rc=' + str(result.returncode)}")
        return result.stdout

    async def _entry(self, out: str, what: str) -> fs.File:
        entry = posix.parse_single(out)
        if entry is None:
            raise HTTPException(status_code=500, detail=f"{what}: could not parse result")
        return entry

    async def chmod(self, resource, user, request_model: fs.PutFileChmodRequest) -> fs.PutFileChmodResponse:
        try:
            script = posix.chmod_script(request_model.path, request_model.mode)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        return fs.PutFileChmodResponse(output=await self._entry(await self._run(resource, user, script, "chmod"), "chmod"))

    async def chown(self, resource, user, request_model: fs.PutFileChownRequest) -> fs.PutFileChownResponse:
        out = await self._run(resource, user, posix.chown_script(request_model.path, request_model.owner, request_model.group), "chown")
        return fs.PutFileChownResponse(output=await self._entry(out, "chown"))

    async def ls(self, resource, user, path, show_hidden, numeric_uid, recursive, dereference) -> fs.GetDirectoryLsResponse:
        out = await self._run(resource, user, posix.ls_script(path, show_hidden, numeric_uid, recursive, dereference), "ls")
        return fs.GetDirectoryLsResponse(output=posix.parse_ls(out))

    async def head(self, resource, user, path, file_bytes, lines, skip_trailing) -> fs.GetFileHeadResponse:
        out = await self._run(resource, user, posix.head_script(path, file_bytes, lines, skip_trailing), "head")
        unit = fs.ContentUnit.lines if lines else fs.ContentUnit.bytes
        return fs.GetFileHeadResponse(output=fs.FileContent(content=out, content_type=unit, start_position=0, end_position=(lines or file_bytes or len(out))))

    async def tail(self, resource, user, path, file_bytes, lines, skip_heading) -> fs.GetFileTailResponse:
        out = await self._run(resource, user, posix.tail_script(path, file_bytes, lines, skip_heading), "tail")
        unit = fs.ContentUnit.lines if lines else fs.ContentUnit.bytes
        return fs.GetFileTailResponse(output=fs.FileContent(content=out, content_type=unit, start_position=0, end_position=(lines or file_bytes or len(out))))

    async def view(self, resource, user, path, size, offset) -> fs.GetViewFileResponse:
        out = await self._run(resource, user, posix.view_script(path, size, offset), "view")
        return fs.GetViewFileResponse(output=fs.FileContent(content=out, content_type=fs.ContentUnit.bytes, start_position=offset, end_position=offset + len(out)))

    async def checksum(self, resource, user, path) -> fs.GetFileChecksumResponse:
        out = await self._run(resource, user, posix.checksum_script(path), "checksum")
        return fs.GetFileChecksumResponse(output=fs.FileChecksum(algorithm="SHA-256", checksum=out.strip()))

    async def file(self, resource, user, path) -> fs.GetFileTypeResponse:
        out = await self._run(resource, user, posix.file_script(path), "file")
        return fs.GetFileTypeResponse(output=out.strip())

    async def stat(self, resource, user, path, dereference) -> fs.GetFileStatResponse:
        out = await self._run(resource, user, posix.stat_script(path, dereference), "stat")
        return fs.GetFileStatResponse(output=posix.parse_stat(out))

    async def rm(self, resource, user, path) -> fs.RemoveResponse:
        try:
            script = posix.rm_script(path)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        await self._run(resource, user, script, "rm")
        return fs.RemoveResponse(output=f"removed {path}")

    async def mkdir(self, resource, user, request_model: fs.PostMakeDirRequest) -> fs.PostMkdirResponse:
        out = await self._run(resource, user, posix.mkdir_script(request_model.path, request_model.parent), "mkdir")
        return fs.PostMkdirResponse(output=await self._entry(out, "mkdir"))

    async def symlink(self, resource, user, request_model: fs.PostFileSymlinkRequest) -> fs.PostFileSymlinkResponse:
        out = await self._run(resource, user, posix.symlink_script(request_model.path, request_model.link_path), "symlink")
        return fs.PostFileSymlinkResponse(output=await self._entry(out, "symlink"))

    async def download(self, resource, user, path) -> fs.GetFileDownloadResponse:
        out = await self._run(resource, user, posix.download_script(path), "download")
        return fs.GetFileDownloadResponse(output=out.strip())

    async def upload(self, resource, user, path, content: str) -> fs.PutFileUploadResponse:
        out = await self._run(resource, user, posix.upload_script(path, posix.b64(content)), "upload")
        entry = await self._entry(out, "upload")
        return fs.PutFileUploadResponse(output=f"uploaded {entry.size} bytes to {path}")

    async def compress(self, resource, user, request_model: fs.PostCompressRequest) -> fs.PostCompressResponse:
        script = posix.compress_script(request_model.path, request_model.target_path, str(request_model.compression), request_model.match_pattern, request_model.dereference)
        return fs.PostCompressResponse(output=await self._entry(await self._run(resource, user, script, "compress"), "compress"))

    async def extract(self, resource, user, request_model: fs.PostExtractRequest) -> fs.PostExtractResponse:
        script = posix.extract_script(request_model.path, request_model.target_path, str(request_model.compression))
        return fs.PostExtractResponse(output=await self._entry(await self._run(resource, user, script, "extract"), "extract"))

    async def mv(self, resource, user, request_model: fs.PostMoveRequest) -> fs.PostMoveResponse:
        out = await self._run(resource, user, posix.mv_script(request_model.path, request_model.target_path), "mv")
        return fs.PostMoveResponse(output=await self._entry(out, "mv"))

    async def cp(self, resource, user, request_model: fs.PostCopyRequest) -> fs.PostCopyResponse:
        out = await self._run(resource, user, posix.cp_script(request_model.path, request_model.target_path, request_model.dereference), "cp")
        return fs.PostCopyResponse(output=await self._entry(out, "cp"))
