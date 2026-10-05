# paneltest — kiểm tra màn hình laptop vừa thay trên Ubuntu Server

Một file Python duy nhất, **không phụ thuộc package nào** ngoài thư viện chuẩn Python 3.8+.
Dùng để nghiệm thu panel laptop (eDP) sau khi thay: kiểm tra panel có được nhận đúng không,
độ phân giải có đúng native không, tìm điểm chết / hở sáng / sai màu / sai tỉ lệ,
và kiểm tra đèn nền.

Ubuntu Server không có X/Wayland nên tool làm việc trực tiếp với **kernel DRM + framebuffer**,
không cần cài desktop.

```
sudo paneltest info        # tổng hợp: GPU, connector, mode, EDID, backlight, lỗi dmesg
sudo paneltest edid        # giải mã EDID: model panel, kích thước, độ phân giải gốc, checksum
sudo paneltest pattern     # test pattern full màn hình qua /dev/fb0
sudo paneltest backlight   # đèn nền / độ sáng
sudo paneltest dmesg       # lỗi driver DRM/eDP
paneltest selftest         # tự kiểm tra phần giải mã (chạy được cả khi không có phần cứng)
```

> Chạy **trực tiếp trên bàn phím laptop**, không qua SSH — màn hình cần kiểm tra chính là
> màn hình của máy đó.

---

## Cài đặt

```bash
git clone https://github.com/<user>/paneltest.git
cd paneltest
sudo make install          # copy vào /usr/local/bin/paneltest
paneltest selftest
```

Không muốn cài thì chạy thẳng:

```bash
python3 src/paneltest/paneltest.py info
```

Hoặc chỉ cần tải 1 file:

```bash
curl -fsSL https://raw.githubusercontent.com/<user>/paneltest/main/src/paneltest/paneltest.py \
  -o paneltest.py && chmod +x paneltest.py
sudo python3 paneltest.py info
```

---

## Quy trình nghiệm thu 6 bước

### 1. Panel có được nhận đúng không (EDID)

EDID là "lý lịch" panel tự khai với máy. Đây là bước quan trọng nhất sau khi thay màn hình.

```bash
sudo paneltest edid
```

```
Nha san xuat    : BOE   (product code 2653)
Ngay SX         : tuan 39 / 2024   <-- so voi ngay ban mua panel
Kich thuoc EDID : 31 cm x 17 cm  (~13.9 inch cheo)   <-- so voi thong so panel ban thay
    DTD #1:
      1920x1080 @ 60.000 Hz  (pixel clock 148.500 MHz)
    DTD #2:
      [Monitor name] NV140FHM-N61

DO PHAN GIAI GOC: 1920x1080 @ 60.000 Hz   <-- Ubuntu phai chay dung con so nay
```

Đối chiếu với phiếu mua / tem panel:

| Trường | Nếu sai thì |
|---|---|
| `Monitor name` khớp mã panel đã mua (`NV140FHM-N61`, `B140HAN01.1`, `LP140WH8`…) | sai loại panel |
| Kích thước inch khớp | sai panel hoặc cáp nối sai |
| `Checksum block 1: OK` | cáp eDP tiếp xúc kém — gắn lại |
| `DTD #1` = độ phân giải gốc | nếu khác, hình sẽ mờ vì đang bị scale |

### 2. Độ phân giải đang chạy có đúng native không

```bash
sudo paneltest info
```

Cuối mục 5 tool tự so sánh và in `KHOP` / `LECH`. Nếu lệch, tạo mode đúng bằng `xrandr`
(`sudo apt install x11-xserver-utils`) hoặc thêm tham số kernel `video=eDP-1:1920x1080@60`
vào GRUB.

### 3. Test pattern

```bash
sudo paneltest pattern                       # chạy hết, 4 giây mỗi pattern
sudo paneltest pattern -p black -t 20        # tắt đèn phòng, tìm điểm chết sáng
sudo paneltest pattern -p gray25 -t 20       # nhạy nhất để thấy clouding
sudo paneltest pattern -p dpi                # đo thước kẻ, kiểm tra kích thước vật lý
sudo paneltest pattern --list                # liệt kê 12 pattern kèm cách đọc
```

| Pattern | Nhìn gì | Bất thường nghĩa là |
|---|---|---|
| `black` (tắt đèn phòng) | chấm sáng li ti cố định | điểm chết sáng / stuck pixel |
| `white` | chấm đen, vệt tối, mép sáng hơn giữa | điểm chết tối, **hở sáng (backlight bleed)** |
| `gray`, `gray25`, `gray75` | vệt loang, vùng sáng tối | clouding — lỗi phổ biến của panel lô |
| `red` `green` `blue` | màu đúng không, chấm màu khác | sub-pixel chết hoặc kênh màu lỗi |
| `rgb` | thứ tự đỏ–lục–lam trái sang phải | sai mapping màu |
| `grid` | đường kẻ thẳng, đủ số | hàng/cột pixel chết, ảnh méo |
| `checker` | ô 16×16 px có vuông không | ảnh đang bị scale sai tỉ lệ |
| `dpi` | đo cạnh hình vuông 100 px bằng thước | mm đo được × 0.254 = inch → so thông số panel |

Mẹo bảo hành: dùng lưới `checker` (16 px/ô) làm thước để ghi toạ độ điểm chết, ví dụ
"3 chấm ở góc trái trên".

### 4. Đèn nền

```bash
sudo paneltest backlight
```

```
intel_backlight: actual=450 / max=937 type=raw
    dieu chinh: echo <0..937> | sudo tee /sys/class/backlight/intel_backlight/brightness
```

Nếu báo **không có `/sys/class/backlight`** → driver backlight chưa nạp, lỗi rất hay gặp
trên Ubuntu Server tối giản. Khắc phục:

```bash
sudo apt install linux-modules-extra-$(uname -r) && sudo reboot
```

Kiểm tra: tăng/giảm từ tối đến sáng, nhìn nghiêng ở nền đen. Hở sáng nhẹ ở mép là bình
thường; mép sáng trắng rõ hoặc quầng vàng → panel/keo viền kém, nên đổi hàng. Nhấp nháy
rõ ở độ sáng thấp → PWM tần số thấp hoặc đèn nền/cáp lỗi.

### 5. Lỗi ở tầng driver

```bash
sudo paneltest dmesg
```

- `link training failed`, `AUX channel timeout` → **cáp eDP gắn chưa chặt / cáp lỗi** (rất hay gặp khi vừa thay màn hình). Gắn lại, kiểm tra lẫy khoá.
- `failed to read EDID` → tiếp xúc chân cáp kém.
- Không có dòng lỗi + bước 1 ra đúng thông số = kết nối tốt.

### 6. Nếu máy không có `/dev/fb0`

Một số cấu hình driver (đặc biệt `amdgpu`) không tạo framebuffer:

```bash
ls -l /dev/fb0
sudo apt install libdrm-tests
sudo modetest -M amdgpu            # xem connector + mode
```

Hoặc mở `tools/test-man-hinh.html` bằng trình duyệt trên máy khác (13 chế độ, phím 1–8,
C, G, mũi tên, F) rồi đối chiếu với màn hình laptop.

---

## Lỗi thường gặp sau khi thay màn hình

| Hiện tượng | Nguyên nhân thường gặp |
|---|---|
| Không lên hình, dmesg báo link training fail | cáp eDP chưa vào hết lẫy, hoặc cáp bị gấp |
| Sai độ phân giải, chữ mờ | chưa đúng mode native, hoặc panel không đúng loại |
| Sọc dọc/ngang, nhiễu hạt | cáp eDP tiếp xúc kém, hoặc panel lỗi |
| Ảnh âm bản / màu lạ | sai loại panel (LVDS vs eDP, khác channel) |
| Rất tối dù max brightness | cháy cầu chì đèn nền trên mainboard (thay màn hình lúc chưa ngắt pin) |
| Sáng nửa màn / loang một góc | đèn nền LED hoặc tấm dẫn sáng hỏng khi lắp |
| Chạm mép thì nhiễu | cáp bị kẹp dưới khung viền |

> **An toàn:** luôn tháo pin / ngắt nguồn trước khi thao tác cáp eDP. Cháy cầu chì đèn
> nền là lỗi tốn tiền nhất khi thay màn hình laptop.

---

## Danh sách nghiệm thu

```
[ ] EDID đọc được, checksum OK
[ ] Model panel + kích thước inch khớp hàng đã mua
[ ] Độ phân giải đang chạy = độ phân giải gốc
[ ] Nền đen (tắt đèn phòng): không có điểm sáng cố định
[ ] Nền trắng: không có điểm đen, không vết bẩn/loang
[ ] Nền xám: không loang màu, không clouding nặng
[ ] Đủ 3 màu đỏ/lục/lam, đúng thứ tự, không ám màu
[ ] Lưới thẳng, không mất hàng/cột pixel, đủ 4 viền
[ ] Ô vuông 100 px đo đúng kích thước vật lý
[ ] Backlight chỉnh được từ tối đến sáng, không nhấp nháy bất thường
[ ] dmesg không có lỗi link training / AUX / EDID
[ ] Gõ nhẹ/lắc nhẹ nắp máy: hình không nhiễu
```

---

## Cấu trúc repo

```
paneltest/
├── Makefile
├── install.sh
├── pyproject.toml              # pip install . (tuỳ chọn)
├── src/paneltest/paneltest.py  # TOÀN BỘ chương trình nằm ở 1 file này
└── tools/
    └── test-man-hinh.html      # bản dự phòng chạy trên trình duyệt, không cần cài gì
```

## Kiểm thử

```bash
make test        # hoặc: python3 src/paneltest/paneltest.py selftest
```

`selftest` dựng EDID 1.4 giả lập và 3 cấu hình framebuffer (BGRA32 có stride padding,
RGB565 16bpp, RGBA32) rồi kiểm tra bộ giải mã EDID, cách tính geometry và cách tổ hợp
pixel theo bitfield — chạy được trên máy không có màn hình. **Selftest không thay được
kiểm tra trên phần cứng thật.**

## Ghi chú kỹ thuật

- Đọc `struct fb_fix_screeninfo.line_length` ở **offset 60** (offset 16 là `smem_start`/`smem_len`) — stride có thể lớn hơn `xres * bpp` do padding.
- Tổ hợp màu theo `fb_bitfield` tại offset **32 / 48 / 64** của `fb_var_screeninfo` (mỗi bitfield 16 byte), không giả sử thứ tự byte BGRA/RGBA.
- Hỗ trợ 16bpp (RGB565) và 32bpp.
- EDID: giải mã DTD theo thứ tự byte VESA (byte 2 = h-active thấp, byte 3 = h-blanking thấp, byte 5 = v-active thấp, byte 6 = v-blanking thấp).

## Giấy phép

MIT — xem [LICENSE](LICENSE).
