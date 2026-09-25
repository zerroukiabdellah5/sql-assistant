import app.schema as schema_mod


def test_inspect_database_discovers_tables_and_fks(sample_db):
    schema = schema_mod.inspect_database(sample_db)
    names = {t["name"] for t in schema["tables"]}
    assert names == {"brands", "categories", "orders", "products", "regions"}
    products = next(t for t in schema["tables"] if t["name"] == "products")
    col_names = {c["name"] for c in products["columns"]}
    assert col_names == {
        "id", "name", "description", "price", "stock",
        "brand_id", "category_id",
    }
    fks = {(fk["from"], fk["table"]) for fk in products["foreign_keys"]}
    assert fks == {("brand_id", "brands"), ("category_id", "categories")}


def test_schema_to_text_contract(sample_db):
    schema = schema_mod.inspect_database(sample_db)
    text = schema_mod.schema_to_text(schema)
    assert "TABLE products" in text
    assert "FOREIGN KEYS:" in text
    assert "brand_id -> brands.id" in text


def test_get_schema_matches_text_contract(sample_db):
    text = schema_mod.get_schema(sample_db)
    assert text.startswith("TABLE ")
    assert text.count("TABLE ") == 5


def test_list_relationships_derived_not_hardcoded(sample_db):
    schema = schema_mod.inspect_database(sample_db)
    rels = schema_mod.list_relationships(schema)
    assert "products.category_id = categories.id" in rels
    assert "products.brand_id = brands.id" in rels
    assert "orders.product_id = products.id" in rels


def test_table_names_quoted(sample_db):
    schema = schema_mod.inspect_database(sample_db)
    names = schema_mod.table_names(schema)
    assert "'products'" in names
    assert "'regions'" in names


def test_works_with_out_of_band_table(sample_db):
    schema = schema_mod.inspect_database(sample_db)
    assert "regions" in {t["name"] for t in schema["tables"]}