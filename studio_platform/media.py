"""Bounded media inspection and explicit reference derivation on CPU."""
from __future__ import annotations

import json
import io
import math
from pathlib import Path
import subprocess

from PIL import Image, ImageOps, UnidentifiedImageError

EXTENSIONS = {".png": "image", ".jpg": "image", ".jpeg": "image", ".webp": "image",
              ".mp4": "video", ".mov": "video", ".wav": "audio", ".mp3": "audio", ".flac": "audio"}
MIMES = {".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".webp": "image/webp",
         ".mp4": "video/mp4", ".mov": "video/quicktime", ".wav": "audio/wav", ".mp3": "audio/mpeg", ".flac": "audio/flac"}
DEMUXERS = "mov,wav,mp3,flac"
INPUT_OPTIONS = ["-protocol_whitelist", "file,pipe", "-format_whitelist", DEMUXERS, "-max_streams", "16",
                 ]
DEMUXER_BY_SUFFIX = {".mp4": "mov", ".mov": "mov", ".wav": "wav", ".mp3": "mp3", ".flac": "flac"}
PROBE_FIELDS = ("format=duration,format_name:stream=codec_type,codec_name,width,height,duration,sample_rate,"
                "channels,nb_frames,r_frame_rate,avg_frame_rate:stream_disposition=attached_pic")


def input_options(path):
    demuxer = DEMUXER_BY_SUFFIX.get(Path(path).suffix.lower())
    if demuxer is None:
        raise MediaError("不支持此媒体文件扩展名")
    options = [*INPUT_OPTIONS, "-f", demuxer]
    if demuxer == "mov":
        options.extend(["-enable_drefs", "0", "-use_absolute_path", "0"])
    return options


class MediaError(ValueError):
    pass


def probe(path: Path):
    try:
        result = subprocess.run(["ffprobe", "-v", "error", *input_options(path), "-show_entries", PROBE_FIELDS,
                                 "-of", "json", str(path)], stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                                timeout=30, check=True)
        value = json.loads(result.stdout)
        formats = set(value.get("format", {}).get("format_name", "").split(","))
        if not formats.intersection(DEMUXERS.split(",")):
            raise MediaError("不支持此媒体容器；引用清单不能用作媒体文件")
        return value
    except (OSError, subprocess.SubprocessError, json.JSONDecodeError):
        raise MediaError("文件不能解码，或本机缺少ffprobe") from None


def ffmpeg(args, timeout=180):
    # Scope these input-only restrictions to EVERY input. Restricting protocols
    # alone still permits concat/HLS manifests to read other local files.
    safe_args = []
    for index, value in enumerate(args):
        if str(value) == "-i":
            if index+1 >= len(args):
                raise MediaError("媒体输入参数不完整")
            safe_args.extend(input_options(args[index+1]))
        safe_args.append(str(value))
    try:
        # Error text is deliberately not consumed: corrupt media may produce
        # arbitrarily many decoder diagnostics. Keep it out of RAM and logs;
        # callers receive the same bounded, static error below.
        subprocess.run(["ffmpeg", "-v", "error", "-nostdin", "-y", "-threads", "2",
                        *safe_args], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                       timeout=timeout, check=True)
    except (OSError, subprocess.SubprocessError):
        raise MediaError("媒体处理失败或超时；原素材保持不变") from None


def inspect(path: Path, kind: str):
    if kind == "image":
        try:
            with Image.open(path) as source:
                expected = {".png": "PNG", ".jpg": "JPEG", ".jpeg": "JPEG", ".webp": "WEBP"}.get(path.suffix.lower())
                if expected is None or source.format != expected:
                    raise MediaError("图片实际格式与扩展名不一致")
                width, height = source.size
                # EXIF rotation swaps sides; these symmetric bounds apply before
                # decoding and prevent allocating oversized pixel buffers.
                if min(width, height) < 256 or max(width, height) > 5760 or not .4 <= width/height <= 2.5:
                    raise MediaError("参考图片边长须256–5760，宽高比0.4–2.5")
                if getattr(source, "n_frames", 1) != 1:
                    raise MediaError("仅支持静态参考图片")
                source.load()
                oriented = ImageOps.exif_transpose(source)
                width, height = oriented.size
                if min(width, height) < 256 or max(width, height) > 5760 or not .4 <= width/height <= 2.5:
                    raise MediaError("参考图片边长须256–5760，宽高比0.4–2.5")
            return {"kind": kind, "width": width, "height": height, "duration": None,
                    "source_duration": None, "has_audio": False, "model_ready": True, "notes": []}
        except (OSError, UnidentifiedImageError, Image.DecompressionBombError):
            raise MediaError("图片解码失败或像素过大") from None
    data = probe(path)
    expected = {".mp4": "mov", ".mov": "mov", ".wav": "wav", ".mp3": "mp3", ".flac": "flac"}.get(path.suffix.lower())
    if expected is None or expected not in data["format"]["format_name"].split(","):
        raise MediaError("媒体实际容器与扩展名不一致")
    videos = [s for s in data.get("streams", []) if s.get("codec_type") == "video" and not s.get("disposition", {}).get("attached_pic")]
    audios = [s for s in data.get("streams", []) if s.get("codec_type") == "audio"]
    try:
        duration = float(data["format"]["duration"])
    except (KeyError, TypeError, ValueError):
        raise MediaError("无法确定素材时长") from None
    if not math.isfinite(duration) or not .1 <= duration <= 3600:
        raise MediaError("素材时长须为0.1秒至1小时；长素材请先本地裁剪")
    if kind == "video" and not videos:
        raise MediaError("视频区需要包含真正的视频轨")
    if kind == "audio" and (videos or not audios):
        raise MediaError("音频区需要纯音频文件")
    result = {"kind": kind, "duration": duration, "source_duration": duration,
              "has_audio": bool(audios), "model_ready": 2 <= duration <= 15, "notes": []}
    if not result["model_ready"]:
        result["notes"].append("已保留原始素材；生成参考需要先选择2–15秒的片段")
    if videos:
        width, height = videos[0].get("width", 0), videos[0].get("height", 0)
        if min(width, height) < 256 or max(width, height) > 5760 or not .4 <= width/height <= 2.5:
            raise MediaError("参考视频边长须256–5760，宽高比0.4–2.5")
        result.update(width=width, height=height)
    return result


class _LimitedImageOutput:
    def __init__(self, target, maximum):
        self.target, self.maximum = target, maximum

    def write(self, value):
        if self.target.tell()+len(value) > self.maximum:
            raise MediaError("规范化图片超过容量上限，原件已保留")
        return self.target.write(value)

    def tell(self):
        return self.target.tell()

    def seek(self, position, whence=0):
        return self.target.seek(position, whence)

    def flush(self):
        return self.target.flush()

    def fileno(self):
        # Force Pillow's Python write path; a direct C file-descriptor write
        # would bypass the byte bound above.
        raise io.UnsupportedOperation("bounded image output")


def check_output_size(path, maximum):
    if type(maximum) is not int or maximum < 1 or path.stat().st_size > maximum:
        raise MediaError("处理后素材超过单文件容量上限，原件已保留")


def normalize(path: Path, metadata: dict, target_dir: Path, *, max_output_bytes=512*1024*1024):
    """Returns model file and model metadata; original bytes are never replaced."""
    result = dict(metadata)
    result["notes"] = list(metadata.get("notes", []))
    if not metadata["model_ready"]:
        return None, result
    kind = metadata["kind"]
    if kind == "image":
        target = target_dir / "normalized.png"
        with Image.open(path) as im, target.open("wb") as stream:
            ImageOps.exif_transpose(im).convert("RGB").save(_LimitedImageOutput(stream, max_output_bytes), format="PNG")
        check_output_size(target, max_output_bytes)
        return target, result
    duration = metadata["source_duration"]
    if kind == "audio":
        target = target_dir / "normalized.wav"
        ffmpeg(["-i", path, "-vn", "-ar", "32000", "-ac", "2", "-fs", max_output_bytes+1, target])
        check_output_size(target, max_output_bytes)
        checked = probe(target)
        stream = next((s for s in checked.get("streams", []) if s.get("codec_type") == "audio"), {})
        if (stream.get("sample_rate") != "32000" or stream.get("channels") != 2
                or abs(float(checked["format"].get("duration", 0))-duration) > .1):
            raise MediaError("音频归一化验证失败")
        result.update(sample_rate=32000, channels=2)
        result["notes"].append("模型副本为32kHz双声道，原音频保留")
        return target, result
    frames = max(56, 17 * math.ceil((math.ceil(duration*24)-5)/17) + 5)
    target = target_dir / "normalized.mp4"
    args = ["-i", path, "-vf", "fps=24,tpad=stop_mode=clone:stop_duration=1", "-frames:v", frames,
            "-c:v", "libx264", "-threads", "2", "-crf", "18", "-pix_fmt", "yuv420p"]
    if metadata["has_audio"]:
        args.extend(["-af", "apad", "-ar", "32000", "-ac", "2", "-c:a", "aac", "-t", frames/24])
    else:
        args.append("-an")
    ffmpeg([*args, "-movflags", "+faststart", "-fs", max_output_bytes+1, target])
    check_output_size(target, max_output_bytes)
    data = probe(target)
    video = next((s for s in data.get("streams", []) if s.get("codec_type") == "video"), {})
    if int(video.get("nb_frames", 0)) != frames:
        raise MediaError("视频归一化帧数验证失败")
    result.update(duration=frames/24, fps=24, frame_count=frames)
    result["notes"].append(f"模型副本为24fps/{frames}帧，末帧延长到原生帧网格；原视频保留")
    return target, result


def derive(path: Path, metadata: dict, target_dir: Path, start, end, *, max_output_bytes=512*1024*1024):
    if metadata["kind"] not in {"video", "audio"}:
        raise MediaError("当前仅视频/音频支持时间选段")
    if (type(start) not in (int, float) or type(end) not in (int, float)
            or not math.isfinite(start) or not math.isfinite(end) or start < 0 or end > metadata["source_duration"] + .01
            or not 2 <= end-start <= 15):
        raise MediaError("选段须在原素材范围内，且长度为2–15秒")
    if metadata["kind"] == "video":
        target = target_dir / "trimmed.mp4"
        args = ["-i", path, "-ss", start, "-t", end-start, "-map", "0:v:0", "-map", "0:a:0?",
                "-c:v", "libx264", "-threads", "2", "-crf", "18", "-pix_fmt", "yuv420p", "-c:a", "aac", "-movflags", "+faststart", target]
    else:
        target = target_dir / "trimmed.wav"
        args = ["-i", path, "-ss", start, "-t", end-start, "-vn", "-ar", "32000", "-ac", "2", target]
    ffmpeg([*args[:-1], "-fs", max_output_bytes+1, args[-1]])
    check_output_size(target, max_output_bytes)
    checked = inspect(target, metadata["kind"])
    if abs(checked["source_duration"] - (end-start)) > .1:
        raise MediaError("选段时长验证失败")
    return target
