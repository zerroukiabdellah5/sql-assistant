import app.prompts as prompts
import app.schema as schema_mod


class _Turn:
    def __init__(self, prompt, sql=""):
        self.prompt = prompt
        self.sql = sql


def test_system_prompt_has_no_store_hardcodes(sample_db):
    schema = schema_mod.inspect_database(sample_db)
    instruction = prompts.build_system_instruction(schema)
    for forbidden in [
        "Nike", "Air Max", "no longer exist", "store database",
        "total_quantity", "SUM(orders.quantity)",
        "brand, category, customer, order, or country",
    ]:
        assert forbidden not in instruction


def test_system_prompt_contains_detected_tables(sample_db):
    schema = schema_mod.inspect_database(sample_db)
    instruction = prompts.build_system_instruction(schema)
    assert "TABLE products" in instruction
    assert "TABLE regions" in instruction


def test_system_prompt_contains_detected_relationships(sample_db):
    schema = schema_mod.inspect_database(sample_db)
    instruction = prompts.build_system_instruction(schema)
    assert "- products.category_id = categories.id" in instruction
    assert "- orders.product_id = products.id" in instruction


def test_system_prompt_json_contract(sample_db):
    schema = schema_mod.inspect_database(sample_db)
    instruction = prompts.build_system_instruction(schema)
    assert '"sql"' in instruction
    assert '"explanation"' in instruction
    assert "JSON" in instruction


def test_system_prompt_has_an_example(sample_db):
    schema = schema_mod.inspect_database(sample_db)
    instruction = prompts.build_system_instruction(schema)
    assert "SELECT * FROM 'brands'" in instruction
    assert " JOIN " in instruction


def test_history_block_truncation():
    turns = [_Turn("x" * 2000)] * 20
    block = prompts.build_history_block(turns)
    assert "Turn 20" not in block
    turn_lines = sum(
        1 for line in block.splitlines() if line.startswith("Turn ")
    )
    assert turn_lines == 10


def test_history_block_first_question():
    block = prompts.build_history_block([])
    assert "first question" in block


def test_user_message():
    message = prompts.build_user_message(
        "How many widgets?",
        [_Turn("Show products", "SELECT * FROM products")],
    )
    assert "CURRENT REQUEST" in message
    assert "How many widgets?" in message