import binaryninja  # type: ignore[import]

EXT = ".bndb"


def canonical_db_name(filename: str) -> str:
    """Canonical, rename-stable identity for a binary's analysis database.
    must end with bndb extension
    """
    return filename if filename.endswith(EXT) else filename + EXT


def get_coordinator_name(bv: binaryninja.BinaryView) -> str:
    return canonical_db_name(bv._file.filename)
