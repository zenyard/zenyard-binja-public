import collections
import zstandard
from pathlib import Path
from binaryninja import BinaryView, Section as BNSection
from .log import log_debug


_HARDCODED_PREFIXES = ["libsystem_c", "libobjc"]

_IGNORED_SEGMENTS = {
    ".extern",
    "extern",
    ".plt",
    "__plt",
    ".plt.got",
    ".plt.sec",
    ".got",
    "__got",
    "__stubs",
    "__objc_stubs",
    "__auth_stubs",
    # TODO: PE, Mach-O segments?
}

_SWIFT_SECTIONS = frozenset(
    {
        "__swift5_types",
        "__swift5_proto",
        "__swift5_fieldmd",
        "__swift5_replace",
        "__swift5_ac_funcs",
        "__swift5_builtin",
    }
)

_IGNORED_SECTIONS = frozenset(
    {
        "__dsc_header",
        "extern",
        ".extern",
        ".plt",
        "__plt",
        ".plt.got",
        ".plt.sec",
        ".got",
        "__got",
        "__auth_got",
        "__stub",
        "__stubs",
        "__objc_stubs",
        "__auth_stubs",
        "__macho_header",
        "__unwind_info",
        "__objc_data",
        ".synthetic_builtins",
        # TODO: PE, Mach-O segments?
    }
)

_MAX_SECTION_DATA = 10 * 1024 * 1024
_ZSTD = zstandard.ZstdCompressor(level=6, threads=0)

SectionData = collections.namedtuple(
    "SectionData", ["library", "section", "segment"]
)


def _bare_section_name(name: str) -> SectionData:
    library = section = segment = None
    if "::" not in name:
        return SectionData(library, name, segment)
    library, rest = name.split("::")
    section = rest
    if "." in rest and rest[0] != ".":
        segment, section = rest.rsplit(".", 1)
    return SectionData(library, section, segment)


def _norm_image(name: str) -> str:
    """Compare images by basename so a blacklist install-path
    matches the bare library prefix that Binary Ninja puts on
    DSC section names (``libsystem_c.dylib``)."""
    return Path(name).name


class IgnoredSections:
    """
    Decides which object addresses to exclude from upload according
    to specific sectionsan loaded_image names
    """

    def __init__(self, container_prefix: str) -> None:
        self._ignored_prefixes = tuple(_HARDCODED_PREFIXES + [container_prefix])
        log_debug(
            f"hardcoded images to ignore={self._ignored_prefixes} "
            f"sections={_IGNORED_SECTIONS}"
        )

    def contains(self, sections: list[str]) -> bool:
        for section in sections:
            if section.startswith(self._ignored_prefixes):
                return True

            data = _bare_section_name(section)
            if data.section in _IGNORED_SECTIONS:
                return True

        return False


def _get_dsc_controller(bv: BinaryView):
    """Return the SharedCacheController for `bv`, or None if not a DSC view."""
    try:
        from binaryninja.sharedcache import SharedCacheController
    except ImportError:
        return None
    controller = SharedCacheController(bv)
    return controller if controller.is_valid() else None


def loaded_image_names(bv: BinaryView) -> list[str] | None:
    controller = _get_dsc_controller(bv)
    if controller is None:
        return None
    return [img.name for img in controller.loaded_images]


def is_ignored_section(section: str) -> bool:
    return section in _IGNORED_SEGMENTS


def is_swift_binary(bv: BinaryView) -> bool:
    return any(name in _SWIFT_SECTIONS for name in bv.sections)


def get_platform(bv: BinaryView) -> str | None:
    platform = bv.platform
    if platform is None:
        return None
    name = platform.name.lower()
    if any(k in name for k in ("ios", "iphone", "ipad")):
        return "ios"
    if any(k in name for k in ("mac", "osx", "darwin")):
        return "macos"
    return None


def binary_mapped_size(bv: BinaryView) -> int:
    """Approximate input-binary size: total bytes the loader mapped from the
    input file (the Binja analog of IDA's file-mapped segment sum).

    Falls back to the raw view's length for segment-less views, and to 0 when
    nothing is measurable — never block on a size we can't compute.
    """
    total = sum(seg.data_length for seg in bv.segments)
    if total > 0:
        return total
    raw = bv.file.raw
    if raw is not None:
        return raw.length
    return 0


def get_section_compressed(bv: BinaryView, section: BNSection) -> bytes | None:
    if section.length >= _MAX_SECTION_DATA:
        return None
    seg = bv.get_segment_at(section.start)
    if seg is None or seg.data_length == 0:
        return None
    data = bv.read(section.start, section.length)
    if not data:
        return None
    return _ZSTD.compress(data)
