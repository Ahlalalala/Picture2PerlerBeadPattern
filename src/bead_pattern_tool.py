# -*- coding: utf-8 -*-
"""
拼豆图纸生成工具 v3
将普通图片转换为拼豆图纸(PNG)，支持自动抠图、手动选区、MARD色系匹配
"""

import os
import threading
import tkinter as tk
from tkinter import ttk, filedialog, messagebox
from PIL import Image, ImageDraw, ImageTk, ImageFont, ImageFilter
import numpy as np

try:
    from windnd import hook_dropfiles
    HAS_DND = True
except ImportError:
    HAS_DND = False

from mard_palette import MARD_PALETTE, get_palette_dict
from color_matcher import get_matcher
from auto_cutout import remove_background, create_full_mask, apply_mask
from cartoon_quantizer import quantize_cartoon_to_grid


class BeadPatternTool:
    """拼豆图纸生成工具主类"""

    PALETTE_DICT = get_palette_dict()

    def __init__(self, root):
        self.root = root
        self.root.title("拼豆图纸生成工具 v3")
        self.root.geometry("1280x850")
        self.root.minsize(1050, 700)

        # ---- 图像状态 ----
        self.original_image = None
        self.mask = None
        self.initial_mask = None
        self.selected_image = None
        self.bead_pattern = None

        # ---- 图纸编辑状态 ----
        self.grid_w = None
        self.grid_h = None
        self.color_ids = None
        self.edit_color = "A1"
        self._color_counts_list = []
        self._protected_outline_ids = set()

        # ---- 画布状态 ----
        self.canvas_w = 750
        self.canvas_h = 680
        self.scale = 1.0
        self.offset_x = 0
        self.offset_y = 0

        # ---- 撤回 ----
        self._undo_stack = []          # [(stage, snapshot), ...]
        self._undo_max = 30
        self._brush_stroke_snapshot = None  # 画笔单次笔画的起始快照
        self.tool_mode = "rect"
        self.painting = False
        self.start_x = self.start_y = None
        self.last_x = self.last_y = None
        self.lasso_points = []
        self.selection_rect = None
        self._edit_dirty = False
        self._render_after_id = None

        # ---- 匹配器 ----
        self._matcher = None

        # ---- 拖放 ----
        self._dnd_pending_paths = []

        # ---- 预览渲染参数 ----
        self._preview_bead_size = 20
        self._preview_zoom = 1.0        # 缩放倍率
        self._preview_show_grid = True
        self._preview_show_ids = True    # 预览默认显示色号

        # ---- 阶段4画布平移 ----
        self._edit_pan_x = 0             # 图像在画布中的偏移（像素）
        self._edit_pan_y = 0
        self._edit_panning = False       # 是否正在拖拽平移
        self._edit_pan_start = None      # 平移起始鼠标位置

        # ---- 阶段参数缓存（返回上一步时保留） ----
        self._stage_params = {}  # {stage_num: {param_key: value, ...}}

        # ---- 查看模式 ----
        self._view_mode = "complete"

        # ---- 颜号显示开关 ----
        self._preview_ids_var = None

        self._build_ui()
        self._show_stage1()
        self._setup_dnd()

    # ==================================================================
    # DND 拖放
    # ==================================================================
    def _setup_dnd(self):
        if not HAS_DND:
            return

        # windnd回调在非主线程执行，不能直接调用任何tkinter API
        # 解决方案：回调仅保存路径到列表，由主线程poll定时检查
        self._dnd_pending_paths = []

        def on_drop(files):
            """windnd回调 - 仅保存路径，不触碰tkinter"""
            if not files:
                return
            raw = files[0]
            path = None
            if isinstance(raw, str):
                path = raw
            else:
                for enc in ('utf-8', 'mbcs', 'gbk', 'cp936', 'latin-1'):
                    try:
                        path = raw.decode(enc)
                        break
                    except (UnicodeDecodeError, LookupError):
                        continue
            if path:
                self._dnd_pending_paths.append(path)

        # 只在root上hook一个widget，避免GIL竞争
        try:
            hook_dropfiles(self.root, func=on_drop)
        except Exception:
            pass

        # 主线程定时poll（200ms）
        self._poll_dnd()

    def _poll_dnd(self):
        """主线程定时检查是否有待处理的拖放文件"""
        if self._dnd_pending_paths:
            path = self._dnd_pending_paths.pop(0)
            self._load_image_from_path(path)
        self.root.after(200, self._poll_dnd)

    @property
    def matcher(self):
        if self._matcher is None:
            self.status_var.set("正在初始化颜色匹配引擎...")
            self.root.update()
            self._matcher = get_matcher()
        return self._matcher

    # ==================================================================
    # UI 构建
    # ==================================================================
    def _build_ui(self):
        status_frame = ttk.Frame(self.root, relief="sunken", borderwidth=1)
        status_frame.pack(fill="x", side="top")
        self.status_var = tk.StringVar(value="就绪 - 请选择图片开始")
        ttk.Label(status_frame, textvariable=self.status_var,
                  font=("Microsoft YaHei", 10)).pack(side="left", padx=10, pady=3)

        main_frame = ttk.Frame(self.root)
        main_frame.pack(fill="both", expand=True, padx=5, pady=5)

        # 左侧画布
        canvas_frame = ttk.LabelFrame(main_frame, text="画布")
        canvas_frame.pack(side="left", fill="both", expand=True, padx=(0, 5))
        self.canvas = tk.Canvas(canvas_frame, bg="#e0e0e0",
                                width=self.canvas_w, height=self.canvas_h)
        self.canvas.pack(fill="both", expand=True, padx=2, pady=2)
        self.canvas.bind("<ButtonPress-1>", self._on_press)
        self.canvas.bind("<B1-Motion>", self._on_drag)
        self.canvas.bind("<ButtonRelease-1>", self._on_release)
        self.canvas.bind("<ButtonPress-3>", self._on_pan_start)     # 右键开始平移
        self.canvas.bind("<B3-Motion>", self._on_pan_move)
        self.canvas.bind("<ButtonRelease-3>", self._on_pan_end)
        self.canvas.bind("<ButtonPress-2>", self._on_pan_start)     # 中键开始平移
        self.canvas.bind("<B2-Motion>", self._on_pan_move)
        self.canvas.bind("<ButtonRelease-2>", self._on_pan_end)
        self.canvas.bind("<MouseWheel>", self._on_canvas_mousewheel)  # 滚轮平移
        self.canvas.bind("<Configure>", self._on_canvas_resize)

        # 右侧面板(可滚动)
        self.ctrl_outer = ttk.Frame(main_frame, width=360)
        self.ctrl_outer.pack(side="right", fill="y", padx=(5, 0))
        self.ctrl_outer.pack_propagate(False)

        self._ctrl_canvas = tk.Canvas(self.ctrl_outer, highlightthickness=0, width=340)
        self._ctrl_scrollbar = ttk.Scrollbar(self.ctrl_outer, orient="vertical",
                                             command=self._ctrl_canvas.yview)
        self.ctrl_content = ttk.Frame(self._ctrl_canvas)

        self.ctrl_content.bind("<Configure>",
                               lambda e: self._ctrl_canvas.configure(
                                   scrollregion=self._ctrl_canvas.bbox("all")))
        self._ctrl_canvas_window = self._ctrl_canvas.create_window((0, 0), window=self.ctrl_content, anchor="nw")
        self._ctrl_canvas.configure(yscrollcommand=self._ctrl_scrollbar.set)
        self._ctrl_scrollbar.pack(side="right", fill="y")
        self._ctrl_canvas.pack(side="left", fill="both", expand=True)

        # 内容窗口宽度跟随画布宽度
        def _on_ctrl_canvas_configure(e):
            self._ctrl_canvas.itemconfig(self._ctrl_canvas_window, width=e.width)
        self._ctrl_canvas.bind("<Configure>", _on_ctrl_canvas_configure)

        # 鼠标滚轮 - 路由到正确的widget
        # 鼠标在左侧画布上 → 平移图纸 / 缩放
        # 鼠标在右侧面板上 → 滚动面板
        self.canvas.bind("<Enter>",
                         lambda e: self.root.bind_all("<MouseWheel>", self._on_canvas_mousewheel))
        self.canvas.bind("<Leave>",
                         lambda e: self.root.unbind_all("<MouseWheel>"))

        self._ctrl_canvas.bind("<Enter>",
                               lambda e: self.root.bind_all("<MouseWheel>", self._on_ctrl_scroll))
        self._ctrl_canvas.bind("<Leave>",
                               lambda e: self.root.unbind_all("<MouseWheel>"))

        # 全局快捷键
        self.root.bind("<Control-z>", self._on_undo)
        self.root.bind("<Control-Z>", self._on_undo)

    def _clear_ctrl(self):
        for w in self.ctrl_content.winfo_children():
            w.destroy()

    # ==================================================================
    # 撤回系统
    # ==================================================================
    def _push_undo(self, snapshot):
        """保存一个撤销快照"""
        self._undo_stack.append((self.stage, snapshot))
        if len(self._undo_stack) > self._undo_max:
            self._undo_stack.pop(0)

    def _on_undo(self, event=None):
        """Ctrl+Z 撤回"""
        if not self._undo_stack:
            return
        stage, snapshot = self._undo_stack.pop()
        if stage == 2 and snapshot is not None:
            self.mask = snapshot.copy()
            self._display_mask_preview()
            self.status_var.set("已撤回选区操作")
        elif stage == 4 and snapshot is not None:
            self.color_ids = snapshot.copy()
            self._rebuild_color_counts()
            self._update_stats_display()
            self._schedule_refresh()
            self.status_var.set("已撤回编辑操作")

    # ==================================================================
    # 阶段参数缓存
    # ==================================================================
    def _save_stage_params(self, stage_num):
        """保存当前阶段的UI参数"""
        p = {}
        if stage_num == 1:
            p['auto_cutout'] = self.auto_cutout_var.get()
        elif stage_num == 3:
            p['size_preset'] = self.size_preset_var.get()
            p['custom_w'] = self.custom_w_var.get()
            p['custom_h'] = self.custom_h_var.get()
            p['max_colors'] = self.max_colors_var.get()
            p['bead_size'] = self.bead_size_var.get()
            p['show_grid'] = self.show_grid_var.get()
            p['show_ids'] = self.show_ids_var.get()
            p['cartoon'] = self._cartoon_var.get()
        elif stage_num == 4:
            p['preview_zoom'] = self._preview_zoom
            p['preview_show_ids'] = self._preview_ids_var.get()
            p['preview_show_grid'] = self._preview_grid_var.get()
            p['tool'] = self.tool_var.get()
            p['action'] = self.select_action_var.get()
            p['view_mode'] = self._view_var.get()
        self._stage_params[stage_num] = p

    def _restore_stage_params(self, stage_num):
        """恢复之前保存的阶段参数"""
        p = self._stage_params.get(stage_num)
        if not p:
            return
        try:
            if stage_num == 1:
                if 'auto_cutout' in p:
                    self.auto_cutout_var.set(p['auto_cutout'])
            elif stage_num == 3:
                if 'size_preset' in p:
                    self.size_preset_var.set(p['size_preset'])
                    self._on_size_preset()
                if 'custom_w' in p:
                    self.custom_w_var.set(p['custom_w'])
                if 'custom_h' in p:
                    self.custom_h_var.set(p['custom_h'])
                if 'max_colors' in p:
                    v = int(p['max_colors'])
                    self.max_colors_var.set(v)
                    self._max_colors_entry.delete(0, "end")
                    self._max_colors_entry.insert(0, str(v))
                    self.max_colors_label.config(
                        text=f"{v}" if v > 0 else "0 = 不限制")
                if 'bead_size' in p:
                    v = int(p['bead_size'])
                    self.bead_size_var.set(v)
                    self._bead_size_entry.delete(0, "end")
                    self._bead_size_entry.insert(0, str(v))
                    self.bead_size_label.config(text=f"{v} px")
                if 'show_grid' in p:
                    self.show_grid_var.set(p['show_grid'])
                if 'show_ids' in p:
                    self.show_ids_var.set(p['show_ids'])
                if 'cartoon' in p:
                    self._cartoon_var.set(p['cartoon'])
            elif stage_num == 4:
                if 'preview_zoom' in p:
                    self._preview_zoom = p['preview_zoom']
                    self._zoom_var.set(p['preview_zoom'])
                    self._zoom_entry.delete(0, "end")
                    self._zoom_entry.insert(0, f"{p['preview_zoom']:.1f}")
                if 'preview_show_ids' in p:
                    self._preview_ids_var.set(p['preview_show_ids'])
                if 'preview_show_grid' in p:
                    self._preview_grid_var.set(p['preview_show_grid'])
                if 'tool' in p:
                    self.tool_var.set(p['tool'])
                if 'action' in p:
                    self.select_action_var.set(p['action'])
                if 'view_mode' in p:
                    self._view_var.set(p['view_mode'])
        except (tk.TclError, AttributeError):
            pass  # UI控件未创建时忽略

    # 阶段 1：加载图片
    # ==================================================================
    def _show_stage1(self):
        self.stage = 1
        self._clear_ctrl()
        self.canvas.delete("all")

        ttk.Label(self.ctrl_content, text="阶段 1：加载图片",
                  font=("Microsoft YaHei", 14, "bold")).pack(pady=(10, 15))
        ttk.Button(self.ctrl_content, text="📂 选择图片...",
                   command=self._load_image).pack(fill="x", pady=5, padx=5)

        if HAS_DND:
            ttk.Label(self.ctrl_content,
                      text="✦ 支持直接拖放图片到窗口或画布",
                      font=("Microsoft YaHei", 9), foreground="gray").pack(pady=(0, 5))

        ttk.Separator(self.ctrl_content, orient="horizontal").pack(fill="x", pady=15, padx=5)
        ttk.Label(self.ctrl_content, text="抠图选项",
                  font=("Microsoft YaHei", 12, "bold")).pack(pady=(5, 10))
        self.auto_cutout_var = tk.BooleanVar(value=True)
        ttk.Checkbutton(self.ctrl_content, text="自动抠出主体 (AI)",
                        variable=self.auto_cutout_var).pack(anchor="w", padx=15)
        ttk.Label(self.ctrl_content,
                  text="勾选后自动识别主体，\n未选中区域可在下一阶段手动精修",
                  font=("Microsoft YaHei", 9), foreground="gray").pack(padx=15, anchor="w")

        ttk.Separator(self.ctrl_content, orient="horizontal").pack(fill="x", pady=15, padx=5)
        self.next_btn = ttk.Button(self.ctrl_content, text="下一步 →",
                                   command=self._go_stage2, state="disabled")
        self.next_btn.pack(fill="x", pady=5, padx=5)
        self._restore_stage_params(1)
        self.status_var.set("就绪 - 请选择图片开始")

    def _load_image(self):
        path = filedialog.askopenfilename(
            title="选择图片",
            filetypes=[("图片文件", "*.jpg *.jpeg *.png *.bmp *.webp *.tiff *.gif"),
                       ("所有文件", "*.*")],
            initialdir=os.path.expanduser("~"))
        if path:
            self._load_image_from_path(path)

    def _load_image_from_path(self, path):
        try:
            self.original_image = Image.open(path).convert("RGBA")
            self._stage_params.clear()  # 换图时清除所有阶段参数缓存
            self._undo_stack.clear()
            self.status_var.set(f"已加载: {os.path.basename(path)} "
                                f"({self.original_image.width}×{self.original_image.height})")
            self.next_btn.config(state="normal")
            self._display_image(self.original_image)
        except Exception as e:
            messagebox.showerror("错误", f"无法加载图片:\n{e}")

    def _go_stage2(self):
        if self.original_image is None:
            return
        self._save_stage_params(1)
        if self.auto_cutout_var.get():
            self.status_var.set("正在自动抠图(首次需下载模型)...")
            self.root.update()
            threading.Thread(target=self._do_auto_cutout, daemon=True).start()
        else:
            self.mask = create_full_mask(self.original_image.width,
                                         self.original_image.height)
            self.initial_mask = self.mask.copy()
            self._show_stage2()

    def _do_auto_cutout(self):
        try:
            self.mask = remove_background(self.original_image)
            self.initial_mask = self.mask.copy()
            self.root.after(0, self._show_stage2)
        except ImportError as e:
            self.root.after(0, self._show_auto_cutout_error, "缺少依赖", str(e))
        except Exception as e:
            self.root.after(
                0,
                self._show_auto_cutout_error,
                "错误",
                f"自动抠图失败:\n{e}",
            )

    def _show_auto_cutout_error(self, title, message):
        self.status_var.set("自动抠图失败，可取消自动抠图后继续")
        messagebox.showerror(title, message)

    # ==================================================================
    # 阶段 2：选区精修
    # ==================================================================
    def _show_stage2(self):
        self.stage = 2
        self._clear_ctrl()
        ttk.Label(self.ctrl_content, text="阶段 2：选区精修",
                  font=("Microsoft YaHei", 14, "bold")).pack(pady=(10, 5))
        ttk.Label(self.ctrl_content, text="PS风格：圈出区域自动填充",
                  font=("Microsoft YaHei", 9), foreground="gray").pack(pady=(0, 10))

        tf = ttk.LabelFrame(self.ctrl_content, text="选区工具")
        tf.pack(fill="x", padx=5, pady=5)
        self.tool_var = tk.StringVar(value="rect")
        for text, val in [("▭ 矩形选框", "rect"), ("✋ 套索工具", "lasso"),
                          ("✏️ 画笔涂抹", "brush")]:
            ttk.Radiobutton(tf, text=text, variable=self.tool_var, value=val).pack(
                anchor="w", padx=5, pady=2)

        af = ttk.LabelFrame(self.ctrl_content, text="操作")
        af.pack(fill="x", padx=5, pady=5)
        self.select_action_var = tk.StringVar(value="add")
        ttk.Radiobutton(af, text="＋ 添加到选区",
                        variable=self.select_action_var, value="add").pack(
            anchor="w", padx=5, pady=2)
        ttk.Radiobutton(af, text="－ 从选区减去",
                        variable=self.select_action_var, value="erase").pack(
            anchor="w", padx=5, pady=2)

        ttk.Separator(self.ctrl_content, orient="horizontal").pack(fill="x", pady=10, padx=5)
        for text, cmd in [("← 上一步", self._back_to_stage1),
                          ("🔄 重置选区", self._reset_mask),
                          ("🔄 全选", self._select_all),
                          ("✅ 确认选区 →", self._go_stage3)]:
            ttk.Button(self.ctrl_content, text=text, command=cmd).pack(fill="x", padx=5, pady=2)

        self.status_var.set("选区精修 - 在画布上框选/圈出目标区域")
        self._display_mask_preview()

    def _reset_mask(self):
        if self.initial_mask:
            self.mask = self.initial_mask.copy()
            self._display_mask_preview()

    def _select_all(self):
        if self.original_image:
            self.mask = create_full_mask(self.original_image.width, self.original_image.height)
            self._display_mask_preview()

    def _back_to_stage1(self):
        self._save_stage_params(2)
        self.stage = 1
        self.mask = self.initial_mask = None
        self._show_stage1()
        if self.original_image:
            self._display_image(self.original_image)

    # ==================================================================
    # 阶段 3：参数设置 (含颜色合并)
    # ==================================================================
    def _go_stage3(self):
        if self.mask is None:
            return
        self._save_stage_params(2)
        self.selected_image = apply_mask(self.original_image, self.mask)
        bbox = self.selected_image.getbbox()
        if bbox:
            self.selected_image = self.selected_image.crop(bbox)
        if self.selected_image.width < 2 or self.selected_image.height < 2:
            messagebox.showwarning("警告", "选区太小")
            return
        self._show_stage3()

    def _show_stage3(self):
        self.stage = 3
        self._clear_ctrl()
        self._display_image(self.selected_image)

        ttk.Label(self.ctrl_content, text="阶段 3：图纸设置",
                  font=("Microsoft YaHei", 14, "bold")).pack(pady=(10, 5))
        ttk.Label(self.ctrl_content,
                  text=f"选区: {self.selected_image.width} × {self.selected_image.height}").pack(pady=3)

        # 尺寸
        sf = ttk.LabelFrame(self.ctrl_content, text="拼豆板尺寸")
        sf.pack(fill="x", padx=5, pady=5)
        self.size_preset_var = tk.StringVar(value="52x52")
        for text, val in [("52 × 52 (标准小)", "52x52"),
                          ("78 × 78 (标准大)", "78x78"),
                          ("自定义尺寸", "custom")]:
            ttk.Radiobutton(sf, text=text, variable=self.size_preset_var,
                            value=val, command=self._on_size_preset).pack(
                anchor="w", padx=10, pady=3)

        cf = ttk.Frame(self.ctrl_content)
        cf.pack(fill="x", padx=5)
        ttk.Label(cf, text="宽:").pack(side="left", padx=(10, 2))
        self.custom_w_var = tk.StringVar(value="52")
        self.custom_w_entry = ttk.Entry(cf, textvariable=self.custom_w_var, width=6, state="disabled")
        self.custom_w_entry.pack(side="left", padx=2)
        ttk.Label(cf, text="高:").pack(side="left", padx=(10, 2))
        self.custom_h_var = tk.StringVar(value="52")
        self.custom_h_entry = ttk.Entry(cf, textvariable=self.custom_h_var, width=6, state="disabled")
        self.custom_h_entry.pack(side="left", padx=2)

        # 颜色合并
        mf = ttk.LabelFrame(self.ctrl_content, text="颜色合并")
        mf.pack(fill="x", padx=5, pady=5)
        ttk.Label(mf, text="最大颜色数量 (0=不限制):",
                  font=("Microsoft YaHei", 9)).pack(anchor="w", padx=10, pady=(5, 0))
        self.max_colors_var = tk.IntVar(value=0)
        self.max_colors_label = ttk.Label(mf, text="0 = 不限制")
        self.max_colors_label.pack(anchor="w", padx=10)
        mc_f = ttk.Frame(mf)
        mc_f.pack(fill="x", padx=10, pady=5)
        self._max_colors_entry = ttk.Entry(mc_f, width=6, font=("Consolas", 9))
        self._max_colors_entry.pack(side="right", padx=2)
        self._max_colors_entry.insert(0, "0")
        self._max_colors_entry.bind("<Return>", self._on_max_colors_entry)
        self._max_colors_entry.bind("<FocusOut>", self._on_max_colors_entry)
        ttk.Scale(mc_f, from_=0, to=50, variable=self.max_colors_var,
                  orient="horizontal",
                  command=lambda v: (self.max_colors_label.config(
                      text=f"{int(float(v))}" if int(float(v)) > 0 else "0 = 不限制"),
                      self._max_colors_entry.delete(0, "end"),
                      self._max_colors_entry.insert(0, str(int(float(v)))))
                  ).pack(side="left", fill="x", expand=True)
        ttk.Label(mf, text="合并相近颜色，减少图纸复杂度",
                  font=("Microsoft YaHei", 8), foreground="gray").pack(padx=10, anchor="w")

        # 卡通优化
        cf = ttk.LabelFrame(self.ctrl_content, text="卡通优化")
        cf.pack(fill="x", padx=5, pady=5)
        self._cartoon_var = tk.BooleanVar(value=False)
        ttk.Checkbutton(cf, text="启用卡通模式",
                        variable=self._cartoon_var).pack(anchor="w", padx=10, pady=(5, 0))
        ttk.Label(cf, text="识别黑色轮廓，并统一平涂区域填色",
                  font=("Microsoft YaHei", 8), foreground="gray").pack(padx=10, anchor="w")

        # 显示设置
        bf = ttk.LabelFrame(self.ctrl_content, text="显示设置")
        bf.pack(fill="x", padx=5, pady=5)
        ttk.Label(bf, text="导出拼豆像素大小:").pack(anchor="w", padx=10, pady=(5, 0))
        self.bead_size_var = tk.IntVar(value=30)
        self.bead_size_label = ttk.Label(bf, text="30 px")
        self.bead_size_label.pack(anchor="w", padx=10)
        bs_f = ttk.Frame(bf)
        bs_f.pack(fill="x", padx=10, pady=5)
        self._bead_size_entry = ttk.Entry(bs_f, width=6, font=("Consolas", 9))
        self._bead_size_entry.pack(side="right", padx=2)
        self._bead_size_entry.insert(0, "30")
        self._bead_size_entry.bind("<Return>", self._on_bead_size_entry)
        self._bead_size_entry.bind("<FocusOut>", self._on_bead_size_entry)
        ttk.Scale(bs_f, from_=12, to=60, variable=self.bead_size_var,
                  orient="horizontal",
                  command=lambda v: (self.bead_size_label.config(text=f"{int(float(v))} px"),
                      self._bead_size_entry.delete(0, "end"),
                      self._bead_size_entry.insert(0, str(int(float(v)))))
                  ).pack(side="left", fill="x", expand=True)

        self.show_grid_var = tk.BooleanVar(value=True)
        ttk.Checkbutton(bf, text="显示网格线",
                        variable=self.show_grid_var).pack(anchor="w", padx=10)
        self.show_ids_var = tk.BooleanVar(value=True)
        ttk.Checkbutton(bf, text="显示色号",
                        variable=self.show_ids_var).pack(anchor="w", padx=10)

        ttk.Separator(self.ctrl_content, orient="horizontal").pack(fill="x", pady=10, padx=5)
        ttk.Button(self.ctrl_content, text="← 上一步",
                   command=self._back_to_stage2).pack(fill="x", padx=5, pady=2)
        ttk.Button(self.ctrl_content, text="🎨 生成拼豆图纸",
                   command=self._generate_pattern).pack(fill="x", padx=5, pady=2)
        self.status_var.set("设置参数后点击生成")
        self._restore_stage_params(3)

    def _on_max_colors_entry(self, event=None):
        """从输入框读取最大颜色数量"""
        try:
            val = int(self._max_colors_entry.get())
            val = max(0, min(50, val))
        except ValueError:
            val = 0
        self.max_colors_var.set(val)
        self._max_colors_entry.delete(0, "end")
        self._max_colors_entry.insert(0, str(val))
        self.max_colors_label.config(
            text=f"{val}" if val > 0 else "0 = 不限制")

    def _on_bead_size_entry(self, event=None):
        """从输入框读取拼豆像素大小"""
        try:
            val = int(self._bead_size_entry.get())
            val = max(12, min(60, val))
        except ValueError:
            val = 30
        self.bead_size_var.set(val)
        self._bead_size_entry.delete(0, "end")
        self._bead_size_entry.insert(0, str(val))
        self.bead_size_label.config(text=f"{val} px")

    def _on_size_preset(self):
        if self.size_preset_var.get() == "custom":
            self.custom_w_entry.config(state="normal")
            self.custom_h_entry.config(state="normal")
        else:
            self.custom_w_entry.config(state="disabled")
            self.custom_h_entry.config(state="disabled")

    def _get_output_size(self):
        p = self.size_preset_var.get()
        if p == "52x52":
            return 52, 52
        elif p == "78x78":
            return 78, 78
        try:
            return max(1, min(500, int(self.custom_w_var.get()))), \
                   max(1, min(500, int(self.custom_h_var.get())))
        except ValueError:
            messagebox.showerror("错误", "请输入有效数字")
            return None, None

    def _back_to_stage2(self):
        self._save_stage_params(3)
        self._show_stage2()

    # ==================================================================
    # 图纸生成 (含颜色合并)
    # ==================================================================
    @staticmethod
    def _bilateral_smooth(rgb_array, radius=2, sigma_color=25.0, iterations=3):
        """边缘保留双边滤波（纯numpy，无额外依赖）

        对每个像素，在半径内按颜色相似度加权平均：
        - 相似色权重≈1 → 平滑（消除平涂区域抗锯齿/JPEG噪声）
        - 差异大的颜色权重≈0 → 不平滑（保护黑色描边不被模糊进棕色）

        Parameters
        ----------
        rgb_array : np.ndarray, shape (H, W, 3), uint8
        radius : int, 邻域半径（实际窗口 2r+1 × 2r+1）
        sigma_color : float, 颜色域标准差（越小越保护边缘）
        iterations : int, 迭代次数（多次迭代效果更干净）

        Returns
        -------
        np.ndarray, shape (H, W, 3), uint8
        """
        img = rgb_array.astype(np.float32)
        h, w = int(img.shape[0]), int(img.shape[1])
        r = int(radius)
        inv_2sigma2 = 1.0 / (2.0 * sigma_color * sigma_color)

        for _ in range(iterations):
            padded = np.pad(img, ((r, r), (r, r), (0, 0)),
                           mode='edge')
            result = np.zeros((h, w, 3), dtype=np.float64)
            weight_sum = np.zeros((h, w), dtype=np.float64)

            for dy in range(-r, r + 1):
                for dx in range(-r, r + 1):
                    ys, ye = int(r + dy), int(r + dy + h)
                    xs, xe = int(r + dx), int(r + dx + w)
                    neighbor = padded[ys:ye, xs:xe].astype(np.float64)
                    color_diff_sq = np.sum(
                        (neighbor - img.astype(np.float64)) ** 2, axis=2)
                    wt = np.exp(-color_diff_sq * inv_2sigma2)
                    result += neighbor * wt[:, :, None]
                    weight_sum += wt

            img = (result / weight_sum[:, :, None]).astype(np.float32)

        return np.clip(img, 0, 255).astype(np.uint8)

    @staticmethod
    def _kmeans_quantize(rgb_array, k, max_iter=30, n_init=3):
        """K-Means颜色聚类量化（梯度加权，保护描边/线条）

        与标准K-Means的区别：
        - 计算每个像素的梯度幅值（与四邻域色差）
        - 梯度高的像素（边缘/描边）获得30倍权重
        - K-Means++初始化偏向高梯度像素
        - 质心更新使用加权平均

        这确保少量但重要的描边像素不被大量平涂像素吞没质心。

        Parameters
        ----------
        rgb_array : np.ndarray, shape (H, W, 3), uint8
        k : int, 聚类数
        max_iter : int, 每次初始质心的最大迭代次数
        n_init : int, 不同初始质心的运行次数，取最优

        Returns
        -------
        np.ndarray, shape (H, W, 3), uint8, 量化后的RGB图像
        """
        h, w = int(rgb_array.shape[0]), int(rgb_array.shape[1])
        pixels = rgb_array.reshape(-1, 3).astype(np.float32)

        # ---- 梯度计算 ----
        padded = np.pad(rgb_array.astype(np.float32), ((1, 1), (1, 1), (0, 0)),
                        mode='edge')
        dx = padded[1:-1, 2:, :] - padded[1:-1, :-2, :]
        dy = padded[2:, 1:-1, :] - padded[:-2, 1:-1, :]
        grad_mag = np.sqrt(np.sum(dx ** 2 + dy ** 2, axis=2))  # (h, w)
        g_flat = grad_mag.reshape(-1)

        # 边缘像素权重放大30倍（线性缩放）
        g_max = g_flat.max()
        if g_max > 1.0:
            weights = 1.0 + 29.0 * (g_flat / g_max)  # 范围 [1, 30]
        else:
            weights = np.ones(len(pixels), dtype=np.float32)

        best_labels = None
        best_inertia = float('inf')

        for _ in range(n_init):
            # K-Means++ 初始化：优先选取高梯度像素作为质心
            centers = np.empty((k, 3), dtype=np.float32)
            # 第一个质心：偏向高梯度像素（描边颜色）
            if g_max > 1.0:
                edge_probs = weights / weights.sum()
                centers[0] = pixels[np.random.choice(len(pixels), p=edge_probs)]
            else:
                centers[0] = pixels[np.random.randint(len(pixels))]

            for i in range(1, k):
                # 分块计算到已有质心的最小距离
                min_dists = np.full(len(pixels), np.inf, dtype=np.float32)
                chunk = 4096
                for s in range(0, len(pixels), chunk):
                    e = min(s + chunk, len(pixels))
                    d = np.sum((pixels[s:e, None, :] - centers[None, :i, :]) ** 2,
                               axis=2)
                    np.minimum(min_dists[s:e], d.min(axis=1),
                               out=min_dists[s:e])
                # 加权概率：梯度高的像素更容易被选中为质心
                probs = min_dists * weights
                probs_sum = probs.sum()
                if probs_sum > 0:
                    probs /= probs_sum
                else:
                    probs = np.ones(len(pixels), dtype=np.float32) / len(pixels)
                centers[i] = pixels[np.random.choice(len(pixels), p=probs)]

            # K-Means 迭代
            for _ in range(max_iter):
                # 分配：每个像素到最近质心（分块计算）
                labels = np.empty(len(pixels), dtype=np.int32)
                chunk = 4096
                for s in range(0, len(pixels), chunk):
                    e = min(s + chunk, len(pixels))
                    d = np.sum((pixels[s:e, None, :] - centers[None, :, :]) ** 2,
                               axis=2)
                    labels[s:e] = np.argmin(d, axis=1)

                # 加权质心更新（np.average，梯度高的像素权重更大）
                new_centers = np.empty_like(centers)
                for j in range(k):
                    mask = labels == j
                    if mask.any():
                        wt = weights[mask]
                        new_centers[j] = np.average(pixels[mask], weights=wt,
                                                      axis=0)
                    else:
                        new_centers[j] = centers[j]

                if np.allclose(centers, new_centers, atol=0.5):
                    centers = new_centers
                    break
                centers = new_centers

            # 计算inertia（分块）
            inertia = 0.0
            chunk = 8192
            for s in range(0, len(pixels), chunk):
                e = min(s + chunk, len(pixels))
                inertia += np.sum((pixels[s:e] - centers[labels[s:e]]) ** 2)
            if inertia < best_inertia:
                best_inertia = inertia
                best_labels = labels
                best_centers = centers.copy()

        # 用最佳质心重建图像
        quantized = best_centers[best_labels].reshape((int(h), int(w), 3))
        return np.clip(quantized, 0, 255).astype(np.uint8)

    def _generate_pattern(self):
        grid_w, grid_h = self._get_output_size()
        if grid_w is None:
            return

        self._save_stage_params(3)
        self.status_var.set("正在生成拼豆图纸...")
        self.root.update()

        try:
            protected_ids = set()

            if self._cartoon_var.get():
                self.status_var.set("卡通模式: 识别轮廓和平涂区域...")
                self.root.update()
                cartoon_result = quantize_cartoon_to_grid(
                    self.selected_image, grid_w, grid_h, self.matcher,
                    self.PALETTE_DICT)
                color_ids = cartoon_result.color_ids
                color_counts = cartoon_result.color_counts
                protected_ids = cartoon_result.protected_ids
            else:
                src_w, src_h = self.selected_image.width, self.selected_image.height
                scale = min(grid_w / src_w, grid_h / src_h)
                new_w = max(1, int(src_w * scale))
                new_h = max(1, int(src_h * scale))
                pad_x = (grid_w - new_w) // 2
                pad_y = (grid_h - new_h) // 2

                resized = self.selected_image.resize((new_w, new_h), Image.LANCZOS)
                grid_img = Image.new("RGBA", (grid_w, grid_h), (0, 0, 0, 0))
                grid_img.paste(resized, (pad_x, pad_y))
                rgba_array = np.array(grid_img)
                color_ids, color_counts = self.matcher.match_image_fast(rgba_array)

            # 提前设置网格尺寸（_merge_colors需要引用）
            self.grid_w = grid_w
            self.grid_h = grid_h
            self._protected_outline_ids = protected_ids

            # 颜色合并
            max_c = self.max_colors_var.get()
            if max_c > 0 and len(color_counts) > max_c:
                self.status_var.set(f"正在合并颜色 ({len(color_counts)} → {max_c})...")
                self.root.update()
                color_ids = self._merge_colors(
                    color_ids, max_c, color_counts,
                    protected_ids=protected_ids)

            self.color_ids = color_ids
            self._rebuild_color_counts()
            self._show_stage4()
            self.status_var.set(f"图纸生成完成! {grid_w}×{grid_h}, "
                                f"{len(self._color_counts_list)} 色 — 可编辑")
        except Exception as e:
            import traceback
            traceback.print_exc()
            messagebox.showerror("错误", f"生成失败:\n{e}")

    def _merge_colors(self, color_ids, target_count, color_counts,
                      protected_ids=None):
        """合并颜色直到数量 <= target_count，使用CIEDE2000距离.

        protected_ids are kept as-is and are not used as fill merge targets.
        This prevents cartoon outlines from being swallowed by nearby browns.
        """
        counts = dict(color_counts)
        protected_ids = {str(cid) for cid in (protected_ids or set())}

        def is_protected(cid):
            return str(cid) in protected_ids

        if protected_ids:
            protected_count = sum(1 for cid in counts if is_protected(cid))
            fill_count = len(counts) - protected_count
            min_target = protected_count + (1 if fill_count > 0 else 0)
            target_count = max(target_count, min_target)

        # 预计算所有颜色的LAB
        lab_cache = {}
        for cid in counts:
            rgb = self.PALETTE_DICT[cid]
            lab_cache[cid] = self._rgb_to_lab_single(*rgb)

        while len(counts) > target_count:
            # 找最少使用的颜色
            merge_sources = [cid for cid in counts if not is_protected(cid)]
            if not merge_sources:
                break
            least = min(merge_sources, key=counts.get)
            least_lab = lab_cache[least]

            # 找CIEDE2000最近的邻居
            best = None
            best_de = float('inf')
            for cid in counts:
                if cid == least or is_protected(cid):
                    continue
                de = self._ciede2000_single(least_lab, lab_cache[cid])
                if de < best_de:
                    best_de = de
                    best = cid
            if best is None:
                break

            # 替换
            for y in range(self.grid_h):
                for x in range(self.grid_w):
                    if color_ids[y, x] == least:
                        color_ids[y, x] = best
            counts[best] = counts.get(best, 0) + counts.pop(least, 0)
            del lab_cache[least]

        return color_ids

    @staticmethod
    def _rgb_to_lab_single(r, g, b):
        """单个RGB转LAB"""
        from color_matcher import _rgb_to_lab
        L, a, bv = _rgb_to_lab(np.array([float(r)]), np.array([float(g)]), np.array([float(b)]))
        return (L[0], a[0], bv[0])

    @staticmethod
    def _ciede2000_single(lab1, lab2):
        """单个CIEDE2000距离"""
        from color_matcher import _ciede2000
        return float(_ciede2000(
            (np.array([lab1[0]]), np.array([lab1[1]]), np.array([lab1[2]])),
            (np.array([lab2[0]]), np.array([lab2[1]]), np.array([lab2[2]]))
        )[0])

    # ==================================================================
    # 阶段 4：编辑 + 导出
    # ==================================================================
    def _show_stage4(self):
        self.stage = 4
        self._clear_ctrl()
        self._view_mode = "complete"

        ttk.Label(self.ctrl_content, text="阶段 4：编辑 & 导出",
                  font=("Microsoft YaHei", 14, "bold")).pack(pady=(10, 5))
        ttk.Label(self.ctrl_content,
                  text=f"图纸: {self.grid_w} × {self.grid_h} 颗",
                  font=("Microsoft YaHei", 10)).pack(pady=3)

        # ---- 缩放与显示 ----
        zf = ttk.LabelFrame(self.ctrl_content, text="缩放与显示")
        zf.pack(fill="x", padx=5, pady=3)

        zoom_f = ttk.Frame(zf)
        zoom_f.pack(fill="x", padx=5, pady=2)
        ttk.Label(zoom_f, text="缩放:").pack(side="left")
        self._zoom_var = tk.DoubleVar(value=1.0)
        # 缩放输入框：支持直接输入数值
        self._zoom_entry = ttk.Entry(zoom_f, width=6, font=("Consolas", 9))
        self._zoom_entry.pack(side="right", padx=2)
        self._zoom_entry.insert(0, "1.0")
        self._zoom_entry.bind("<Return>", self._on_zoom_entry)
        self._zoom_entry.bind("<FocusOut>", self._on_zoom_entry)
        zoom_scale = ttk.Scale(zoom_f, from_=0.3, to=4.0, variable=self._zoom_var,
                               orient="horizontal",
                               command=self._on_zoom_change)
        zoom_scale.pack(side="left", fill="x", expand=True, padx=3)

        # 预览色号 + 网格开关
        opt_f = ttk.Frame(zf)
        opt_f.pack(fill="x", padx=5, pady=2)
        self._preview_ids_var = tk.BooleanVar(value=self._preview_show_ids)
        ttk.Checkbutton(opt_f, text="显示色号",
                        variable=self._preview_ids_var,
                        command=self._on_preview_ids_change).pack(side="left")
        self._preview_grid_var = tk.BooleanVar(value=True)
        ttk.Checkbutton(opt_f, text="网格线",
                        variable=self._preview_grid_var,
                        command=self._on_preview_grid_change).pack(side="left", padx=10)

        ttk.Label(zf, text="提示: 右键/中键拖拽平移，滚轮上下滚动",
                  font=("Microsoft YaHei", 8), foreground="gray").pack(padx=5, pady=(0, 3), anchor="w")

        # ---- 颜色选择器 (颜料盘) ----
        pf = ttk.LabelFrame(self.ctrl_content, text="颜色选择 (点击选取)")
        pf.pack(fill="x", padx=5, pady=5)

        # 当前颜色预览
        preview_f = ttk.Frame(pf)
        preview_f.pack(fill="x", padx=5, pady=3)
        self._color_preview = tk.Label(
            preview_f, text=f"  {self.edit_color}  ",
            bg=self._cid_to_hex(self.edit_color), fg="black",
            font=("Consolas", 12, "bold"), relief="raised", width=8)
        self._color_preview.pack(side="left", padx=(0, 10))

        # 颜色搜索
        ttk.Label(preview_f, text="搜索:").pack(side="left")
        self._color_search_var = tk.StringVar()
        search_entry = ttk.Entry(preview_f, textvariable=self._color_search_var, width=6)
        search_entry.pack(side="left", padx=2)

        # 颜料盘Canvas
        palette_canvas_frame = ttk.Frame(pf)
        palette_canvas_frame.pack(fill="x", padx=5, pady=3)

        self._palette_canvas = tk.Canvas(palette_canvas_frame, height=130,
                                         bg="white", highlightthickness=1,
                                         highlightbackground="#ccc")
        palette_scroll = ttk.Scrollbar(palette_canvas_frame, orient="vertical",
                                        command=self._palette_canvas.yview)
        self._palette_canvas.configure(yscrollcommand=palette_scroll.set)
        palette_scroll.pack(side="right", fill="y")
        self._palette_canvas.pack(fill="x")

        # 颜料盘滚轮绑定（鼠标进入/离开时切换全局滚轮目标）
        self._palette_canvas.bind("<Enter>", self._on_palette_enter)
        self._palette_canvas.bind("<Leave>", self._on_palette_leave)

        self._draw_palette_grid()

        # ---- 编辑工具 ----
        ef = ttk.LabelFrame(self.ctrl_content, text="编辑工具")
        ef.pack(fill="x", padx=5, pady=5)
        self.tool_var = tk.StringVar(value="point")
        for text, val in [("⊙ 点选", "point"), ("▭ 框选", "rect"),
                          ("✋ 套索", "lasso"), ("✏️ 画笔", "brush"),
                          ("💧 取色器", "eyedropper")]:
            ttk.Radiobutton(ef, text=text, variable=self.tool_var, value=val).pack(
                anchor="w", padx=5, pady=1)
        # 取色器/画笔模式下改变光标
        self.tool_var.trace_add("write", self._on_tool_cursor_change)

        # 操作 + 查看模式（合并节省空间）
        of = ttk.LabelFrame(self.ctrl_content, text="操作 / 查看")
        of.pack(fill="x", padx=5, pady=3)
        self.select_action_var = tk.StringVar(value="add")
        row_act1 = ttk.Frame(of)
        row_act1.pack(fill="x", padx=5, pady=1)
        ttk.Radiobutton(row_act1, text="＋ 添加", variable=self.select_action_var,
                        value="add").pack(side="left", padx=(0, 8))
        ttk.Radiobutton(row_act1, text="－ 删除", variable=self.select_action_var,
                        value="erase").pack(side="left")
        row_act2 = ttk.Frame(of)
        row_act2.pack(fill="x", padx=5, pady=1)
        self._view_var = tk.StringVar(value="complete")
        ttk.Radiobutton(row_act2, text="完整", variable=self._view_var,
                        value="complete",
                        command=self._on_view_change).pack(side="left", padx=(0, 8))
        ttk.Radiobutton(row_act2, text="仅当前色", variable=self._view_var,
                        value="edit_color",
                        command=self._on_view_change).pack(side="left")

        # ---- 去杂色 ----
        rf = ttk.LabelFrame(self.ctrl_content, text="去杂色 / 颜色替换")
        rf.pack(fill="x", padx=5, pady=5)

        row1 = ttk.Frame(rf)
        row1.pack(fill="x", padx=5, pady=2)
        ttk.Label(row1, text="替换:").pack(side="left")
        self._replace_src_var = tk.StringVar()
        self._replace_src_combo = ttk.Combobox(row1, textvariable=self._replace_src_var,
                                               width=6, state="readonly")
        self._replace_src_combo.pack(side="left", padx=2)
        ttk.Label(row1, text="→").pack(side="left", padx=3)
        self._replace_dst_var = tk.StringVar()
        self._replace_dst_combo = ttk.Combobox(row1, textvariable=self._replace_dst_var,
                                               width=6, state="readonly")
        self._replace_dst_combo.pack(side="left", padx=2)
        self._update_replace_combos()
        ttk.Button(row1, text="执行", command=self._do_replace_color).pack(side="left", padx=5)

        # 色号统计
        ttk.Separator(self.ctrl_content, orient="horizontal").pack(fill="x", pady=5, padx=5)
        ttk.Label(self.ctrl_content, text="色号统计:",
                  font=("Microsoft YaHei", 10, "bold")).pack(pady=(5, 2))
        self._stats_text = tk.Text(self.ctrl_content, height=6, font=("Consolas", 9),
                                   state="disabled", wrap="none")
        self._stats_text.pack(fill="x", padx=5, pady=2)
        self._update_stats_display()

        # 导出
        ttk.Separator(self.ctrl_content, orient="horizontal").pack(fill="x", pady=5, padx=5)
        ttk.Button(self.ctrl_content, text="💾 导出全部 (完整+分色+总览)",
                   command=self._export_all).pack(fill="x", padx=5, pady=2)
        ttk.Button(self.ctrl_content, text="💾 仅导出完整图纸(含总览)",
                   command=self._export_pattern).pack(fill="x", padx=5, pady=2)

        ttk.Separator(self.ctrl_content, orient="horizontal").pack(fill="x", pady=5, padx=5)
        ttk.Button(self.ctrl_content, text="← 返回参数",
                   command=self._back_to_stage3).pack(fill="x", padx=5, pady=2)
        ttk.Button(self.ctrl_content, text="🏠 重新开始",
                   command=self._restart).pack(fill="x", padx=5, pady=2)

        self._refresh_edit_view()
        self._center_edit_image()
        self._display_edit_image()
        # 恢复阶段4参数（可能触发缩放重渲染）
        old_zoom = self._preview_zoom
        self._restore_stage_params(4)
        if self._preview_zoom != old_zoom:
            self._schedule_refresh()

    # ---- 颜料盘 ----
    def _draw_palette_grid(self):
        """绘制颜料盘网格"""
        c = self._palette_canvas
        c.delete("all")
        cell_size = 24
        cols_per_row = 12
        x_pad = 5
        y_pad = 5
        row_h = cell_size + 2

        # 按分组排列
        from mard_palette import MARD_PALETTE
        groups = {}
        for item in MARD_PALETTE:
            prefix = item['id'][0]
            if prefix not in groups:
                groups[prefix] = []
            groups[prefix].append(item)

        y = y_pad
        self._palette_cells = {}  # rect_id -> cid
        # 预计算已使用颜色集合（避免每个item重复计算）
        used = set(cid for cid, _ in self._color_counts_list) if self._color_counts_list else set()

        for group_name in sorted(groups.keys()):
            # 组标签
            c.create_text(x_pad, y, text=group_name, anchor="nw",
                          font=("Arial", 8, "bold"), fill="#666")
            y += 14

            items = groups[group_name]
            col = 0
            for item in items:
                cx = x_pad + col * (cell_size + 1)
                cy = y
                rgb = item['rgb']
                hex_c = "#{:02x}{:02x}{:02x}".format(*rgb)
                fg = "#000000" if (0.299 * rgb[0] + 0.587 * rgb[1] + 0.114 * rgb[2]) > 128 else "#ffffff"
                # 为每个颜色块使用唯一tag，矩形、文字、标记条共享tag
                tag = f"cell_{item['id']}"
                rect = c.create_rectangle(cx, cy, cx + cell_size, cy + cell_size,
                                          fill=hex_c, outline="#999", width=1, tags=(tag,))
                # 色号文字
                txt = c.create_text(cx + cell_size // 2, cy + cell_size // 2,
                                    text=item['id'], font=("Consolas", 8), fill=fg, tags=(tag,))
                # 标记已使用的颜色
                if item['id'] in used:
                    c.create_rectangle(cx, cy, cx + cell_size, cy + 2,
                                       fill="#333", outline="", tags=(tag,))
                # 将矩形放在最上面以保证视觉正确
                c.tag_raise(rect, txt)
                # 整个tag组绑定点击（文字在上层也能响应）
                c.tag_bind(tag, "<Button-1>",
                           lambda e, cid=item['id']: self._palette_select(cid))
                self._palette_cells[rect] = item['id']

                col += 1
                if col >= cols_per_row:
                    col = 0
                    y += row_h

            if col > 0:
                y += row_h
            y += 4  # 组间距

        # 更新滚动区域
        c.configure(scrollregion=c.bbox("all"))

    def _palette_select(self, cid):
        """选中颜料盘中的颜色"""
        self.edit_color = cid
        hex_c = self._cid_to_hex(cid)
        rgb = self.PALETTE_DICT.get(cid, (128, 128, 128))
        fg = "#000000" if (0.299 * rgb[0] + 0.587 * rgb[1] + 0.114 * rgb[2]) > 128 else "#ffffff"
        self._color_preview.config(text=f"  {cid}  ", bg=hex_c, fg=fg)

    def _on_palette_enter(self, event):
        """鼠标进入颜料盘时，将滚轮绑定到颜料盘"""
        self.root.bind_all("<MouseWheel>", self._on_palette_scroll)

    def _on_palette_leave(self, event):
        """鼠标离开颜料盘时，恢复滚轮绑定到主面板"""
        self.root.bind_all("<MouseWheel>", self._on_ctrl_scroll)

    def _on_palette_scroll(self, event):
        """颜料盘滚轮滚动"""
        self._palette_canvas.yview_scroll(int(-1 * (event.delta / 120)), "units")

    def _on_ctrl_scroll(self, event):
        """主控制面板滚轮滚动"""
        self._ctrl_canvas.yview_scroll(int(-1 * (event.delta / 120)), "units")

    def _update_replace_combos(self):
        """更新去杂色下拉框"""
        if self._color_counts_list:
            cids = [cid for cid, _ in self._color_counts_list]
        else:
            cids = sorted(self.PALETTE_DICT.keys())
        self._replace_src_combo['values'] = cids
        self._replace_dst_combo['values'] = cids
        if cids:
            self._replace_src_var.set(cids[0])
            self._replace_dst_var.set(cids[1] if len(cids) > 1 else cids[0])

    def _do_replace_color(self):
        """执行颜色替换(去杂色)"""
        src = self._replace_src_var.get()
        dst = self._replace_dst_var.get()
        if not src or not dst or src == dst:
            messagebox.showinfo("提示", "请选择不同的源颜色和目标颜色")
            return
        self._push_undo(self.color_ids.copy())
        count = 0
        for y in range(self.grid_h):
            for x in range(self.grid_w):
                if self.color_ids[y, x] == src:
                    self.color_ids[y, x] = dst
                    count += 1
        if count == 0:
            messagebox.showinfo("提示", f"图纸中没有 {src}")
            return
        if str(src) in self._protected_outline_ids:
            self._protected_outline_ids.discard(str(src))
            self._protected_outline_ids.add(str(dst))
        self._rebuild_color_counts()
        self._update_stats_display()
        self._update_replace_combos()
        self._draw_palette_grid()
        self._schedule_refresh()
        self.status_var.set(f"已将 {count} 颗 {src} 替换为 {dst}")

    def _rebuild_color_counts(self):
        """重新统计色号"""
        counts = {}
        for y in range(self.grid_h):
            for x in range(self.grid_w):
                cid = self.color_ids[y, x]
                if cid is not None:
                    counts[cid] = counts.get(cid, 0) + 1
        protected = {str(cid) for cid in getattr(self, "_protected_outline_ids", set())}
        if protected:
            self._color_counts_list = sorted(
                counts.items(),
                key=lambda x: (0 if str(x[0]) in protected else 1,
                               -x[1], str(x[0]))
            )
        else:
            self._color_counts_list = sorted(counts.items(), key=lambda x: -x[1])

    def _update_stats_display(self):
        """更新统计文本"""
        self._stats_text.config(state="normal")
        self._stats_text.delete("1.0", "end")
        total = 0
        for cid, cnt in self._color_counts_list:
            hex_c = self._cid_to_hex(cid)
            self._stats_text.insert("end", f"  {cid}: {cnt}颗  ")
            total += cnt
        self._stats_text.insert("end", f"\n总计: {total}颗, {len(self._color_counts_list)}色")
        self._stats_text.config(state="disabled")

    def _on_view_change(self):
        self._view_mode = self._view_var.get()
        self._schedule_refresh()

    def _on_preview_ids_change(self):
        self._preview_show_ids = self._preview_ids_var.get()
        self._schedule_refresh()

    def _on_preview_grid_change(self):
        self._preview_show_grid = self._preview_grid_var.get()
        self._schedule_refresh()

    def _on_zoom_change(self, val):
        new_zoom = round(float(val), 1)
        if new_zoom == self._preview_zoom:
            return
        old_zoom = self._preview_zoom
        self._preview_zoom = new_zoom
        # 同步更新输入框
        self._zoom_entry.delete(0, "end")
        self._zoom_entry.insert(0, f"{new_zoom:.1f}")
        # 以画布中心为锚点缩放
        cw, ch = self.canvas_w, self.canvas_h
        center_cx = cw / 2
        center_cy = ch / 2
        # 画布中心在旧图像上的位置
        old_img_x = center_cx - self._edit_pan_x
        old_img_y = center_cy - self._edit_pan_y
        # 缩放比例
        ratio = new_zoom / old_zoom if old_zoom > 0 else 1
        # 新图像上的位置
        new_img_x = old_img_x * ratio
        new_img_y = old_img_y * ratio
        # 新的偏移量，使画布中心对应同一点
        self._edit_pan_x = center_cx - new_img_x
        self._edit_pan_y = center_cy - new_img_y
        self._schedule_refresh()

    def _cid_to_hex(self, cid):
        rgb = self.PALETTE_DICT.get(cid, (200, 200, 200))
        return "#{:02x}{:02x}{:02x}".format(*rgb)

    # ==================================================================
    # 阶段4画布平移 (右键/中键拖拽, 滚轮平移)
    # ==================================================================
    def _on_pan_start(self, event):
        if self.stage != 4 or self.color_ids is None:
            return
        self._edit_panning = True
        self._edit_pan_start = (event.x, event.y)

    def _on_pan_move(self, event):
        if not self._edit_panning or self._edit_pan_start is None:
            return
        dx = event.x - self._edit_pan_start[0]
        dy = event.y - self._edit_pan_start[1]
        self._edit_pan_start = (event.x, event.y)
        self._edit_pan_x += dx
        self._edit_pan_y += dy
        self._redisplay_edit_image()

    def _on_pan_end(self, event):
        self._edit_panning = False
        self._edit_pan_start = None

    def _on_canvas_mousewheel(self, event):
        """左侧画布滚轮：Ctrl+滚轮=缩放，滚轮=平移"""
        if self.stage != 4 or self.color_ids is None:
            return
        # Ctrl+滚轮 → 缩放
        if event.state & 0x4:  # Ctrl
            factor = 1.1 if event.delta > 0 else 0.9
            new_zoom = round(max(0.3, min(4.0, self._preview_zoom * factor)), 1)
            if new_zoom == self._preview_zoom:
                return
            old_zoom = self._preview_zoom
            self._preview_zoom = new_zoom
            # 同步UI
            self._zoom_var.set(new_zoom)
            self._zoom_entry.delete(0, "end")
            self._zoom_entry.insert(0, f"{new_zoom:.1f}")
            # 以鼠标位置为锚点缩放
            mx, my = event.x, event.y
            old_img_x = mx - self._edit_pan_x
            old_img_y = my - self._edit_pan_y
            ratio = new_zoom / old_zoom if old_zoom > 0 else 1
            self._edit_pan_x = mx - old_img_x * ratio
            self._edit_pan_y = my - old_img_y * ratio
            self._schedule_refresh()
        else:
            # 普通滚轮 → 平移
            delta = -1 if event.delta > 0 else 1
            if event.state & 0x1:  # Shift → 左右平移
                self._edit_pan_x += delta * 30
            else:
                self._edit_pan_y += delta * 30
            self._redisplay_edit_image()

    def _on_zoom_entry(self, event=None):
        """从输入框读取缩放值"""
        try:
            val = float(self._zoom_entry.get())
            val = round(max(0.3, min(4.0, val)), 1)
        except ValueError:
            val = self._preview_zoom
        self._zoom_var.set(val)
        self._zoom_entry.delete(0, "end")
        self._zoom_entry.insert(0, f"{val:.1f}")
        old_zoom = self._preview_zoom
        self._preview_zoom = val
        if old_zoom == val:
            return
        # 以画布中心为锚点
        cw, ch = self.canvas_w, self.canvas_h
        center_cx, center_cy = cw / 2, ch / 2
        old_img_x = center_cx - self._edit_pan_x
        old_img_y = center_cy - self._edit_pan_y
        ratio = val / old_zoom if old_zoom > 0 else 1
        self._edit_pan_x = center_cx - old_img_x * ratio
        self._edit_pan_y = center_cy - old_img_y * ratio
        self._schedule_refresh()

    def _center_edit_image(self):
        """将渲染后的图像居中显示在画布上"""
        if not hasattr(self, 'bead_pattern_preview') or self.bead_pattern_preview is None:
            return
        img = self.bead_pattern_preview
        cw, ch = self.canvas_w, self.canvas_h
        # 居中：图像中心 = 画布中心
        self._edit_pan_x = (cw - img.width) // 2
        self._edit_pan_y = (ch - img.height) // 2

    def _display_edit_image(self):
        """将编辑预览图像1:1显示到画布上，支持平移"""
        self.canvas.delete("all")
        img = self.bead_pattern_preview
        if img is None:
            return
        cw, ch = self.canvas_w, self.canvas_h
        px, py = self._edit_pan_x, self._edit_pan_y
        self._edit_photo = ImageTk.PhotoImage(img)
        self._edit_canvas_img = self.canvas.create_image(
            px, py, anchor="nw", image=self._edit_photo)

    def _redisplay_edit_image(self):
        """平移时快速重绘（复用PhotoImage，只移动位置）"""
        if not hasattr(self, '_edit_photo') or self._edit_photo is None:
            return
        # 复用PhotoImage，仅移动canvas image位置
        if hasattr(self, '_edit_canvas_img'):
            self.canvas.coords(self._edit_canvas_img,
                               self._edit_pan_x, self._edit_pan_y)
        else:
            self.canvas.delete("all")
            self._edit_canvas_img = self.canvas.create_image(
                self._edit_pan_x, self._edit_pan_y, anchor="nw",
                image=self._edit_photo)

    # ==================================================================
    # 画布交互
    # ==================================================================
    def _on_canvas_resize(self, event):
        self.canvas_w = event.width
        self.canvas_h = event.height
        if self.stage == 2 and self.mask is not None:
            self._display_mask_preview()
        elif self.stage == 3 and self.selected_image is not None:
            self._display_image(self.selected_image)
        elif self.stage == 4 and self.color_ids is not None:
            # 阶段4 resize：重新居中并重绘（不重新渲染图像）
            self._center_edit_image()
            self._display_edit_image()
        elif self.original_image is not None:
            self._display_image(self.original_image)

    def _canvas_to_image(self, cx, cy):
        return int((cx - self.offset_x) / self.scale), int((cy - self.offset_y) / self.scale)

    def _canvas_to_grid(self, cx, cy):
        """画布坐标 → 网格坐标"""
        if self.grid_w is None:
            return -1, -1
        ml, mt_base = self._get_label_margin(self.grid_w, self.grid_h)
        zoom = self._preview_zoom
        bs = max(4, int(20 * zoom))
        gap = 1 if self._preview_show_grid else 0
        cell = bs + gap
        ml = max(20, int(ml * min(zoom, 1.5)))
        mt = max(16, int(mt_base * min(zoom, 1.5)))

        # 阶段4：图像1:1显示，使用平移偏移
        if self.stage == 4:
            img_x = cx - self._edit_pan_x
            img_y = cy - self._edit_pan_y
        else:
            img_x = (cx - self.offset_x) / self.scale
            img_y = (cy - self.offset_y) / self.scale

        # 再减去margin，除以cell尺寸
        gx = int((img_x - ml) / cell)
        gy = int((img_y - mt) / cell)
        return gx, gy

    def _on_press(self, event):
        if self.stage == 2 and self.mask is not None:
            self._on_press_stage2(event)
        elif self.stage == 4 and self.color_ids is not None:
            self._on_press_stage4(event)

    def _on_drag(self, event):
        if not self.painting:
            return
        if self.stage == 2:
            self._on_drag_stage2(event)
        elif self.stage == 4:
            self._on_drag_stage4(event)

    def _on_release(self, event):
        if not self.painting:
            return
        if self.stage == 2:
            self._on_release_stage2(event)
        elif self.stage == 4:
            self._on_release_stage4(event)
        self.painting = False

    # ---- 阶段2 交互 ----
    def _on_press_stage2(self, event):
        self.painting = True
        self.start_x = self.last_x = event.x
        self.start_y = self.last_y = event.y
        self.lasso_points = [(event.x, event.y)]
        self.selection_rect = None
        # 画笔开始前保存快照（整个笔画只记录一次撤回）
        if self.tool_var.get() == "brush":
            self._brush_stroke_snapshot = self.mask.copy()

    def _on_drag_stage2(self, event):
        self.last_x, self.last_y = event.x, event.y
        tool = self.tool_var.get()
        if tool == "rect":
            self.selection_rect = [self.start_x, self.start_y, event.x, event.y]
            self._display_mask_preview_with_sel()
        elif tool == "brush":
            self._paint_brush_seg(self.last_x, self.last_y, event.x, event.y)
        elif tool == "lasso":
            self.lasso_points.append((event.x, event.y))
            self._display_mask_preview_with_sel()

    def _on_release_stage2(self, event):
        tool = self.tool_var.get()
        action = self.select_action_var.get()
        if tool == "rect" and self.start_x is not None:
            self._push_undo(self.mask.copy())
            self._fill_rect_mask(self.start_x, self.start_y, event.x, event.y, action)
            self.selection_rect = None
        elif tool == "lasso" and len(self.lasso_points) > 2:
            self._push_undo(self.mask.copy())
            self._fill_lasso_mask(self.lasso_points, action)
            self.lasso_points = []
        elif tool == "brush" and self._brush_stroke_snapshot is not None:
            # 画笔笔画结束，用起始快照作为撤回点
            self._push_undo(self._brush_stroke_snapshot)
            self._brush_stroke_snapshot = None
        self._display_mask_preview()

    # ---- 阶段4 交互 ----
    def _eyedropper_pick(self, cx, cy):
        """取色器：从画布获取颜色号并设为当前编辑颜色"""
        gx, gy = self._canvas_to_grid(cx, cy)
        if 0 <= gx < self.grid_w and 0 <= gy < self.grid_h:
            cid = self.color_ids[gy, gx]
            if cid is not None:
                self._palette_select(cid)
                self._highlight_palette_cell(cid)
                self.status_var.set(f"取色: {cid}")
            else:
                self.status_var.set("该位置为空")
        else:
            self.status_var.set("取色: 超出图纸范围")

    def _highlight_palette_cell(self, cid):
        """在颜料盘中高亮指定颜色（视觉反馈）"""
        if not hasattr(self, '_palette_cells'):
            return
        # 取消之前的高亮
        self._palette_canvas.delete("highlight")
        # 找到匹配的矩形并添加高亮
        for rect_id, cell_cid in self._palette_cells.items():
            if cell_cid == cid:
                coords = self._palette_canvas.coords(rect_id)
                if coords:
                    self._palette_canvas.create_rectangle(
                        coords[0] - 2, coords[1] - 2, coords[2] + 2, coords[3] + 2,
                        outline="#ff3300", width=3, tags="highlight")
                    # 滚动到该颜色可见位置
                    self._palette_canvas.see(0, 0)  # reset
                    self._palette_canvas.yview_moveto(0)
                    # 确保该区域可见
                    canvas_total = self._palette_canvas.bbox("all")
                    if canvas_total:
                        target_y = (coords[1] + coords[3]) / 2
                        total_h = canvas_total[3] - canvas_total[1]
                        canvas_h = self._palette_canvas.winfo_height()
                        scroll_pos = (target_y - canvas_total[1]) / total_h - canvas_h / 2 / total_h
                        scroll_pos = max(0, min(1, scroll_pos))
                        self._palette_canvas.yview_moveto(scroll_pos)
                break
        # 更新颜色预览
        if hasattr(self, '_color_preview_canvas'):
            self._update_color_preview()

    def _on_tool_cursor_change(self, *args):
        """根据工具模式切换画布光标"""
        tool = self.tool_var.get()
        if tool == "eyedropper":
            self.canvas.config(cursor="crosshair")
        elif tool == "brush":
            self.canvas.config(cursor="circle")
        else:
            self.canvas.config(cursor="")

    def _on_press_stage4(self, event):
        self.painting = True
        self.start_x = self.last_x = event.x
        self.start_y = self.last_y = event.y
        self.lasso_points = [(event.x, event.y)]
        self.selection_rect = None
        tool = self.tool_var.get()
        if tool == "eyedropper":
            # 取色器：获取点击位置的颜色号
            self._eyedropper_pick(event.x, event.y)
            self.painting = False
        elif tool == "point":
            self._push_undo(self.color_ids.copy())
            self._edit_point(event.x, event.y)
            self.painting = False
        elif tool == "brush":
            # 画笔笔画开始，保存快照
            self._brush_stroke_snapshot = self.color_ids.copy()

    def _on_drag_stage4(self, event):
        self.last_x, self.last_y = event.x, event.y
        tool = self.tool_var.get()
        if tool == "rect":
            self.selection_rect = [self.start_x, self.start_y, event.x, event.y]
            self._draw_edit_overlay()
        elif tool == "lasso":
            self.lasso_points.append((event.x, event.y))
            self._draw_edit_overlay()
        elif tool == "brush":
            self._edit_brush(event.x, event.y)

    def _on_release_stage4(self, event):
        tool = self.tool_var.get()
        if tool == "rect" and self.start_x is not None:
            self._push_undo(self.color_ids.copy())
            self._edit_rect(self.start_x, self.start_y, event.x, event.y)
            self.selection_rect = None
        elif tool == "lasso" and len(self.lasso_points) > 2:
            self._push_undo(self.color_ids.copy())
            self._edit_lasso(self.lasso_points)
            self.lasso_points = []
        elif tool == "brush" and self._brush_stroke_snapshot is not None:
            self._push_undo(self._brush_stroke_snapshot)
            self._brush_stroke_snapshot = None
            self._rebuild_color_counts()
            self._update_stats_display()
            self._schedule_refresh()

    def _edit_point(self, cx, cy):
        gx, gy = self._canvas_to_grid(cx, cy)
        if 0 <= gx < self.grid_w and 0 <= gy < self.grid_h:
            self.color_ids[gy, gx] = self.edit_color if self.select_action_var.get() == "add" else None
            self._rebuild_color_counts()
            self._update_stats_display()
            self._schedule_refresh()

    def _edit_rect(self, cx0, cy0, cx1, cy1):
        gx0, gy0 = self._canvas_to_grid(min(cx0, cx1), min(cy0, cy1))
        gx1, gy1 = self._canvas_to_grid(max(cx0, cx1), max(cy0, cy1))
        gx0, gy0 = max(0, gx0), max(0, gy0)
        gx1, gy1 = min(gx1, self.grid_w - 1), min(gy1, self.grid_h - 1)
        act = self.select_action_var.get()
        for y in range(gy0, gy1 + 1):
            for x in range(gx0, gx1 + 1):
                self.color_ids[y, x] = self.edit_color if act == "add" else None
        self._rebuild_color_counts()
        self._update_stats_display()
        self._schedule_refresh()

    def _edit_lasso(self, canvas_points):
        pts = []
        for cx, cy in canvas_points:
            pts.append(self._canvas_to_grid(cx, cy))
        if len(pts) < 3:
            return
        tmp = Image.new("L", (self.grid_w, self.grid_h), 0)
        ImageDraw.Draw(tmp).polygon(pts, fill=255)
        mask = np.array(tmp) > 0
        act = self.select_action_var.get()
        for y in range(self.grid_h):
            for x in range(self.grid_w):
                if mask[y, x]:
                    self.color_ids[y, x] = self.edit_color if act == "add" else None
        self._rebuild_color_counts()
        self._update_stats_display()
        self._schedule_refresh()

    def _edit_brush(self, cx, cy):
        gx, gy = self._canvas_to_grid(cx, cy)
        if 0 <= gx < self.grid_w and 0 <= gy < self.grid_h:
            self.color_ids[gy, gx] = self.edit_color if self.select_action_var.get() == "add" else None

    def _fill_rect_mask(self, cx0, cy0, cx1, cy1, action):
        ix0, iy0 = self._canvas_to_image(min(cx0, cx1), min(cy0, cy1))
        ix1, iy1 = self._canvas_to_image(max(cx0, cx1), max(cy0, cy1))
        ImageDraw.Draw(self.mask).rectangle([ix0, iy0, ix1, iy1],
                                            fill=255 if action == "add" else 0)

    def _fill_lasso_mask(self, pts, action):
        img_pts = [self._canvas_to_image(cx, cy) for cx, cy in pts]
        if len(img_pts) >= 3:
            ImageDraw.Draw(self.mask).polygon(img_pts,
                                             fill=255 if action == "add" else 0)

    def _paint_brush_seg(self, cx0, cy0, cx1, cy1):
        if self.mask is None:
            return
        ix0, iy0 = self._canvas_to_image(cx0, cy0)
        ix1, iy1 = self._canvas_to_image(cx1, cy1)
        r = max(1, int(10 / self.scale))
        color = 255 if self.select_action_var.get() == "add" else 0
        draw = ImageDraw.Draw(self.mask)
        dist = max(1, int(np.hypot(ix1 - ix0, iy1 - iy0)))
        for i in range(dist + 1):
            t = i / dist
            x, y = int(ix0 + t * (ix1 - ix0)), int(iy0 + t * (iy1 - iy0))
            draw.ellipse([x - r, y - r, x + r, y + r], fill=color)
        self._display_mask_preview()

    # ==================================================================
    # 图片显示
    # ==================================================================
    def _calc_fit_scale(self, img):
        cw = max(self.canvas_w, 100)
        ch = max(self.canvas_h, 100)
        self.scale = min(cw / img.width, ch / img.height, 2.0)
        dw = int(img.width * self.scale)
        dh = int(img.height * self.scale)
        self.offset_x = (cw - dw) // 2
        self.offset_y = (ch - dh) // 2
        return dw, dh

    def _display_image(self, img):
        self.canvas.delete("all")
        if img is None:
            return
        dw, dh = self._calc_fit_scale(img)
        resized = img.resize((dw, dh), Image.LANCZOS)
        self._photo = ImageTk.PhotoImage(resized)
        self.canvas.create_image(self.offset_x, self.offset_y, anchor="nw", image=self._photo)

    def _display_mask_preview(self):
        if self.original_image is None or self.mask is None:
            return
        img = self.original_image.copy()
        mr = self.mask if self.mask.size == img.size else self.mask.resize(img.size, Image.LANCZOS)
        ia = np.array(img).astype(np.float32)
        ma = np.array(mr).astype(np.float32) / 255.0
        gray = np.repeat(np.mean(ia[:, :, :3], axis=2, keepdims=True), 3, axis=2)
        alpha = ma[:, :, np.newaxis]
        result = np.clip(ia[:, :, :3] * alpha + gray * (1 - alpha) * 0.4 +
                         np.array([200, 200, 200]) * (1 - alpha) * 0.6, 0, 255).astype(np.uint8)
        edge = np.array(mr.filter(ImageFilter.FIND_EDGES))
        result[(edge > 50) & (ma > 0.5)] = [255, 50, 50]
        self._display_image(Image.fromarray(result, "RGB"))

    def _display_mask_preview_with_sel(self):
        self._display_mask_preview()
        if self.tool_var.get() == "rect" and self.selection_rect:
            x0, y0, x1, y1 = self.selection_rect
            self.canvas.create_rectangle(x0, y0, x1, y1, outline="#00aaff", width=2, dash=(5, 3))
        elif self.tool_var.get() == "lasso" and len(self.lasso_points) > 1:
            flat = [c for p in self.lasso_points for c in p]
            self.canvas.create_line(flat, fill="#00aaff", width=2, dash=(5, 3), smooth=True)

    # ==================================================================
    # 编辑预览 (轻量级)
    # ==================================================================
    def _schedule_refresh(self):
        """防抖刷新，避免频繁渲染"""
        if self._render_after_id is not None:
            self.root.after_cancel(self._render_after_id)
        self._render_after_id = self.root.after(50, self._do_refresh)

    def _do_refresh(self):
        """执行防抖刷新"""
        self._render_after_id = None
        self._refresh_edit_view()

    def _refresh_edit_view(self):
        """预览渲染 — numpy向量化，根据缩放级别调整渲染分辨率"""
        if self.color_ids is None:
            return
        zoom = self._preview_zoom
        bs = max(4, int(20 * zoom))
        gap = 1 if self._preview_show_grid else 0
        cell = bs + gap
        ml_base, mt_base = self._get_label_margin(self.grid_w, self.grid_h)
        ml = max(20, int(ml_base * min(zoom, 1.5)))
        mt = max(16, int(mt_base * min(zoom, 1.5)))

        gw_px = self.grid_w * cell - gap
        gh_px = self.grid_h * cell - gap
        tw = ml + gw_px
        th = mt + gh_px

        # ---- numpy向量化填充 ----
        arr = np.full((th, tw, 3), 245, dtype=np.uint8)

        # 预建 cid→rgb 映射数组（一次性查找，避免逐像素dict.get）
        # 将 color_ids 转为连续int索引，批量查找RGB
        unique_cids = set()
        for y in range(self.grid_h):
            for x in range(self.grid_w):
                c = self.color_ids[y, x]
                if c is not None:
                    unique_cids.add(c)
        # 构建查找表
        cid_rgb = {}
        for cid in unique_cids:
            cid_rgb[cid] = self.PALETTE_DICT.get(cid, (200, 200, 200))

        # 构建 (gh, gw, 3) 的RGB数组
        rgb_grid = np.full((self.grid_h, self.grid_w, 3), 235, dtype=np.uint8)
        edit_color_mode = (self._view_mode == "edit_color")
        for y in range(self.grid_h):
            for x in range(self.grid_w):
                cid = self.color_ids[y, x]
                if cid is None:
                    continue
                if edit_color_mode and cid != self.edit_color:
                    rgb_grid[y, x] = 240
                else:
                    rgb_grid[y, x] = cid_rgb[cid]

        # 用 np.repeat 扩展到像素级别
        # rgb_grid: (gh, gw, 3) → (gh, gw*cell, 3) 水平扩展
        rgb_h = np.repeat(rgb_grid, cell, axis=1)
        # (gh, gw*cell, 3) → (gh*cell, gw*cell, 3) 垂直扩展
        rgb_full = np.repeat(rgb_h, cell, axis=0)

        # 限制到图案区域大小
        pat_h = min(rgb_full.shape[0], gh_px)
        pat_w = min(rgb_full.shape[1], gw_px)

        # 放入arr
        arr[mt:mt + pat_h, ml:ml + pat_w] = rgb_full[:pat_h, :pat_w]

        # 网格线（numpy切片，非循环）
        if gap > 0 and self._preview_show_grid:
            # 垂直网格线
            for x in range(self.grid_w - 1):
                lx = ml + (x + 1) * cell - 1
                if lx < ml + gw_px:
                    arr[mt:mt + gh_px, lx] = 200
            # 水平网格线
            for y in range(self.grid_h - 1):
                ly = mt + (y + 1) * cell - 1
                if ly < mt + gh_px:
                    arr[ly, ml:ml + gw_px] = 200

        # 坐标轴
        if mt > 2 and ml > 2:
            arr[:mt - 1, ml - 1] = 150
            arr[mt - 1, :tw] = 150
            arr[:, ml - 1] = 150

        img = Image.fromarray(arr, "RGB")
        draw = ImageDraw.Draw(img)

        # 坐标标签
        if zoom >= 0.5:
            label_font_size = max(7, int(9 * zoom))
            font = self._get_font(label_font_size)
            step_x = max(1, self.grid_w // max(1, int(25 / zoom)))
            for x in range(0, self.grid_w, step_x):
                label = str(x + 1)
                px = ml + x * cell + bs // 2
                bbox = draw.textbbox((0, 0), label, font=font)
                draw.text((px - (bbox[2] - bbox[0]) // 2, 1), label,
                          fill=(80, 80, 80), font=font)
            step_y = max(1, self.grid_h // max(1, int(25 / zoom)))
            for y in range(0, self.grid_h, step_y):
                label = str(y + 1)
                py = mt + y * cell + bs // 2
                bbox = draw.textbbox((0, 0), label, font=font)
                draw.text((1, py - (bbox[3] - bbox[1]) // 2), label,
                          fill=(80, 80, 80), font=font)

        # 色号文字
        if self._preview_show_ids and bs >= 8:
            id_font_size = max(6, int(bs * 0.45))
            id_font = self._get_font(id_font_size)
            for y in range(self.grid_h):
                for x in range(self.grid_w):
                    cid = self.color_ids[y, x]
                    if cid is None:
                        continue
                    if edit_color_mode and cid != self.edit_color:
                        continue
                    rgb = cid_rgb[cid]
                    brightness = 0.299 * rgb[0] + 0.587 * rgb[1] + 0.114 * rgb[2]
                    tc = (0, 0, 0) if brightness > 128 else (255, 255, 255)
                    px = ml + x * cell + bs // 2
                    py = mt + y * cell + bs // 2
                    bbox = draw.textbbox((0, 0), cid, font=id_font)
                    tw2, th2 = bbox[2] - bbox[0], bbox[3] - bbox[1]
                    max_sz = bs - 2
                    if tw2 > max_sz or th2 > max_sz:
                        shrink = min(max_sz / max(tw2, 1), max_sz / max(th2, 1))
                        fs2 = max(5, int(id_font_size * shrink))
                        f2 = self._get_font(fs2)
                        bbox = draw.textbbox((0, 0), cid, font=f2)
                        tw2, th2 = bbox[2] - bbox[0], bbox[3] - bbox[1]
                        draw.text((px - tw2 // 2, py - th2 // 2), cid, fill=tc, font=f2)
                    else:
                        draw.text((px - tw2 // 2, py - th2 // 2), cid, fill=tc, font=id_font)

        self.bead_pattern_preview = img
        self._display_edit_image()

    def _draw_edit_overlay(self):
        """在预览上叠加编辑选框（不重新渲染图案，直接用缓存图像）"""
        if self.bead_pattern_preview is not None:
            self._display_edit_image()
        if self.tool_var.get() == "rect" and self.selection_rect:
            x0, y0, x1, y1 = self.selection_rect
            self.canvas.create_rectangle(x0, y0, x1, y1, outline="#ff6600", width=2, dash=(5, 3))
        elif self.tool_var.get() == "lasso" and len(self.lasso_points) > 1:
            flat = [c for p in self.lasso_points for c in p]
            self.canvas.create_line(flat, fill="#ff6600", width=2, dash=(5, 3), smooth=True)

    # ==================================================================
    # 高质量渲染 (用于导出，2x超采样)
    # ==================================================================
    def _get_label_margin(self, grid_w, grid_h):
        cd = len(str(grid_w))
        rd = len(str(grid_h))
        return max(rd * 8 + 12, 30), max(cd * 8 + 12, 25)

    def _get_font(self, size, chinese=False):
        """获取PIL字体。chinese=True时优先使用中文字体。"""
        if chinese:
            # 中文字体优先
            candidates = ("msyhbd.ttc", "msyh.ttc", "simhei.ttf", "simsun.ttc",
                          "arialbd.ttf", "arial.ttf", "consolas.ttf", "consola.ttf")
        else:
            candidates = ("consola.ttf", "consolas.ttf", "arialbd.ttf", "arial.ttf",
                          "msyhbd.ttc", "msyh.ttc")
        for name in candidates:
            try:
                return ImageFont.truetype(name, size)
            except (OSError, IOError):
                continue
        return ImageFont.load_default()

    def _render_export(self, color_ids, grid_w, grid_h, bead_size, show_grid, show_ids,
                       highlight_cid=None, with_stats=True):
        """高质量渲染 (2x超采样)，可选带色号总览"""
        ml, mt = self._get_label_margin(grid_w, grid_h)
        gap = 1 if show_grid else 0
        sf = 2  # 超采样
        bs = bead_size * sf
        gp = gap * sf
        sml = ml * sf
        smt = mt * sf

        gw_px = grid_w * (bs + gp) - gp
        gh_px = grid_h * (bs + gp) - gp
        tw = sml + gw_px
        th = smt + gh_px

        # 统计
        counts = {}
        for y in range(grid_h):
            for x in range(grid_w):
                cid = color_ids[y, x]
                if cid is not None:
                    counts[cid] = counts.get(cid, 0) + 1
        sorted_counts = sorted(counts.items(), key=lambda x: -x[1])
        total = sum(counts.values())

        # 计算统计栏高度
        stats_h = 0
        if with_stats and sorted_counts:
            cols_per_row = max(1, gw_px // (bs * 3))
            rows = (len(sorted_counts) + cols_per_row - 1) // cols_per_row
            stats_h = rows * (bs + 8 * sf) + bs * 2 + 10 * sf

        full_h = th + stats_h
        img = Image.new("RGBA", (tw, full_h), (255, 255, 255, 255))
        draw = ImageDraw.Draw(img)

        font_label = self._get_font(max(9, bead_size // 2) * sf)
        max_text = bs - 4 * sf
        font_size = max(6 * sf, int(max_text * 0.72))
        font_bead = self._get_font(font_size)

        # 坐标标签
        for x in range(grid_w):
            label = str(x + 1)
            px = sml + x * (bs + gp) + bs // 2
            bbox = draw.textbbox((0, 0), label, font=font_label)
            draw.text((px - (bbox[2] - bbox[0]) // 2, 2 * sf), label,
                      fill=(80, 80, 80, 255), font=font_label)
        for y in range(grid_h):
            label = str(y + 1)
            py = smt + y * (bs + gp) + bs // 2
            bbox = draw.textbbox((0, 0), label, font=font_label)
            draw.text((2 * sf, py - (bbox[3] - bbox[1]) // 2), label,
                      fill=(80, 80, 80, 255), font=font_label)

        # 拼豆网格
        for y in range(grid_h):
            for x in range(grid_w):
                cid = color_ids[y, x]
                px = sml + x * (bs + gp)
                py = smt + y * (bs + gp)
                if cid is None:
                    draw.rectangle([px + 1, py + 1, px + bs - 2, py + bs - 2],
                                   fill=(245, 245, 245, 80), outline=(220, 220, 220, 100))
                    continue
                rgb = self.PALETTE_DICT.get(cid, (200, 200, 200))
                cx, cy = px + bs // 2, py + bs // 2
                border = tuple(max(0, c - 30) for c in rgb)
                draw.rectangle([px + 1, py + 1, px + bs - 2, py + bs - 2],
                               fill=rgb + (255,), outline=border + (200,))

                if highlight_cid is not None and cid != highlight_cid:
                    draw.rectangle([px, py, px + bs - 1, py + bs - 1], fill=(240, 240, 240, 180))

                if show_ids and bead_size >= 14:
                    tc = (0, 0, 0) if (0.299 * rgb[0] + 0.587 * rgb[1] + 0.114 * rgb[2]) > 128 else (255, 255, 255)
                    bbox = draw.textbbox((0, 0), cid, font=font_bead)
                    tw2, th2 = bbox[2] - bbox[0], bbox[3] - bbox[1]
                    if tw2 > max_text or th2 > max_text:
                        shrink = min(max_text / max(tw2, 1), max_text / max(th2, 1))
                        fs2 = max(5 * sf, int(font_size * shrink))
                        f2 = self._get_font(fs2)
                        bbox = draw.textbbox((0, 0), cid, font=f2)
                        tw2, th2 = bbox[2] - bbox[0], bbox[3] - bbox[1]
                        draw.text((cx - tw2 // 2, cy - th2 // 2), cid, fill=tc, font=f2)
                    else:
                        draw.text((cx - tw2 // 2, cy - th2 // 2), cid, fill=tc, font=font_bead)

                if show_grid and highlight_cid is None:
                    lc = (200, 200, 200, 180)
                    if x < grid_w - 1:
                        draw.line([px + bs - 1, py, px + bs - 1, py + bs - 2], fill=lc, width=sf)
                    if y < grid_h - 1:
                        draw.line([px, py + bs - 1, px + bs - 2, py + bs - 1], fill=lc, width=sf)

        # 坐标轴装饰线
        ac = (150, 150, 150, 220)
        draw.line([sml - 2, 0, sml - 2, th - 1], fill=ac, width=sf)
        draw.line([0, smt - 2, tw - 1, smt - 2], fill=ac, width=sf)

        # ---- 色号总览 (底部) ----
        if with_stats and sorted_counts:
            sy = th + 5 * sf
            # 分隔线
            draw.line([5 * sf, sy, tw - 5 * sf, sy], fill=(180, 180, 180, 255), width=sf)
            sy += 8 * sf

            # 标题
            title_font = self._get_font(10 * sf, chinese=True)
            draw.text((5 * sf, sy), f"色号总览 — 共 {total} 颗, {len(sorted_counts)} 色",
                      fill=(60, 60, 60, 255), font=title_font)
            sy += 16 * sf

            item_font = self._get_font(8 * sf, chinese=True)
            swatch_size = bs
            item_h = swatch_size + 6 * sf
            cols_per_row = max(1, (tw - 10 * sf) // (swatch_size * 3))

            for i, (cid, cnt) in enumerate(sorted_counts):
                col = i % cols_per_row
                row = i // cols_per_row
                ix = 5 * sf + col * (swatch_size * 3)
                iy = sy + row * item_h

                rgb = self.PALETTE_DICT.get(cid, (200, 200, 200))
                draw.rectangle([ix, iy, ix + swatch_size - 2, iy + swatch_size - 2],
                               fill=rgb + (255,), outline=(100, 100, 100, 200))

                tc = (0, 0, 0) if (0.299 * rgb[0] + 0.587 * rgb[1] + 0.114 * rgb[2]) > 128 else (255, 255, 255)
                cid_bbox = draw.textbbox((0, 0), cid, font=item_font)
                draw.text((ix + swatch_size // 2 - (cid_bbox[2] - cid_bbox[0]) // 2,
                           iy + swatch_size // 2 - (cid_bbox[3] - cid_bbox[1]) // 2),
                          cid, fill=tc, font=item_font)

                draw.text((ix + swatch_size + 4 * sf, iy + 2 * sf),
                          f"{cnt}颗", fill=(60, 60, 60, 255), font=item_font)

        # 缩放回目标尺寸
        target_w = ml + gw_px + (0 if not with_stats else 0)
        target_h = mt + gh_px + (stats_h // sf if with_stats else 0)
        return img.resize((target_w, target_h), Image.LANCZOS)

    def _render_export_single(self, color_ids, grid_w, grid_h,
                              bead_size, show_grid, show_ids, target_cid):
        """分色图纸渲染"""
        return self._render_export(color_ids, grid_w, grid_h, bead_size, show_grid, show_ids,
                                   highlight_cid=target_cid, with_stats=False)

    # ==================================================================
    # 导出
    # ==================================================================
    def _export_pattern(self):
        if self.color_ids is None:
            return
        path = filedialog.asksaveasfilename(
            title="保存完整图纸", defaultextension=".png",
            filetypes=[("PNG", "*.png")], initialfile="bead_pattern_complete.png")
        if not path:
            return
        try:
            bs = self.bead_size_var.get()
            sg = self.show_grid_var.get()
            si = self.show_ids_var.get()
            img = self._render_export(self.color_ids, self.grid_w, self.grid_h,
                                      bs, sg, si, with_stats=True)
            img.save(path, "PNG")
            self.status_var.set(f"已保存: {path}")
            messagebox.showinfo("成功", f"图纸已保存到:\n{path}")
        except Exception as e:
            messagebox.showerror("错误", f"保存失败:\n{e}")

    def _export_all(self):
        if self.color_ids is None:
            return
        folder = filedialog.askdirectory(title="选择导出文件夹")
        if not folder:
            return
        try:
            bs = self.bead_size_var.get()
            sg = self.show_grid_var.get()
            si = self.show_ids_var.get()

            # 完整图纸(含总览)
            complete = self._render_export(self.color_ids, self.grid_w, self.grid_h,
                                           bs, sg, si, with_stats=True)
            os.path.join(folder, "bead_pattern_complete.png")
            complete.save(os.path.join(folder, "bead_pattern_complete.png"), "PNG")

            # 分色图纸
            for cid, _ in self._color_counts_list:
                single = self._render_export_single(
                    self.color_ids, self.grid_w, self.grid_h, bs, sg, si, cid)
                single.save(os.path.join(folder, f"bead_pattern_{cid}.png"), "PNG")

            count = 1 + len(self._color_counts_list)
            self.status_var.set(f"已导出 {count} 张到: {folder}")
            messagebox.showinfo("成功",
                                f"已导出 {count} 张:\n"
                                f"  1 完整图纸 (含色号总览)\n"
                                f"  {len(self._color_counts_list)} 分色图纸")
        except Exception as e:
            import traceback
            traceback.print_exc()
            messagebox.showerror("错误", f"导出失败:\n{e}")

    def _back_to_stage3(self):
        self._save_stage_params(4)
        self._show_stage3()

    def _restart(self):
        self.original_image = self.mask = self.initial_mask = None
        self.selected_image = self.bead_pattern = self.color_ids = None
        self.grid_w = self.grid_h = None
        self._protected_outline_ids = set()
        self._stage_params.clear()  # 清除所有阶段参数缓存
        self._undo_stack.clear()
        self.stage = 1
        self._show_stage1()


def main():
    root = tk.Tk()
    try:
        from ctypes import windll
        windll.shcore.SetProcessDpiAwareness(1)
    except Exception:
        pass
    BeadPatternTool(root)
    root.mainloop()


if __name__ == "__main__":
    main()
