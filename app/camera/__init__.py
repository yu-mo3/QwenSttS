"""L2 设备层 · 摄像头。

与 app/audio/ 并列的第二个物理设备域。分开而不是塞进 app/audio/，
是因为后者的模块名与文档处处假设「音频」，混进一个 camera.py
会让那个包的语义变糊。
"""

from __future__ import annotations

from app.camera.capture import Camera, encode_jpeg, scale_to_fit

__all__ = ["Camera", "encode_jpeg", "scale_to_fit"]
