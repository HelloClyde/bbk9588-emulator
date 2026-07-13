#!/usr/bin/env python3
"""Probe the QEMU process backend and the QEMU web frontend."""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
import time
from pathlib import Path

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from emu.qemu.system import build_bbk_qemu_config, classify_guest_pc, find_qemu, run_qemu
from tests.run_frontend_web_smoke import (
    BUILD,
    find_free_port,
    http_json,
    start_frontend,
    summarize_status,
    wait_http,
)


def qemu_backend_probe(ns: argparse.Namespace) -> dict[str, object]:
    config = build_bbk_qemu_config(
        boot_mode=ns.boot_mode,
        executable=ns.qemu,
        ram_mb=ns.ram_mb,
        timeout_seconds=ns.qemu_timeout,
        machine=ns.qemu_machine,
        nand_image=ns.nand_image,
        cpu=ns.qemu_cpu,
        accel=ns.qemu_accel,
        firmware_patches=ns.qemu_firmware_patch,
    )
    if find_qemu(ns.qemu) is None:
        return {"ok": False, "failure": f"{ns.qemu!r} was not found"}
    result = run_qemu(config, hmp_sample_offsets=tuple(ns.hmp_sample))
    pcs = [
        str(sample.get("pc"))
        for sample in result.hmp_samples
        if isinstance(sample, dict) and sample.get("pc")
    ]
    return {
        "ok": result.returncode == 0 or result.timed_out,
        "kind": "qemu-system-mipsel",
        **result.to_json_dict(),
        "sampled_pcs": pcs,
        "pc_progressed": bool(pcs and pcs[-1] != f"0x{config.boot_pc & 0xFFFFFFFF:08x}"),
        "pc_classifications": [classify_guest_pc(pc) for pc in pcs],
    }


def qemu_web_probe(ns: argparse.Namespace) -> dict[str, object]:
    port = find_free_port(ns.host)
    values = vars(ns).copy()
    values.update(
        backend="qemu",
        port=port,
        use_existing=False,
        frame_push_min_interval=0.04,
        nand_image=ns.nand_image,
        no_nand=ns.nand_image is None,
        frontend_profile_out=None,
        qemu_extra_arg=[],
        qemu_machine_option=[],
        qemu_firmware_patch=ns.qemu_firmware_patch,
    )
    args = argparse.Namespace(**values)
    proc: subprocess.Popen[bytes] | None = None
    started = time.perf_counter()
    try:
        proc = start_frontend(args, port)
        wait_http(ns.host, port, 30)
        status = http_json(ns.host, port, "GET", "/api/status?detail=full")
        qemu = status.get("qemu") if isinstance(status.get("qemu"), dict) else {}
        stopped = http_json(ns.host, port, "POST", "/api/command", {"op": "stop"})
        return {
            "ok": status.get("backend") == "qemu" and bool(qemu.get("register_sample")),
            "kind": "web-qemu-backend",
            "elapsed_seconds": round(time.perf_counter() - started, 3),
            "status": summarize_status(status),
            "qemu": qemu,
            "stop_status": summarize_status(stopped),
        }
    finally:
        if proc is not None:
            try:
                http_json(ns.host, port, "POST", "/api/shutdown")
            except Exception:
                pass
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait(timeout=2)


def write_outputs(ns: argparse.Namespace, summary: dict[str, object]) -> tuple[Path, Path]:
    ns.out_dir.mkdir(parents=True, exist_ok=True)
    summary_path = ns.out_dir / f"{ns.prefix}_summary.json"
    report_path = ns.out_dir / f"{ns.prefix}_report.md"
    summary_path.write_text(json.dumps(summary, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    lines = [
        "# QEMU Backend Benchmark",
        "",
        f"- Result: {'PASS' if summary['ok'] else 'FAIL'}",
        f"- Boot mode: {summary['boot_mode']}",
        "",
        "## Results",
    ]
    for name in ("qemu_process", "qemu_web"):
        row = summary.get(name)
        if not isinstance(row, dict):
            continue
        lines.append(f"- {name}: ok={row.get('ok')} elapsed={row.get('elapsed_seconds')}s kind={row.get('kind')}")
        if name == "qemu_process":
            lines.append(f"  sampled_pcs={row.get('sampled_pcs')} pc_progressed={row.get('pc_progressed')}")
            regions = [
                item.get("region")
                for item in row.get("pc_classifications", [])
                if isinstance(item, dict)
            ]
            lines.append(f"  regions={regions}")
        if name == "qemu_web":
            qemu = row.get("qemu") if isinstance(row.get("qemu"), dict) else {}
            sample = qemu.get("register_sample") if isinstance(qemu.get("register_sample"), dict) else {}
            compact_sample = {key: sample.get(key) for key in ("sample_at_seconds", "pc", "ra", "sp", "gp")}
            lines.append(f"  register_sample={compact_sample}")
    if summary.get("failures"):
        lines += ["", "## Failures", *[f"- {failure}" for failure in summary["failures"]]]
    report_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return summary_path, report_path


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Probe the QEMU process backend and web frontend.")
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--boot-mode", choices=["nand", "c200", "uboot"], default="c200")
    ap.add_argument("--qemu", default="qemu-system-mipsel")
    ap.add_argument(
        "--nand-image",
        type=Path,
        help="Caller-owned writable NAND fixture; omitted for direct boot.",
    )
    ap.add_argument("--qemu-machine", default="bbk9588")
    ap.add_argument("--qemu-cpu", default="24Kf")
    ap.add_argument("--qemu-accel", default="tcg,thread=multi,tb-size=256")
    ap.add_argument("--qemu-gdb", dest="qemu_gdb", default=None)
    ap.add_argument("--qemu-timeout", type=float, default=2.0)
    ap.add_argument("--qemu-firmware-patch", action="append", default=None)
    ap.add_argument("--hmp-sample", type=float, action="append", default=[0.2, 1.0])
    ap.add_argument("--ram-mb", type=int, default=160)
    ap.add_argument("--out-dir", type=Path, default=BUILD)
    ap.add_argument("--prefix", default="qemu_backend_benchmark")
    ns = ap.parse_args(argv)

    started = time.perf_counter()
    failures: list[str] = []
    qemu_process = qemu_backend_probe(ns)
    if not qemu_process.get("ok"):
        failures.append("qemu process probe failed")
    qemu_web = qemu_web_probe(ns)
    if not qemu_web.get("ok"):
        failures.append("qemu web backend probe failed")
    summary: dict[str, object] = {
        "ok": not failures,
        "boot_mode": ns.boot_mode,
        "elapsed_seconds": round(time.perf_counter() - started, 3),
        "failures": failures,
        "qemu_process": qemu_process,
        "qemu_web": qemu_web,
    }
    summary_path, report_path = write_outputs(ns, summary)
    print(json.dumps({"ok": summary["ok"], "summary": str(summary_path), "report": str(report_path), "failures": failures}, ensure_ascii=False))
    return 0 if summary["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
