import pytest

from app import safety


@pytest.mark.parametrize(
    "sql, expected",
    [
        ("SELECT * FROM products", safety.AUTO),
        ("WITH x AS (SELECT 1) SELECT * FROM x", safety.AUTO),
        ("  select name from brands", safety.AUTO),
        ("INSERT INTO brands (name) VALUES ('X')", safety.APPROVAL),
        ("UPDATE products SET price = 1", safety.APPROVAL),
        ("DELETE FROM orders", safety.APPROVAL),
        ("DROP TABLE products", safety.APPROVAL),
        ("CREATE TABLE t (id INTEGER)", safety.APPROVAL),
        ("ATTACH 'x.db' AS y", safety.APPROVAL),
    ],
)
def test_classify_operation(sql, expected):
    assert safety.classify_operation(sql) == expected


def test_classify_empty_raises():
    with pytest.raises(ValueError):
        safety.classify_operation("   ")


def test_describe_operation():
    label = safety.describe_operation(
        "INSERT INTO brands (name) VALUES ('Acme')"
    )
    assert label.startswith("INSERT — ")
    assert "Acme" in label


def test_describe_operation_truncates():
    long_sql = "INSERT INTO t VALUES (" + ",".join(["1"] * 50) + ")"
    label = safety.describe_operation(long_sql)
    assert "…" in label
    assert "INSERT — " in label


def test_ensure_auto_accepts_select():
    assert safety.ensure_auto("SELECT 1") == safety.AUTO


def test_ensure_auto_rejects_mutation():
    with pytest.raises(ValueError):
        safety.ensure_auto("DELETE FROM products")