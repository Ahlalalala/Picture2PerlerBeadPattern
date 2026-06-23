# -*- coding: utf-8 -*-
"""Cartoon-oriented bead color quantization.

This module intentionally stays independent from Tkinter.  The cartoon path
works on source image footprints for each bead cell instead of using a resized
average pixel, so thin dark outlines are preserved before flat fill regions are
matched to the MARD palette.
"""

from dataclasses import dataclass
from collections import deque

import numpy as np

from color_matcher import _rgb_to_lab, _ciede2000


ALPHA_OPAQUE = 128
MIN_ALPHA_COVERAGE = 0.15

LINE_STRICT_LUMA = 95.0
LINE_SOFT_LUMA = 120.0
LINE_LOCAL_CONTRAST = 30.0
LINE_SOFT_MAX_CHROMA = 100.0
OUTLINE_COVERAGE = 0.08
THIN_OUTLINE_COVERAGE = 0.04
THIN_OUTLINE_P15_LUMA = 70.0

FILL_COMPONENT_DE = 18.0
TINY_COMPONENT_DE = 14.0
HIGH_CHROMA = 30.0


@dataclass
class CartoonQuantizeResult:
    color_ids: np.ndarray
    color_counts: dict
    protected_ids: set


def quantize_cartoon_to_grid(selected_rgba, grid_w, grid_h, matcher,
                             palette_dict):
    """Convert a selected RGBA image to flat cartoon bead color ids.

    Parameters
    ----------
    selected_rgba:
        PIL image. RGB images are accepted and converted to RGBA.
    grid_w, grid_h:
        Output bead grid size.
    matcher:
        Existing ColorMatcher instance.
    palette_dict:
        Mapping of MARD id -> RGB tuple.
    """
    if selected_rgba.mode != "RGBA":
        selected_rgba = selected_rgba.convert("RGBA")

    src = np.array(selected_rgba)
    src_h, src_w = src.shape[:2]
    color_ids = np.empty((grid_h, grid_w), dtype=object)
    color_ids[:, :] = None

    fill_rgb = np.zeros((grid_h, grid_w, 3), dtype=np.float64)
    fill_valid = np.zeros((grid_h, grid_w), dtype=bool)
    outline_mask = np.zeros((grid_h, grid_w), dtype=bool)
    initial_ids = np.empty((grid_h, grid_w), dtype=object)
    initial_ids[:, :] = None

    scale = min(grid_w / src_w, grid_h / src_h)
    new_w = max(1, int(src_w * scale))
    new_h = max(1, int(src_h * scale))
    pad_x = (grid_w - new_w) // 2
    pad_y = (grid_h - new_h) // 2
    scale_x = new_w / src_w
    scale_y = new_h / src_h

    line_pixels = []

    for gy in range(grid_h):
        for gx in range(grid_w):
            cell = _sample_source_cell(src, gx, gy, pad_x, pad_y, new_w, new_h,
                                       scale_x, scale_y)
            if cell is None:
                continue

            rgb = cell[:, :, :3].astype(np.float64)
            alpha = cell[:, :, 3]
            opaque = alpha > ALPHA_OPAQUE
            opaque_count = int(np.count_nonzero(opaque))
            if opaque_count == 0:
                continue
            if opaque_count / float(opaque.size) < MIN_ALPHA_COVERAGE:
                continue

            opaque_rgb = rgb[opaque]
            luma = _luminance(opaque_rgb)
            chroma = opaque_rgb.max(axis=1) - opaque_rgb.min(axis=1)
            local_contrast = float(luma.max() - luma.min()) if len(luma) else 0.0
            line_candidate_flat = (
                (luma <= LINE_STRICT_LUMA) |
                ((luma <= LINE_SOFT_LUMA) &
                 (local_contrast >= LINE_LOCAL_CONTRAST) &
                 (chroma <= LINE_SOFT_MAX_CHROMA))
            )
            line_count = int(np.count_nonzero(line_candidate_flat))
            line_coverage = line_count / float(opaque_count)
            p15_luma = float(np.percentile(luma, 15)) if len(luma) else 255.0
            is_outline = (
                line_coverage >= OUTLINE_COVERAGE or
                (line_coverage >= THIN_OUTLINE_COVERAGE and
                 p15_luma <= THIN_OUTLINE_P15_LUMA)
            )

            if is_outline:
                outline_mask[gy, gx] = True
                core = opaque_rgb[luma <= 75.0]
                if len(core) == 0:
                    core = opaque_rgb[line_candidate_flat]
                if len(core) > 0:
                    line_pixels.append(core)
                continue

            sample_mask_flat = ~line_candidate_flat
            if np.count_nonzero(sample_mask_flat) < max(3, int(opaque_count * 0.2)):
                sample_mask_flat = np.ones(opaque_count, dtype=bool)
            sample_rgb = opaque_rgb[sample_mask_flat]
            median_rgb = _trimmed_median_rgb(sample_rgb)
            fill_rgb[gy, gx] = median_rgb
            fill_valid[gy, gx] = True
            initial_ids[gy, gx] = _match_id(matcher, median_rgb)

    outline_id = _choose_outline_id(line_pixels, matcher, palette_dict)
    protected_ids = set()
    if outline_id is not None and np.any(outline_mask):
        protected_ids.add(outline_id)
        color_ids[outline_mask] = outline_id

    _flatten_fill_components(color_ids, fill_valid, fill_rgb, initial_ids,
                             matcher)
    _smooth_low_chroma_noise(color_ids, outline_mask, fill_valid, fill_rgb)

    return CartoonQuantizeResult(
        color_ids=color_ids,
        color_counts=_count_color_ids(color_ids),
        protected_ids=protected_ids,
    )


def _sample_source_cell(src, gx, gy, pad_x, pad_y, new_w, new_h, scale_x,
                        scale_y):
    if gx < pad_x or gy < pad_y:
        return None
    if gx >= pad_x + new_w or gy >= pad_y + new_h:
        return None

    sx0 = (gx - pad_x) / scale_x
    sx1 = (gx + 1 - pad_x) / scale_x
    sy0 = (gy - pad_y) / scale_y
    sy1 = (gy + 1 - pad_y) / scale_y

    ix0 = max(0, int(np.floor(sx0)))
    ix1 = min(src.shape[1], int(np.ceil(sx1)))
    iy0 = max(0, int(np.floor(sy0)))
    iy1 = min(src.shape[0], int(np.ceil(sy1)))
    if ix1 <= ix0 or iy1 <= iy0:
        return None
    return src[iy0:iy1, ix0:ix1]


def _flatten_fill_components(color_ids, fill_valid, fill_rgb, initial_ids,
                             matcher):
    h, w = fill_valid.shape
    visited = np.zeros((h, w), dtype=bool)
    component_grid = np.full((h, w), -1, dtype=np.int32)
    components = []

    for sy in range(h):
        for sx in range(w):
            if visited[sy, sx] or not fill_valid[sy, sx]:
                continue
            comp_index = len(components)
            cells = []
            rgbs = []
            queue = deque([(sy, sx)])
            visited[sy, sx] = True
            running_rgb = fill_rgb[sy, sx].copy()
            seed_id = initial_ids[sy, sx]

            while queue:
                y, x = queue.popleft()
                cells.append((y, x))
                rgbs.append(fill_rgb[y, x])
                component_grid[y, x] = comp_index

                for ny, nx in _neighbors4(y, x, h, w):
                    if visited[ny, nx] or not fill_valid[ny, nx]:
                        continue
                    same_initial = (
                        seed_id is not None and initial_ids[ny, nx] == seed_id
                    )
                    chroma_barrier = (
                        abs(_rgb_chroma(fill_rgb[ny, nx]) -
                            _rgb_chroma(running_rgb)) > HIGH_CHROMA
                    )
                    close = (
                        _delta_e_rgb(fill_rgb[ny, nx], running_rgb) <= FILL_COMPONENT_DE
                        and not chroma_barrier
                    )
                    if same_initial or close:
                        visited[ny, nx] = True
                        queue.append((ny, nx))
                        running_rgb = np.median(
                            np.vstack((np.asarray(rgbs), fill_rgb[ny, nx])),
                            axis=0,
                        )

            comp_rgb = _trimmed_median_rgb(np.asarray(rgbs))
            components.append({
                "cells": cells,
                "rgb": comp_rgb,
                "id": _match_id(matcher, comp_rgb),
                "size": len(cells),
                "chroma": _rgb_chroma(comp_rgb),
            })

    _merge_tiny_components(components, component_grid)

    for idx, comp in enumerate(components):
        cid = comp["id"]
        for y, x in comp["cells"]:
            color_ids[y, x] = cid


def _merge_tiny_components(components, component_grid):
    h, w = component_grid.shape
    for idx, comp in enumerate(components):
        if comp["size"] > 2 or comp["chroma"] >= HIGH_CHROMA:
            continue

        neighbor_counts = {}
        for y, x in comp["cells"]:
            for ny, nx in _neighbors4(y, x, h, w):
                nidx = int(component_grid[ny, nx])
                if nidx >= 0 and nidx != idx:
                    neighbor_counts[nidx] = neighbor_counts.get(nidx, 0) + 1
        if not neighbor_counts:
            continue

        candidates = sorted(
            neighbor_counts,
            key=lambda nidx: (-components[nidx]["size"],
                              _delta_e_rgb(comp["rgb"], components[nidx]["rgb"]))
        )
        best = candidates[0]
        if _delta_e_rgb(comp["rgb"], components[best]["rgb"]) <= TINY_COMPONENT_DE:
            comp["id"] = components[best]["id"]


def _smooth_low_chroma_noise(color_ids, outline_mask, fill_valid, fill_rgb):
    h, w = fill_valid.shape
    updates = []
    for y in range(h):
        for x in range(w):
            if outline_mask[y, x] or not fill_valid[y, x]:
                continue
            if _rgb_chroma(fill_rgb[y, x]) >= HIGH_CHROMA:
                continue
            cid = color_ids[y, x]
            counts = {}
            counts4 = {}
            for ny in range(max(0, y - 1), min(h, y + 2)):
                for nx in range(max(0, x - 1), min(w, x + 2)):
                    if ny == y and nx == x:
                        continue
                    ncid = color_ids[ny, nx]
                    if ncid is None or outline_mask[ny, nx]:
                        continue
                    counts[ncid] = counts.get(ncid, 0) + 1
                    if abs(ny - y) + abs(nx - x) == 1:
                        counts4[ncid] = counts4.get(ncid, 0) + 1
            if not counts:
                continue
            best, best_count = max(counts.items(), key=lambda item: item[1])
            best4 = counts4.get(best, 0)
            if best != cid and (best_count >= 5 or best4 >= 3):
                updates.append((y, x, best))

    for y, x, cid in updates:
        color_ids[y, x] = cid


def _choose_outline_id(line_pixels, matcher, palette_dict):
    if not line_pixels:
        return None
    pixels = np.vstack(line_pixels)
    median_rgb = _trimmed_median_rgb(pixels)
    luma = float(_luminance(median_rgb.reshape(1, 3))[0])
    chroma = _rgb_chroma(median_rgb)

    dark_neutral_ids = [cid for cid in ("H7", "H6", "H5")
                        if cid in palette_dict]
    if dark_neutral_ids and luma <= 120.0 and chroma <= 80.0:
        if luma <= 80.0 and "H7" in palette_dict:
            return "H7"
        return _nearest_palette_id(median_rgb, dark_neutral_ids, palette_dict)
    return _match_id(matcher, median_rgb)


def _nearest_palette_id(rgb, candidate_ids, palette_dict):
    return min(candidate_ids,
               key=lambda cid: _delta_e_rgb(rgb, np.asarray(palette_dict[cid])))


def _trimmed_median_rgb(rgb_values):
    arr = np.asarray(rgb_values, dtype=np.float64).reshape(-1, 3)
    if len(arr) == 0:
        return np.array([0.0, 0.0, 0.0], dtype=np.float64)
    if len(arr) < 8:
        return np.median(arr, axis=0)
    lum = _luminance(arr)
    order = np.argsort(lum)
    lo = int(len(order) * 0.15)
    hi = int(len(order) * 0.95)
    if hi <= lo:
        trimmed = arr
    else:
        trimmed = arr[order[lo:hi]]
    return np.median(trimmed, axis=0)


def _match_id(matcher, rgb):
    rgb_int = tuple(int(np.clip(round(v), 0, 255)) for v in rgb)
    cid, _ = matcher.match_single(rgb_int)
    return str(cid)


def _count_color_ids(color_ids):
    counts = {}
    for cid in color_ids.ravel():
        if cid is not None:
            counts[cid] = counts.get(cid, 0) + 1
    return counts


def _neighbors4(y, x, h, w):
    if y > 0:
        yield y - 1, x
    if y + 1 < h:
        yield y + 1, x
    if x > 0:
        yield y, x - 1
    if x + 1 < w:
        yield y, x + 1


def _luminance(rgb):
    arr = np.asarray(rgb, dtype=np.float64)
    return arr[..., 0] * 0.299 + arr[..., 1] * 0.587 + arr[..., 2] * 0.114


def _rgb_chroma(rgb):
    arr = np.asarray(rgb, dtype=np.float64)
    return float(arr.max() - arr.min())


def _delta_e_rgb(rgb1, rgb2):
    a = np.asarray(rgb1, dtype=np.float64)
    b = np.asarray(rgb2, dtype=np.float64)
    l1, a1, b1 = _rgb_to_lab(
        np.array([a[0]]), np.array([a[1]]), np.array([a[2]])
    )
    l2, a2, b2 = _rgb_to_lab(
        np.array([b[0]]), np.array([b[1]]), np.array([b[2]])
    )
    return float(_ciede2000((l1[0], a1[0], b1[0]),
                            (l2[0], a2[0], b2[0])))
