import base64
import unittest

from PIL import Image
import test_image_model as experiment


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


if __name__ == "__main__":
    unittest.main()
