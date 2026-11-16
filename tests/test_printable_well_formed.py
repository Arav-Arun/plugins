import unittest

from ardia_cli.printable import well_formed


class WellFormed(unittest.TestCase):
    def test_a_lone_surrogate_becomes_a_replacement_character(self):
        self.assertEqual(well_formed("a \ud83e b"), "a � b")
        well_formed("a \ud83e b").encode("utf-8")

    def test_a_surrogate_pair_becomes_its_character(self):
        self.assertEqual(well_formed("🤝"), "\U0001f91d")

    def test_ordinary_text_is_returned_as_is(self):
        text = "Ka'Chava 🤝 café"
        self.assertIs(well_formed(text), text)


if __name__ == "__main__":
    unittest.main()
