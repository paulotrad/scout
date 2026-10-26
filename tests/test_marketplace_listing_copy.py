"""Card titles and generated copy stay grounded in saved listing evidence."""

import json
from pathlib import Path
import tempfile
import unittest
from marketplace_listing_copy import display_title, run


class ListingCopyTests(unittest.TestCase):
    def test_zero_price_titles_show_the_item(self):
        self.assertEqual(display_title("free couch!", 0, "couch"), "Couch")
        self.assertEqual(display_title("Free chair", 0, "office chair"), "Chair")
        self.assertEqual(display_title("FREE", 0, "moving box"), "Moving box")
        self.assertEqual(display_title("Free couch!", 25, "couch"), "Free couch!")

    def test_generated_copy_keeps_original_title_and_only_photo_supported_detail(self):
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "input.json"
            output = Path(directory) / "output.json"
            source.write_text(json.dumps({"listings": [{
                "listing_id": "1", "title": "free couch!", "asking_price": 0,
                "description": "Only the couch is included.",
                "vision": {"observations": {
                    "listing_kind": "product", "item_type": "couch",
                    "classification_evidence": [
                        "Photo: The main object in the photo is a brown fabric couch, "
                        "which is the item being offered."],
                    "wear_or_damage": ["couch shows signs of wear"],
                }},
            }]},), encoding="utf-8")
            result = run(source, output)
            couch = result["listings"][0]
            self.assertEqual(couch["title"], "free couch!")
            self.assertEqual(couch["display_title"], "Couch")
            self.assertEqual(couch["generated_description_status"], "generated")
            self.assertEqual(couch["generated_description"],
                             "The photo shows a brown fabric couch. "
                             "Seller says only the couch is included.")


if __name__ == "__main__":
    unittest.main()
