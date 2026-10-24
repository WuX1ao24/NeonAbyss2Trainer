# -*- coding: utf-8 -*-
"""
====================================================================
  trainer_ui_template.py
  通用游戏修改器（Trainer）UI 模板 —— 纯原生 Tkinter 实现
====================================================================
运行环境 : Python 3.11 + 原生 Tkinter（禁止 CustomTkinter 等任何第三方 UI 库）
窗口风格 : 无边框深色主题（#1B1C1E）+ 橙色高亮（#FF8C00）
布局结构 : 左右分栏
             ├─ 左侧 280px  游戏信息区（封面占位 + 进程信息 + 版本/更新）
             └─ 右侧自适应  上半：Hotkeys / Options 快捷键功能列表（可滚动）
                            下半：Selected Character Bonus Stats 属性面板（Grid 4 列）

核心组件（面向对象封装）：
    TrainerGUI    —— 主窗口类，负责整体布局、拖动、关闭、语言切换
    ToggleSwitch  —— 自定义开关（tk.Canvas 画圆角矩形 + 滑动圆圈，点击切换）
    SpinEntry     —— 数值调节框（深色 Entry + < > 增减按钮）
    ScrollableFrame —— 可滚动容器（ttk.Style 美化 TScrollbar）

使用方法：
    python trainer_ui_template.py            # 正常启动
    python trainer_ui_template.py --selftest # 自检模式：创建窗口后立即退出

预留接入说明：
    - self.game_cover_path / set_game_cover() ：替换左侧封面图（支持 PNG/GIF）
    - self.func_specs / self.var_bindings      ：功能列表数据绑定变量，
      后续接入 ctypes 内存读写时，直接读取/写入这些变量即可。
    - TrainerGUI.update_binding(label, value)  ：供后台线程更新 UI 数值。
====================================================================
"""

import sys
import argparse
import tkinter as tk
from tkinter import ttk

# ------------------------------------------------------------------
# 主题色常量（集中管理，便于全局统一配色）
# ------------------------------------------------------------------
COL_BG        = "#1B1C1E"   # 主背景（深灰黑）
COL_BG_LIGHT  = "#232426"   # 次级背景（卡片/分区底色）
COL_ENTRY_BG  = "#2A2A2A"   # 输入框背景
COL_ENTRY_FG  = "#FFFFFF"   # 输入框文字（白）
COL_TEXT      = "#E0E0E0"   # 正文浅灰
COL_WHITE     = "#FFFFFF"   # 纯白
COL_GRAY      = "#8A8A8A"   # 弱化灰（快捷键未激活、说明文字）
COL_DARK_GRAY = "#3A3B3D"   # 封面占位深灰
COL_ORANGE    = "#FF8C00"   # 高亮橙（Toggle 开、标题、更新链接）
COL_ACTIVE_BG = "#FF8C00"   # 快捷键 ACTIVE 状态背景（同橙色）
COL_ACTIVE_FG = "#000000"   # 快捷键 ACTIVE 状态文字（黑）
COL_BORDER    = "#333435"   # 分隔线/边框

FONT_SMALL   = ("Segoe UI", 9)
FONT_NORMAL  = ("Segoe UI", 10)
FONT_BOLD    = ("Segoe UI", 10, "bold")
FONT_TITLE   = ("Segoe UI", 12, "bold")
FONT_COVER   = ("Segoe UI", 14, "bold")


# ==================================================================
# 组件一：ToggleSwitch —— 自定义 Toggle 开关
# ------------------------------------------------------------------
# 实现原理：
#   使用 tk.Canvas 绘制一个"圆角矩形轨道" + 一个"圆形滑块"。
#   轨道圆角通过 create_polygon + smooth=True 形成（见 _rounded_rect）。
#   点击画布时切换 self._state，并通过 _animate() 以 after 定时器
#   分步移动滑块 x 坐标，形成平滑滑动动画（开=橙色轨道/滑块靠右，
#   关=灰色轨道/滑块靠左）。
#   每个开关内置一个 tk.BooleanVar（self.var），供外部数据绑定：
#       读取：  toggle.var.get()          → True / False
#       设置：  toggle.var.set(True)
#       回调：  ToggleSwitch(..., command=fn)  fn(new_state) 在切换后触发
# ==================================================================
class ToggleSwitch(tk.Canvas):
    # 轨道与滑块几何尺寸（相对布局，便于统一）
    _W = 44            # 画布总宽
    _H = 22            # 画布总高
    _R = 10            # 轨道圆角半径
    _PAD = 3           # 滑块距轨道边缘的内边距
    _DIA = _H - 2 * _PAD   # 滑块直径（16px）

    def __init__(self, master, initial=False, command=None, **kw):
        super().__init__(master, width=self._W, height=self._H,
                         bg=COL_BG, highlightthickness=0, **kw)
        self._state = bool(initial)          # 当前开关状态
        self.var = tk.BooleanVar(value=bool(initial))  # 数据绑定变量（预留 ctypes 接入）
        self._command = command              # 切换后回调（可选）
        self._anim_after = None              # 动画定时器句柄（防止重叠动画）
        self.bind("<Button-1>", self._on_click)
        self._draw()

    # ---- 绘制圆角矩形（polygon + smooth 平滑圆角）----
    def _rounded_rect(self, x1, y1, x2, y2, r, **kw):
        pts = [
            x1 + r, y1,
            x2 - r, y1, x2, y1, x2, y1 + r,
            x2, y2 - r, x2, y2, x2 - r, y2,
            x1 + r, y2, x1, y2, x1, y2 - r,
            x1, y1 + r, x1, y1,
        ]
        return self.create_polygon(pts, smooth=True, **kw)

    def _draw(self):
        """根据当前状态绘制轨道与滑块（无动画，直接定位）。"""
        self.delete("all")
        # 轨道：开=橙色，关=灰色
        track_color = COL_ORANGE if self._state else COL_GRAY
        self._track = self._rounded_rect(0, 0, self._W, self._H, self._R,
                                         fill=track_color, outline=track_color)
        # 滑块：白色小圆（y 方向居中，x 方向按状态左右）
        cx = self._W - self._PAD - self._DIA if self._state else self._PAD
        self._knob = self.create_oval(cx, self._PAD,
                                      cx + self._DIA, self._PAD + self._DIA,
                                      fill=COL_WHITE, outline="")

    # ---- 点击切换 ----
    def _on_click(self, event):
        # 取消上一次未完成的动画，避免动画堆叠
        if self._anim_after is not None:
            self.after_cancel(self._anim_after)
            self._anim_after = None
        self._state = not self._state
        self.var.set(self._state)            # 同步数据绑定变量
        if self._command:
            self._command(self._state)
        self._animate()

    # ---- 平滑滑动动画 ----
    # 目标 x 坐标 = 开→右端 / 关→左端；每次 after(15ms) 移动 2px，
    # 直到到达目标，删除旧滑块并重绘（或直接移动滑块坐标）。
    def _animate(self):
        target = self._W - self._PAD - self._DIA if self._state else self._PAD

        def _step():
            self._anim_after = None
            coords = self.coords(self._knob)
            cur = coords[0]
            if abs(target - cur) > 2:        # 未到位则继续滑动
                nxt = cur + (2 if target > cur else -2)
                self.coords(self._knob, nxt, self._PAD,
                            nxt + self._DIA, self._PAD + self._DIA)
                self._anim_after = self.after(15, _step)
            else:                            # 到位后重绘（更新轨道颜色）
                self.itemconfigure(self._track, fill=COL_ORANGE if self._state else COL_GRAY,
                                   outline=COL_ORANGE if self._state else COL_GRAY)
                self.coords(self._knob, target, self._PAD,
                            target + self._DIA, self._PAD + self._DIA)

        _step()

    # ---- 外部接口：直接设置状态（供内存线程调用）----
    def set_state(self, state):
        if self._anim_after is not None:
            self.after_cancel(self._anim_after)
            self._anim_after = None
        self._state = bool(state)
        self.var.set(self._state)
        self._draw()


# ==================================================================
# 组件二：SpinEntry —— 数值调节框
# ------------------------------------------------------------------
# 实现原理：
#   左侧 tk.Entry（深色背景 #2A2A2A + 白字）直接输入；
#   右侧两个小按钮 "<" / ">" 用于按 step 增减数值。
#   内部使用 tk.StringVar 保存文本，提供 get_value() / set_value()
#   并支持 min / max 钳制。后续 ctypes 内存读写可直接绑定该数值。
# ==================================================================
class SpinEntry(tk.Frame):
    def __init__(self, master, value=0, step=1, min_value=None, max_value=None,
                 width=9, command=None, **kw):
        super().__init__(master, bg=COL_BG, **kw)
        self.step = step
        self.min_value = min_value
        self.max_value = max_value
        self._command = command
        self.var = tk.StringVar(value=str(value))     # 数据绑定变量

        # 数值输入框（深色 Entry 白字）
        self.entry = tk.Entry(self, textvariable=self.var, width=width,
                              bg=COL_ENTRY_BG, fg=COL_ENTRY_FG,
                              insertbackground=COL_WHITE, relief="flat",
                              justify="right", font=FONT_NORMAL)
        self.entry.pack(side="left", ipady=2)
        self.entry.bind("<Return>", lambda e: self._clamp())

        # 减少 / 增加按钮（深色底、橙色文字）
        self.btn_dec = self._mk_btn("<", lambda: self._bump(-self.step))
        self.btn_dec.pack(side="left", padx=(2, 0))
        self.btn_inc = self._mk_btn(">", lambda: self._bump(self.step))
        self.btn_inc.pack(side="left", padx=(0, 2))

    def _mk_btn(self, text, cmd):
        return tk.Button(self, text=text, command=cmd, width=2,
                         bg=COL_BG_LIGHT, fg=COL_ORANGE, relief="flat",
                         font=FONT_BOLD, cursor="hand2",
                         activebackground=COL_ORANGE, activeforeground="#000000")

    # 步进增减
    def _bump(self, delta):
        try:
            val = float(self.var.get())
        except ValueError:
            val = 0.0
        val += delta
        if self.min_value is not None and val < self.min_value:
            val = self.min_value
        if self.max_value is not None and val > self.max_value:
            val = self.max_value
        self.var.set(str(int(val) if val == int(val) else val))
        if self._command:
            self._command(self.get_value())

    # 钳制到 min/max（回车时触发）
    def _clamp(self):
        try:
            val = float(self.var.get())
        except ValueError:
            val = 0.0
        if self.min_value is not None and val < self.min_value:
            val = self.min_value
        if self.max_value is not None and val > self.max_value:
            val = self.max_value
        self.var.set(str(int(val) if val == int(val) else val))

    # ---- 外部接口 ----
    def get_value(self):
        try:
            v = float(self.var.get())
            return int(v) if v == int(v) else v
        except ValueError:
            return 0

    def set_value(self, value):
        self.var.set(str(value))


# ==================================================================
# 组件三：ScrollableFrame —— 可滚动容器
# ------------------------------------------------------------------
# 实现原理：
#   Canvas 作为视口，内部 Frame 通过 create_window 挂载；
#   内容尺寸变化时更新 scrollregion；滚轮事件转发给画布。
#   TScrollbar 使用 ttk.Style 美化（暗色主题，见 _setup_style）。
# ==================================================================
class ScrollableFrame(tk.Frame):
    def __init__(self, master, width=0, bg=COL_BG, **kw):
        super().__init__(master, bg=bg, **kw)
        self.canvas = tk.Canvas(self, bg=bg, highlightthickness=0)
        self.vbar = ttk.Scrollbar(self, orient="vertical",
                                  style="Trainer.TScrollbar",
                                  command=self.canvas.yview)
        self.inner = tk.Frame(self.canvas, bg=bg)
        self._win_id = self.canvas.create_window((0, 0), window=self.inner, anchor="nw")
        self.canvas.configure(yscrollcommand=self.vbar.set)
        self.canvas.pack(side="left", fill="both", expand=True)
        self.vbar.pack(side="right", fill="y")

        # 内容尺寸变化 → 更新画布滚动区域 & 窗口宽度（跟随视口）
        self.inner.bind("<Configure>",
                        lambda e: self.canvas.configure(scrollregion=self.canvas.bbox("all")))
        self.canvas.bind("<Configure>",
                         lambda e: self.canvas.itemconfigure(self._win_id, width=e.width))

        # 滚轮滚动（Windows: delta 为 120 的倍数）
        def _on_wheel(event):
            self.canvas.yview_scroll(int(-event.delta / 120), "units")
        self.canvas.bind_all("<MouseWheel>", _on_wheel)

    # 供外部向滚动区内添加内容的入口
    def content(self):
        return self.inner


# ==================================================================
# 主类：TrainerGUI —— 通用 Trainer 窗口
# ==================================================================
class TrainerGUI:
    def __init__(self, root: tk.Tk):
        self.root = root
        self.root.title("Trainer UI Template")

        # ---- 窗口基本属性：无边框 + 深色背景 + 约 1000x600 ----
        self.W, self.H = 1000, 600
        self.root.overrideredirect(True)                       # 无边框窗口
        self.root.configure(bg=COL_BG)
        # 居中显示（基于屏幕分辨率）
        sw, sh = self.root.winfo_screenwidth(), self.root.winfo_screenheight()
        self.root.geometry(f"{self.W}x{self.H}+{max(0, (sw - self.W)//2)}+{max(0, (sh - self.H)//2 - 20)}")

        # ---- 暗色主题：美化 ttk 组件（滚动条 / 输入框）----
        self._setup_style()

        # ---- 数据绑定预留区（后续接入 ctypes 内存读写的挂载点）----
        self.game_info = {
            "process": "[YourGame.exe]",   # 实际接入时改为进程名
            "pid": "[12345]",              # 实际接入时改为 PID
            "developer": "[YourName]",     # 作者署名
            "version": "v1.0.0",
        }
        self.game_cover_path = None        # 封面图片路径（预留替换接口）
        self.var_bindings = {}             # {功能名: 绑定的变量}，供内存线程读写
        self.func_controls = {}            # {功能名: 控件对象}，供 UI 更新
        self._lang = "简体"                # 当前语言（仅 UI 展示）

        # ---- 功能列表数据规格（模板示例，可自由增删）----
        # kind 三种取值：
        #   "toggle" : ToggleSwitch 开关（value=初始状态）
        #   "spin"   : SpinEntry 数值调节（value=初始值, step=步进, min/max=范围）
        #   "text"   : 纯文本状态（value=显示文本，只读展示）
        # hotkey_active 表示该快捷键当前是否处于 ACTIVE（橙色高亮）
        self.func_specs = [
            {"hotkey": "Num 1",      "label": "Unlimited Health", "kind": "toggle", "value": False, "hotkey_active": False},
            {"hotkey": "Num 2",      "label": "One Hit Kill",     "kind": "toggle", "value": False, "hotkey_active": False},
            {"hotkey": "Num 3",      "label": "Unlimited Mana",   "kind": "toggle", "value": False, "hotkey_active": False},
            {"hotkey": "Ctrl+Num 1", "label": "Money Value",      "kind": "spin",   "value": 9999999, "step": 1000,  "min": 0, "hotkey_active": True},
            {"hotkey": "Ctrl+Num 2", "label": "Speed Multiplier", "kind": "spin",   "value": 1.0,     "step": 0.1,   "min": 0.1, "hotkey_active": False},
            {"hotkey": "F1",         "label": "Kill Counter",     "kind": "text",   "value": "9999999", "hotkey_active": False},
            {"hotkey": "F2",         "label": "Score",            "kind": "text",   "value": "0",       "hotkey_active": False},
            {"hotkey": "F3",         "label": "God Mode",         "kind": "toggle", "value": False, "hotkey_active": False},
            {"hotkey": "F4",         "label": "Set Move Speed",   "kind": "spin",   "value": 5.0,     "step": 0.5,   "min": 0.5, "hotkey_active": False},
            {"hotkey": "F5",         "label": "Infinite Ammo",    "kind": "toggle", "value": True,  "hotkey_active": True},
            {"hotkey": "F6",         "label": "Max Gold Gain",    "kind": "spin",   "value": 100,     "step": 10,    "min": 1, "hotkey_active": False},
            {"hotkey": "F7",         "label": "Time Scale",       "kind": "spin",   "value": 1.0,     "step": 0.1,   "min": 0.1, "hotkey_active": False},
        ]

        # ---- 属性面板数据规格（Selected Character Bonus Stats）----
        self.stats_specs = [
            ("Ctrl+F1", "Max HP"),
            ("Ctrl+F2", "Max MP"),
            ("Ctrl+F3", "Max Shield"),
            ("Ctrl+F4", "Base Damage"),
            ("Ctrl+F5", "Move Speed"),
            ("Ctrl+F6", "Gold Gain"),
        ]

        # ================= 构建界面 =================
        self._build_topbar()          # 顶部标题栏（可拖动 + 语言切换 + 关闭）
        self._build_body()            # 主体左右分栏

    # ------------------------------------------------------------
    # ttk.Style 暗色主题
    # ------------------------------------------------------------
    def _setup_style(self):
        style = ttk.Style(self.root)
        style.theme_use("clam")       # clam 主题最易自定义颜色
        # 重要：clam 主题下自定义带点样式名的 Scrollbar 必须先复制
        # 默认的 Vertical.TScrollbar layout，否则创建时抛
        # "Layout Vertical.Trainer.TScrollbar not found"。
        style.layout("Trainer.TScrollbar", style.layout("Vertical.TScrollbar"))
        # 滚动条：深灰轨道 + 橙色滑块（箭头改为无边框扁平）
        style.configure("Trainer.TScrollbar",
                        background=COL_BG_LIGHT,
                        troughcolor=COL_BG,
                        bordercolor=COL_BG,
                        arrowcolor=COL_ORANGE,
                        lightcolor=COL_BG_LIGHT,
                        darkcolor=COL_BG_LIGHT,
                        relief="flat")
        style.map("Trainer.TScrollbar",
                  background=[("active", "#3A3B3D")])
        # 输入框：深色背景 + 白字（供 ttk.Entry 使用）
        style.configure("Trainer.TEntry",
                        fieldbackground=COL_ENTRY_BG,
                        foreground=COL_ENTRY_FG,
                        insertcolor=COL_WHITE,
                        bordercolor=COL_BG_LIGHT,
                        lightcolor=COL_BG_LIGHT,
                        darkcolor=COL_BG_LIGHT,
                        relief="flat",
                        padding=3)

    # ------------------------------------------------------------
    # 窗口拖动实现
    # ------------------------------------------------------------
    # 原理：无边框窗口没有标题栏，需要自己实现拖动。
    # 在拖动目标控件上记录鼠标按下时的全局坐标与窗口左上角坐标的差值，
    # 随后在鼠标移动事件中用新坐标减去该差值，得到窗口新位置并 geometry 设置。
    # 注意：几何坐标 winfo_x()/winfo_y() 返回的是窗口左上角在屏幕上的位置，
    #       拖动对象坐标 event.x_root/event.y_root 是鼠标的屏幕坐标。
    def _bind_drag(self, widget):
        widget.bind("<ButtonPress-1>", self._drag_start)
        widget.bind("<B1-Motion>", self._drag_move)

    def _drag_start(self, event):
        # 记录按下瞬间：鼠标屏幕坐标 - 窗口左上角坐标 = 固定偏移
        self._drag_off_x = event.x_root - self.root.winfo_x()
        self._drag_off_y = event.y_root - self.root.winfo_y()

    def _drag_move(self, event):
        # 移动时：窗口左上角 = 鼠标屏幕坐标 - 固定偏移
        x = event.x_root - self._drag_off_x
        y = event.y_root - self._drag_off_y
        self.root.geometry(f"+{x}+{y}")

    # ------------------------------------------------------------
    # 顶部标题栏
    # ------------------------------------------------------------
    def _build_topbar(self):
        bar = tk.Frame(self.root, bg=COL_BG_LIGHT, height=34)
        bar.pack(fill="x")
        bar.pack_propagate(False)
        self._bind_drag(bar)

        # 左侧标题文字（可拖动区域之一）
        title = tk.Label(bar, text="GAME TRAINER", bg=COL_BG_LIGHT,
                         fg=COL_ORANGE, font=FONT_TITLE)
        title.pack(side="left", padx=12)
        self._bind_drag(title)

        # 右上角：语言切换（简体/繁体/English，仅 UI 展示） + 关闭 X
        right = tk.Frame(bar, bg=COL_BG_LIGHT)
        right.pack(side="right", padx=8)
        self._build_lang_switcher(right)
        # 关闭按钮
        close = tk.Label(right, text="X", bg=COL_BG_LIGHT, fg=COL_TEXT,
                         font=FONT_BOLD, padx=8, cursor="hand2")
        close.pack(side="left", padx=(8, 0))
        close.bind("<Button-1>", lambda e: self.root.destroy())
        self._bind_drag(close)

    # 语言切换（仅展示，点击切换高亮语言）
    def _build_lang_switcher(self, parent):
        self._lang_labels = {}
        for lang in ("简体", "繁体", "English"):
            lab = tk.Label(parent, text=lang, bg=COL_BG_LIGHT,
                           fg=COL_ORANGE if lang == self._lang else COL_GRAY,
                           font=FONT_NORMAL, cursor="hand2", padx=6)
            lab.pack(side="left")
            lab.bind("<Button-1>", lambda e, l=lang: self._switch_lang(l))
            self._bind_drag(lab)
            self._lang_labels[lang] = lab

    def _switch_lang(self, lang):
        """仅 UI 展示的语言切换：高亮当前语言，其余置灰。"""
        self._lang = lang
        for name, lab in self._lang_labels.items():
            lab.configure(fg=COL_ORANGE if name == lang else COL_GRAY)

    # ------------------------------------------------------------
    # 主体：左右分栏
    # ------------------------------------------------------------
    def _build_body(self):
        body = tk.Frame(self.root, bg=COL_BG)
        body.pack(fill="both", expand=True)
        self._build_left_panel(body)   # 左：280px 游戏信息区
        self._build_right_panel(body)  # 右：功能区 + 属性面板

    # ---------- 左侧：游戏信息区（宽 280px） ----------
    def _build_left_panel(self, parent):
        left = tk.Frame(parent, bg=COL_BG, width=280)
        left.pack(side="left", fill="y")
        left.pack_propagate(False)     # 固定 280px 宽度

        # --- 封面占位（3:4 比例）---
        # 占位：宽 150 高 200（3:4）；调用 set_game_cover(path) 可替换为真实图片
        cover = tk.Label(left, text="GAME COVER", bg=COL_DARK_GRAY,
                         fg=COL_GRAY, font=FONT_COVER, width=18, height=8)
        cover.pack(pady=(16, 10))
        self.cover_label = cover

        # --- 游戏信息（居中浅灰小字，字号 9）---
        info = tk.Frame(left, bg=COL_BG)
        info.pack(fill="both", expand=True)
        lines = [
            f"Game Process Name: {self.game_info['process']}",
            f"Process ID: {self.game_info['pid']}",
            "─" * 22,                                   # 分隔线
            f"Trainer Developer: {self.game_info['developer']}",
        ]
        for text in lines:
            tk.Label(info, text=text, bg=COL_BG, fg=COL_GRAY,
                     font=FONT_SMALL).pack(pady=2)

        # --- 底部：版本 + 可点击橙色更新链接 ---
        bottom = tk.Frame(left, bg=COL_BG)
        bottom.pack(side="bottom", fill="x", pady=(0, 14))
        tk.Label(bottom, text=f"Trainer Version: {self.game_info['version']}",
                 bg=COL_BG, fg=COL_GRAY, font=FONT_SMALL).pack()
        update_link = tk.Label(bottom, text="Check for updates", bg=COL_BG,
                               fg=COL_ORANGE, font=FONT_SMALL, cursor="hand2")
        update_link.pack(pady=(2, 0))
        update_link.bind("<Button-1>", lambda e: self._check_updates())

    # 封面替换接口：预留，传入本地图片路径即可显示
    def set_game_cover(self, path):
        """替换左侧封面占位为真实图片（支持 PNG/GIF）。
        用法：trainer.set_game_cover("C:/path/cover.png")
        """
        self.game_cover_path = path
        try:
            img = tk.PhotoImage(file=path)
            # 等比例缩放至占位区域（宽≤150 高≤200）
            w, h = img.width(), img.height()
            scale = min(150 / w, 200 / h)
            if scale < 1:
                img = img.subsample(max(1, int(1 / scale)))
            self.cover_label.configure(image=img, text="", width=img.width(), height=img.height())
            self.cover_label.image = img   # 防止被 GC 回收
        except Exception:
            # 加载失败时保持占位文字
            self.cover_label.configure(image="", text="GAME COVER")

    def _check_updates(self):
        """更新检查（模板占位：此处可接入网络/本地版本比对）。"""
        # 简单演示：在标题栏闪烁提示（仅示例）
        self._lang_switch_flash("No updates available")

    def _lang_switch_flash(self, msg):
        """临时在右上角区域显示提示文字（演示用）。"""
        # 简化实现：直接输出到控制台；接入实际项目时可改为状态栏 Label
        print(f"[Trainer] {msg}")

    # ---------- 右侧：功能区（上半列表 + 下半属性面板） ----------
    def _build_right_panel(self, parent):
        right = tk.Frame(parent, bg=COL_BG)
        right.pack(side="left", fill="both", expand=True)
        self._build_functions_area(right)   # 上半
        self._build_stats_area(right)       # 下半

    # ---------- 上半：Hotkeys / Options 功能列表（可滚动） ----------
    def _build_functions_area(self, parent):
        upper = tk.Frame(parent, bg=COL_BG)
        upper.pack(side="top", fill="both", expand=True, padx=14, pady=(10, 4))

        # 两列标题
        hdr = tk.Frame(upper, bg=COL_BG)
        hdr.pack(fill="x", pady=(0, 4))
        tk.Label(hdr, text="Hotkeys", bg=COL_BG, fg=COL_WHITE,
                 font=FONT_BOLD, width=16, anchor="w").pack(side="left")
        tk.Label(hdr, text="Options", bg=COL_BG, fg=COL_WHITE,
                 font=FONT_BOLD, anchor="w").pack(side="left")

        # 可滚动列表容器（TScrollbar 已用暗色主题）
        scroller = ScrollableFrame(upper, bg=COL_BG)
        scroller.pack(fill="both", expand=True)
        content = scroller.content()

        # 逐行构建功能项（每行 Frame 高约 25px）
        for spec in self.func_specs:
            self._add_func_row(content, spec)

    # 单行：Hotkeys 文本 + Options（功能名 + 右侧控件）
    def _add_func_row(self, parent, spec):
        row = tk.Frame(parent, bg=COL_BG, height=25)
        row.pack(fill="x", pady=1)
        row.pack_propagate(False)          # 固定行高

        # --- Hotkeys 列 ---
        # ACTIVE 状态：橙色背景 + 黑字；否则浅灰文字
        hk_bg = COL_ACTIVE_BG if spec.get("hotkey_active") else COL_BG
        hk_fg = COL_ACTIVE_FG if spec.get("hotkey_active") else COL_GRAY
        hk = tk.Label(row, text=spec["hotkey"], bg=hk_bg, fg=hk_fg,
                      font=FONT_NORMAL, width=14, anchor="w", padx=4)
        hk.pack(side="left", fill="y")

        # --- Options 列：功能名（白色）+ 右侧控件 ---
        opt = tk.Frame(row, bg=COL_BG)
        opt.pack(side="left", fill="x", expand=True)
        lab = tk.Label(opt, text=spec["label"], bg=COL_BG, fg=COL_WHITE,
                       font=FONT_NORMAL, anchor="w")
        lab.pack(side="left", padx=(10, 8))

        ctrl = tk.Frame(opt, bg=COL_BG)
        ctrl.pack(side="right")

        # 三种控件样式：toggle / spin / text
        kind = spec["kind"]
        if kind == "toggle":
            tg = ToggleSwitch(ctrl, initial=spec.get("value", False),
                              command=lambda s, v: self._on_toggle(s, v))
            tg.pack(side="left")
            self.func_controls[spec["label"]] = tg
            self.var_bindings[spec["label"]] = tg.var     # 绑定变量
        elif kind == "spin":
            sp = SpinEntry(ctrl, value=spec.get("value", 0),
                           step=spec.get("step", 1),
                           min_value=spec.get("min"),
                           max_value=spec.get("max"),
                           command=lambda s, v: self._on_spin(s, v))
            sp.pack(side="left")
            self.func_controls[spec["label"]] = sp
            self.var_bindings[spec["label"]] = sp.var     # 绑定变量
        elif kind == "text":
            txt = tk.Label(ctrl, text=spec.get("value", "0"), bg=COL_BG,
                           fg=COL_TEXT, font=FONT_NORMAL)
            txt.pack(side="left")
            self.func_controls[spec["label"]] = txt
            self.var_bindings[spec["label"]] = None       # 只读展示

        # 保存每行的 hotkey Label 引用（后续可按 ACTIVE 状态动态刷新）
        spec["_hk_label"] = hk

    # 控件回调示例（接入内存读写时可在此写入游戏进程）
    def _on_toggle(self, label, state):
        print(f"[Trainer] Toggle '{label}' -> {state}")

    def _on_spin(self, label, value):
        print(f"[Trainer] Spin '{label}' -> {value}")

    # ---------- 下半：Selected Character Bonus Stats 属性面板 ----------
    def _build_stats_area(self, parent):
        lower = tk.Frame(parent, bg=COL_BG_LIGHT)
        lower.pack(side="bottom", fill="x", padx=14, pady=(6, 12))

        # 橙色加粗标题（grid 跨 4 列，与下方表格共用 grid 管理器）
        tk.Label(lower, text="Selected Character Bonus Stats", bg=COL_BG_LIGHT,
                 fg=COL_ORANGE, font=FONT_BOLD, anchor="w").grid(
            row=0, column=0, columnspan=4, sticky="w", padx=10, pady=(8, 4))
        lower.grid_columnconfigure(2, weight=1)   # 数值列可拉伸

        # Grid 4 列多行：快捷键 | 属性名 | 数值 Entry | 状态/单位（第4列占位）
        # 4 列表头（第 1 行）
        for col, text, anchor in ((0, "Hotkey", "w"), (1, "Stat", "w"),
                                  (2, "Value", "e"), (3, "", "w")):
            tk.Label(lower, text=text, bg=COL_BG_LIGHT, fg=COL_GRAY,
                     font=FONT_SMALL, anchor=anchor).grid(row=1, column=col,
                                                          padx=(10, 20), pady=(0, 2), sticky="w")

        # 数据行（从第 2 行开始）
        self.stats_entries = {}
        for i, (hotkey, name) in enumerate(self.stats_specs, start=2):
            # 快捷键（灰色小字）
            tk.Label(lower, text=hotkey, bg=COL_BG_LIGHT, fg=COL_GRAY,
                     font=FONT_SMALL, width=10, anchor="w").grid(
                row=i, column=0, padx=(10, 20), pady=2, sticky="w")
            # 属性名（白色）
            tk.Label(lower, text=name, bg=COL_BG_LIGHT, fg=COL_WHITE,
                     font=FONT_NORMAL, anchor="w").grid(
                row=i, column=1, padx=(0, 20), pady=2, sticky="w")
            # 数值 Entry（深色背景，ttk 暗色主题）
            var = tk.StringVar(value="100")
            ent = ttk.Entry(lower, textvariable=var, style="Trainer.TEntry", width=12)
            ent.grid(row=i, column=2, padx=(0, 20), pady=2, sticky="e")
            self.stats_entries[name] = (var, ent)
            # 第4列：单位占位（可放 % / pts 等）
            tk.Label(lower, text="pts", bg=COL_BG_LIGHT, fg=COL_GRAY,
                     font=FONT_SMALL).grid(row=i, column=3, padx=(0, 10), pady=2, sticky="w")

    # ------------------------------------------------------------
    # 对外接口：更新某个功能的绑定数值（供 ctypes 后台线程调用）
    # ------------------------------------------------------------
    def update_binding(self, label, value):
        """按功能名更新 UI 数值/状态。
        示例：trainer.update_binding("Money Value", 123456)
        """
        ctrl = self.func_controls.get(label)
        if ctrl is None:
            return
        if isinstance(ctrl, ToggleSwitch):
            ctrl.set_state(bool(value))
        elif isinstance(ctrl, SpinEntry):
            ctrl.set_value(value)
        elif isinstance(ctrl, tk.Label):
            ctrl.configure(text=str(value))


# ==================================================================
# 入口
# ==================================================================
def main():
    parser = argparse.ArgumentParser(description="通用 Trainer UI 模板")
    parser.add_argument("--selftest", action="store_true",
                        help="自检模式：创建窗口后立即退出（用于验证语法与可启动性）")
    args = parser.parse_args()

    root = tk.Tk()
    app = TrainerGUI(root)
    if args.selftest:
        root.update_idletasks()
        root.update()
        # 自检断言：关键组件已构建
        assert hasattr(app, "cover_label"), "封面占位缺失"
        assert len(app.func_controls) == len(app.func_specs), "功能控件数量不符"
        assert len(app.stats_entries) == len(app.stats_specs), "属性面板行数不符"
        root.destroy()
        print("SELFTEST OK  窗口=1000x600 功能项=%d 属性行=%d 组件=ToggleSwitch/SpinEntry/ScrollableFrame"
              % (len(app.func_controls), len(app.stats_entries)))
        return
    root.mainloop()


if __name__ == "__main__":
    main()
