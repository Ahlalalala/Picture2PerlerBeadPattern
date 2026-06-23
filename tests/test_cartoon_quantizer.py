import sys
import unittest
from pathlib import Path

import numpy as np
from PIL import Image, ImageDraw

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from bead_pattern_tool import BeadPatternTool
from cartoon_quantizer import quantize_cartoon_to_grid
from color_matcher import get_matcher
from mard_palette import get_palette_dict


PALETTE = get_palette_dict()


def _antialiased_image(size, draw_func, scale=4):
    big = Image.new("RGBA", (size[0] * scale, size[1] * scale), (0, 0, 0, 0))
    draw = ImageDraw.Draw(big)
    draw_func(draw, scale)
    return big.resize(size, Image.Resampling.LANCZOS)


class CartoonQuantizerTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.matcher = get_matcher()

    def test_antialiased_black_outline_stays_single_outline_color(self):
        def draw_shape(draw, s):
            draw.rounded_rectangle(
                [20 * s, 20 * s, 220 * s, 140 * s],
                radius=32 * s,
                fill=(238, 204, 150, 255),
                outline=(0, 0, 0, 255),
                width=16 * s,
            )
            draw.line(
                [(82 * s, 94 * s), (150 * s, 96 * s)],
                fill=(0, 0, 0, 255),
                width=8 * s,
            )

        image = _antialiased_image((240, 160), draw_shape)
        result = quantize_cartoon_to_grid(
            image, 24, 16, self.matcher, PALETTE)

        self.assertEqual(result.protected_ids, {"H7"})
        self.assertIn("H7", result.color_counts)
        self.assertNotIn("G14", result.color_counts)
        self.assertNotIn("G17", result.color_counts)

        fill_ids = [cid for cid in result.color_counts if cid != "H7"]
        self.assertEqual(len(fill_ids), 1)

    def test_thin_black_line_is_detected_as_outline(self):
        image = Image.new("RGBA", (100, 100), (238, 204, 150, 255))
        draw = ImageDraw.Draw(image)
        draw.line([(0, 50), (99, 50)], fill=(0, 0, 0, 255), width=1)

        result = quantize_cartoon_to_grid(
            image, 10, 10, self.matcher, PALETTE)

        self.assertIn("H7", result.color_counts)
        self.assertGreaterEqual(result.color_counts["H7"], 8)

    def test_high_chroma_small_region_is_preserved(self):
        def draw_shape(draw, s):
            draw.rectangle(
                [20 * s, 20 * s, 180 * s, 120 * s],
                fill=(238, 204, 150, 255),
                outline=(0, 0, 0, 255),
                width=10 * s,
            )
            draw.rectangle(
                [60 * s, 62 * s, 150 * s, 74 * s],
                fill=(235, 70, 82, 255),
            )

        image = _antialiased_image((200, 140), draw_shape)
        result = quantize_cartoon_to_grid(
            image, 20, 14, self.matcher, PALETTE)

        red_like = []
        for cid in result.color_counts:
            if cid == "H7":
                continue
            r, g, b = PALETTE[cid]
            if r > 180 and g < 150 and b < 160:
                red_like.append(cid)

        self.assertIn("H7", result.color_counts)
        self.assertTrue(red_like, result.color_counts)

    def test_merge_colors_keeps_protected_outline(self):
        tool = BeadPatternTool.__new__(BeadPatternTool)
        tool.grid_w = 3
        tool.grid_h = 1
        tool.PALETTE_DICT = PALETTE

        color_ids = np.array([["H7", "A11", "G11"]], dtype=object)
        counts = {"H7": 1, "A11": 1, "G11": 1}

        merged = BeadPatternTool._merge_colors(
            tool, color_ids, 1, counts, protected_ids={"H7"})
        merged_ids = {cid for cid in merged.ravel() if cid is not None}

        self.assertIn("H7", merged_ids)
        self.assertEqual(len(merged_ids), 2)
        self.assertNotEqual(merged[0, 1], "H7")
        self.assertNotEqual(merged[0, 2], "H7")


if __name__ == "__main__":
    unittest.main()
