import collections
import zstandard
from binaryninja import BinaryView, Section as BNSection
from .log import log_debug


_HARDCODED_PREFIXES = ["libsystem_c", "libobjc"]

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


class IgnoredSections:
    """
    Decides which object addresses to exclude from upload according
    to specific sectionsan loaded_image names
    """

    def __init__(self, container_prefix: str = "") -> None:
        # An empty container_prefix means "no cache container to drop"; never
        # add it to the prefix tuple — ``str.startswith("")`` is always True
        # and would match every section.
        prefixes = list(_HARDCODED_PREFIXES)
        if container_prefix:
            prefixes.append(container_prefix)
        self._ignored_prefixes = tuple(prefixes)
        log_debug(
            f"hardcoded images to ignore={self._ignored_prefixes} "
            f"sections={_IGNORED_SECTIONS}"
        )

    def contains(self, sections: list[str]) -> bool:
        for section in sections:
            if section.startswith(self._ignored_prefixes):
                return True

            data = _bare_section_name(section)
            if data.section.startswith(tuple(_IGNORED_SECTIONS)):
                return True

        return False


def is_dsc_view(bv: BinaryView) -> bool:
    """True for an Apple dyld shared cache view (DSCView)."""
    return "dscview" in bv.view_type.lower()


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
    return section in _IGNORED_SECTIONS


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


# Auto-loaded dependency dylibs to exclude from a DSC's loaded-size estimate
# (matched as substrings of the image path). In a DSC, the deps analysis pulls
# in are the low-level system libs under /usr/lib/ (libobjc, libsystem_*, ...).
_DSC_AUTOLOADED_PREFIXES = ("/usr/lib/",)


def _dsc_loaded_size(bv: BinaryView) -> int | None:
    """Estimated size of the frameworks a user explicitly loaded into a DSC
    view: sum of IMAGE region bytes, excluding the /usr/lib/ system dylibs that
    analysis auto-pulls in. Returns None when the shared-cache controller is
    unavailable (caller then falls back to the generic measurement)."""
    controller = _get_dsc_controller(bv)
    if controller is None:
        return None
    try:
        from binaryninja.sharedcache import SharedCacheRegionType
    except ImportError:
        return None
    image_type = SharedCacheRegionType.SharedCacheRegionTypeImage
    names = {im.header_address: im.name for im in controller.images}
    total = 0
    for region in controller.loaded_regions:
        if region.region_type != image_type:
            continue
        name = names.get(region.image_start)
        if name is None or any(p in name for p in _DSC_AUTOLOADED_PREFIXES):
            continue
        total += region.size
    return total


def binary_mapped_size(bv: BinaryView) -> int:
    """Approximate input-binary size: total bytes the loader mapped from the
    input file (the Binja analog of IDA's file-mapped segment sum).

    For a DSC view, measures the user-loaded frameworks (excluding auto-loaded
    /usr/lib/ deps) instead, since DSC segments are not file-backed.

    Falls back to the raw view's length for segment-less views, and to 0 when
    nothing is measurable — never block on a size we can't compute.
    """
    if is_dsc_view(bv):
        dsc_size = _dsc_loaded_size(bv)
        if dsc_size is not None:
            return dsc_size
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
