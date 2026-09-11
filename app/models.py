from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(frozen=True)
class Warehouse:
    cabinet_key: str
    cabinet_name: str
    warehouse_id: int
    name: str


@dataclass(frozen=True)
class ProductVariant:
    cabinet_key: str
    chrt_id: int
    nm_id: int
    skus: tuple[str, ...]


@dataclass
class DistributionLine:
    barcode: str
    source_qty: int
    allocations: dict[tuple[str, int], int] = field(default_factory=dict)
    reserve_qty: int = 0
    found_in_cabinets: set[str] = field(default_factory=set)


@dataclass
class RunSummary:
    input_units: int = 0
    allocated_units: int = 0
    reserve_units: int = 0
    input_lines: int = 0
    output_files: int = 0
    no_sales_barcodes: list[str] = field(default_factory=list)
    not_found_barcodes: list[str] = field(default_factory=list)
