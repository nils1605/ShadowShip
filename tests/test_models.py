from shadowship.models import BehaviorDifference, Classification, JudgeDecision, Verdict


def test_enums_serialize_as_plain_strings():
    assert Classification.REGRESSION == "regression"
    assert Verdict.BLOCK.value == "BLOCK"


def test_dataclass_defaults_are_independent():
    a = JudgeDecision(verdict=Verdict.PROMOTE, score=100, summary="ok")
    b = JudgeDecision(verdict=Verdict.PROMOTE, score=100, summary="ok")
    a.generated_tests.append("tests/generated/test_x.py")
    assert b.generated_tests == []


def test_behavior_difference_fields():
    diff = BehaviorDifference(
        journey="large-cart-checkout",
        step="checkout",
        field="total",
        baseline=90.0,
        candidate=81.0,
        classification=Classification.REGRESSION,
        reason="total changed outside the intended discount",
    )
    assert diff.classification is Classification.REGRESSION
