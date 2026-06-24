# -*- coding: utf-8 -*-
"""
自动抠图模块 - 使用rembg移除背景
生成二值mask: 白色(255)=前景/主体, 黑色(0)=背景
"""

import numpy as np
from PIL import Image


def remove_background(image):
    """使用rembg移除背景，返回前景mask

    Args:
        image: PIL.Image, 输入图片(RGB/RGBA均可)

    Returns:
        PIL.Image: 灰度mask图, mode='L'
                   白色(255)=前景主体, 黑色(0)=背景
    """
    try:
        from rembg import remove
    except ImportError:
        raise ImportError(
            "需要安装rembg库: pip install \"rembg[cpu]\"\n"
            "首次运行会自动下载u2net.onnx模型文件(约176MB)，"
            "也可按README手动放到用户目录的.u2net缓存中。"
        )

    # 确保输入为RGB
    if image.mode == "RGBA":
        # 如果已有alpha通道，直接用alpha作为mask参考
        pass
    if image.mode != "RGBA":
        image = image.convert("RGBA")

    # rembg处理
    result = remove(image)

    # 从结果中提取alpha通道作为mask
    if result.mode == "RGBA":
        mask = result.split()[3]  # 获取alpha通道
    else:
        # 如果rembg返回RGB(不太可能但安全处理)
        # 将白色区域以外设为前景
        gray = np.array(result.convert("L"))
        hist = np.bincount(gray.ravel(), minlength=256).astype(np.float64)
        total = gray.size
        cumulative_weight = np.cumsum(hist)
        cumulative_mean = np.cumsum(hist * np.arange(256))
        global_mean = cumulative_mean[-1]

        numerator = (global_mean * cumulative_weight - cumulative_mean) ** 2
        denominator = cumulative_weight * (total - cumulative_weight)
        variance = np.divide(
            numerator,
            denominator,
            out=np.zeros_like(numerator),
            where=denominator > 0,
        )
        threshold = int(np.argmax(variance))
        mask = Image.fromarray((gray > threshold).astype(np.uint8) * 255, "L")

    # 确保mask为mode='L'
    if mask.mode != "L":
        mask = mask.convert("L")

    return mask


def create_full_mask(width, height):
    """创建全白mask（整张图都是前景）
    用于不使用自动抠图时的默认mask
    """
    return Image.new("L", (width, height), 255)


def create_empty_mask(width, height):
    """创建全黑mask（整张图都是背景）"""
    return Image.new("L", (width, height), 0)


def apply_mask(image, mask):
    """将mask应用到图片上，非选中区域变透明

    Args:
        image: PIL.Image, 原始图片
        mask: PIL.Image (mode='L'), 白色=保留, 黑色=透明

    Returns:
        PIL.Image: RGBA格式，mask为黑色的区域alpha=0
    """
    if image.mode != "RGBA":
        image = image.convert("RGBA")

    if mask.size != image.size:
        mask = mask.resize(image.size, Image.LANCZOS)

    # 将mask转为alpha通道
    img_array = np.array(image)
    mask_array = np.array(mask)

    # mask白色(255)=保留, 黑色(0)=透明
    img_array[:, :, 3] = mask_array

    return Image.fromarray(img_array, "RGBA")
