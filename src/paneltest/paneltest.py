#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
paneltest - kiem tra man hinh laptop (eDP) tren Ubuntu Server, khong can do hoa.

Mot file duy nhat, khong phu thuoc bat cu package nao ngoai thu vien chuan Python 3.8+.

Cach dung nhanh:
    sudo python3 paneltest.py info                 # tong hop: GPU, connector, mode, EDID, backlight, loi dmesg
    sudo python3 paneltest.py edid                 # giai ma EDID cua panel
    sudo python3 paneltest.py pattern              # chay het test pattern (diem chet, ho sang, mau...)
    sudo python3 paneltest.py pattern -p black -t 20
    sudo python3 paneltest.py backlight            # den nen / do sang
    sudo python3 paneltest.py dmesg                # loi driver DRM/eDP
    python3 paneltest.py selftest                  # tu kiem tra phan giai ma (khong can phan cung)

Chay truc tiep tren ban phim laptop, khong qua SSH: man hinh can kiem tra la man hinh cua may do.
"""

from __future__ import annotations

import argparse
import fcntl
import glob
import math
import os
import re
import struct
import subprocess
import sys
import termios
import time

VERSION = "1.0.0"
PROGRAM = "paneltest"

# --------------------------------------------------------------------------- #
# ioctl fbdev (uapi/linux/fb.h)
# --------------------------------------------------------------------------- #
FBIOGET_VSCREENINFO = 0x4600
FBIOGET_FSCREENINFO = 0x4602

# struct fb_var_screeninfo: 8 x __u32 (32 byte) roi toi red, green, blue,
# moi struct fb_bitfield dai 16 byte (offset, length, msb_right, reserved).
BITFIELD_RED = 32
BITFIELD_GREEN = 48
BITFIELD_BLUE = 64

# struct fb_fix_screeninfo: char id[16]; __u32 smem_start; __u32 smem_len;
# __u32 type, type_aux, visual, xpanstep, ypanstep, ywrapstep; __u32 line_length (offset 60)
FIX_ID_LEN = 16
FIX_SMEM_OFF = 16
FIX_LINE_LENGTH_OFF = 60

SOLID_COLORS = {
    "black": (0, 0, 0),
    "white": (255, 255, 255),
    "red": (255, 0, 0),
    "green": (0, 255, 0),
    "blue": (0, 0, 255),
    "gray": (128, 128, 128),
    "gray25": (64, 64, 64),
    "gray75": (192, 192, 192),
}
GEOMETRY_PATTERNS = ("rgb", "checker", "grid", "dpi")
PATTERNS = tuple(SOLID_COLORS) + GEOMETRY_PATTERNS

PATTERN_HINT = {
    "black": "tim diem chet SANG (stuck pixel) - nen tat den phong",
    "white": "tim diem chet TOI, vet ban, ho quang (backlight bleed)",
    "red": "kiem tra kenh mau do, sub-pixel chet",
    "green": "kiem tra kenh mau luc",
    "blue": "kiem tra kenh mau lam",
    "gray": "phat hien loang mau, clouding",
    "gray25": "nhay nhat de thay clouding va ho quang",
    "gray75": "nhay nhat de thay vet ban va ho quang",
    "rgb": "thu tu mau do-luc-lam + 4 vien man hinh",
    "checker": "o 16x16 px: phat hien anh bi keo gian / sai ti le, lam thuoc ghi toa do diem chet",
    "grid": "tim hang/cot pixel chet va kiem tra du 4 vien",
    "dpi": "hinh vuong canh 100 px: do thuoc ke kiem tra kich thuoc vat ly",
}


# --------------------------------------------------------------------------- #
# Tien ich
# --------------------------------------------------------------------------- #
def hr(title: str = "") -> None:
    print("-" * 72)
    if title:
        print(title.upper())
        print("-" * 72)


def read_text(path: str) -> str:
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as fh:
            return fh.read().strip()
    except OSError:
        return ""


def run(cmd: list[str], root_hint: bool = True) -> str:
    """Chay lenh ngoai, tra ve stdout+stderr; khong bao gio nem exception."""
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
    except (OSError, subprocess.SubprocessError) as exc:
        return f"(khong chay duoc {' '.join(cmd)}: {exc})"
    out = (proc.stdout or "") + (proc.stderr or "")
    if not out.strip() and root_hint and proc.returncode != 0:
        out = f"(khong co du lieu; thu lai voi sudo: {' '.join(cmd)})"
    return out.strip()


# --------------------------------------------------------------------------- #
# EDID
# --------------------------------------------------------------------------- #
EDID_HEADER = bytes([0x00, 0xFF, 0xFF, 0xFF, 0xFF, 0xFF, 0xFF, 0x00])
EDID_DESCRIPTOR_NAMES = {
    0xFF: "Monitor serial number",
    0xFE: "Unspecified text",
    0xFD: "Monitor range limits",
    0xFC: "Monitor name",
    0xF7: "Established timings III",
}


def edid_manufacturer(data: bytes) -> str:
    raw = data[8] << 8 | data[9]
    return "".join(chr(ord("A") - 1 + ((raw >> shift) & 0x1F)) for shift in (10, 5, 0))


def edid_checksum_ok(block: bytes) -> bool:
    return sum(block) % 256 == 0


def edid_ascii_field(block: bytes) -> str:
    return bytes(b for b in block[5:18] if b >= 32).decode("ascii", "replace").strip()


def edid_color_depth(byte20: int) -> str:
    # EDID 1.4, byte 20 bit 6..4: 001=6bpc 010=8bpc 011=10bpc 100=12bpc 101=14bpc 110=16bpc
    table = {0b001: 6, 0b010: 8, 0b011: 10, 0b100: 12, 0b101: 14, 0b110: 16}
    value = table.get((byte20 >> 4) & 0x07)
    return f"{value} bit/kenh" if value else "khong khai bao"


def edid_parse_descriptor(block: bytes, offset: int) -> str:
    """Giai ma 1 Detailed Timing Descriptor (18 byte)."""
    b = block[offset:offset + 18]
    if b[0] == 0 and b[1] == 0:                       # display descriptor, khong phai timing
        tag = b[3]
        if tag == 0xFD:
            v = b[5:11]
            return (f"      [Monitor range limits] V-sync {v[0]}-{v[1]} Hz, "
                    f"H-sync {v[2]}-{v[3]} kHz, max pixel clock {v[4] * 10} MHz")
        return f"      [{EDID_DESCRIPTOR_NAMES.get(tag, hex(tag))}] {edid_ascii_field(b)}"

    pixel_clock_khz = (b[0] | b[1] << 8) * 10
    hactive = b[2] | ((b[4] >> 4) << 8)
    hblank = b[3] | ((b[4] & 0x0F) << 8)
    vactive = b[5] | ((b[7] >> 4) << 8)
    vblank = b[6] | ((b[7] & 0x0F) << 8)
    flags = b[17]
    interlaced = "i" if flags & 0x80 else ""
    stereo = {0b00: "-", 0b01: "field seq R", 0b10: "field seq L"}.get((flags >> 5) & 0x03, "-")
    sync = {0b00: "analog composite", 0b01: "bipolar analog composite",
            0b10: "digital composite", 0b11: "digital separate"}[(flags >> 3) & 0x03]
    vsync = "+" if flags & 0x04 else "-"
    hsync = "+" if flags & 0x02 else "-"
    freq = pixel_clock_khz * 1000.0 / ((hactive + hblank) * (vactive + vblank))
    return (f"      {hactive}x{vactive}{interlaced} @ {freq:.3f} Hz  "
            f"(pixel clock {pixel_clock_khz / 1000.0:.3f} MHz)\n"
            f"        h: {hactive}+{hblank}   v: {vactive}+{vblank}   sync: {sync}"
            f" hsync{hsync} vsync{vsync} stereo:{stereo}")


def edid_native_mode(data: bytes) -> tuple[int, int, float] | None:
    """Tra ve (ngang, doc, Hz) cua DTD dau tien la timing that = do phan giai goc."""
    for offset in (54, 72, 90, 108):
        b = data[offset:offset + 18]
        if len(b) < 18 or (b[0] == 0 and b[1] == 0):
            continue
        pixel_clock_khz = (b[0] | b[1] << 8) * 10
        hactive = b[2] | ((b[4] >> 4) << 8)
        hblank = b[3] | ((b[4] & 0x0F) << 8)
        vactive = b[5] | ((b[7] >> 4) << 8)
        vblank = b[6] | ((b[7] & 0x0F) << 8)
        freq = pixel_clock_khz * 1000.0 / ((hactive + hblank) * (vactive + vblank))
        return hactive, vactive, freq
    return None


def edid_report(data: bytes, source: str) -> dict:
    """Giai ma va in toan bo EDID. Tra ve dict thong so de chuong trinh khac dung."""
    if len(data) < 128:
        raise ValueError(f"{source}: chi co {len(data)} byte (EDID can it nhat 128 byte)")

    problems = []
    print(f"Nguon           : {source}")
    print(f"So byte         : {len(data)}  ({len(data) // 128} block x 128 byte)")
    if data[:8] != EDID_HEADER:
        problems.append("header 00h sai")
        print(f"Header 00h      : SAI ({data[:8].hex(' ')})")
    else:
        print("Header 00h      : OK")
    for i in range(len(data) // 128):
        blk = data[i * 128:(i + 1) * 128]
        ok = edid_checksum_ok(blk)
        if not ok:
            problems.append(f"checksum block {i + 1} sai")
        print(f"Checksum block {i + 1}: {'OK' if ok else 'SAI - cap eDP long hoac EDID loi'}")

    serial = data[12] | data[13] << 8 | data[14] << 16 | data[15] << 24
    week, year_raw = data[16], data[17]
    year = "model year (xem tuan)" if year_raw == 0xFF else str(1990 + year_raw)
    diag = ((data[21] ** 2 + data[22] ** 2) ** 0.5) / 2.54

    print(f"Nha san xuat    : {edid_manufacturer(data)}   (product code {data[10] | data[11] << 8})")
    print(f"Serial (EDID)   : {serial} (0x{serial:08X})")
    print(f"EDID version    : {data[18]}.{data[19]}")
    print(f"Ngay SX         : tuan {week} / {year}   <-- so voi ngay ban mua panel")
    print(f"Kieu man hinh   : {'digital' if data[20] & 0x80 else 'analog'}, {edid_color_depth(data[20])}")
    print(f"Kich thuoc EDID : {data[21]} cm x {data[22]} cm  (~{diag:.1f} inch cheo)"
          "   <-- so voi thong so panel ban thay")
    if not (5 <= diag <= 20):
        problems.append(f"kich thuoc {diag:.1f} inch bat thuong")

    print("Detailed timing descriptors:")
    for index, offset in enumerate((54, 72, 90, 108), start=1):
        print(f"    DTD #{index}:")
        print(edid_parse_descriptor(data, offset))
    if data[126]:
        print(f"So block mo rong: {data[126]} (CTA-861 co the chua them mode/HDR)")

    native = edid_native_mode(data)
    if native:
        print(f"\nDO PHAN GIAI GOC: {native[0]}x{native[1]} @ {native[2]:.3f} Hz"
              "   <-- Ubuntu phai chay dung con so nay")
    else:
        problems.append("khong tim thay DTD timing nao")

    if problems:
        print("\nCANH BAO: " + "; ".join(problems))
    else:
        print("\nEDID hop le.")

    return {"manufacturer": edid_manufacturer(data), "native": native,
            "diagonal_inch": diag, "problems": problems}


def find_edid_files() -> list[str]:
    return sorted(p for p in glob.glob("/sys/class/drm/card*-*/edid") if os.path.getsize(p))


# --------------------------------------------------------------------------- #
# Framebuffer + test pattern
# --------------------------------------------------------------------------- #
class Framebuffer:
    """Ghi test pattern truc tiep vao /dev/fb0 (khong can X/Wayland)."""

    def __init__(self, dev: str = "/dev/fb0"):
        if not os.path.exists(dev):
            raise FileNotFoundError(
                f"Khong tim thay {dev}. Kiem tra: ls -l /dev/fb0 ; lsmod | grep -E 'drm|fb'\n"
                "Neu that su khong co fbdev: sudo apt install libdrm-tests roi dung 'modetest',"
                " hoac mo tools/test-man-hinh.html bang trinh duyet.")
        self.dev = dev
        self.fd = os.open(dev, os.O_RDWR)
        var = bytearray(160)
        fcntl.ioctl(self.fd, FBIOGET_VSCREENINFO, var)
        fix = bytearray(80)
        fcntl.ioctl(self.fd, FBIOGET_FSCREENINFO, fix)
        self._setup(bytes(var), bytes(fix))

    @classmethod
    def _from_structs(cls, var: bytes, fix: bytes) -> "Framebuffer":
        """Tao instance khong mo thiet bi (dung cho selftest)."""
        self = cls.__new__(cls)
        self.dev = "<memory>"
        self.fd = -1
        self._setup(var, fix)
        return self

    def _setup(self, var: bytes, fix: bytes) -> None:
        self.var = var
        (self.xres, self.yres, self.xres_virtual, self.yres_virtual,
         self.xoffset, self.yoffset, self.bits_per_pixel) = struct.unpack_from("<7I", var, 0)
        self.id = fix[:FIX_ID_LEN].split(b"\0")[0].decode("ascii", "replace")
        self.smem_start, self.smem_len = struct.unpack_from("<2I", fix, FIX_SMEM_OFF)
        (self.line_length,) = struct.unpack_from("<I", fix, FIX_LINE_LENGTH_OFF)
        self.bpp = self.bits_per_pixel // 8
        if self.bpp not in (2, 4):
            raise ValueError(f"bpp={self.bits_per_pixel} chua duoc ho tro (chi ho tro 16bpp va 32bpp)")
        if self.line_length == 0:
            self.line_length = self.xres * self.bpp
        if self.line_length < self.xres * self.bpp:
            raise ValueError(f"line_length={self.line_length} < xres*bpp={self.xres * self.bpp}")
        if self.smem_len and self.line_length * self.yres > self.smem_len:
            print(f"      canh bao: buffer {self.line_length * self.yres} > smem_len {self.smem_len}")
        self.buf = bytearray(self.line_length * self.yres)

    def info(self) -> str:
        return (f"{self.dev}: id='{self.id}'  {self.xres}x{self.yres}"
                f" (virtual {self.xres_virtual}x{self.yres_virtual})"
                f"  bpp={self.bits_per_pixel}  line_length={self.line_length}"
                f"  smem_len={self.smem_len}")

    # ---- mau sac: dung bitfield cua driver, khong gia su thu tu byte ----
    def pixel(self, rgb: tuple[int, int, int]) -> bytes:
        value = 0
        for channel, off in zip(rgb, (BITFIELD_RED, BITFIELD_GREEN, BITFIELD_BLUE)):
            fo, flen = struct.unpack_from("<2I", self.var, off)
            if not flen:
                continue
            scaled = channel >> (8 - flen) if flen < 8 else channel
            value |= (scaled & ((1 << flen) - 1)) << fo
        return struct.pack("<I", value)[:self.bpp]

    def row(self, colors: list[bytes]) -> bytearray:
        line = bytearray(self.line_length)
        pos = 0
        for px in colors:
            line[pos:pos + self.bpp] = px
            pos += self.bpp
        return line

    def put(self, y: int, line) -> None:
        base = y * self.line_length
        self.buf[base:base + self.line_length] = line

    def flush(self) -> None:
        if self.fd < 0:
            return
        os.lseek(self.fd, 0, os.SEEK_SET)
        os.write(self.fd, bytes(self.buf))

    def close(self) -> None:
        if self.fd >= 0:
            os.close(self.fd)
            self.fd = -1

    # ---- pattern ----
    def pattern_solid(self, rgb: tuple[int, int, int]) -> None:
        line = self.row([self.pixel(rgb)] * self.xres)
        for y in range(self.yres):
            self.put(y, line)
        self.flush()

    def pattern_rgb(self) -> None:
        red, green, blue = (self.pixel(c) for c in ((255, 0, 0), (0, 255, 0), (0, 0, 255)))
        third = self.xres // 3
        line = self.row([red if x < third else (green if x < 2 * third else blue)
                         for x in range(self.xres)])
        for y in range(self.yres):
            self.put(y, line)
        self.flush()

    def pattern_checker(self, size: int = 16) -> None:
        on, off = self.pixel((255, 255, 255)), self.pixel((0, 0, 0))
        for y in range(self.yres):
            flip = (y // size) % 2
            self.put(y, self.row([on if ((x // size) % 2) == flip else off
                                  for x in range(self.xres)]))
        self.flush()

    def pattern_grid(self, step: int | None = None) -> None:
        step = step or max(self.xres // 32, 1)
        white, black = self.pixel((255, 255, 255)), self.pixel((0, 0, 0))
        full_white = self.row([white] * self.xres)
        for y in range(self.yres):
            if y % step == 0 or y in (0, self.yres - 1):
                self.put(y, full_white)
            else:
                self.put(y, self.row([white if (x % step == 0 or x in (0, self.xres - 1)) else black
                                      for x in range(self.xres)]))
        self.flush()

    def pattern_dpi(self) -> None:
        self.pattern_solid((255, 255, 255))
        black = self.pixel((0, 0, 0))
        boxes = [(self.xres // 2 - 50, self.yres // 2 - 50), (10, 10),
                 (self.xres - 110, 10), (10, self.yres - 110),
                 (self.xres - 110, self.yres - 110)]
        for y in range(self.yres):
            for ox, oy in boxes:
                if oy <= y < oy + 100:
                    horizontal = y in (oy, oy + 99)
                    xs = range(ox, ox + 100) if horizontal else (ox, ox + 99)
                    for x in xs:
                        if 0 <= x < self.xres:
                            pos = y * self.line_length + x * self.bpp
                            self.buf[pos:pos + self.bpp] = black
        self.flush()
        print(f"      Do thuoc ke: cac hinh vuong canh 100 pixel."
              f" So mm do duoc x 0.254 = inch. Duong cheo = {math.hypot(100, 100):.2f} pixel.")

    def render(self, name: str) -> None:
        if name in SOLID_COLORS:
            self.pattern_solid(SOLID_COLORS[name])
        elif name in GEOMETRY_PATTERNS:
            getattr(self, "pattern_" + name)()
        else:
            raise ValueError(f"pattern khong hop le: {name}")


def suspend_console() -> tuple[int, list] | tuple[None, None]:
    """Tam tat echo cua console de chu khong ve len test pattern."""
    try:
        fd = os.open("/dev/tty", os.O_RDWR)
        return fd, termios.tcgetattr(fd)
    except OSError:
        return None, None


def cmd_pattern(args: argparse.Namespace) -> int:
    try:
        fb = Framebuffer(args.dev)
    except (FileNotFoundError, ValueError, PermissionError, OSError) as exc:
        print(f"Loi: {exc}", file=sys.stderr)
        return 1
    print(fb.info())
    wanted = list(PATTERNS) if args.pattern == "all" else [p.strip() for p in args.pattern.split(",")]
    bad = [p for p in wanted if p not in PATTERNS]
    if bad:
        print(f"Pattern khong hop le: {bad}. Hop le: {list(PATTERNS)}", file=sys.stderr)
        return 2

    tty_fd, tty_state = suspend_console()
    try:
        if tty_fd is not None:
            termios.tcsetattr(tty_fd, termios.TCSANOW, [0, 0, 0, 0, 0, 0, bytes(termios.NCCS)])
        for name in wanted:
            print(f"  [ {name} ] {args.seconds}s - {PATTERN_HINT[name]}", flush=True)
            fb.render(name)
            time.sleep(args.seconds)
        fb.pattern_solid((0, 0, 0))
    finally:
        if tty_fd is not None and tty_state is not None:
            termios.tcsetattr(tty_fd, termios.TCSANOW, tty_state)
            os.close(tty_fd)
        fb.close()
    print("Xong. Neu thay diem bat thuong, chay lai tung pattern de ghi toa do.")
    return 0


# --------------------------------------------------------------------------- #
# Thu thap thong tin he thong
# --------------------------------------------------------------------------- #
def cmd_info(args: argparse.Namespace) -> int:
    hr("1. He thong")
    print(f"Ngay chay : {time.strftime('%Y-%m-%d %H:%M:%S')}")
    print(f"{PROGRAM}   : {VERSION}   Python {sys.version.split()[0]}")
    print(f"Kernel    : {run(['uname', '-r'], root_hint=False)}")
    pretty = ""
    for line in read_text("/etc/os-release").splitlines():
        if line.startswith("PRETTY_NAME="):
            pretty = line.split("=", 1)[1].strip('"')
    print(f"OS        : {pretty}")
    print(f"Model may : {read_text('/sys/class/dmi/id/product_name')} / "
          f"{read_text('/sys/class/dmi/id/product_version')}")

    hr("2. GPU & driver")
    gpu = run(["lspci", "-nnk"])
    if "khong chay duoc" in gpu:
        print("  (can: sudo apt install pciutils)")
    else:
        keep = False
        for line in gpu.splitlines():
            if re.search(r"VGA|3D|Display", line, re.I):
                keep = True
            elif line.strip() == "":
                keep = False
            if keep:
                print("  " + line.rstrip())

    hr("3. Connector (eDP = man hinh laptop)")
    found = False
    for path in sorted(glob.glob("/sys/class/drm/card*-*/")):
        name = os.path.basename(path.rstrip("/"))
        print(f"  {name:<18} status={read_text(path + 'status') or '-':<12} "
              f"enabled={read_text(path + 'enabled') or '-'}")
        found = True
    if not found:
        print("  (khong co connector nao - driver DRM chua nap)")

    hr("4. Do phan giai dang xuat ra")
    virtual = read_text("/sys/class/graphics/fb0/virtual_size")
    if virtual:
        print(f"  fb0 virtual_size : {virtual}")
    modes = read_text("/sys/class/graphics/fb0/modes")
    if modes:
        print(f"  fb0 modes        : {' '.join(modes.splitlines()[:3])}")
    for path in sorted(glob.glob("/sys/class/drm/card*-eDP-*/modes")):
        print(f"  {os.path.basename(os.path.dirname(path))} modes:")
        for line in read_text(path).splitlines():
            print(f"      {line}")

    hr("5. EDID cua panel")
    edids = find_edid_files()
    if not edids:
        print("  Khong doc duoc EDID nao (can quyen root, hoac driver DRM chua nap).")
        print("  Thu:  sudo paneltest edid   |   ls -l /sys/class/drm/   |   sudo dmesg | grep -i edp")
    for path in edids:
        with open(path, "rb") as fh:
            data = fh.read()
        native = edid_native_mode(data) if len(data) >= 128 else None
        print(f"  {path} ({len(data)} byte)")
        if native:
            print(f"      do phan giai goc: {native[0]}x{native[1]} @ {native[2]:.3f} Hz")
        print(f"      giai ma chi tiet: sudo {PROGRAM} edid -f {path}")
    if virtual and native:
        want = f"{native[0]},{native[1]}"
        print(f"\n  SO SANH: fb0 = {virtual}  |  panel goc = {want}  ->  "
              + ("KHOP" if virtual == want else "LECH - man hinh dang bi scale, chu se mo"))

    hr("6. Backlight")
    report_backlight()

    hr("7. Loi dang chu y trong dmesg")
    report_dmesg(limit=8)
    print("\nBuoc tiep theo: sudo paneltest edid   |   sudo paneltest pattern")
    return 0


def report_backlight() -> None:
    paths = sorted(glob.glob("/sys/class/backlight/*/"))
    if not paths:
        print("  Khong co /sys/class/backlight -> driver backlight chua nap.")
        print("  Khac phuc: sudo apt install linux-modules-extra-$(uname -r) && sudo reboot")
        return
    for path in paths:
        name = os.path.basename(path.rstrip("/"))
        actual = read_text(path + "actual_brightness")
        maximum = read_text(path + "max_brightness")
        kind = read_text(path + "type")
        print(f"  {name}: actual={actual} / max={maximum} type={kind}")
        print(f"      dieu chinh: echo <0..{maximum}> | sudo tee {path}brightness")


def report_dmesg(limit: int = 20) -> None:
    out = run(["dmesg"])
    if "khong chay duoc" in out:
        print("  (can quyen root: sudo dmesg | grep -i edp)")
        return
    interesting = re.compile(r"error|fail|warn|timeout|link training|aux", re.I)
    scope = re.compile(r"drm|edp|panel|i915|amdgpu|nouveau|backlight", re.I)
    hits = [l for l in out.splitlines() if interesting.search(l) and scope.search(l)]
    if not hits:
        print("  Khong thay loi lien quan DRM/eDP.")
    for line in hits[-limit:]:
        print("  " + line.strip())


def cmd_edid(args: argparse.Namespace) -> int:
    if args.file:
        try:
            with open(args.file, "rb") as fh:
                data = fh.read()
        except OSError as exc:
            print(f"Loi doc {args.file}: {exc}", file=sys.stderr)
            return 1
        edid_report(data, args.file)
        return 0

    sources = find_edid_files()
    if not sources:
        print("Khong tim thay EDID trong /sys/class/drm/. Kiem tra:", file=sys.stderr)
        print("  ls -l /sys/class/drm/          (rong -> driver DRM chua nap)", file=sys.stderr)
        print("  sudo dmesg | grep -i edp       (link training fail -> cap eDP long)", file=sys.stderr)
        return 1
    for path in sources:
        with open(path, "rb") as fh:
            data = fh.read()
        edid_report(data, path)
        print()
    return 0


def cmd_backlight(_args: argparse.Namespace) -> int:
    hr("Backlight / do sang")
    report_backlight()
    return 0


def cmd_dmesg(args: argparse.Namespace) -> int:
    hr("Loi DRM / eDP trong dmesg")
    report_dmesg(limit=args.limit)
    return 0


# --------------------------------------------------------------------------- #
# Selftest - chay duoc tren may khong co man hinh
# --------------------------------------------------------------------------- #
def build_test_edid() -> bytes:
    """EDID 1.4 gia lap: BOE NV140FHM-N61, 1920x1080 @ 60.000 Hz."""
    def manuf(code: str) -> bytes:
        value = 0
        for ch in code:
            value = (value << 5) | (ord(ch) - ord("A") + 1)
        return value.to_bytes(2, "big")

    b = bytearray(128)
    b[0:8] = EDID_HEADER
    b[8:10] = manuf("BOE")
    b[10:12] = (0x0A5D).to_bytes(2, "little")
    b[12:16] = (0x1234).to_bytes(4, "little")
    b[16], b[17] = 39, 34                       # tuan 39, nam 1990+34 = 2024
    b[18], b[19] = 1, 4
    b[20] = 0xA5                                # digital, 8 bpc
    b[21], b[22] = 31, 17                       # ~13.9 inch
    b[23] = 120                                 # gamma 2.2
    b[35], b[36] = 0, 0x80
    # DTD #1: 1920x1080 @ 60.000 Hz, pixel clock 148.50 MHz (dung thu tu byte VESA)
    pixel_clock, hactive, hblank, vactive, vblank = 14850, 1920, 280, 1080, 45
    b[54] = pixel_clock & 0xFF
    b[55] = (pixel_clock >> 8) & 0xFF
    b[56] = hactive & 0xFF                      # byte 2 = h-active thap
    b[57] = hblank & 0xFF                       # byte 3 = h-blanking thap
    b[58] = ((hactive >> 8) & 0xF) << 4 | ((hblank >> 8) & 0xF)
    b[59] = vactive & 0xFF                      # byte 5 = v-active thap
    b[60] = vblank & 0xFF                       # byte 6 = v-blanking thap
    b[61] = ((vactive >> 8) & 0xF) << 4 | ((vblank >> 8) & 0xF)
    b[62], b[63], b[64] = 88, 4, 0x40
    b[66], b[67] = 255, 143
    b[70] = 0x18
    # DTD #2: ten panel
    name_block = bytearray(18)
    name_block[3] = 0xFC
    name = b"NV140FHM-N61"
    name_block[5:5 + len(name)] = name
    b[72:90] = name_block
    # DTD #4: serial
    serial_block = bytearray(18)
    serial_block[3] = 0xFF
    serial = b"ABCD123456789"
    serial_block[5:5 + len(serial)] = serial
    b[108:126] = serial_block
    b[127] = (256 - (sum(b[:127]) % 256)) % 256
    return bytes(b)


def build_test_var(xres: int, yres: int, bpp: int, fields: list[tuple[int, int]]) -> bytes:
    var = bytearray(160)
    struct.pack_into("<8I", var, 0, xres, yres, xres, yres, 0, 0, bpp, 0)
    for index, (offset, length) in enumerate(fields):
        struct.pack_into("<2I", var, 32 + 16 * index, offset, length)
    return bytes(var)


def build_test_fix(name: str, line_length: int, smem_len: int) -> bytes:
    fix = bytearray(80)
    fix[0:FIX_ID_LEN] = name.encode()[:FIX_ID_LEN]
    struct.pack_into("<2I", fix, FIX_SMEM_OFF, 0, smem_len)
    struct.pack_into("<I", fix, FIX_LINE_LENGTH_OFF, line_length)
    return bytes(fix)


def selftest_edid() -> list[str]:
    failures = []
    data = build_test_edid()
    checks = [
        ("checksum", edid_checksum_ok(data), True),
        ("header", data[:8] == EDID_HEADER, True),
        ("manufacturer", edid_manufacturer(data), "BOE"),
        ("native mode", edid_native_mode(data), (1920, 1080, 60.0)),
        ("color depth", edid_color_depth(0xA5), "8 bit/kenh"),
        ("monitor name", edid_parse_descriptor(data, 72), "      [Monitor name] NV140FHM-N61"),
    ]
    for label, got, want in checks:
        ok = got == want
        print(f"    {'PASS' if ok else 'FAIL'}  {label}: {got!r}")
        if not ok:
            failures.append(f"{label} = {got!r}, mong doi {want!r}")
    return failures


def selftest_framebuffer() -> list[str]:
    failures = []
    cases = [
        # (ten, var, fix, line_length, xres, yres, bpp, {rgb: gia tri pixel})
        ("800x600 BGRA32 (co stride padding)",
         build_test_var(800, 600, 32, [(16, 8), (8, 8), (0, 8)]),
         build_test_fix("i915drmfb", 3232, 3232 * 600), 3232, 800, 600, 4,
         {(255, 0, 0): 0x00FF0000, (0, 255, 0): 0x0000FF00, (0, 0, 255): 0x000000FF,
          (255, 255, 255): 0x00FFFFFF, (0, 0, 0): 0}),
        ("1366x768 RGB565 (16bpp)",
         build_test_var(1366, 768, 16, [(11, 5), (5, 6), (0, 5)]),
         build_test_fix("amdgpu", 2732, 2732 * 768), 2732, 1366, 768, 2,
         {(255, 0, 0): 0xF800, (0, 255, 0): 0x07E0, (0, 0, 255): 0x001F,
          (255, 255, 255): 0xFFFF}),
        ("1920x1080 RGBA32",
         build_test_var(1920, 1080, 32, [(0, 8), (8, 8), (16, 8)]),
         build_test_fix("nouveaufb", 7680, 7680 * 1080), 7680, 1920, 1080, 4,
         {(255, 0, 0): 0x000000FF, (0, 0, 255): 0x00FF0000}),
    ]
    for name, var, fix, line_length, xres, yres, bpp, expect in cases:
        fb = Framebuffer._from_structs(var, fix)
        captured = {}
        fb.flush = lambda: captured.update(data=bytes(fb.buf))
        geometry_ok = (fb.xres == xres and fb.yres == yres and fb.line_length == line_length)
        print(f"    {'PASS' if geometry_ok else 'FAIL'}  {name}: {fb.info()}")
        if not geometry_ok:
            failures.append(f"{name}: geometry sai")
            continue
        for rgb, want in expect.items():
            fb.pattern_solid(rgb)
            blob = captured["data"]
            fmt = "<H" if bpp == 2 else "<I"
            got = struct.unpack_from(fmt, blob, 0)[0]
            last_col = struct.unpack_from(fmt, blob, (xres - 1) * bpp)[0]
            last_row = struct.unpack_from(fmt, blob, (yres - 1) * line_length)[0]
            ok = (got == want == last_col == last_row and len(blob) == line_length * yres)
            print(f"      {'PASS' if ok else 'FAIL'}  solid{rgb} = 0x{got:0{bpp * 2}X}"
                  f" (mong doi 0x{want:0{bpp * 2}X}), {len(blob)} byte")
            if not ok:
                failures.append(f"{name} solid{rgb} = 0x{got:X}, mong doi 0x{want:X}")
        for pattern in GEOMETRY_PATTERNS:
            fb.render(pattern)
            blob = captured["data"]
            colors = len({bytes(blob[i:i + bpp]) for i in range(0, len(blob), bpp)})
            ok = len(blob) == line_length * yres and colors >= 2
            print(f"      {'PASS' if ok else 'FAIL'}  pattern_{pattern}: {len(blob)} byte, {colors} mau")
            if not ok:
                failures.append(f"{name} pattern_{pattern}")
    return failures


def cmd_selftest(_args: argparse.Namespace) -> int:
    hr("Selftest - kiem tra phan giai ma EDID va to hop pixel (khong can phan cung)")
    failures = selftest_edid() + selftest_framebuffer()
    print("-" * 72)
    if failures:
        print(f"KET QUA: {len(failures)} loi")
        for item in failures:
            print(f"  - {item}")
        return 1
    print("KET QUA: TAT CA PASS")
    print("Luu y: selftest khong the kiem tra phan cung that. Chay 'sudo paneltest info' tren laptop.")
    return 0


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #
def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog=PROGRAM,
        description="Kiem tra man hinh laptop (eDP) tren Ubuntu Server, khong can do hoa.",
        epilog="Vi du: sudo paneltest info | sudo paneltest edid | sudo paneltest pattern -p black -t 20",
    )
    parser.add_argument("--version", action="version", version=f"{PROGRAM} {VERSION}")
    sub = parser.add_subparsers(dest="command")

    sub.add_parser("info", help="tong hop thong tin panel, driver, mode, backlight, loi").set_defaults(
        func=cmd_info)

    p_edid = sub.add_parser("edid", help="giai ma EDID cua panel")
    p_edid.add_argument("-f", "--file", help="file EDID .bin (mac dinh: doc tu /sys/class/drm)")
    p_edid.set_defaults(func=cmd_edid)

    p_pat = sub.add_parser("pattern", help="hien test pattern de tim diem chet / ho quang / sai mau")
    p_pat.add_argument("-p", "--pattern", default="all",
                       help="ten pattern, phan cach bang dau phay, hoac 'all' (mac dinh)")
    p_pat.add_argument("-t", "--seconds", type=float, default=4.0, help="so giay moi pattern")
    p_pat.add_argument("--dev", default="/dev/fb0", help="thiet bi framebuffer (mac dinh /dev/fb0)")
    p_pat.add_argument("--list", action="store_true", help="chua chay gi, chi liet ke pattern")
    p_pat.set_defaults(func=cmd_pattern)

    sub.add_parser("backlight", help="kiem tra den nen va do sang").set_defaults(func=cmd_backlight)

    p_dm = sub.add_parser("dmesg", help="loc loi DRM/eDP trong dmesg")
    p_dm.add_argument("-n", "--limit", type=int, default=20, help="so dong toi da (mac dinh 20)")
    p_dm.set_defaults(func=cmd_dmesg)

    sub.add_parser("selftest", help="tu kiem tra phan giai ma, khong can phan cung").set_defaults(
        func=cmd_selftest)
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if not getattr(args, "command", None):
        parser.print_help()
        return 0
    if args.command == "pattern" and args.list:
        for name in PATTERNS:
            print(f"{name:<10} {PATTERN_HINT[name]}")
        return 0
    try:
        return args.func(args)
    except KeyboardInterrupt:
        print("\nDa huy.", file=sys.stderr)
        return 130


if __name__ == "__main__":
    sys.exit(main())
