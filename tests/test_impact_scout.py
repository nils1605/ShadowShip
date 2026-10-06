import subprocess
import textwrap

import pytest

from shadowship.agents.impact_scout import (
    MODULE_LEVEL,
    changed_line_numbers,
    imported_modules,
    intent_from_commits,
    journeys_hitting,
    module_name,
    route_matches,
    scout,
    top_level_symbols,
)
from shadowship.models import Journey, JourneyStep

# --- pure helpers ---------------------------------------------------------------------


def test_changed_line_numbers_parses_hunks():
    diff = textwrap.dedent("""\
        diff --git a/app.py b/app.py
        --- a/app.py
        +++ b/app.py
        @@ -3 +3,2 @@ def f():
        -    return 1
        +    x = 2
        +    return x
        @@ -10,2 +11,0 @@
        -gone
        -gone
        diff --git a/new.py b/new.py
        --- /dev/null
        +++ b/new.py
        @@ -0,0 +1 @@
        +print()
        """)
    assert changed_line_numbers(diff) == {
        "app.py": ({3, 10, 11}, {3, 4}),
        "new.py": (set(), {1}),
    }


SHOP = textwrap.dedent("""\
    from fastapi import FastAPI
    from shop import pricing

    app = FastAPI()
    RATE = 0.08


    def helper():
        return pricing.tax_on(1)


    @app.post("/checkout")
    def checkout():
        return helper()


    @app.get("/cart/{cart_id}")
    def get_cart(cart_id: str):
        return {}
    """)


def test_top_level_symbols_finds_functions_assignments_and_routes():
    symbols = {s.name: s for s in top_level_symbols("shop/main.py", SHOP)}
    assert set(symbols) == {"app", "RATE", "helper", "checkout", "get_cart"}
    assert symbols["checkout"].route == "POST /checkout"
    assert symbols["get_cart"].route == "GET /cart/{cart_id}"
    assert symbols["helper"].route is None
    assert {"pricing", "tax_on"} <= symbols["helper"].references
    assert symbols["checkout"].is_function and not symbols["RATE"].is_function


def test_top_level_symbols_tolerates_syntax_errors():
    assert top_level_symbols("bad.py", "def (:") == []


def test_imported_modules_and_module_name():
    assert {"fastapi", "shop", "shop.pricing"} <= imported_modules(SHOP)
    assert module_name("shop/pricing.py") == "shop.pricing"
    assert module_name("shop/__init__.py") == "shop"


def test_route_matches_templates():
    assert route_matches("POST /cart/{cart_id}/items", "POST", "/cart/abc123/items")
    assert route_matches("GET /products", "get", "/products?page=2")
    assert not route_matches("POST /cart/{cart_id}/items", "GET", "/cart/abc123/items")
    assert not route_matches("POST /cart/{cart_id}/items", "POST", "/cart/abc/items/extra")
    assert not route_matches("POST /checkout", "POST", "/checkout/extra")


def test_journeys_hitting_handles_placeholders():
    journeys = [
        Journey("add", [JourneyStep("add", "POST", "/cart/{cart_id}/items")]),
        Journey("browse", [JourneyStep("list", "GET", "/products")]),
    ]
    assert journeys_hitting(["POST /cart/{cart_id}/items"], journeys) == ["add"]


def test_intent_from_commits_joins_messages_and_drops_trailers():
    messages = ["Add SAVE10 code\n\nGives 10% off.\n\nCo-Authored-By: Bot <bot@example.com>", "Fix tax!"]
    assert intent_from_commits(messages) == "Add SAVE10 code. Gives 10% off. Fix tax!"
    assert intent_from_commits([]) == "No commit messages found."


# --- end to end in a throwaway git repo ---------------------------------------------------


def _git(repo, *args):
    subprocess.run(["git", "-c", "user.name=t", "-c", "user.email=t@example.com", *args], cwd=repo, check=True,
                   capture_output=True)


PRICING = textwrap.dedent("""\
    CODES = {"WELCOME5": 5}


    def discount_for(code):
        return CODES.get(code, 0)


    def tax_on(amount):
        return amount * 0.08
    """)

MAIN = textwrap.dedent("""\
    from fastapi import FastAPI
    from shop import pricing

    app = FastAPI()


    @app.get("/products")
    def products():
        return []


    @app.post("/checkout")
    def checkout(code: str):
        return {"discount": pricing.discount_for(code)}


    @app.post("/tax")
    def tax(amount: float):
        return {"tax": pricing.tax_on(amount)}
    """)


@pytest.fixture
def shop_repo(tmp_path):
    repo = tmp_path / "repo"
    (repo / "shop" / "tests").mkdir(parents=True)
    (repo / "shop" / "__init__.py").write_text("")
    (repo / "shop" / "pricing.py").write_text(PRICING)
    (repo / "shop" / "main.py").write_text(MAIN)
    (repo / "shop" / "tests" / "test_pricing.py").write_text("from shop import pricing\n")
    _git(repo, "init", "-q", "-b", "main")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "shop")
    _git(repo, "checkout", "-q", "-b", "feature")
    return repo


def test_scout_follows_references_from_changed_constant_to_route(shop_repo):
    pricing = shop_repo / "shop" / "pricing.py"
    pricing.write_text(PRICING.replace('{"WELCOME5": 5}', '{"WELCOME5": 5, "SAVE10": 10}'))
    _git(shop_repo, "commit", "-q", "-am", "Add SAVE10 discount code")

    journeys = [
        Journey("checkout", [JourneyStep("pay", "POST", "/checkout")]),
        Journey("browse", [JourneyStep("list", "GET", "/products")]),
    ]
    report = scout("main", "feature", shop_repo, journeys)

    assert report.changed_files == ["shop/pricing.py"]
    assert report.changed_symbols == ["shop/pricing.py::CODES"]
    assert report.affected_routes == ["POST /checkout"]  # CODES -> discount_for -> checkout; not /tax
    assert report.dependents == ["shop/main.py", "shop/tests/test_pricing.py"]
    assert report.relevant_journeys == ["checkout"]
    assert report.intent_summary == "Add SAVE10 discount code."
    assert any("Money-related" in hint for hint in report.risk_hints)
    assert "No tests were added or changed." in report.risk_hints


def test_scout_flags_changed_function_without_test_and_removed_symbols(shop_repo):
    main = shop_repo / "shop" / "main.py"
    main.write_text(MAIN.replace('@app.post("/tax")\ndef tax(amount: float):\n    return {"tax": pricing.tax_on(amount)}\n', ""))
    pricing = shop_repo / "shop" / "pricing.py"
    pricing.write_text(PRICING.replace("amount * 0.08", "amount * 0.10"))
    tests = shop_repo / "shop" / "tests" / "test_pricing.py"
    tests.write_text("from shop import pricing\n\n\ndef test_discount():\n    assert pricing.discount_for('X') == 0\n")
    _git(shop_repo, "commit", "-q", "-am", "Raise tax and drop /tax")

    report = scout("main", "feature", shop_repo)

    assert "shop/main.py::tax" in report.changed_symbols
    assert "shop/pricing.py::tax_on" in report.changed_symbols
    assert not any(symbol.endswith(MODULE_LEVEL) for symbol in report.changed_symbols)
    assert "Changed without any updated test referencing it: tax, tax_on." in report.risk_hints
    assert "Removed or renamed: shop/main.py::tax. Callers elsewhere may break." in report.risk_hints
