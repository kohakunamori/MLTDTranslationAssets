import base64
import unittest

from PIL import Image
import test_image_model as experiment
import image_text_regions as regions


class ImageExperimentTests(unittest.TestCase):
    def test_request_uses_native_gemini_parts_and_image_modalities(self):
        body = experiment.payload("汉化", b"png", "1:4", "2K")
        parts = body["contents"][0]["parts"]
        self.assertEqual(parts[1]["inlineData"], {"mimeType": "image/png", "data": "cG5n"})
        self.assertEqual(body["generationConfig"]["responseModalities"], ["TEXT", "IMAGE"])
        self.assertNotIn("messages", body)

    def test_assembly_retains_part_order_and_original_pixel_coordinates(self):
        a, b = Image.new("RGBA", (3, 2), "red"), Image.new("RGBA", (3, 1), "blue")
        canvas, layout = experiment.assemble([("a", a), ("b", b)])
        self.assertEqual(canvas.size, (3, 3))
        self.assertEqual(canvas.getpixel((1, 1)), a.getpixel((1, 1)))
        self.assertEqual(canvas.getpixel((1, 2)), b.getpixel((1, 0)))
        self.assertEqual(layout[1]["box"], [0, 2, 3, 3])

    def test_response_requires_one_actual_image_and_ignores_thought_parts(self):
        raw = experiment.png(Image.new("RGB", (2, 2), "red"))
        inline = {"inlineData": {"mimeType": "image/png", "data": base64.b64encode(raw).decode()}}
        doc = {"candidates": [{"content": {"parts": [dict(inline, thought=True), inline]}}]}
        image, _ = experiment.decode_response(doc)
        self.assertEqual(image.size, (2, 2))
        doc["candidates"][0]["content"]["parts"].append(inline)
        with self.assertRaisesRegex(ValueError, "got 2"):
            experiment.decode_response(doc)


class TextRegionRestoreTests(unittest.TestCase):
    def test_source_decoder_rounding_tolerance_does_not_admit_art_changes(self):
        source = Image.new("RGBA", (2, 2), (100, 100, 100, 255))
        rounded = Image.new("RGBA", (2, 2), (101, 100, 99, 255))
        self.assertEqual(regions.verify_decoded_source(source, rounded), 1)
        with self.assertRaisesRegex(ValueError, "differs"):
            regions.verify_decoded_source(source, Image.new("RGBA", (2, 2), (102, 100, 100, 255)))
        with self.assertRaisesRegex(ValueError, "differs"):
            regions.verify_decoded_source(source, Image.new("RGBA", (2, 2), (100, 100, 100, 254)))

    def test_rgb_changes_with_unchanged_alpha_are_checked_and_confined_to_mask(self):
        original = Image.new("RGBA", (4, 4), (255, 0, 0, 128))
        generated = Image.new("RGBA", (8, 8), (0, 255, 0, 0))
        result, report = regions.restore(original, generated, (4, 4), [{"box": [1, 1, 3, 3]}], [[0, 0, 1, 1]])
        self.assertEqual(result.getpixel((0, 0)), original.getpixel((0, 0)))
        self.assertEqual(result.getpixel((1, 1)), (0, 255, 0, 128))
        self.assertEqual(list(result.getchannel("A").getdata()), list(original.getchannel("A").getdata()))
        self.assertEqual(report["outside_mask_changed_pixels"], 0)
        self.assertEqual(report["changed_pixels"], 4)

    def test_a_region_cannot_include_protected_artwork(self):
        source = Image.new("RGB", (4, 4))
        with self.assertRaisesRegex(ValueError, "overlaps protected"):
            regions.restore(source, source, (4, 4), [{"box": [0, 0, 3, 3]}], [[2, 2, 4, 4]])

    def test_changed_canvas_ratio_is_refused(self):
        with self.assertRaisesRegex(ValueError, "aspect ratio"):
            regions.restore(Image.new("RGB", (4, 4)), Image.new("RGB", (4, 8)), (4, 4), [])

    def test_out_of_bounds_or_empty_regions_are_refused(self):
        for box in ([0, 0, 5, 4], [1, 1, 1, 2]):
            with self.assertRaisesRegex(ValueError, "coordinates"):
                regions.text_mask((4, 4), [{"box": box}])


if __name__ == "__main__":
    unittest.main()
