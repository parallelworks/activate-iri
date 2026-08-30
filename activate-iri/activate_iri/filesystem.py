"""filesystem domain: FirecREST-style POSIX operations executed as the caller on the cluster."""
from __future__ import annotations

from app.routers.filesystem import facility_adapter
from app.routers.filesystem import models as fs
from app.routers.status import models as status_models
from app.types.user import User
from fastapi import HTTPException

from . import posix
from .auth import ActivateAuthMixin
from .gateway import split_id
from .runtime import get_runtime


class FilesystemAdapter(ActivateAuthMixin, facility_adapter.FacilityAdapter):
    @staticmethod
    async def _forward(resource, user, op: str, body: dict):
        """Gateway mode: forward the operation to the upstream facility and return its result output."""
        rt = get_runtime()
        fac, uid = split_id(resource.id)
        if not (fac and rt.gateway and fac in rt.gateway.upstreams):
            return None
        result = await rt.gateway.filesystem(fac, uid, op, user.api_key, body)
        return result if isinstance(result, dict) else {"output": result}

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
        fwd = await self._forward(resource, user, "chmod", {"path": request_model.path, "mode": request_model.mode})
        if fwd is not None:
            return fs.PutFileChmodResponse.model_validate(fwd)
        try:
            script = posix.chmod_script(request_model.path, request_model.mode)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        return fs.PutFileChmodResponse(output=await self._entry(await self._run(resource, user, script, "chmod"), "chmod"))

    async def chown(self, resource, user, request_model: fs.PutFileChownRequest) -> fs.PutFileChownResponse:
        fwd = await self._forward(resource, user, "chown", {"path": request_model.path, "owner": request_model.owner, "group": request_model.group})
        if fwd is not None:
            return fs.PutFileChownResponse.model_validate(fwd)
        out = await self._run(resource, user, posix.chown_script(request_model.path, request_model.owner, request_model.group), "chown")
        return fs.PutFileChownResponse(output=await self._entry(out, "chown"))

    async def ls(self, resource, user, path, show_hidden, numeric_uid, recursive, dereference) -> fs.GetDirectoryLsResponse:
        fwd = await self._forward(resource, user, "ls", {"path": path, "showHidden": show_hidden, "numericUid": numeric_uid, "recursive": recursive, "dereference": dereference})
        if fwd is not None:
            return fs.GetDirectoryLsResponse.model_validate(fwd)
        out = await self._run(resource, user, posix.ls_script(path, show_hidden, numeric_uid, recursive, dereference), "ls")
        return fs.GetDirectoryLsResponse(output=posix.parse_ls(out))

    async def head(self, resource, user, path, file_bytes, lines, skip_trailing) -> fs.GetFileHeadResponse:
        fwd = await self._forward(resource, user, "head", {"path": path, **({"lines": lines} if lines else {"bytes": file_bytes or 1024}), "skipTrailing": skip_trailing})
        if fwd is not None:
            return fs.GetFileHeadResponse.model_validate(fwd)
        out = await self._run(resource, user, posix.head_script(path, file_bytes, lines, skip_trailing), "head")
        unit = fs.ContentUnit.lines if lines else fs.ContentUnit.bytes
        return fs.GetFileHeadResponse(output=fs.FileContent(content=out, content_type=unit, start_position=0, end_position=(lines or file_bytes or len(out))))

    async def tail(self, resource, user, path, file_bytes, lines, skip_heading) -> fs.GetFileTailResponse:
        fwd = await self._forward(resource, user, "tail", {"path": path, **({"lines": lines} if lines else {"bytes": file_bytes or 1024}), "skipHeading": skip_heading})
        if fwd is not None:
            return fs.GetFileTailResponse.model_validate(fwd)
        out = await self._run(resource, user, posix.tail_script(path, file_bytes, lines, skip_heading), "tail")
        unit = fs.ContentUnit.lines if lines else fs.ContentUnit.bytes
        return fs.GetFileTailResponse(output=fs.FileContent(content=out, content_type=unit, start_position=0, end_position=(lines or file_bytes or len(out))))

    async def view(self, resource, user, path, size, offset) -> fs.GetViewFileResponse:
        fwd = await self._forward(resource, user, "view", {"path": path, "size": size, "offset": offset})
        if fwd is not None:
            return fs.GetViewFileResponse.model_validate(fwd)
        out = await self._run(resource, user, posix.view_script(path, size, offset), "view")
        return fs.GetViewFileResponse(output=fs.FileContent(content=out, content_type=fs.ContentUnit.bytes, start_position=offset, end_position=offset + len(out)))

    async def checksum(self, resource, user, path) -> fs.GetFileChecksumResponse:
        fwd = await self._forward(resource, user, "checksum", {"path": path})
        if fwd is not None:
            return fs.GetFileChecksumResponse.model_validate(fwd)
        out = await self._run(resource, user, posix.checksum_script(path), "checksum")
        return fs.GetFileChecksumResponse(output=fs.FileChecksum(algorithm="SHA-256", checksum=out.strip()))

    async def file(self, resource, user, path) -> fs.GetFileTypeResponse:
        fwd = await self._forward(resource, user, "file", {"path": path})
        if fwd is not None:
            return fs.GetFileTypeResponse.model_validate(fwd)
        out = await self._run(resource, user, posix.file_script(path), "file")
        return fs.GetFileTypeResponse(output=out.strip())

    async def stat(self, resource, user, path, dereference) -> fs.GetFileStatResponse:
        fwd = await self._forward(resource, user, "stat", {"path": path, "dereference": dereference})
        if fwd is not None:
            return fs.GetFileStatResponse.model_validate(fwd)
        out = await self._run(resource, user, posix.stat_script(path, dereference), "stat")
        return fs.GetFileStatResponse(output=posix.parse_stat(out))

    async def rm(self, resource, user, path) -> fs.RemoveResponse:
        fwd = await self._forward(resource, user, "rm", {"path": path})
        if fwd is not None:
            return fs.RemoveResponse.model_validate(fwd)
        try:
            script = posix.rm_script(path)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        await self._run(resource, user, script, "rm")
        return fs.RemoveResponse(output=f"removed {path}")

    async def mkdir(self, resource, user, request_model: fs.PostMakeDirRequest) -> fs.PostMkdirResponse:
        fwd = await self._forward(resource, user, "mkdir", {"path": request_model.path, "parent": request_model.parent})
        if fwd is not None:
            return fs.PostMkdirResponse.model_validate(fwd)
        out = await self._run(resource, user, posix.mkdir_script(request_model.path, request_model.parent), "mkdir")
        return fs.PostMkdirResponse(output=await self._entry(out, "mkdir"))

    async def symlink(self, resource, user, request_model: fs.PostFileSymlinkRequest) -> fs.PostFileSymlinkResponse:
        fwd = await self._forward(resource, user, "symlink", {"path": request_model.path, "link_path": request_model.link_path})
        if fwd is not None:
            return fs.PostFileSymlinkResponse.model_validate(fwd)
        out = await self._run(resource, user, posix.symlink_script(request_model.path, request_model.link_path), "symlink")
        return fs.PostFileSymlinkResponse(output=await self._entry(out, "symlink"))

    async def download(self, resource, user, path) -> fs.GetFileDownloadResponse:
        fwd = await self._forward(resource, user, "download", {"path": path})
        if fwd is not None:
            return fs.GetFileDownloadResponse.model_validate(fwd)
        out = await self._run(resource, user, posix.download_script(path), "download")
        return fs.GetFileDownloadResponse(output=out.strip())

    async def upload(self, resource, user, path, content: str) -> fs.PutFileUploadResponse:
        out = await self._run(resource, user, posix.upload_script(path, posix.b64(content)), "upload")
        entry = await self._entry(out, "upload")
        return fs.PutFileUploadResponse(output=f"uploaded {entry.size} bytes to {path}")

    async def compress(self, resource, user, request_model: fs.PostCompressRequest) -> fs.PostCompressResponse:
        fwd = await self._forward(resource, user, "compress", request_model.model_dump(exclude_none=True))
        if fwd is not None:
            return fs.PostCompressResponse.model_validate(fwd)
        script = posix.compress_script(request_model.path, request_model.target_path, str(request_model.compression), request_model.match_pattern, request_model.dereference)
        return fs.PostCompressResponse(output=await self._entry(await self._run(resource, user, script, "compress"), "compress"))

    async def extract(self, resource, user, request_model: fs.PostExtractRequest) -> fs.PostExtractResponse:
        fwd = await self._forward(resource, user, "extract", request_model.model_dump(exclude_none=True))
        if fwd is not None:
            return fs.PostExtractResponse.model_validate(fwd)
        script = posix.extract_script(request_model.path, request_model.target_path, str(request_model.compression))
        return fs.PostExtractResponse(output=await self._entry(await self._run(resource, user, script, "extract"), "extract"))

    async def mv(self, resource, user, request_model: fs.PostMoveRequest) -> fs.PostMoveResponse:
        fwd = await self._forward(resource, user, "mv", request_model.model_dump(exclude_none=True))
        if fwd is not None:
            return fs.PostMoveResponse.model_validate(fwd)
        out = await self._run(resource, user, posix.mv_script(request_model.path, request_model.target_path), "mv")
        return fs.PostMoveResponse(output=await self._entry(out, "mv"))

    async def cp(self, resource, user, request_model: fs.PostCopyRequest) -> fs.PostCopyResponse:
        fwd = await self._forward(resource, user, "cp", request_model.model_dump(exclude_none=True))
        if fwd is not None:
            return fs.PostCopyResponse.model_validate(fwd)
        out = await self._run(resource, user, posix.cp_script(request_model.path, request_model.target_path, request_model.dereference), "cp")
        return fs.PostCopyResponse(output=await self._entry(out, "cp"))
