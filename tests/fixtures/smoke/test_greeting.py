import unittest

from greeting import greet


class GreetingTests(unittest.TestCase):
    def test_named_greeting(self):
        self.assertEqual(greet("Ada"), "Hello, Ada!")
