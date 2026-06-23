# -*- coding: utf-8 -*-
"""
CIEDE2000 颜色匹配模块
将任意RGB颜色映射到最近的MARD拼豆颜色
使用CIEDE2000色差公式，这是最接近人眼感知的色差标准
"""

import numpy as np
from mard_palette import MARD_PALETTE, get_palette_rgb_list


def _srgb_to_linear(c):
    """sRGB gamma校正 → 线性RGB"""
    c = c / 255.0
    return np.where(c <= 0.04045, c / 12.92, ((c + 0.055) / 1.055) ** 2.4)


def _xyz_to_lab(x, y, z):
    """XYZ → CIE L*a*b* (D65白点)"""
    # D65参考白点
    xn, yn, zn = 0.95047, 1.0, 1.08883
    fx = _lab_f(x / xn)
    fy = _lab_f(y / yn)
    fz = _lab_f(z / zn)
    L = 116.0 * fy - 16.0
    a = 500.0 * (fx - fy)
    b = 200.0 * (fy - fz)
    return L, a, b


def _lab_f(t):
    """CIE LAB f函数"""
    delta = 6.0 / 29.0
    return np.where(t > delta ** 3, np.cbrt(t), t / (3.0 * delta ** 2) + 4.0 / 29.0)


def _rgb_to_lab(r, g, b):
    """RGB → CIE L*a*b*"""
    # sRGB → 线性
    rl = _srgb_to_linear(r)
    gl = _srgb_to_linear(g)
    bl = _srgb_to_linear(b)
    # 线性RGB → XYZ (sRGB矩阵)
    x = 0.4124564 * rl + 0.3575761 * gl + 0.1804375 * bl
    y = 0.2126729 * rl + 0.7151522 * gl + 0.0721750 * bl
    z = 0.0193339 * rl + 0.1191920 * gl + 0.9503041 * bl
    return _xyz_to_lab(x, y, z)


def _ciede2000(lab1, lab2):
    """CIEDE2000 色差计算（向量化版本）"""
    L1, a1, b1 = lab1
    L2, a2, b2 = lab2

    # 步骤1: 计算C'ab和h'ab
    C1 = np.sqrt(a1 ** 2 + b1 ** 2)
    C2 = np.sqrt(a2 ** 2 + b2 ** 2)
    C_avg = (C1 + C2) / 2.0

    C_avg_7 = C_avg ** 7
    G = 0.5 * (1 - np.sqrt(C_avg_7 / (C_avg_7 + 25 ** 7)))

    a1p = a1 * (1 + G)
    a2p = a2 * (1 + G)

    C1p = np.sqrt(a1p ** 2 + b1 ** 2)
    C2p = np.sqrt(a2p ** 2 + b2 ** 2)

    h1p = np.degrees(np.arctan2(b1, a1p)) % 360
    h2p = np.degrees(np.arctan2(b2, a2p)) % 360

    # 步骤2: 计算ΔL', ΔC', ΔH'
    dLp = L2 - L1
    dCp = C2p - C1p

    dhp = np.where(
        (C1p * C2p) == 0,
        0.0,
        np.where(
            np.abs(h2p - h1p) <= 180,
            h2p - h1p,
            np.where(h2p - h1p > 180, h2p - h1p - 360, h2p - h1p + 360),
        ),
    )

    dHp = 2.0 * np.sqrt(C1p * C2p) * np.sin(np.radians(dhp / 2.0))

    # 步骤3: 计算CIEDE2000
    Lp_avg = (L1 + L2) / 2.0
    Cp_avg = (C1p + C2p) / 2.0

    hp_sum = h1p + h2p
    hp_diff = h1p - h2p

    hp_avg = np.where(
        (C1p * C2p) == 0,
        hp_sum,
        np.where(np.abs(hp_diff) <= 180, hp_sum / 2.0,
                 np.where(hp_sum < 360, (hp_sum + 360) / 2.0,
                          (hp_sum - 360) / 2.0)),
    )

    T = (1
         - 0.17 * np.cos(np.radians(hp_avg - 30))
         + 0.24 * np.cos(np.radians(2 * hp_avg))
         + 0.32 * np.cos(np.radians(3 * hp_avg + 6))
         - 0.20 * np.cos(np.radians(4 * hp_avg - 63)))

    Lp_avg_50sq = (Lp_avg - 50) ** 2
    SL = 1 + 0.015 * Lp_avg_50sq / np.sqrt(20 + Lp_avg_50sq)
    SC = 1 + 0.045 * Cp_avg
    SH = 1 + 0.015 * Cp_avg * T

    Cp_avg_7 = Cp_avg ** 7
    RT = np.where(
        (C1p * C2p) == 0,
        0.0,
        -2 * np.sqrt(Cp_avg_7 / (Cp_avg_7 + 25 ** 7)) *
        np.sin(np.radians(60 * np.exp(-((hp_avg - 275) / 25) ** 2))),
    )

    dE = np.sqrt(
        (dLp / SL) ** 2 +
        (dCp / SC) ** 2 +
        (dHp / SH) ** 2 +
        RT * (dCp / SC) * (dHp / SH)
    )

    return dE


class ColorMatcher:
    """预计算MARD色板LAB值，支持批量快速匹配"""

    def __init__(self):
        palette = get_palette_rgb_list()
        self.ids = np.array([c[0] for c in palette])
        self.rgb = np.array([c[1] for c in palette], dtype=np.float64)  # (N, 3)

        # 预计算所有MARD色的LAB值
        r = self.rgb[:, 0]
        g = self.rgb[:, 1]
        b = self.rgb[:, 2]
        self.L, self.a, self.b = _rgb_to_lab(r, g, b)  # 各为 (N,)

        # 构建RGB→LAB查找缓存（量化到整数加速）
        self._cache = {}

    def match_single(self, rgb):
        """匹配单个RGB颜色到最近的MARD色号
        Args:
            rgb: (R, G, B) 元组，各分量0-255
        Returns:
            (色号, 色差值)
        """
        key = (int(rgb[0]), int(rgb[1]), int(rgb[2]))
        if key in self._cache:
            return self._cache[key]

        r, g, b = float(key[0]), float(key[1]), float(key[2])
        L, a, bv = _rgb_to_lab(
            np.array([r]), np.array([g]), np.array([b])
        )
        L, a, bv = L[0], a[0], bv[0]

        dE = _ciede2000(
            (L, a, bv),
            (self.L, self.a, self.b)
        )
        idx = np.argmin(dE)
        result = (self.ids[idx], float(dE[idx]))
        self._cache[key] = result
        return result

    def match_image(self, rgba_array):
        """批量匹配图像像素到MARD色号
        Args:
            rgba_array: numpy数组, shape=(H, W, 4), dtype=uint8, RGBA格式
        Returns:
            color_ids: numpy数组, shape=(H, W), dtype=object (色号字符串)
            color_map: {色号: 使用数量} 字典
        """
        h, w = rgba_array.shape[:2]
        color_ids = np.empty((h, w), dtype=object)
        color_counts = {}

        # 获取所有非透明像素
        mask = rgba_array[:, :, 3] > 128
        pixels = rgba_array[:, :, :3].astype(np.float64)

        # 转LAB（全图）
        L_img, a_img, b_img = _rgb_to_lab(
            pixels[:, :, 0], pixels[:, :, 1], pixels[:, :, 2]
        )

        # 对非透明像素逐个匹配
        # 为提高性能，先量化再匹配
        for y in range(h):
            for x in range(w):
                if not mask[y, x]:
                    color_ids[y, x] = None
                    continue
                rgb = (int(pixels[y, x, 0]),
                       int(pixels[y, x, 1]),
                       int(pixels[y, x, 2]))
                cid, _ = self.match_single(rgb)
                color_ids[y, x] = cid
                color_counts[cid] = color_counts.get(cid, 0) + 1

        return color_ids, color_counts

    def match_image_fast(self, rgba_array):
        """快速批量匹配（向量化，适合大图）
        Args:
            rgba_array: numpy数组, shape=(H, W, 4), dtype=uint8
        Returns:
            color_ids: numpy数组, shape=(H, W), dtype=object
            color_map: {色号: 使用数量}
        """
        h, w = rgba_array.shape[:2]
        color_ids = np.empty((h, w), dtype=object)
        color_counts = {}

        mask = rgba_array[:, :, 3] > 128
        pixels = rgba_array[:, :, :3].astype(np.float64)

        # 全图转LAB
        L_img, a_img, b_img = _rgb_to_lab(
            pixels[:, :, 0], pixels[:, :, 1], pixels[:, :, 2]
        )

        # 提取唯一颜色进行匹配
        unique_rgbs = np.unique(pixels[mask].astype(np.uint8), axis=0)
        match_cache = {}

        for urgb in unique_rgbs:
            key = (int(urgb[0]), int(urgb[1]), int(urgb[2]))
            if key in self._cache:
                match_cache[key] = self._cache[key]
            else:
                r, g, b = float(key[0]), float(key[1]), float(key[2])
                L, a, bv = _rgb_to_lab(
                    np.array([r]), np.array([g]), np.array([b])
                )
                dE = _ciede2000(
                    (L[0], a[0], bv[0]),
                    (self.L, self.a, self.b)
                )
                idx = np.argmin(dE)
                result = (self.ids[idx], float(dE[idx]))
                self._cache[key] = result
                match_cache[key] = result

        # 映射回图像
        for y in range(h):
            for x in range(w):
                if not mask[y, x]:
                    color_ids[y, x] = None
                    continue
                key = (int(pixels[y, x, 0]),
                       int(pixels[y, x, 1]),
                       int(pixels[y, x, 2]))
                cid, _ = match_cache[key]
                color_ids[y, x] = cid
                color_counts[cid] = color_counts.get(cid, 0) + 1

        return color_ids, color_counts


# 全局单例
_matcher_instance = None


def get_matcher():
    """获取全局ColorMatcher实例（延迟初始化，避免启动时卡顿）"""
    global _matcher_instance
    if _matcher_instance is None:
        _matcher_instance = ColorMatcher()
    return _matcher_instance
