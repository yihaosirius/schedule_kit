"""图片校验与落盘。

**不做 HEIC 解码**（PLAN.md §18-④）：iPhone 拍照默认是 HEIC，而 Pillow
原生解不了它。为一个可能用不到的解码器引入 ``pillow-heif`` 不划算，
所以约定由客户端转换——快捷指令里有原生的「转换图像」动作。
服务端对 HEIC 返回 415 并给出可操作的提示，而不是含糊的"格式错误"。
"""

from __future__ import annotations

import hashlib
import io
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from PIL import Image, UnidentifiedImageError

from app.logging import get_logger, kv

log = get_logger("media")

ALLOWED_MIME = {
    "image/jpeg": ".jpg",
    "image/png": ".png",
    "image/webp": ".webp",
    "image/gif": ".gif",
}

#: ISO-BMFF 的 HEIC/HEIF 品牌标识
HEIC_BRANDS = {b"heic", b"heix", b"hevc", b"hevx", b"heim", b"heis", b"hevm", b"hevs", b"mif1", b"msf1"}

HEIC_HINT = (
    "收到的是 HEIC/HEIF 图片，服务端不支持该格式。"
    "请在客户端先转成 JPEG：快捷指令里加一个「转换图像」动作，格式选 JPEG，再上传。"
)


class MediaError(ValueError):
    """图片不合规。``status_code`` 供路由层直接映射成 HTTP 状态。"""

    status_code = 400


class PayloadTooLarge(MediaError):
    status_code = 413


class UnsupportedMedia(MediaError):
    status_code = 415


@dataclass(frozen=True)
class StoredImage:
    relative_path: str  # 相对 uploads 目录，POSIX 分隔
    sha256: str
    mime: str
    width: int
    height: int
    bytes: int


def is_heic(data: bytes) -> bool:
    # ISO-BMFF: [size:4][ 'ftyp' ][ major_brand:4 ]
    return len(data) >= 12 and data[4:8] == b"ftyp" and data[8:12] in HEIC_BRANDS


def sniff_mime(data: bytes) -> str | None:
    if data.startswith(b"\xff\xd8\xff"):
        return "image/jpeg"
    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png"
    if data.startswith((b"GIF87a", b"GIF89a")):
        return "image/gif"
    if len(data) >= 12 and data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "image/webp"
    return None


def store_image(
    data: bytes,
    *,
    uploads_dir: Path,
    max_bytes: int,
    now: datetime | None = None,
) -> StoredImage:
    """校验并保存图片，返回可入库的元数据。

    校验顺序有意为之：先看体积（最便宜的拒绝），再看 HEIC（给出最有用的
    错误），然后才是通用格式与真实解码。
    """
    if not data:
        raise MediaError("图片内容为空")
    if len(data) > max_bytes:
        raise PayloadTooLarge(
            f"图片过大：{len(data)} 字节，上限 {max_bytes} 字节。请压缩后重试。"
        )
    if is_heic(data):
        raise UnsupportedMedia(HEIC_HINT)

    mime = sniff_mime(data)
    if mime is None or mime not in ALLOWED_MIME:
        raise UnsupportedMedia(
            f"不支持的图片格式（识别为 {mime or '未知'}）。支持：JPEG / PNG / WebP / GIF。"
        )

    try:
        with Image.open(io.BytesIO(data)) as probe:
            probe.verify()  # 只校验完整性，verify 之后对象不可再用
        with Image.open(io.BytesIO(data)) as probe:
            width, height = probe.size
    except (UnidentifiedImageError, OSError) as exc:
        raise MediaError(f"图片无法解码，可能已损坏：{exc}") from exc

    digest = hashlib.sha256(data).hexdigest()
    bucket = (now or datetime.now()).strftime("%Y-%m-%d")
    directory = uploads_dir / bucket
    directory.mkdir(parents=True, exist_ok=True)
    filename = f"{digest[:16]}{ALLOWED_MIME[mime]}"
    target = directory / filename
    if not target.exists():
        target.write_bytes(data)

    relative = f"{bucket}/{filename}"
    log.info(
        "media.stored %s",
        kv(path=relative, mime=mime, bytes=len(data), width=width, height=height, sha256=digest),
    )
    return StoredImage(
        relative_path=relative, sha256=digest, mime=mime, width=width, height=height, bytes=len(data)
    )


def resolve_upload(uploads_dir: Path, relative_path: str) -> Path | None:
    """把库里的相对路径还原成绝对路径，并挡住目录穿越。"""
    candidate = (uploads_dir / relative_path).resolve()
    try:
        candidate.relative_to(uploads_dir.resolve())
    except ValueError:
        log.warning("media.path_traversal_blocked %s", kv(path=relative_path))
        return None
    return candidate if candidate.is_file() else None
