from django.core.exceptions import ValidationError
from django.test import TestCase

from portfolio.models import Portfolio

from .helpers import make_portfolio, make_stock


class ExpectedCompositionValidationTests(TestCase):
    def setUp(self):
        self.aaa = make_stock("AAA", "100")
        self.bbb = make_stock("BBB", "50")

    def assert_rejected(self, targets: dict, message: str):
        with self.assertRaises(ValidationError) as ctx:
            make_portfolio(targets=targets)
        self.assertIn(message, str(ctx.exception.message_dict["expected_composition"]))
        self.assertEqual(Portfolio.objects.count(), 0)

    def test_weights_summing_less_than_one_are_rejected(self):
        self.assert_rejected({self.aaa: "0.3", self.bbb: "0.6"}, "Weights must sum to 1 (got 0.9)")

    def test_weights_summing_more_than_one_are_rejected(self):
        self.assert_rejected({self.aaa: "0.5", self.bbb: "0.6"}, "Weights must sum to 1 (got 1.1)")

    def test_negative_weights_are_rejected_even_if_sum_is_one(self):
        self.assert_rejected({self.aaa: "1.2", self.bbb: "-0.2"}, "must be >= 0")

    def test_weights_summing_exactly_one_are_accepted(self):
        portfolio = make_portfolio(targets={self.aaa: "0.4", self.bbb: "0.6"})
        self.assertEqual(Portfolio.objects.get().pk, portfolio.pk)

    def test_zero_weight_is_accepted(self):
        make_portfolio(targets={self.aaa: "1", self.bbb: "0"})
        self.assertEqual(Portfolio.objects.count(), 1)
