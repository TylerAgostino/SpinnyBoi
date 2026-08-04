import unittest

from modules.incidentCog import ReactionManager


class TestReactionManager(unittest.TestCase):
    def setUp(self):
        self.reaction_manager = ReactionManager()

    def test_yield_reactions(self):
        expected_reactions = [
            "regional_indicator_a",
            "regional_indicator_b",
            "regional_indicator_c",
            "regional_indicator_d",
            "regional_indicator_e",
            "regional_indicator_f",
            "regional_indicator_g",
            "regional_indicator_h",
            "regional_indicator_i",
            "regional_indicator_j",
            "regional_indicator_k",
            "regional_indicator_l",
            "regional_indicator_m",
            "regional_indicator_n",
            "regional_indicator_o",
            "regional_indicator_p",
            "regional_indicator_q",
            "regional_indicator_r",
            "regional_indicator_s",
            "regional_indicator_t",
            "regional_indicator_u",
            "regional_indicator_v",
            "regional_indicator_w",
            "regional_indicator_x",
            "regional_indicator_y",
            "regional_indicator_z"
        ]
        actual_reactions = list(self.reaction_manager.yield_reactions())
        self.assertEqual(actual_reactions, expected_reactions)
        self.assertEqual(self.reaction_manager.reactions, 26)

    def test_apply_reactions(self):
        class MockMessage:
            def __init__(self):
                self.reactions = []

            def add_reaction(self, reaction):
                self.reactions.append(reaction)

        mock_message = MockMessage()
        self.reaction_manager.reactions = 5  # Simulate that 5 reactions have been yielded
        self.reaction_manager.apply_reactions(mock_message)

        expected_reactions = ["🇦", "🇧", "🇨", "🇩", "🇪"]
        self.assertEqual(mock_message.reactions, expected_reactions)
