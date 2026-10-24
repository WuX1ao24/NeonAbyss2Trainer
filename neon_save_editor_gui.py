# -*- coding: utf-8 -*-
"""
《霓虹深渊2》修改器 —— 图形界面版（现代化圆角美化版）
=============================================================
无边框深色圆角主题（#1E1E24 / #282830 / #E0E0E0 / #FF9800），1000x600。
  顶部标题栏：可拖动；右上角 简体/繁体/English（仅 UI 展示）+ 最小化 + X 关闭。
  主体左右分栏：
    左侧 280px 游戏信息区（3:4 真实封面图 cover.png，240x320 圆角 10px，
      缺失时深灰圆角占位写「封面图缺失」+ 进程名/动态 PID/开发者 + 版本 + Check for updates）；
    右侧 Hotkeys / Options 两列功能列表（Toggle 开关 / 数值调节框，
      可用功能完整保留，受限项不重复展示）。
  双页签（ttk.Notebook）：页1「游戏内修改」（默认）+ 页2「存档修改」（原离线存档功能完整保留）。

窗口圆角：Win11 物理圆角 DwmSetWindowAttribute(DWMWA_WINDOW_CORNER_PREFERENCE)；
  系统不支持时用 Canvas 深色圆角背景兜底。最小化按钮：overrideredirect(False)+iconify，
  <Map> 事件恢复 overrideredirect(True)。

运行方式
--------
  python neon_save_editor_gui.py            # 启动图形界面（默认停在游戏内修改页）
  python neon_save_editor_gui.py --selftest # 自检：创建窗口后立即退出

作者：Marvis File Agent
"""

import os
import sys
import json
import struct
import shutil
import glob
import datetime
import argparse
import threading
import tkinter as tk
from tkinter import ttk, filedialog
from tkinter import BooleanVar, StringVar

try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass

# ============================== 内存模块 ==============================
from neon_memory import NeonProcess, TrainerEngine, CHEATS, CheatSpec

# ============================== 核心常量（存档） ==============================
SAVE_KEY = b"8G$!kL9@qP4*Zx3#rT"
DLPN_MAGIC = b"DLPN"
GAME_DIR_CANDIDATES = [
    r"D:\Steam\steamapps\common\Neon Abyss 2",
    r"C:\Program Files (x86)\Steam\steamapps\common\Neon Abyss 2",
    r"C:\Program Files\Steam\steamapps\common\Neon Abyss 2",
    r"E:\Steam\steamapps\common\Neon Abyss 2",
]

# ============================== XXTEA（核心逻辑，原样保留） ==============================

def _to_uint32(data):
    n = (len(data) + 3) // 4
    v = [0] * n
    for i in range(n):
        idx = i * 4
        v[i] = int.from_bytes(data[idx:idx + 4].ljust(4, b"\x00"), "little") & 0xFFFFFFFF
    return v


def _mx(sumv, y, z, p, e, k):
    return (((z >> 5 ^ y << 2) + (y >> 3 ^ z << 4)) ^ ((sumv ^ y) + (k[(p & 3) ^ e] ^ z))) & 0xFFFFFFFF


def xxtea_encrypt(data, key):
    kb = key[:16].ljust(16, b"\x00")
    k = [int.from_bytes(kb[i * 4:(i + 1) * 4], "little") for i in range(4)]
    length = len(data)
    n = (length + 3) // 4 + 1
    v = [0] * n
    for i in range(n - 1):
        idx = i * 4
        v[i] = int.from_bytes(data[idx:idx + 4].ljust(4, b"\x00"), "little") & 0xFFFFFFFF
    v[n - 1] = length
    delta = 0x9E3779B9
    q = 6 + 52 // n
    sumv = 0
    z = v[n - 1]
    y = v[0]
    for _ in range(q):
        sumv = (sumv + delta) & 0xFFFFFFFF
        e = (sumv >> 2) & 3
        for p in range(n - 1):
            y = v[p + 1]
            v[p] = (v[p] + _mx(sumv, y, z, p, e, k)) & 0xFFFFFFFF
            z = v[p]
        y = v[0]
        v[n - 1] = (v[n - 1] + _mx(sumv, y, z, n - 1, e, k)) & 0xFFFFFFFF
        z = v[n - 1]
    out = bytearray()
    for i in range(n):
        out += struct.pack("<I", v[i] & 0xFFFFFFFF)
    return bytes(out)


def xxtea_decrypt(data, key):
    kb = key[:16].ljust(16, b"\x00")
    k = [int.from_bytes(kb[i * 4:(i + 1) * 4], "little") for i in range(4)]
    v = _to_uint32(data)
    n = len(v)
    if n < 2:
        return b""
    delta = 0x9E3779B9
    q = 6 + 52 // n
    y = v[0]
    z = v[n - 1]
    sumv = (q * delta) & 0xFFFFFFFF
    while sumv != 0:
        e = (sumv >> 2) & 3
        for p in range(n - 1, 0, -1):
            z = v[p - 1]
            v[p] = (v[p] - _mx(sumv, y, z, p, e, k)) & 0xFFFFFFFF
            y = v[p]
        z = v[n - 1]
        v[0] = (v[0] - _mx(sumv, y, z, 0, e, k)) & 0xFFFFFFFF
        y = v[0]
        sumv = (sumv - delta) & 0xFFFFFFFF
    ln = v[n - 1]
    out = bytearray()
    for i in range(n - 1):
        out += struct.pack("<I", v[i] & 0xFFFFFFFF)
    return bytes(out[:ln])


# ============================== DLPN 容器（核心逻辑，原样保留） ==============================

class SaveEntry:
    def __init__(self, key_bytes, type_id, value_bytes):
        self.key_bytes = key_bytes
        self.type_id = type_id
        self.value_bytes = value_bytes

    @property
    def key(self):
        return self.key_bytes.rstrip(b"\x00").decode("utf-8", "replace")

    def json_value(self):
        try:
            return json.loads(self.value_bytes.decode("utf-8"))
        except Exception:
            return None

    def set_json(self, obj):
        self.value_bytes = json.dumps(obj, ensure_ascii=False, separators=(",", ":")).encode("utf-8")


def parse_dlpn(data):
    if not data.startswith(DLPN_MAGIC):
        raise ValueError("不是 DLPN 容器")
    pos = 4
    version, cnt = struct.unpack_from("<II", data, pos)
    pos += 8
    entries = []
    for _ in range(cnt):
        klen = struct.unpack_from("<I", data, pos)[0]
        pos += 4
        key_bytes = data[pos:pos + klen]
        pos += klen
        type_id = struct.unpack_from("<I", data, pos)[0]
        pos += 4
        vlen = struct.unpack_from("<I", data, pos)[0]
        pos += 4
        value_bytes = data[pos:pos + vlen]
        pos += vlen
        entries.append(SaveEntry(key_bytes, type_id, value_bytes))
    return version, entries


def build_dlpn(version, entries):
    out = bytearray(DLPN_MAGIC)
    out += struct.pack("<II", version, len(entries))
    for e in entries:
        out += struct.pack("<I", len(e.key_bytes))
        out += e.key_bytes
        out += struct.pack("<I", e.type_id)
        out += struct.pack("<I", len(e.value_bytes))
        out += e.value_bytes
    return bytes(out)


# ============================== 存档文件（核心逻辑，原样保留） ==============================

def find_saves_dir(explicit=None):
    if explicit:
        d = os.path.join(explicit, "SavesDir") if not explicit.endswith("SavesDir") else explicit
        if os.path.isdir(d):
            return d
        if os.path.isdir(explicit):
            return explicit
        return None
    here = os.path.dirname(os.path.abspath(__file__))
    if os.path.isdir(os.path.join(here, "SavesDir")):
        return os.path.join(here, "SavesDir")
    for gd in GAME_DIR_CANDIDATES:
        d = os.path.join(gd, "SavesDir")
        if os.path.isdir(d):
            return d
    return None


def list_slots(saves_dir):
    owners = sorted(glob.glob(os.path.join(saves_dir, "*_*.owner.sav")))
    return [os.path.basename(p) for p in owners]


def decrypt_owner_file(path):
    raw = open(path, "rb").read()
    plain = xxtea_decrypt(raw, SAVE_KEY)
    if not plain.startswith(DLPN_MAGIC):
        raise ValueError("解密结果不是 DLPN 容器，请检查存档路径")
    version, entries = parse_dlpn(plain)
    return version, entries


def encrypt_owner_file(version, entries):
    plain = build_dlpn(version, entries)
    return xxtea_encrypt(plain, SAVE_KEY)


def backup_file(path):
    ts = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
    bak = path + f".bak_{ts}"
    shutil.copy2(path, bak)
    return bak


def write_owner_file(path, version, entries, do_backup=True):
    bak = backup_file(path) if do_backup else None
    cipher = encrypt_owner_file(version, entries)
    with open(path, "wb") as f:
        f.write(cipher)
    v2, e2 = decrypt_owner_file(path)
    if v2 != version or len(e2) != len(entries):
        raise RuntimeError("写回后校验失败！备份保留在：" + str(bak))
    return bak


def get_entry(entries, key):
    for e in entries:
        if e.key == key:
            return e
    return None


def load_current_values(entries):
    v = {}
    sd = get_entry(entries, "safe-deposit.resources")
    if sd and sd.json_value():
        d = sd.json_value()
        v["coin"] = d.get("coin", 0)
        v["bomb"] = d.get("bomb", 0)
        v["key"] = d.get("key", 0)
        v["egg"] = d.get("egg", 0)
    else:
        v.update(coin=0, bomb=0, key=0, egg=0)
    tb = get_entry(entries, "token.balance")
    if tb and tb.json_value():
        toks = tb.json_value().get("entries", [])
        v["token"] = toks[0]["value"] if toks else 0
    else:
        v["token"] = 0
    ev = get_entry(entries, "evolution.progression")
    if ev and ev.json_value():
        d = ev.json_value()
        v["level"] = d.get("level", 1)
        v["exp"] = d.get("currentExperience", 0)
    else:
        v["level"] = 1
        v["exp"] = 0
    cu = get_entry(entries, "profile.character-unlock")
    v["char_ids"] = list(cu.json_value().get("ids", [])) if cu and cu.json_value() else []
    pu = get_entry(entries, "progression.content-unlock")
    v["content_ids"] = list(pu.json_value().get("ids", [])) if pu and pu.json_value() else []
    return v


def apply_resource(entries, coin, bomb, key, egg):
    sd = get_entry(entries, "safe-deposit.resources")
    if sd is None:
        return False, "存档中没有 safe-deposit.resources 键，无法修改保险库资源"
    d = sd.json_value()
    if not isinstance(d, dict):
        return False, "保险库资源数据格式异常，未修改"
    d["coin"] = max(0, int(coin))
    d["bomb"] = max(0, int(bomb))
    d["key"] = max(0, int(key))
    d["egg"] = max(0, int(egg))
    sd.set_json(d)
    return True, "保险库资源已更新"


def apply_token(entries, token):
    tb = get_entry(entries, "token.balance")
    if tb is None:
        return False, "存档中没有 token.balance 键，无法修改霓虹币"
    d = tb.json_value()
    if not isinstance(d, dict):
        return False, "霓虹币数据格式异常，未修改"
    toks = d.setdefault("entries", [])
    if toks:
        toks[0]["value"] = max(0, int(token))
    else:
        toks.append({"key": 1, "value": max(0, int(token))})
    tb.set_json(d)
    return True, "霓虹币 Token 已更新"


def apply_evolution(entries, level, exp):
    ev = get_entry(entries, "evolution.progression")
    if ev is None:
        return False, "存档中没有 evolution.progression 键，无法修改进化"
    d = ev.json_value()
    if not isinstance(d, dict):
        return False, "进化数据格式异常，未修改"
    d["level"] = max(1, int(level))
    d["currentExperience"] = max(0, int(exp))
    ev.set_json(d)
    return True, "进化等级/经验已更新"


def apply_append_ids(entries, key, ids_text, name):
    en = get_entry(entries, key)
    if en is None:
        return False, f"存档中没有 {key} 键，无法解锁{name}"
    d = en.json_value()
    if not isinstance(d, dict):
        return False, f"{name}数据格式异常，未修改"
    ids = d.setdefault("ids", [])
    added = []
    for part in ids_text.replace("，", ",").split(","):
        part = part.strip()
        if not part:
            continue
        try:
            val = int(part)
        except ValueError:
            continue
        if val not in ids:
            ids.append(val)
            added.append(val)
    if not added:
        return False, f"没有新增{name}ID（输入为空或都已存在）"
    en.set_json(d)
    return True, f"已追加 {len(added)} 个{name}ID"


# ============================== 主题（最终视觉精修配色规范） ==============================
COL_BG          = "#1A1A1A"   # 主背景
COL_BG2         = "#242424"   # 行卡片/面板背景（兼容旧名）
COL_PANEL       = COL_BG2     # 面板背景
COL_PANEL_ALT   = "#282828"   # 列表行交替色
COL_PANEL_HOVER = "#303030"   # 列表行悬停背景
COL_ENTRY       = "#151515"   # 输入框背景（内凹深黑）
COL_BORDER      = "#333333"   # 行边框/分隔线
COL_BTN         = "#3A3A3A"   # 普通按钮底色（深灰）
COL_DISABLED    = "#444444"   # 禁用按钮背景/文字变暗
COL_ORANGE      = "#FF9800"   # 高亮橙
COL_GOLD        = COL_ORANGE  # 兼容旧名（存档页）
COL_GOLD_D      = "#B8660A"
COL_TEXT        = "#E0E0E0"   # 浅灰文字
COL_TEXT_W      = "#FFFFFF"   # 白色文字
COL_DIM         = "#999999"   # 次要灰
COL_TIP         = "#888888"   # 存档页灰色小字
COL_OK          = "#7CFC00"
COL_ERR         = "#FF5252"
COL_LOCK        = "#4FC3F7"
FONT        = ("Microsoft YaHei UI", 10)
FONT_B      = ("Microsoft YaHei UI", 10, "bold")
FONT_TITLE  = ("Microsoft YaHei UI", 14, "bold")
FONT_SUB    = ("Microsoft YaHei UI", 12, "bold")
FONT_SMALL  = ("Microsoft YaHei UI", 9)
FONT_TINY   = ("Microsoft YaHei UI", 8)


# ============================== 通用绘制辅助 ==============================

def _round_rect(cv, x1, y1, x2, y2, r, **kw):
    """在 Canvas 上画圆角矩形（返回 item id）。"""
    points = [
        x1 + r, y1, x2 - r, y1, x2, y1, x2, y1 + r,
        x2, y2 - r, x2, y2, x2 - r, y2, x1 + r, y2,
        x1, y2, x1, y2 - r, x1, y1 + r, x1, y1,
    ]
    return cv.create_polygon(points, smooth=True, **kw)


def _brighten(hex_color, amt):
    """颜色提亮 amt（0-255）。"""
    h = hex_color.lstrip("#")
    r = min(255, int(h[0:2], 16) + amt)
    g = min(255, int(h[2:4], 16) + amt)
    b = min(255, int(h[4:6], 16) + amt)
    return "#{:02X}{:02X}{:02X}".format(r, g, b)


def apply_win11_corners(root):
    """Win11 物理圆角：DwmSetWindowAttribute(DWMWA_WINDOW_CORNER_PREFERENCE, DWMWCP_ROUND)。
    失败（系统不支持/无 dwmapi）返回 False。"""
    try:
        import ctypes
        hwnd = root.winfo_id()
        if not hwnd:
            return False
        DWMWA_WINDOW_CORNER_PREFERENCE = 33
        DWMWCP_ROUND = 2
        val = ctypes.c_int(DWMWCP_ROUND)
        ctypes.windll.dwmapi.DwmSetWindowAttribute(
            ctypes.c_void_p(hwnd), DWMWA_WINDOW_CORNER_PREFERENCE,
            ctypes.byref(val), ctypes.sizeof(val))
        return True
    except Exception:
        return False


# ============================== 通用组件（圆角化） ==============================

class Tooltip:
    """黑色小浮窗：鼠标悬停显示文本（spec.desc），移开自动消失。"""

    def __init__(self, widget, text, wrap=400):
        self.widget = widget
        self.text = text
        self.wrap = wrap
        self._tip = None
        widget.bind("<Enter>", self._enter)
        widget.bind("<Leave>", self._leave)
        widget.bind("<Motion>", self._motion)

    def _enter(self, _e=None):
        self._show()

    def _motion(self, _e=None):
        self._show()

    def _show(self):
        try:
            x = self.widget.winfo_rootx()
            y = self.widget.winfo_rooty() + self.widget.winfo_height() + 6
        except Exception:
            return
        self._hide()
        tip = tk.Toplevel(self.widget)
        tip.wm_overrideredirect(True)
        tip.wm_geometry(f"+{x}+{y}")
        tip.configure(bg="#111111")
        tk.Label(tip, text=self.text, bg="#111111", fg="#E0E0E0", font=FONT_SMALL,
                 justify="left", wraplength=self.wrap, padx=8, pady=6).pack()
        self._tip = tip

    def _leave(self, _e=None):
        self._hide()

    def _hide(self):
        if self._tip is not None:
            try:
                self._tip.destroy()
            except Exception:
                pass
            self._tip = None


class RoundedButton(tk.Canvas):
    """Canvas 自绘圆角按钮：圆角矩形主体 + 居中文本，悬停变亮，可禁用。"""

    def __init__(self, master, text="", command=None, width=88, height=30, radius=10,
                 fill="#3A3A3A", fg=COL_TEXT, active_fill=None, active_fg=None,
                 font=FONT, state="normal", cursor="hand2",
                 disabled_fill=COL_DISABLED, disabled_fg="#6E6E6E"):
        bg = master.cget("bg") if isinstance(master, tk.Widget) else COL_BG
        super().__init__(master, width=width, height=height, bg=bg,
                         highlightthickness=0, bd=0,
                         cursor=("arrow" if state == "disabled" else cursor))
        self._text = text
        self._command = command
        self._wd, self._ht, self._radius = width, height, radius
        self._fill = fill
        self._active_fill = active_fill or _brighten(fill, 16)
        self._active_fg = active_fg
        self._fg = fg
        self._font = font
        self._state = state
        self._disabled_fill = disabled_fill
        self._disabled_fg = disabled_fg
        self._cursor = cursor
        self._hover = False
        self.bind("<Enter>", self._on_enter)
        self.bind("<Leave>", self._on_leave)
        self.bind("<Button-1>", self._on_click)
        self._draw()

    def _on_enter(self, _e):
        self._hover = True
        self._draw()

    def _on_leave(self, _e):
        self._hover = False
        self._draw()

    def _on_click(self, _e):
        if self._state == "normal" and self._command:
            self._command()
        # 返回 "break" 阻断事件继续向 bindtags 上层（class/toplevel/all）传递，
        # 避免点击自绘按钮时触发标题栏拖动、全局绑定或其它干扰（关闭/最小化隔离）
        return "break"

    def _draw(self):
        self.delete("all")
        w, h = self._wd, self._ht
        if self._state == "disabled":
            fill, fg = self._disabled_fill, self._disabled_fg
        elif self._hover:
            fill, fg = self._active_fill, self._active_fg or self._fg
        else:
            fill, fg = self._fill, self._fg
        _round_rect(self, 2, 2, w - 2, h - 2, self._radius, fill=fill, outline="")
        self.create_text(w / 2, h / 2, text=self._text, fill=fg, font=self._font)

    # ---- 兼容旧代码的状态更新 API ----
    def set_text(self, text):
        self._text = text
        self._draw()

    def set_state(self, state):
        self._state = state
        self.configure(cursor="arrow" if state == "disabled" else self._cursor)
        self._draw()
        # 钩子：附加成功后 set_state("normal") 触发显示「定位实例 / 刷新资源」
        if state == "normal":
            try:
                if getattr(self, "_on_enable", None):
                    self._on_enable()
            except Exception:
                pass

    def set_disabled_style(self, fill, fg):
        """自定义禁用态外观（如「已附加」后保持激活橙）。"""
        self._disabled_fill = fill
        self._disabled_fg = fg
        self._draw()

    def set_fill(self, fill):
        self._fill = fill
        self._active_fill = _brighten(fill, 16)
        self._draw()

    def set_fg(self, fg):
        self._fg = fg
        self._draw()

    def set_bg(self, c):
        """更新 Canvas 周边背景（行悬停联动），按钮主体色不变。"""
        self.configure(bg=c)
        self._draw()


class ToggleSwitch(tk.Canvas):
    """Canvas 自绘 Toggle 开关：36x20 圆角胶囊（半径 8）+ 白色圆圈滑动。
    未激活灰 #4A4A52 / 悬停 #56565E，激活橙 #FF9800。
    点击触发滑块滑动动画（ease-out）+ 悬停变亮；回调在动画开始时立即触发。"""

    ANIM_FRAMES = 7
    ANIM_MS = 14

    def __init__(self, master, initial=False, command=None, width=36, height=20,
                 bg=COL_PANEL):
        super().__init__(master, width=width, height=height, bg=bg,
                         highlightthickness=0, bd=0, cursor="hand2")
        self._state = BooleanVar(value=bool(initial))
        self._command = command
        self._wd, self._ht = width, height
        self._anim_id = None
        self._anim_from = 0.0
        self._anim_to = 0.0
        self._anim_frame = 0
        self._hover = False
        self._knob_x = self._knob_pos_for(self._state.get())
        self.bind("<Button-1>", self._on_click)
        self.bind("<Enter>", self._on_enter)
        self.bind("<Leave>", self._on_leave)
        self._draw()

    def _knob_pos_for(self, on):
        d = self._ht - 8  # 圆圈直径 12
        return float(self._wd - 4 - d) if on else 4.0

    def _on_enter(self, _e):
        self._hover = True
        self._draw()

    def _on_leave(self, _e):
        self._hover = False
        self._draw()

    def _on_click(self, _e=None):
        self.set(not self._state.get())

    def set(self, val):
        val = bool(val)
        if val == self._state.get():
            return
        self._state.set(val)
        if self._command:
            self._command(val)
        # 以回调后的最终状态为准启动动画：若回调弹回了状态（如未附加进程），
        # 开关保持关闭位；正常开关时开/关双向均有滑动动画。
        self._start_anim(self._state.get())

    def set_silent(self, val):
        """静默设置状态（不触发 command 回调），仅更新并重绘开关。
        用于受限拦截 / 应用失败时把开关弹回，避免递归触发 _on_toggle。"""
        val = bool(val)
        if val == self._state.get():
            return
        self._state.set(val)
        self._start_anim(val)

    def get(self):
        return self._state.get()

    def set_bg(self, c):
        self.configure(bg=c)
        self._draw()

    def _start_anim(self, target_on):
        if self._anim_id is not None:
            try:
                self.after_cancel(self._anim_id)
            except Exception:
                pass
            self._anim_id = None
        self._anim_from = self._knob_x
        self._anim_to = self._knob_pos_for(target_on)
        self._anim_frame = 0
        self._tick()

    def _tick(self):
        self._anim_frame += 1
        total = self.ANIM_FRAMES
        t = min(1.0, self._anim_frame / total)
        ease = 1 - (1 - t) ** 3  # ease-out 缓动
        self._knob_x = self._anim_from + (self._anim_to - self._anim_from) * ease
        self._draw()
        if t >= 1.0:
            self._anim_id = None
            self._knob_x = self._anim_to
            self._draw()
        else:
            self._anim_id = self.after(self.ANIM_MS, self._tick)

    def _draw(self):
        self.delete("all")
        on = self._state.get()
        # iOS 胶囊：未激活深灰底 + 灰圆；激活橙底 + 白圆
        track = COL_ORANGE if on else ("#56565E" if self._hover else "#444444")
        knob = "#FFFFFF"  # 关灰底白圆 / 开橙底白圆
        # 胶囊：宽 36 高 20，半径 8（高度一半=胶囊）
        _round_rect(self, 2, 2, self._wd - 2, self._ht - 2, (self._ht - 4) / 2,
                    fill=track, outline="")
        d = self._ht - 8  # 圆圈直径 12
        kx = round(self._knob_x)
        self.create_oval(kx, 4, kx + d, 4 + d, fill=knob, outline="")


class RoundedEntry(tk.Frame):
    """深色圆角输入框：外层 Frame（透明背景）+ Canvas 圆角矩形容器 + 无边框 Entry。
    内部 Entry relief="flat", bd=0, highlightthickness=1，
    边框 #333333 常态 / 聚焦 #FF9800（内凹深黑 #151515）。"""

    def __init__(self, master, textvariable=None, width=7, font=("Consolas", 9),
                 bg=None, fill=COL_ENTRY, radius=8, height=24, fg=COL_TEXT_W,
                 justify="right", on_change=None, readonly=False, ipady=0):
        bg = bg or (master.cget("bg") if isinstance(master, tk.Widget) else COL_PANEL)
        super().__init__(master, bg=bg)
        self._fill = fill
        self._radius = radius
        self.entry = tk.Entry(self, textvariable=textvariable, bg=fill, fg=fg,
                              insertbackground=fg, relief="flat", bd=0,
                              highlightthickness=1, highlightbackground="#333333",
                              highlightcolor=COL_ORANGE, font=font, width=width,
                              justify=justify)
        pix_w = max(64, width * 10 + 20)  # 按 YaHei 10 号约 10px/字符，防文字截断
        pix_h = height
        self.canvas = tk.Canvas(self, width=pix_w, height=pix_h, bg=bg,
                                highlightthickness=0)
        self.canvas.pack()
        _round_rect(self.canvas, 2, 2, pix_w - 2, pix_h - 2, radius,
                    fill=fill, outline="")
        # ipady>0 时把 Entry 撑满圆角矩形内高：文字垂直居中、上下留内边距，防截断
        if ipady:
            self._win = self.canvas.create_window(pix_w // 2, pix_h // 2,
                                                  window=self.entry,
                                                  height=max(20, pix_h - 2))
        else:
            self._win = self.canvas.create_window(pix_w // 2, pix_h // 2,
                                                  window=self.entry)
        if readonly:
            # 修正：禁用用 configure(state=...)，不使用 entry.state 赋值
            self.entry.configure(state="disabled")
        if on_change:
            self.entry.bind("<Return>", lambda e: on_change())

    def set_bg(self, c):
        self.configure(bg=c)
        self.canvas.configure(bg=c)

    def get(self):
        return self.entry.get()

    def set(self, v):
        self.entry.delete(0, "end")
        self.entry.insert(0, str(v))


class SpinEntry(tk.Frame):
    """数值调节框：圆角 Entry + Canvas 圆角 < > 步进按钮。"""

    def __init__(self, master, value=0.0, step=1.0, decimals=0, on_change=None,
                 width=7, bg=COL_PANEL):
        super().__init__(master, bg=bg)
        self._step = step
        self._decimals = decimals
        self._on_change = on_change
        self._fmt = lambda v: f"{v:.{decimals}f}".rstrip("0").rstrip(".") if decimals else str(int(round(v)))
        self.var = StringVar(value=self._fmt(float(value)))

        # 极简精修：去掉 < > 步进按钮，只保留圆角输入框（#1E1E1E 深底白字）
        self._entry = RoundedEntry(self, textvariable=self.var, width=width,
                                   bg=bg, fill="#1E1E1E")
        self._entry.pack(side="left")
        # 回车 / 失焦触发回调（原按钮步进逻辑移除后的等价输入通道）
        self._entry.entry.bind("<Return>", lambda e: self._emit())
        self._entry.entry.bind("<FocusOut>", lambda e: self._emit())

    def set_bg(self, c):
        self.configure(bg=c)
        self._entry.set_bg(c)

    def _emit(self):
        if self._on_change:
            try:
                self._on_change(float(self.var.get()))
            except ValueError:
                pass

    def get(self):
        try:
            return float(self.var.get())
        except ValueError:
            return 0.0

    def set(self, v):
        self.var.set(self._fmt(float(v)))


class RowCard(tk.Frame):
    """圆角卡片行：Canvas 圆角矩形（间隔 2px）+ 内嵌内容 Frame。
    悬停背景变亮 #32323C 且指针 hand2；通过 on_hover 回调联动内部组件。
    height=None 时行高由内容自适应（content.winfo_reqheight），保证右侧控件不被裁切；
    传入 height 时保持固定行高（存档页使用）。"""

    def __init__(self, master, height=None, radius=8, bg=COL_BG, fill=COL_PANEL,
                 hover=COL_PANEL_HOVER, cursor="hand2", on_hover=None, pady=1):
        super().__init__(master, bg=bg)
        self._h = height
        self._radius = radius
        self._fill = fill
        self._hover_c = hover
        self._on_hover = on_hover
        self.canvas = tk.Canvas(self, bg=bg, highlightthickness=0, height=height or 40,
                                cursor=cursor)
        self.canvas.pack(fill="x", pady=pady)  # 上下各 pady => 行间隔 2*pady
        self.content = tk.Frame(self.canvas, bg=fill)
        self._win = self.canvas.create_window((radius, 2),
                                              window=self.content, anchor="nw")
        self.canvas.bind("<Configure>", self._on_resize)
        self.canvas.bind("<Enter>", lambda e: self._apply_fill(self._hover_c))
        self.canvas.bind("<Leave>", lambda e: self._apply_fill(self._fill))

    def fit(self):
        """按内容实际需求高度刷新 Canvas 高度（动态行高时使用）。"""
        try:
            self.canvas.configure(height=self.content.winfo_reqheight() + 4)
        except Exception:
            pass
        self._redraw()

    def _on_resize(self, e):
        self.canvas.itemconfigure(self._win,
                                  width=max(10, e.width - 2 * self._radius))
        if self._h is not None:
            self.canvas.itemconfigure(self._win,
                                      height=max(10, self._h - 4))
        else:
            self.fit()
        self._redraw()

    def _redraw(self):
        self.canvas.delete("bgshape")
        w = self.canvas.winfo_width()
        h = self.canvas.winfo_height()
        if w > 10 and h > 10:
            _round_rect(self.canvas, 1, 1, w - 1, h - 1, self._radius,
                        fill=self._fill, outline="", tags=("bgshape",))

    def _apply_fill(self, c):
        self._fill = c
        self._redraw()
        self.content.configure(bg=c)
        if self._on_hover:
            self._on_hover(c)


class SaveCard(tk.Canvas):
    """SaveTab 专用像素级圆角卡片行：纯 Canvas 行容器，高 50px、圆角半径 8px、
    底板 #242428（radius=8 内缩 2px 描边区）。左侧白标题 + 下方 #999999 灰色小字，
    右侧放输入框；悬停底色变亮 #303030。
    与 RowCard 不同：整个容器就是 Canvas（无外层 Frame 直角边角），
    从根本上消除背景直角方块问题。"""

    def __init__(self, master, height=50, radius=8, fill=COL_PANEL, bg=COL_BG,
                 hover=COL_PANEL_HOVER, cursor="hand2", on_hover=None):
        super().__init__(master, bg=bg, highlightthickness=0, bd=0,
                         height=height, cursor=cursor)
        self._h = height
        self._radius = radius
        self._fill = fill
        self._hover_c = hover
        self._on_hover = on_hover
        self.content = tk.Frame(self, bg=fill)
        self._win = self.create_window((radius, 2), window=self.content, anchor="nw")
        self.bind("<Configure>", self._on_resize)
        self.bind("<Enter>", lambda e: self._apply_fill(self._hover_c))
        self.bind("<Leave>", lambda e: self._apply_fill(self._fill))

    def _on_resize(self, e):
        self.itemconfigure(self._win,
                           width=max(10, e.width - 2 * self._radius),
                           height=max(10, self._h - 4))
        self._redraw()

    def _redraw(self):
        self.delete("bgshape")
        w = self.winfo_width()
        h = self.winfo_height()
        if w > 10 and h > 10:
            _round_rect(self, 1, 1, w - 1, h - 1, self._radius,
                        fill=self._fill, outline="", tags=("bgshape",))

    def _apply_fill(self, c):
        self._fill = c
        self._redraw()
        self.content.configure(bg=c)
        if self._on_hover:
            self._on_hover(c)


class ScrollableFrame(tk.Frame):
    """带暗色滚动条的滚动容器（ttk.Style 定制）。
    MouseWheel 仅在鼠标进入列表区域时 bind_all、离开后延迟 unbind_all。"""

    def __init__(self, master, bg=COL_BG):
        super().__init__(master, bg=bg)
        style = ttk.Style()
        try:
            style.theme_use("clam")
            layout = style.layout("Vertical.TScrollbar")
            style.layout("Dark.Vertical.TScrollbar", layout)
            style.configure("Dark.Vertical.TScrollbar",
                            background="#3A3A3C", troughcolor=COL_BG,
                            bordercolor=COL_BG, arrowcolor="#9A9A9A",
                            lightcolor="#3A3A3C", darkcolor="#3A3A3C")
        except Exception:
            pass
        self.canvas = tk.Canvas(self, bg=bg, highlightthickness=0)
        self.scroll = ttk.Scrollbar(self, orient="vertical", style="Dark.Vertical.TScrollbar",
                                    command=self.canvas.yview)
        self.inner = tk.Frame(self.canvas, bg=bg)
        self.window = self.canvas.create_window((0, 0), window=self.inner, anchor="nw")
        self.inner.bind("<Configure>", lambda e: self.canvas.configure(
            scrollregion=self.canvas.bbox("all")))
        self.canvas.bind("<Configure>", lambda e: self.canvas.itemconfigure(
            self.window, width=e.width))
        self.canvas.configure(yscrollcommand=self.scroll.set)
        self.canvas.pack(side="left", fill="both", expand=True)
        self.scroll.pack(side="right", fill="y")

        self._wheel_bound = False
        self._leave_job = None
        self.canvas.bind("<Enter>", self._on_enter, add="+")
        self.canvas.bind("<Leave>", self._on_leave, add="+")

    def _on_enter(self, _e):
        if self._leave_job is not None:
            self.after_cancel(self._leave_job)
            self._leave_job = None
        if not self._wheel_bound:
            self.canvas.bind_all("<MouseWheel>", self._on_wheel)
            self._wheel_bound = True

    def _on_leave(self, _e):
        if self._leave_job is not None:
            self.after_cancel(self._leave_job)
        self._leave_job = self.after(80, self._maybe_unbind)

    def _maybe_unbind(self):
        self._leave_job = None
        try:
            x, y = self.winfo_pointerxy()
            w = self.winfo_containing(x, y)
        except Exception:
            w = None
        if w is not None and self._in_tree(w):
            return
        if self._wheel_bound:
            self.canvas.unbind_all("<MouseWheel>")
            self._wheel_bound = False

    def _in_tree(self, w):
        node = w
        while node is not None:
            if node is self.canvas or node is self.inner or node is self:
                return True
            node = getattr(node, "master", None)
        return False

    def _on_wheel(self, e):
        self.canvas.yview_scroll(int(-1 * (e.delta / 120)), "units")


# ============================== 功能行映射 ==============================
# 23 项功能快捷键（仅展示，不绑定按键）
HOTKEYS = {
    "god": "Num 1", "hp": "Num 2", "shield": "Num 3", "mana": "Num 4",
    "firespeed": "Num 5", "bomb": "Num 6", "nocooldown": "Num 7",
    "hatch": "Num 8", "crit": "Num 9", "onedmg": "Num 0",
    "dmgmult": "Ctrl+Num 1", "coin": "Ctrl+Num 2", "coingain": "Ctrl+Num 3",
    "key": "Ctrl+Num 4", "evolve": "Ctrl+Num 5", "hajibao": "Ctrl+Num 6",
    "fatepoint": "Ctrl+Num 7", "fatecard": "Ctrl+Num 8", "char": "Ctrl+Num 9",
    "gamespeed": "Ctrl+Num 0", "movespeed": "F1", "jump": "F2", "gravity": "F3",
}
# 受限项（UI 禁用 / 纯文本状态）
LIMITED = ("evolve", "fatepoint", "fatecard", "char", "gamespeed")
# 数值调节项：fid -> (默认值, 步长, 小数位)
SPIN_SPEC = {
    "dmgmult":  (10.0,   1.0,   0),
    "coin":     (99999.0, 1000.0, 0),
    "coingain": (5.0,    1.0,   0),
    "key":      (99.0,   1.0,   0),
    "movespeed":(2.0,    0.1,   1),
    "gravity":  (0.5,    0.1,   1),
}


# ============================== 页1：游戏内修改（圆角 Trainer 布局） ==============================

class MemoryTab:
    """「游戏内修改」页面 —— 左右分栏 Trainer 模板（仅 UI 外观圆角化）。"""

    def __init__(self, parent, status_setter):
        self.parent = parent
        self.set_status = status_setter
        self.proc = None
        self.engine = None
        self.rows = {}
        self.attached = False
        self.game_cover_path = None  # 预留封面替换接口
        self._build_left()
        self._build_right()

    # ---------------- 左栏：游戏信息区（真实封面图） ----------------
    def _build_left(self):
        left = tk.Frame(self.parent, bg=COL_BG, width=280)
        left.pack(side="left", fill="y")
        left.pack_propagate(False)

        # 真实封面图：外层 Frame 极细边框（#333333 / 1px）包裹，内圆角遮罩
        self._cover_photo = None
        try:
            from PIL import Image, ImageDraw, ImageTk
            p = os.path.join(os.path.dirname(os.path.abspath(__file__)), "cover.png")
            if not os.path.isfile(p):
                raise FileNotFoundError(p)
            img = Image.open(p).convert("RGBA").resize((240, 320), Image.LANCZOS)
            mask = Image.new("L", (240, 320), 0)
            ImageDraw.Draw(mask).rounded_rectangle([0, 0, 239, 319], radius=15,
                                                   fill=255)
            img.putalpha(mask)
            # 圆角细边框（1px #333333，沿遮罩边缘）
            ImageDraw.Draw(img).rounded_rectangle([0, 0, 239, 319], radius=15,
                                                  outline="#333333", width=1)
            self._cover_photo = ImageTk.PhotoImage(img)
            cover_frame = tk.Frame(left, bg=COL_BG, highlightbackground="#333333",
                                   highlightthickness=1)
            cover_frame.pack(pady=(14, 10))
            cover_lbl = tk.Label(cover_frame, image=self._cover_photo, bg=COL_BG)
            cover_lbl.pack(padx=1, pady=1)
        except Exception:
            # 缺失时：深灰圆角占位 + 「封面图缺失」
            cover_cv = tk.Canvas(left, width=240, height=320, bg=COL_BG,
                                 highlightthickness=0)
            _round_rect(cover_cv, 0, 0, 239, 319, 15, fill="#242424",
                        outline=COL_BORDER)
            cover_cv.create_text(120, 160, text="封面图缺失", fill=COL_DIM,
                                 font=FONT_SMALL)
            cover_cv.pack(pady=(14, 10))

        # 附加按钮：保持显眼（橙底黑字）
        btns = tk.Frame(left, bg=COL_BG)
        btns.pack(fill="x", padx=14, pady=(6, 2))
        self.attach_btn = RoundedButton(btns, text="附加游戏进程", command=self.attach_game,
                                        width=252, height=32, radius=10, fill=COL_ORANGE,
                                        fg="#111111", font=FONT_B,
                                        active_fill="#FFB74D", active_fg="#111111")
        self.attach_btn.pack(fill="x")

        # 定位实例 / 刷新资源：初始隐藏（pack_forget），附加成功后显示
        row2 = tk.Frame(left, bg=COL_BG)
        self.locate_btn = RoundedButton(row2, text="定位实例", command=self.locate,
                                        width=122, height=26, radius=8, fill=COL_BTN,
                                        fg=COL_TEXT, font=FONT_SMALL, state="disabled")
        self.locate_btn.pack(side="left", fill="x", expand=True)
        self.refresh_btn = RoundedButton(row2, text="刷新资源", command=self.refresh_attrs,
                                         width=122, height=26, radius=8, fill=COL_BTN,
                                         fg=COL_TEXT, font=FONT_SMALL, state="disabled")
        self.refresh_btn.pack(side="left", fill="x", expand=True, padx=(6, 0))
        self._locate_row = row2
        self.locate_btn._on_enable = self._show_sub_btns
        self.refresh_btn._on_enable = self._show_sub_btns

        # 底部信息区：进程 / PID / 开发者 / 版本 / 更新 灰小字，字号 8 紧凑藏拙
        info = tk.Frame(left, bg=COL_BG)
        info.pack(side="bottom", fill="x", padx=10, pady=(0, 10))
        tk.Label(info, text="Game Process Name: NeonAbyss2.exe", bg=COL_BG,
                 fg=COL_DIM, font=FONT_TINY).pack(pady=(0, 1))
        self.pid_var = StringVar(value="Process ID: 未附加")
        tk.Label(info, textvariable=self.pid_var, bg=COL_BG, fg=COL_DIM,
                 font=FONT_TINY).pack(pady=(0, 1))
        tk.Label(info, text="Trainer Developer: Marvis File Agent", bg=COL_BG,
                 fg=COL_DIM, font=FONT_TINY).pack(pady=(0, 1))
        tk.Label(info, text="Trainer Version: v1.0.0", bg=COL_BG, fg=COL_DIM,
                 font=FONT_TINY).pack(pady=(0, 1))
        upd = tk.Label(info, text="Check for updates", bg=COL_BG, fg=COL_ORANGE,
                       font=FONT_TINY, cursor="hand2")
        upd.pack(pady=(1, 0))
        upd.bind("<Button-1>",
                 lambda e: self.set_status("当前已是最新版本（演示提示）", True))

    def _show_sub_btns(self):
        """附加成功后显示「定位实例 / 刷新资源」按钮组。"""
        try:
            if not self._locate_row.winfo_ismapped():
                self._locate_row.pack(fill="x", padx=14, pady=(6, 2))
        except Exception:
            pass

    # ---------------- 右栏：功能列表（70%） + 底部属性面板（30%） ----------------
    def _build_right(self):
        right = tk.Frame(self.parent, bg=COL_BG)
        right.pack(side="left", fill="both", expand=True)
        right.grid_columnconfigure(0, weight=1)
        right.grid_rowconfigure(0, weight=7)
        right.grid_rowconfigure(1, weight=3)

        # 上部：Hotkeys / Options 功能列表（70%，左栏与列表之间 padx=20 留白）
        upper = tk.Frame(right, bg=COL_BG)
        upper.grid(row=0, column=0, sticky="nsew", padx=(20, 14), pady=(10, 6))

        hdr = tk.Frame(upper, bg=COL_BG)
        hdr.pack(fill="x")
        tk.Label(hdr, text="Hotkeys", bg=COL_BG, fg=COL_ORANGE, font=FONT_B,
                 width=10, anchor="w", padx=6).pack(side="left")
        tk.Label(hdr, text="Options", bg=COL_BG, fg=COL_ORANGE, font=FONT_B,
                 anchor="w", padx=6).pack(side="left")
        tk.Frame(hdr, bg=COL_BORDER, height=1).pack(fill="x", pady=(4, 0))

        self.list_box = ScrollableFrame(upper, bg=COL_BG)
        self.list_box.pack(fill="both", expand=True, pady=(4, 0))

        # 可用功能完整保留，受限项跳过不渲染（行背景统一 #242424）
        idx = 0
        for spec in CHEATS:
            if spec.fid in LIMITED:
                continue
            self._add_row(self.list_box.inner, spec, idx)
            idx += 1

        # 下部：Selected Character Bonus Stats 面板（30%）
        self._build_stats_panel(right)

    def _build_stats_panel(self, parent):
        """底部属性面板：4 列网格，只读属性用深色背景框 Label 展示。"""
        panel = tk.Frame(parent, bg=COL_BG)
        panel.grid(row=1, column=0, sticky="nsew", padx=(20, 14), pady=(6, 12))

        tk.Label(panel, text="Selected Character Bonus Stats", bg=COL_BG,
                 fg=COL_TEXT_W, font=FONT_B, anchor="w").pack(fill="x")
        tk.Frame(panel, bg=COL_BORDER, height=1).pack(fill="x", pady=(4, 0))

        grid = tk.Frame(panel, bg=COL_BG)
        grid.pack(fill="both", expand=True, pady=(8, 0))
        for c in range(4):
            grid.columnconfigure(c, weight=1, uniform="col")
        cols = [
            ["Max HP: 1000", "Shield: 500", "Mana: 300", "Bombs: 99"],
            ["Damage: x10", "Fire Rate: +25%", "Crit: 100%", "Move Speed: x2.0"],
            ["Jump: +5", "Gravity: 0.5", "Coin Gain: x5", "Key: 99"],
            ["Hatch: 100%", "Cooldown: 0s", "Fate: 3", "Luck: 7"],
        ]
        for r, items in enumerate(cols):
            for c, txt in enumerate(items):
                tk.Label(grid, text=txt, bg="#1E1E1E", fg="#E0E0E0",
                         font=FONT_SMALL, anchor="w", padx=8, pady=5).grid(
                             row=r, column=c, sticky="nsew", padx=3, pady=3)

    @staticmethod
    def _short_hotkey(hk):
        """快捷键短格式：Ctrl+Num 1 -> C+N1，保证宽 8 内完整显示。"""
        return hk.replace("Ctrl+", "C+").replace("Num ", "N").replace("Num", "N")

    # ---------------- 功能行（极简瘦身：36-40px，单行布局，无长描述） ----------------
    def _add_row(self, parent, spec: CheatSpec, idx=0):
        fid = spec.fid
        row = {"spec": spec, "hotkey": None, "kind": None, "active": False,
               "fill": COL_PANEL}
        # 行高 38px、去边框、背景统一 #242424、行间距 pady=2、悬停 #303030
        card = RowCard(parent, height=38, radius=8, fill=COL_PANEL,
                       on_hover=lambda c, r=row: self._row_hover(r, c))
        card.pack(fill="x", pady=2)
        self.rows[fid] = row
        content = card.content

        # 左侧：快捷键（灰 #999，宽 8，Consolas 小号）
        hk = tk.Label(content, text=self._short_hotkey(HOTKEYS.get(fid, "—")),
                      bg=COL_PANEL, fg=COL_DIM, font=("Consolas", 8),
                      width=8, anchor="center")
        hk.pack(side="left", padx=(8, 0))
        row["hotkey"] = hk

        # 中间：功能名称（白色靠左），悬停弹出黑色 Tooltip 显示 spec.desc
        name = tk.Label(content, text=spec.label, bg=COL_PANEL, fg=COL_TEXT_W,
                        font=FONT, anchor="w", cursor="question_arrow")
        name.pack(side="left", padx=(10, 12))
        Tooltip(name, text=spec.desc, wrap=420)
        row["name_lbl"] = name

        if fid in SPIN_SPEC:
            # 右侧控件：Toggle + 无按钮圆角输入框（紧凑靠右 padx=10）
            ctrl = tk.Frame(content, bg=COL_PANEL)
            ctrl.pack(side="right", padx=10)
            default, step, dec = SPIN_SPEC[fid]
            tgl = ToggleSwitch(ctrl, initial=spec.locked, bg=COL_PANEL,
                               command=lambda on, s=spec: self._on_toggle(s, on))
            tgl.pack(side="right")
            # 输入框默认值：spec.default（无则回退 SPIN_SPEC 默认），宽 8 白字
            init = spec.default if spec.default else default
            spin = SpinEntry(ctrl, value=init, step=step, decimals=dec,
                             on_change=lambda v, s=spec: self._on_spin(s, v),
                             width=8, bg=COL_PANEL)
            spin.pack(side="right", padx=(8, 0))
            row["kind"] = "spin"
            row["toggle"] = tgl
            row["spin"] = spin
            row["ctrl"] = ctrl
        else:
            # 右侧开关（紧凑靠右 padx=10）
            ctrl = tk.Frame(content, bg=COL_PANEL)
            ctrl.pack(side="right", padx=10)
            tgl = ToggleSwitch(ctrl, initial=spec.locked, bg=COL_PANEL,
                               command=lambda on, s=spec: self._on_toggle(s, on))
            tgl.pack(side="right")
            row["kind"] = "toggle"
            row["toggle"] = tgl
            row["ctrl"] = ctrl

    def _row_hover(self, row, c):
        """行悬停联动：更新行内组件背景（激活中的快捷键保持橙色高亮）。"""
        if not row.get("active"):
            row["hotkey"].configure(bg=c)
        row["name_lbl"].configure(bg=c)
        row["toggle"].set_bg(c)
        if row["kind"] == "spin":
            row["spin"].set_bg(c)
        row["ctrl"].configure(bg=c)

    # ---------------- 行为（业务逻辑，原样保留） ----------------
    def _on_toggle(self, spec, on):
        """开关回调：受限拦截 -> engine.set_locked 对接 -> 线程 start/stop。
        步骤：1) 取状态 is_on；2) engine.set_locked(spec, is_on, custom_value)；
        3) is_on=True 且引擎线程未运行则 start；4) is_on=False 且全关则 stop。"""
        is_on = bool(on)
        print(f"点击开关: {spec.label}, 状态: {is_on}")
        row = self.rows.get(spec.fid)

        # 受限项拦截：desc 含「受限」禁止打开，弹回并提示
        if is_on and ("受限" in (spec.desc or "")):
            if row is not None:
                row["toggle"].set_silent(False)
            self._set_active(spec.fid, False)
            self.set_status(f"该项受限，无法修改：{spec.label}（{spec.desc}）", False)
            return

        # 关闭分支：先解锁引擎，再视全关情况停线程
        if not is_on:
            if self.engine:
                self.engine.set_locked(spec, False, None)
                if not self._any_locked():
                    self.engine.stop()
            self.set_status(f"已停止锁定：{spec.label}", True)
            self._set_active(spec.fid, False)
            return

        # 开启分支
        if not self.engine:
            self.set_status("请先附加游戏进程并定位实例", False)
            if row is not None:
                row["toggle"].set_silent(False)
            return
        custom = row["spin"].get() if row is not None and row.get("spin") is not None else None
        # 对接引擎：set_locked 内部置 spec.locked 并在需要时自启线程
        self.engine.set_locked(spec, True, custom)
        ok, msg = self.apply_spec(spec, custom)
        if not ok:
            # 应用失败：解锁并弹回开关
            self.engine.set_locked(spec, False, None)
            if row is not None:
                row["toggle"].set_silent(False)
            self.set_status(f"[{spec.label}] {msg}", False)
            return
        if not (self.engine._thread and self.engine._thread.is_alive()):
            self.engine.start()
        self.set_status(f"[{spec.label}] 已开启锁定", True)
        self._set_active(spec.fid, True)

    def _on_spin(self, spec, val):
        spec.custom = val
        if self.engine and spec.locked:
            ok, msg = self.apply_spec(spec)
            self.set_status(f"[{spec.label}] {msg}", ok)

    def _any_locked(self):
        return any(s.locked for s in CHEATS)

    def _set_active(self, fid, active):
        hk = self.rows[fid]["hotkey"]
        self.rows[fid]["active"] = bool(active)
        if active:
            hk.configure(bg=COL_ORANGE, fg="#111111")
        else:
            hk.configure(bg=self.rows[fid]["fill"], fg=COL_DIM)

    def set_game_cover(self, path):
        """预留：替换左侧封面占位图。"""
        self.game_cover_path = path

    def attach_game(self):
        try:
            if self.proc is None:
                self.proc = NeonProcess()
            self.proc.attach()
            self.engine = TrainerEngine(self.proc)
            self.attached = True
            # 仅按钮外观更新（业务逻辑不变）：已附加 -> 激活亮橙
            self.attach_btn.set_text("已附加 ✓")
            self.attach_btn.set_state("disabled")
            self.attach_btn.set_disabled_style(COL_ORANGE, "#111111")
            self.locate_btn.set_state("normal")
            self.refresh_btn.set_state("normal")
            self.pid_var.set(f"Process ID: {self.proc.pid}")
            self.set_status(f"附加成功 PID={self.proc.pid}，点击「定位实例」初始化游戏内修改", True)
            self.locate()
        except Exception as e:
            self.set_status(f"附加失败：{e}", False)

    def locate(self):
        if not self.engine:
            return
        try:
            self.engine.locate()
            inst = self.engine.instances
            ps = inst.get("player_state")
            pa = inst.get("player_actor")
            eggs = len(inst.get("eggs") or [])
            self.set_status(f"实例定位成功（PS={hex(ps or 0)} PA={hex(pa or 0)} 蛋×{eggs}）", True)
        except Exception as e:
            self.set_status(f"定位失败：{e}（请进入对局后再试）", False)

    def refresh_attrs(self):
        if not self.engine or not self.engine.attrs:
            self.set_status("请先定位实例", False)
            return
        vals = self.engine.attr_values()
        lines = "; ".join(f"{k}={v:g}" for k, v in list(vals.items())[:12])
        self.set_status(f"当前 attrs（前12键）: {lines}", True)

    def apply_spec(self, spec, custom=None):
        if not self.engine:
            self.set_status("请先附加进程并定位实例", False)
            return False, "未附加"
        try:
            ok, msg = self.engine.apply_once(spec, custom)
            return ok, msg
        except Exception as e:
            return False, f"失败：{e}"

    def lock_all(self):
        if not self.engine:
            self.set_status("请先附加进程并定位实例", False)
            return
        n = 0
        for fid, r in self.rows.items():
            r["toggle"].set(True)
            r["spec"].locked = True
            n += 1
        if self.engine and not (self.engine._thread and self.engine._thread.is_alive()):
            self.engine.start()
        self.set_status(f"已启用 {n} 项锁定（锁定循环每 0.2s 写入）", True)

    def unlock_all(self):
        for fid, r in self.rows.items():
            r["toggle"].set(False)
            r["spec"].locked = False
        if self.engine:
            self.engine.stop()
        self.set_status("已停止全部锁定", True)


# ============================== 页2：存档修改（原功能保留，圆角化换肤） ==============================

class SaveTab:
    """「存档修改」页面（离线存档：XXTEA+DLPN，保险库/Token/进化/解锁）。"""

    def __init__(self, parent, status_setter):
        self.parent = parent
        self.set_status = status_setter
        self.entries = None
        self.version = None
        self.owner_path = None
        self.rows = {}
        self._build_path_bar()
        self._build_func_panel()
        self._build_bottom()
        self._auto_locate()

    def _build_path_bar(self):
        # 存档槽位下拉：深底白字、去边框（像素级：背景 #242428、前景白、YaHei 10）
        style = ttk.Style()
        try:
            style.theme_use("clam")
            style.configure("Trainer.TCombobox",
                            fieldbackground="#242428", background="#242428",
                            foreground="#FFFFFF", arrowcolor="#FFFFFF",
                            bordercolor="#333333", lightcolor="#333333",
                            darkcolor="#333333", padding=4,
                            selectbackground=COL_ORANGE, selectforeground="#111111")
            style.map("Trainer.TCombobox",
                      fieldbackground=[("readonly", "#242428")],
                      background=[("readonly", "#242428")],
                      foreground=[("readonly", "#FFFFFF")],
                      arrowcolor=[("readonly", "#FFFFFF")],
                      selectbackground=[("readonly", COL_ORANGE)],
                      selectforeground=[("readonly", "#111111")])
            style.configure("Trainer.TCombobox.Listbox",
                            background="#242428", foreground="#FFFFFF",
                            bordercolor="#333333",
                            selectbackground=COL_ORANGE,
                            selectforeground="#111111")
        except Exception:
            pass

        # 顶部整体留白 pady=15
        frm = tk.Frame(self.parent, bg=COL_BG)
        frm.pack(fill="x", padx=14, pady=(15, 4))
        tk.Label(frm, text="存档目录", bg=COL_BG, fg=COL_TEXT, font=FONT).pack(side="left")
        self.dir_var = StringVar()
        self.dir_entry = RoundedEntry(frm, textvariable=self.dir_var, width=30,
                                      bg=COL_BG, font=FONT, fg=COL_TEXT_W,
                                      fill="#18181B", height=34, ipady=4)
        self.dir_entry.pack(side="left", padx=6)
        self._mkbtn(frm, "浏览", self._browse_dir).pack(side="left", padx=2)
        self._mkbtn(frm, "自动定位", self._auto_locate).pack(side="left", padx=2)

        frm2 = tk.Frame(self.parent, bg=COL_BG)
        frm2.pack(fill="x", padx=14, pady=(2, 4))
        tk.Label(frm2, text="存档槽位", bg=COL_BG, fg=COL_TEXT, font=FONT).pack(side="left")
        self.slot_combo = ttk.Combobox(frm2, state="readonly", width=30, font=FONT,
                                       style="Trainer.TCombobox")
        self.slot_combo.pack(side="left", padx=6)
        self._mkbtn(frm2, "读取存档", self.load_save).pack(side="left", padx=4)

    def _build_func_panel(self):
        wrap = tk.Frame(self.parent, bg=COL_BG)
        # 顶部（路径/槽位）与下方列表之间 pady=15
        wrap.pack(fill="both", expand=True, padx=14, pady=(15, 6))
        self.var_coin = StringVar()
        self.var_bomb = StringVar()
        self.var_key = StringVar()
        self.var_egg = StringVar()
        self.var_token = StringVar()
        self.var_level = StringVar()
        self.var_exp = StringVar()
        rows = [
            ("保险库金币", self.var_coin, "金币（基地保险柜存款）"),
            ("保险库炸弹", self.var_bomb, "炸弹（基地保险柜存款）"),
            ("保险库钥匙", self.var_key, "钥匙（基地保险柜存款）"),
            ("保险库蛋蛋", self.var_egg, "蛋（基地保险柜存款）"),
            ("霓虹币 Token", self.var_token, "主货币余额"),
            ("进化等级", self.var_level, "进化系统等级"),
            ("进化经验", self.var_exp, "当前经验值"),
        ]
        for i, (name, var, tip) in enumerate(rows):
            self._add_row(wrap, i, name, var, tip)
        self.char_lbl = StringVar(value="角色解锁（已解锁 0 个）")
        self.content_lbl = StringVar(value="内容解锁（已解锁 0 个）")
        self._add_append_row(wrap, len(rows), self.char_lbl, "profile.character-unlock", "角色")
        self._add_append_row(wrap, len(rows) + 1, self.content_lbl, "progression.content-unlock", "内容")

    def _row_hover(self, row, c):
        left = row.get("left")
        if left is not None:
            left.configure(bg=c)
            for w in left.winfo_children():
                if isinstance(w, tk.Label):
                    w.configure(bg=c)
        entry = row.get("entry")
        if entry is not None:
            entry.set_bg(c)

    def _add_row(self, wrap, idx, name, var, tip):
        row = {}
        # 像素级圆角卡片：Canvas 行容器高 50px、半径 8px、底板 #242428，卡片间距 pady=6
        card = SaveCard(wrap, height=50, radius=8, fill=COL_PANEL,
                        on_hover=lambda c, r=row: self._row_hover(r, c))
        card.pack(fill="x", pady=3)
        self.rows[idx] = row
        content = card.content
        left = tk.Frame(content, bg=COL_PANEL)
        left.pack(side="left", fill="both", expand=True, padx=(10, 0), pady=4)
        # 左侧白色标题（清晰可见）
        tk.Label(left, text=name, bg=COL_PANEL, fg=COL_TEXT_W, font=FONT_B,
                 anchor="w").pack(fill="x", pady=(0, 0))
        # 下方 #999999 灰色小字描述
        tk.Label(left, text=tip, bg=COL_PANEL, fg=COL_DIM, font=FONT_SMALL,
                 anchor="w").pack(fill="x", pady=(0, 1))
        # 右侧输入框：宽 15、右对齐、白字深底（fg=#FFFFFF bg=#18181B）、YaHei 10 + ipady=4 防截断
        re = RoundedEntry(content, textvariable=var, width=15, bg=COL_PANEL,
                          fill="#18181B", fg=COL_TEXT_W, font=FONT, justify="right",
                          height=34, ipady=4)
        re.pack(side="right", padx=12, pady=6)
        row["left"] = left
        row["entry"] = re

    def _add_append_row(self, wrap, idx, lbl_var, key, name):
        row = {}
        card = SaveCard(wrap, height=50, radius=8, fill=COL_PANEL,
                        on_hover=lambda c, r=row: self._row_hover(r, c))
        card.pack(fill="x", pady=3)
        self.rows["append_" + key] = row
        content = card.content
        lbl = tk.Label(content, textvariable=lbl_var, bg=COL_PANEL, fg=COL_TEXT_W,
                       font=FONT_B, anchor="w", width=28)
        lbl.pack(side="left", padx=10, pady=12)
        row["left"] = lbl
        add_var = StringVar()
        e = RoundedEntry(content, textvariable=add_var, width=15, bg=COL_PANEL,
                         fill="#18181B", fg=COL_TEXT_W, font=FONT, justify="right",
                         height=34, ipady=4)
        e.pack(side="left", padx=4, pady=6)
        self._mkbtn(content, "追加", lambda ev, k=key, n=name, v=add_var: self.append_ids(k, n, v)).pack(side="left", padx=4, pady=6)
        row["entry"] = e
        row["append_btn"] = e

    def _build_bottom(self):
        bottom = tk.Frame(self.parent, bg=COL_BG)
        bottom.pack(fill="x", padx=14, pady=(8, 12))
        self.save_btn = RoundedButton(bottom, text="保存修改", command=self.save_changes,
                                      width=160, height=36, radius=10, fill=COL_ORANGE,
                                      fg="#111111", font=FONT_SUB, active_fill="#FFB74D")
        self.save_btn.pack(side="left")
        tk.Label(bottom, text="说明：写回前自动备份，重启游戏生效", bg=COL_BG, fg=COL_DIM,
                 font=FONT_SMALL).pack(side="left", padx=10)

    def _mkbtn(self, parent, text, cmd):
        # Canvas 圆角按钮：底色 #333333，悬停变橙 #FF9800 且文字变黑
        return RoundedButton(parent, text=text, command=cmd, width=74, height=26,
                             radius=8, fill="#333333", fg=COL_TEXT, font=FONT_SMALL,
                             active_fill=COL_ORANGE, active_fg="#111111")

    def _browse_dir(self):
        d = filedialog.askdirectory(title="选择游戏存档目录（SavesDir）")
        if d:
            self.dir_var.set(d)
            self._refresh_slots()

    def _auto_locate(self):
        sd = find_saves_dir()
        if sd:
            self.dir_var.set(sd)
            self._refresh_slots()
        else:
            self.set_status("未自动找到存档目录，请点击「浏览」手动选择", False)

    def _refresh_slots(self):
        sd = self.dir_var.get().strip()
        if not sd or not os.path.isdir(sd):
            self.slot_combo["values"] = []
            return
        slots = list_slots(sd)
        if slots:
            self.slot_combo["values"] = slots
            self.slot_combo.current(0)

    def load_save(self):
        sd = self.dir_var.get().strip()
        slot = self.slot_combo.get().strip()
        if not sd or not os.path.isdir(sd):
            self.set_status("存档目录无效，请先选择或自动定位", False)
            return
        if not slot:
            self.set_status("未选择存档槽位", False)
            return
        path = os.path.join(sd, slot)
        if not os.path.isfile(path):
            self.set_status(f"存档文件不存在：{path}", False)
            return
        try:
            self.version, self.entries = decrypt_owner_file(path)
        except Exception as e:
            self.set_status(f"读取存档失败：{e}", False)
            return
        self.owner_path = path
        vals = load_current_values(self.entries)
        self.var_coin.set(str(vals["coin"]))
        self.var_bomb.set(str(vals["bomb"]))
        self.var_key.set(str(vals["key"]))
        self.var_egg.set(str(vals["egg"]))
        self.var_token.set(str(vals["token"]))
        self.var_level.set(str(vals["level"]))
        self.var_exp.set(str(vals["exp"]))
        self.char_lbl.set(f"角色解锁（已解锁 {len(vals['char_ids'])} 个）")
        self.content_lbl.set(f"内容解锁（已解锁 {len(vals['content_ids'])} 个）")
        self.set_status(f"已读取存档：{slot}（{len(self.entries)} 个数据键）", True)

    def append_ids(self, key, name, var):
        if self.entries is None:
            self.set_status("请先「读取存档」", False)
            return
        text = var.get().strip()
        try:
            ok, msg = apply_append_ids(self.entries, key, text, name)
        except Exception as e:
            ok, msg = False, f"追加{name}失败：{e}"
        if ok:
            var.set("")
            now = load_current_values(self.entries)
            if key == "profile.character-unlock":
                self.char_lbl.set(f"角色解锁（已解锁 {len(now['char_ids'])} 个）")
            else:
                self.content_lbl.set(f"内容解锁（已解锁 {len(now['content_ids'])} 个）")
            self.set_status(f"{msg}。请点「保存修改」写回存档。", True)
        else:
            self.set_status(msg, False)

    def save_changes(self):
        if self.entries is None or self.owner_path is None:
            self.set_status("请先「读取存档」再保存", False)
            return
        try:
            coin = int(self.var_coin.get().strip())
            bomb = int(self.var_bomb.get().strip())
            key = int(self.var_key.get().strip())
            egg = int(self.var_egg.get().strip())
            token = int(self.var_token.get().strip())
            level = int(self.var_level.get().strip())
            exp = int(self.var_exp.get().strip())
        except ValueError:
            self.set_status("数值格式不正确：请填写整数（如 999999）", False)
            return
        try:
            msgs = []
            ok, m = apply_resource(self.entries, coin, bomb, key, egg)
            msgs.append(m)
            ok2, m2 = apply_token(self.entries, token)
            msgs.append(m2)
            ok3, m3 = apply_evolution(self.entries, level, exp)
            msgs.append(m3)
            if not (ok and ok2 and ok3):
                self.set_status("；".join(msgs), False)
                return
            bak = write_owner_file(self.owner_path, self.version, self.entries, do_backup=True)
        except Exception as e:
            self.set_status(f"保存失败：{e}（已保留原档，可再次读取）", False)
            return
        self.set_status(f"保存成功！备份：{os.path.basename(bak)}，写回已校验通过。重启游戏生效。", True)


# ============================== 主窗口 ==============================

class TrainerApp:
    """霓虹深渊2 修改器 —— 现代化圆角 Trainer 布局。"""

    W, H = 1000, 800

    def __init__(self, root):
        self.root = root
        self._minimizing = False
        root.title("霓虹深渊2 修改器")
        root.configure(bg=COL_BG)
        root.overrideredirect(True)
        root.protocol("WM_DELETE_WINDOW", self._shutdown)
        try:
            root.attributes("-topmost", True)
        except Exception:
            pass
        root.geometry(f"{self.W}x{self.H}+{max(0, (root.winfo_screenwidth() - self.W)//2)}+{max(0, (root.winfo_screenheight() - self.H)//2 - 20)}")

        self._build_topbar()
        self._build_notebook()
        self._build_statusbar()
        self.status_var.set("就绪：默认页「游戏内修改」— 先启动游戏再点「附加游戏进程」")

        # Win11 物理圆角；不支持则 Canvas 深色圆角背景兜底
        root.update_idletasks()
        if not apply_win11_corners(root):
            self._fallback_corner_decor()
        root.bind("<Map>", self._on_map)

    # ---------- 顶部标题栏（可拖动 + 语言切换 + 最小化 + 关闭） ----------
    def _build_topbar(self):
        bar = tk.Frame(self.root, bg="#26262E", height=38)
        bar.pack(fill="x")
        bar.pack_propagate(False)

        tk.Label(bar, text="NEON ABYSS 2 TRAINER", bg="#26262E", fg=COL_ORANGE,
                 font=FONT_TITLE, padx=14).pack(side="left")

        # 关闭（最右）
        self.close_btn = RoundedButton(bar, text="X", command=self._shutdown,
                                       width=30, height=24, radius=8, fill="#33343C",
                                       fg=COL_TEXT, font=FONT_B)
        self.close_btn.pack(side="right", padx=(2, 8), pady=7)
        # 最小化（X 左侧）
        self.min_btn = RoundedButton(bar, text="—", command=self._minimize,
                                     width=30, height=24, radius=8, fill="#33343C",
                                     fg=COL_TEXT, font=FONT_B)
        self.min_btn.pack(side="right", padx=(4, 2), pady=7)

        self.lang_btns = {}
        for i, lang in enumerate(("简体", "繁体", "English")):
            lb = tk.Label(bar, text=lang, bg="#26262E",
                          fg=COL_ORANGE if i == 0 else COL_DIM,
                          font=FONT_SMALL, padx=8, cursor="hand2")
            lb.pack(side="right")
            lb.bind("<Button-1>", lambda e, t=lang, k=lang: self._switch_lang(t))
            self.lang_btns[lang] = lb

        # 拖动
        bar.bind("<ButtonPress-1>", self._start_move)
        bar.bind("<B1-Motion>", self._on_move)

    def _start_move(self, event):
        self._drag_x = event.x
        self._drag_y = event.y

    def _on_move(self, event):
        x = self.root.winfo_x() + event.x - self._drag_x
        y = self.root.winfo_y() + event.y - self._drag_y
        self.root.geometry(f"+{x}+{y}")

    def _minimize(self):
        """最小化：先去掉无边框，iconify 后任务栏图标不消失；<Map> 恢复无边框。"""
        self._minimizing = True
        try:
            self.root.overrideredirect(False)
        except Exception:
            pass
        self.root.iconify()

    def _on_map(self, _e):
        if self._minimizing:
            self._minimizing = False
            try:
                self.root.overrideredirect(True)
            except Exception:
                pass
            self.root.update_idletasks()
            apply_win11_corners(self.root)

    def _fallback_corner_decor(self):
        """系统不支持 Win11 物理圆角时：Canvas 深色圆角背景兜底。"""
        cv = tk.Canvas(self.root, bg=COL_BG, highlightthickness=0)
        cv.place(x=0, y=0, relwidth=1, relheight=1)
        _round_rect(cv, 5, 5, self.W - 5, self.H - 5, 14, fill=COL_BG,
                    outline="#3A3A44", width=2, tags=("deco",))
        cv.lower()

    def _switch_lang(self, lang):
        for k, lb in self.lang_btns.items():
            lb.configure(fg=COL_ORANGE if k == lang else COL_DIM)
        self.set_status(f"语言切换：{lang}（演示，UI 仅展示）", True)

    # ---------- 页签 ----------
    def _build_notebook(self):
        style = ttk.Style()
        try:
            style.theme_use("clam")
            style.configure("Trainer.TNotebook", background=COL_BG, borderwidth=0,
                            tabmargins=(10, 6, 0, 0))
            # 极简精修：未选中 #222222 底 + 灰字；选中橙底黑字；去 border/focus 防橙色溢出
            style.layout("Trainer.TNotebook.Tab",
                         [("Notebook.tab", {"sticky": "nswe", "children":
                             [("Notebook.padding", {"side": "top", "sticky": "nswe",
                                 "children": [("Notebook.focus", {"side": "top",
                                     "sticky": "nswe", "children":
                                         [("Notebook.label", {"side": "top",
                                                              "sticky": ""})]})]})]}),
                          ("Notebook.border", {"sticky": "nswe", "children": []})])
            style.configure("Trainer.TNotebook.Tab", background="#222222",
                            foreground=COL_DIM, font=FONT_B, padding=(18, 6),
                            borderwidth=0, focuscolor=COL_BG)
            style.map("Trainer.TNotebook.Tab",
                      background=[("selected", COL_ORANGE), ("!selected", "#222222")],
                      foreground=[("selected", "#111111"), ("!selected", COL_DIM)],
                      borderwidth=[("selected", 0), ("!selected", 0)],
                      padding=[("selected", (18, 6)), ("!selected", (18, 6))],
                      lightcolor=[("selected", COL_ORANGE), ("!selected", "#222222")],
                      darkcolor=[("selected", COL_ORANGE), ("!selected", "#222222")])
            # 存档槽位下拉：深底白字、选中橙、去边框
            style.configure("Trainer.TCombobox",
                            fieldbackground=COL_ENTRY, background=COL_ENTRY,
                            foreground=COL_TEXT, arrowcolor=COL_ORANGE,
                            bordercolor=COL_BORDER, lightcolor=COL_BORDER,
                            darkcolor=COL_BORDER, padding=4,
                            selectbackground=COL_ORANGE, selectforeground="#111111")
            style.map("Trainer.TCombobox",
                      fieldbackground=[("readonly", COL_ENTRY)],
                      foreground=[("readonly", COL_TEXT)],
                      selectbackground=[("readonly", COL_ORANGE)],
                      selectforeground=[("readonly", "#111111")])
        except Exception:
            pass
        self.notebook = ttk.Notebook(self.root, style="Trainer.TNotebook")
        self.notebook.pack(fill="both", expand=True, padx=15, pady=(10, 2))

        self.page1 = tk.Frame(self.notebook, bg=COL_BG)
        self.page2 = tk.Frame(self.notebook, bg=COL_BG)
        self.notebook.add(self.page1, text="游戏内修改")
        self.notebook.add(self.page2, text="存档修改")
        self.notebook.select(self.page1)  # 默认停在游戏内修改页

        self.mem_tab = MemoryTab(self.page1, self._set_status)
        self.save_tab = SaveTab(self.page2, self._set_status)

    # ---------- 状态栏 ----------
    def _build_statusbar(self):
        bottom = tk.Frame(self.root, bg=COL_BG)
        bottom.pack(fill="x", padx=15, pady=(2, 12))
        self.status_var = StringVar(value="就绪")
        self.status_label = tk.Label(bottom, textvariable=self.status_var, bg=COL_BG,
                                     fg=COL_DIM, font=FONT_SMALL, anchor="w",
                                     justify="left", wraplength=900)
        self.status_label.pack(fill="x")

    def _set_status(self, msg, ok=True):
        self.status_var.set(msg)
        self.status_label.configure(fg=COL_OK if ok else COL_ERR)

    def _shutdown(self):
        try:
            if self.mem_tab.engine:
                self.mem_tab.engine.stop()
        except Exception:
            pass
        # overrideredirect(True) 下直接 destroy 可能导致假死/残留：先恢复窗口边框
        # 属性（恢复正常窗口），再销毁，避免无边框窗口关闭异常
        try:
            self.root.overrideredirect(False)
        except Exception:
            pass
        try:
            self.root.destroy()
        except Exception:
            pass
        # PyInstaller 打包后强制结束引导进程，避免窗口关闭后进程残留
        try:
            os._exit(0)
        except Exception:
            pass


# ============================== 启动 ==============================

def main():
    ap = argparse.ArgumentParser(description="霓虹深渊2 修改器 GUI 版（现代化圆角 Trainer 模板）")
    ap.add_argument("--selftest", action="store_true", help="自检模式：创建窗口后立即退出")
    args = ap.parse_args()

    root = tk.Tk()
    app = TrainerApp(root)
    if args.selftest:
        root.update_idletasks()
        root.update()
        # 验证默认页
        tabs = [app.notebook.tab(app.notebook.select(), "text")]
        assert tabs == ["游戏内修改"], f"默认页错误: {tabs}"
        # 验证功能行完整（受限项不展示，可用项全保留）
        assert len(app.mem_tab.rows) == 23 - len(LIMITED), \
            f"功能行数错误: {len(app.mem_tab.rows)}"
        fids = {s.fid for s in CHEATS if s.fid not in LIMITED}
        assert fids == set(app.mem_tab.rows.keys()), "功能清单与行映射不一致"
        kinds = [r["kind"] for r in app.mem_tab.rows.values()]
        assert kinds.count("toggle") == 12 and kinds.count("spin") == 6, \
            f"控件类型分布异常: toggle={kinds.count('toggle')} spin={kinds.count('spin')}"
        root.destroy()
        print(f"SELFTEST OK 默认页={tabs[0]} 功能项={len(app.mem_tab.rows)} "
              f"(toggle={kinds.count('toggle')} spin={kinds.count('spin')}) "
              f"内存模块={NeonProcess.__name__} 可用")
        return
    root.mainloop()


if __name__ == "__main__":
    main()
