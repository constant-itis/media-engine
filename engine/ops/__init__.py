"""Per-category operation modules. Each exposes an `OPS: list[Operation]`."""
from .video import OPS as VIDEO_OPS
from .audio import OPS as AUDIO_OPS
from .image import OPS as IMAGE_OPS

ALL_OPS = [*VIDEO_OPS, *AUDIO_OPS, *IMAGE_OPS]
