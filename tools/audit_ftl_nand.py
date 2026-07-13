from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from emu.qemu.ftl import (
    count_legacy_logical_tail_pages,
    inject_remap_power_cut,
    inject_tail_power_cut,
    scan_ftl_image,
    sequence_is_newer,
)


def _record_json(record) -> dict[str, object]:
    return {
        "physical": record.physical,
        "kind": record.kind,
        "sequence": record.sequence,
        "logical": record.logical,
        "tail": record.tail,
        "last_valid_page": record.last_valid_page,
        "marker": record.marker,
        "reason": record.reason,
    }


def _report(path: Path) -> dict[str, object]:
    result = scan_ftl_image(path)
    anomalies = [
        _record_json(record)
        for record in result.records
        if record.kind in {"bad", "torn", "invalid"}
    ]
    return {
        "path": str(result.path),
        "block_count": result.block_count,
        "scan_start_block": result.scan_start_block,
        "counts": result.counts,
        "mapped_logical_blocks": len(result.mapping),
        "has_logical_zero": 0 in result.mapping,
        "legacy_logical_tail_pages": count_legacy_logical_tail_pages(path),
        "duplicate_logical_blocks": {
            str(logical): [_record_json(record) for record in records]
            for logical, records in result.duplicate_logical_blocks.items()
        },
        "anomalies": anomalies,
    }


def _comparison(reference, current) -> dict[str, object]:
    reference_by_physical = {record.physical: record for record in reference.records}
    current_by_physical = {record.physical: record for record in current.records}
    changed = []
    transitions = []
    for logical in sorted(set(reference.mapping) | set(current.mapping)):
        before = reference.mapping.get(logical)
        after = current.mapping.get(logical)
        before_key = None if before is None else (before.sequence, before.physical)
        after_key = None if after is None else (after.sequence, after.physical)
        if before_key == after_key:
            continue
        changed.append(
            {"logical": logical, "reference": before_key, "current": after_key}
        )
        if before is None or after is None or before.physical == after.physical:
            continue
        both_valid = (
            after
            if sequence_is_newer(after.sequence or 0, before.sequence or 0)
            else before
        )
        new_before = reference_by_physical.get(after.physical)
        old_after = current_by_physical.get(before.physical)
        transitions.append(
            {
                "logical": logical,
                "previous": before_key,
                "committed": after_key,
                "new_physical_before": None if new_before is None else new_before.kind,
                "old_physical_after": None if old_after is None else old_after.kind,
                "both_valid_winner": (both_valid.sequence, both_valid.physical),
                "torn_new_fallback": before_key,
            }
        )
    return {
        "reference": str(reference.path),
        "changed_mappings": changed,
        "remap_transitions": transitions,
    }


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Audit BBK 9588 raw NAND using the U-Boot/C200 FTL scan rules."
    )
    parser.add_argument("image", type=Path)
    parser.add_argument("--compare", type=Path)
    parser.add_argument("--json", action="store_true")
    parser.add_argument("--strict", action="store_true")
    injection = parser.add_mutually_exclusive_group()
    injection.add_argument("--inject-tail-power-cut", type=lambda value: int(value, 0))
    injection.add_argument("--inject-remap-power-cut", type=lambda value: int(value, 0))
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()

    if args.inject_tail_power_cut is not None:
        if args.output is None:
            parser.error("--inject-tail-power-cut requires --output")
        inject_tail_power_cut(args.image, args.output, args.inject_tail_power_cut)
    if args.inject_remap_power_cut is not None:
        if args.output is None or args.compare is None:
            parser.error("--inject-remap-power-cut requires --compare and --output")
        inject_remap_power_cut(
            args.compare,
            args.image,
            args.output,
            args.inject_remap_power_cut,
        )

    injected = (
        args.inject_tail_power_cut is not None
        or args.inject_remap_power_cut is not None
    )
    report = _report(args.output if injected else args.image)
    if args.compare is not None:
        reference = scan_ftl_image(args.compare)
        current = scan_ftl_image(
            args.output if injected else args.image
        )
        report["comparison"] = _comparison(reference, current)

    if args.json:
        print(json.dumps(report, ensure_ascii=False, indent=2))
    else:
        print(f"image: {report['path']}")
        print(
            f"blocks: {report['block_count']} scan_start=0x{report['scan_start_block']:x} "
            f"mapped={report['mapped_logical_blocks']} logical0={report['has_logical_zero']} "
            f"legacy_tail_pages={report['legacy_logical_tail_pages']}"
        )
        print("counts: " + ", ".join(f"{key}={value}" for key, value in sorted(report["counts"].items())))
        for anomaly in report["anomalies"]:
            print(
                f"{anomaly['kind']}: physical=0x{anomaly['physical']:x} "
                f"logical={anomaly['logical']} sequence={anomaly['sequence']} "
                f"reason={anomaly['reason']}"
            )
        comparison = report.get("comparison")
        if comparison:
            print(f"changed mappings: {len(comparison['changed_mappings'])}")

    if args.strict and (
        not report["has_logical_zero"]
        or any(item["kind"] in {"torn", "invalid"} for item in report["anomalies"])
    ):
        raise SystemExit(1)


if __name__ == "__main__":
    main()
