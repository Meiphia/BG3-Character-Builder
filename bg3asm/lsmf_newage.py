"""LSMF ("NewAge") blob reader for BG3 saves.

Vendored from bg3-newage by Jessica Smith (MIT License, https://github.com/NodeJSmith/bg3-newage),
src/bg3_newage/lsmf.py, lightly adapted (no 3.12-only syntax).
"""
import struct
from dataclasses import dataclass
from typing import NamedTuple, Optional

MAGIC = b"LSMF"
BASE_OFFSET = 48  # header size; all stored offsets are relative to the end of the header
GUID_SIZE = 16
LEVEL_COMPONENT = "core.v0.Level"
ENTITY_ID_COMPONENT = "core.v0.EntityId"

EMPTY_RANGE = 0xFFFF_FFFF_FFFF_FFFF  # stored in place of an offset when a range or owner map is empty

U64 = struct.Struct("<Q")
U32 = struct.Struct("<I")
I32 = struct.Struct("<i")
I64 = struct.Struct("<q")
# A [start, end) pair of offsets, the usual shape of a variable-length field
RANGE = struct.Struct("<QQ")
# magic, version, hash, block_offset, block_size, name_block_size, component_count, unknown u16, unknown u64
HEADER = struct.Struct("<4sIQQQIHHQ")
# Each component table entry; name_offset is relative to block_offset
TABLE_ENTRY = struct.Struct("<QQQIIQQ")
# core.v0.Level's single element: [entities_start, entities_end) and [owner_maps_start, owner_maps_end)
LEVEL_RECORD = struct.Struct("<QQQQ")
# One owner map: [indices_start, indices_end) of u32 entity indices, the component it describes, index count
OWNER_MAP_ENTRY = struct.Struct("<QQQQ")


class Header(NamedTuple):
    magic: bytes
    version: int
    hash: int
    block_offset: int
    block_size: int
    name_block_size: int
    component_count: int
    unknown1: int
    unknown2: int


class TableEntry(NamedTuple):
    name_offset: int
    name_size: int
    hash: int
    element_size: int
    version: int
    element_count: int
    data_offset: int


class LsmfError(ValueError):
    pass


@dataclass(frozen=True)
class Component:
    index: int
    name: str
    element_size: int
    element_count: int
    data_offset: int
    owners: object


class NewAge:
    def __init__(self, data: bytes) -> None:
        if len(data) < HEADER.size or data[:4] != MAGIC:
            raise LsmfError("not an LSMF blob (bad magic)")
        self.data = data
        header = Header._make(HEADER.unpack_from(data, 0))
        table_start = header.block_offset + header.name_block_size
        entries = [
            TableEntry._make(self.read_struct(TABLE_ENTRY, table_start + i * TABLE_ENTRY.size))
            for i in range(header.component_count)
        ]
        # name_offset/name_size are u32 values in u64 slots; some saves leave junk in the high halves
        names = [self.read(header.block_offset + (e.name_offset & 0xFFFFFFFF), e.name_size & 0xFFFFFFFF)
                 .decode("utf-8") for e in entries]
        if not names or names[0] != LEVEL_COMPONENT:
            raise LsmfError(f"expected {LEVEL_COMPONENT} as component 0, got {names[:1]}")

        entities_start, entities_end, maps_start, maps_end = self.read_struct(LEVEL_RECORD, entries[0].data_offset)
        self.entities = tuple(
            self.read(offset, GUID_SIZE) for offset in range(entities_start, entities_end, GUID_SIZE)
        )
        self.entity_by_guid = {guid: i for i, guid in enumerate(self.entities)}
        owner_maps = self.read_owner_maps(maps_start, maps_end)

        self.components = {}
        for i, (name, entry) in enumerate(zip(names, entries, strict=True)):
            owners = owner_maps.get(i)
            if owners is not None and len(owners) != entry.element_count:
                raise LsmfError(f"{name}: owner map has {len(owners)} entries for {entry.element_count} elements")
            self.components[name] = Component(
                i, name, entry.element_size, entry.element_count, entry.data_offset, owners
            )

    def read(self, offset: int, size: int) -> bytes:
        start = offset + BASE_OFFSET
        if start < 0 or start + size > len(self.data):
            raise LsmfError(f"read of {size} bytes at {offset:#x} is out of bounds")
        return self.data[start : start + size]

    def read_struct(self, fmt: struct.Struct, offset: int) -> tuple:
        return fmt.unpack(self.read(offset, fmt.size))

    def read_owner_maps(self, start: int, end: int) -> dict:
        maps = {}
        for offset in range(start, end, OWNER_MAP_ENTRY.size):
            # indices_end is redundant with count * 4
            indices_start, _, component_index, count = self.read_struct(OWNER_MAP_ENTRY, offset)
            maps[component_index] = tuple(i for (i,) in self.read_range(indices_start, indices_start + count * 4, U32))
        return maps

    def elements(self, name: str) -> list:
        """All elements of a component, paired with their owning entity (None for side tables)."""
        comp = self.components.get(name)
        if comp is None:
            return []
        return [
            (
                comp.owners[k] if comp.owners is not None else None,
                self.read(comp.data_offset + k * comp.element_size, comp.element_size),
            )
            for k in range(comp.element_count)
        ]

    def by_entity(self, name: str) -> dict:
        """Elements keyed by owning entity, for components with at most one element per entity."""
        result = {}
        for owner, raw in self.elements(name):
            if owner is None:
                continue
            if owner in result:
                raise LsmfError(f"{name} has more than one element for entity {owner}")
            result[owner] = raw
        return result

    def single(self, name: str) -> Optional[bytes]:
        """The one element of a singleton component (camp supplies, days passed), or None if absent."""
        elements = self.elements(name)
        if len(elements) > 1:
            raise LsmfError(f"{name} has {len(elements)} elements; expected a singleton")
        return elements[0][1] if elements else None

    def read_string(self, offset: int, length: int) -> str:
        """Strings (spell, recipe, waypoint names) are stored as (pointer, length) views into a shared block."""
        return self.read(offset, length).decode("utf-8")

    def read_range(self, start: int, end: int, record: struct.Struct) -> list:
        """Unpack each fixed-size record in a [start, end) range; an all-ones start marks an empty range.

        Every record comes back as a tuple, even a single-field one like ``U64``.
        """
        if start == EMPTY_RANGE:
            return []
        if end < start or (end - start) % record.size:
            raise LsmfError(f"range {start:#x}..{end:#x} is not a whole number of {record.size}-byte records")
        return [self.read_struct(record, offset) for offset in range(start, end, record.size)]

    def resolve_ref(self, offset: int) -> Optional[int]:
        """Turn an entity reference (an offset into the EntityId table) into an entity index."""
        table = self.components.get(ENTITY_ID_COMPONENT)
        if table is None:
            return None
        if not table.data_offset <= offset < table.data_offset + table.element_count * table.element_size:
            return None
        return self.entity_by_guid.get(self.read(offset, GUID_SIZE))


def read_u64_field(raw: bytes) -> int:
    """Decode a component element that is a single u64 (a pointer or an entity reference)."""
    return U64.unpack_from(raw)[0]


def read_i32_field(raw) -> Optional[int]:
    """Decode a component element that is a single i32, or None when the component is absent."""
    return None if raw is None else I32.unpack_from(raw)[0]
