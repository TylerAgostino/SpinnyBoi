# pyright: basic
"""Table-driven tests for reactions.txt parsing (issue #113): a blank or
comma-less line must never register the empty-string key, which would match
every message via str.find()."""
import unittest
from pathlib import Path
from unittest import mock

from modules.reactionsCog import parse_reaction_lines


class ParseReactionLinesTests(unittest.TestCase):
    def test_well_formed_lines(self):
        parsed = parse_reaction_lines(
            ["lol,emote1\n", "rofl,emote2,emote3\n", "LOL2, emote4 \n"]
        )
        self.assertEqual(
            parsed,
            {
                "lol": ["emote1"],
                "rofl": ["emote2", "emote3"],
                "lol2": ["emote4"],
            },
        )

    def test_malformed_lines_are_skipped_never_empty_key(self):
        with mock.patch("modules.reactionsCog.logging.warning") as warn:
            parsed = parse_reaction_lines(
                [
                    "\n",  # blank
                    "   \n",  # whitespace only
                    "wot\n",  # comma-less: no emote ids
                    ",emote1\n",  # empty key
                    "ok,\n",  # key but only empty emote fields
                ]
            )
        self.assertEqual(parsed, {})
        self.assertEqual(warn.call_count, 5)
        # warnings must identify the offending line number
        warned_lines = [str(call.args[0]) for call in warn.call_args_list]
        self.assertTrue(all(f"line {n}:" in msg for n, msg in enumerate(warned_lines, 1)))

    def test_good_lines_survive_next_to_bad_ones(self):
        parsed = parse_reaction_lines(
            ["good,emote_ok\n", "\n", "bad,\n", "also,emote_a,emote_b\n"]
        )
        self.assertEqual(
            parsed, {"good": ["emote_ok"], "also": ["emote_a", "emote_b"]}
        )

    def test_duplicate_key_last_line_wins(self):
        parsed = parse_reaction_lines(["dup,first\n", "dup,second\n"])
        self.assertEqual(parsed, {"dup": ["second"]})

    def test_empty_middle_emote_dropped_but_key_kept(self):
        parsed = parse_reaction_lines(["mid,,emote2\n"])
        self.assertEqual(parsed, {"mid": ["emote2"]})
class RealFileTests(unittest.TestCase):
    def test_shipped_reactions_file_parses_clean_and_warns_nothing(self):
        shipped = Path(__file__).resolve().parent.parent / "reactions.txt"
        with open(shipped, encoding="utf-8") as fp:
            lines = fp.readlines()
        with mock.patch("modules.reactionsCog.logging.warning") as warn:
            parsed = parse_reaction_lines(lines)
        warn.assert_not_called()
        self.assertTrue(parsed)
        self.assertTrue(all(key and emotes for key, emotes in parsed.items()))


if __name__ == "__main__":
    unittest.main()
