"""Read a GGUF file's metadata (architecture, context length, attention
shape, chat template) without loading the model. Tensor data is never read.

Format: magic "GGUF", uint32 version, uint64 tensor count, uint64 key/value
count, then the key/value pairs. Long arrays (the tokenizer's vocabulary) are
skipped, not stored.
"""

import struct

_SCALARS = {0: "<B", 1: "<b", 2: "<H", 3: "<h", 4: "<I", 5: "<i", 6: "<f", 7: "<?", 10: "<Q", 11: "<q", 12: "<d"}
STRING, ARRAY = 8, 9
KEEP_ARRAY_MAX = 4096  # longer arrays (token lists) are skipped


class _Reader:
    def __init__(self, handle):
        self.handle = handle

    def take(self, fmt):
        size = struct.calcsize(fmt)
        data = self.handle.read(size)
        if len(data) != size:
            raise ValueError("unexpected end of file")
        return struct.unpack(fmt, data)[0]

    def string(self):
        length = self.take("<Q")
        if length > 1 << 26:
            raise ValueError("string too long")
        return self.handle.read(length).decode("utf-8", errors="replace")

    def skip_string(self):
        self.handle.seek(self.take("<Q"), 1)

    def value(self, kind):
        if kind in _SCALARS:
            return self.take(_SCALARS[kind])
        if kind == STRING:
            return self.string()
        if kind == ARRAY:
            item, count = self.take("<I"), self.take("<Q")
            if count > KEEP_ARRAY_MAX:
                self.skip(item, count)
                return None
            return [self.value(item) for _ in range(count)]
        raise ValueError(f"unknown GGUF value type {kind}")

    def skip(self, kind, count):
        if kind in _SCALARS:
            self.handle.seek(struct.calcsize(_SCALARS[kind]) * count, 1)
        elif kind == STRING:
            for _ in range(count):
                self.skip_string()
        else:
            for _ in range(count):
                self.value(kind)


def is_gguf(path):
    try:
        with open(path, "rb") as handle:
            return handle.read(4) == b"GGUF"
    except OSError:
        return False


def metadata(path):
    """{key: value} from a GGUF file's header. Raises ValueError if it isn't one."""
    with open(path, "rb") as handle:
        if handle.read(4) != b"GGUF":
            raise ValueError("not a GGUF file")
        reader = _Reader(handle)
        version = reader.take("<I")
        if version < 2:
            raise ValueError(f"GGUF version {version} isn't supported")
        reader.take("<Q")  # tensor count
        pairs = reader.take("<Q")
        info = {}
        for _ in range(pairs):
            key = reader.string()
            info[key] = reader.value(reader.take("<I"))
        return info
