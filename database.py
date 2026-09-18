import sqlite3


def init_db():
    conn = sqlite3.connect("store.db")
    cursor = conn.cursor()

    cursor.execute("PRAGMA foreign_keys = ON")

    # Drop in reverse dependency order so foreign keys stay valid
    cursor.execute("DROP TABLE IF EXISTS orders")
    cursor.execute("DROP TABLE IF EXISTS products")
    cursor.execute("DROP TABLE IF EXISTS brands")
    cursor.execute("DROP TABLE IF EXISTS categories")

    cursor.execute(
        """
        CREATE TABLE categories (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL UNIQUE
        )
        """
    )

    cursor.execute(
        """
        CREATE TABLE brands (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL UNIQUE,
            country TEXT NOT NULL
        )
        """
    )

    cursor.execute(
        """
        CREATE TABLE products (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL,
            category_id INTEGER NOT NULL,
            brand_id INTEGER NOT NULL,
            price REAL NOT NULL,
            stock INTEGER NOT NULL,
            FOREIGN KEY (category_id) REFERENCES categories(id),
            FOREIGN KEY (brand_id) REFERENCES brands(id)
        )
        """
    )

    cursor.execute(
        """
        CREATE TABLE orders (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            product_id INTEGER NOT NULL,
            customer_name TEXT NOT NULL,
            quantity INTEGER NOT NULL,
            order_date TEXT NOT NULL,
            FOREIGN KEY (product_id) REFERENCES products(id)
        )
        """
    )

    cursor.executemany(
        "INSERT INTO categories (name) VALUES (?)",
        [
            ("Clothing",),
            ("Cosmetics",),
        ],
    )

    cursor.executemany(
        "INSERT INTO brands (name, country) VALUES (?, ?)",
        [
            ("Nike", "USA"),
            ("Adidas", "Germany"),
            ("Levi's", "USA"),
            ("Zara", "Spain"),
            ("Gucci", "Italy"),
            ("Burberry", "UK"),
            ("Dior", "France"),
            ("Estee Lauder", "USA"),
            ("Guerlain", "France"),
            ("Make Up For Ever", "France"),
            ("Lancome", "France"),
            ("Yves Saint Laurent", "France"),
        ],
    )

    # category_id: 1 = Clothing, 2 = Cosmetics
    # brand_id follows insertion order above
    sample_products = [
        # Clothing
        ("Air Max 90 Sneakers", 1, 1, 129.99, 45),
        ("Classic Fit Hoodie", 1, 2, 65.00, 120),
        ("Slim Fit Denim Jeans", 1, 3, 89.50, 30),
        ("Basic Cotton T-Shirt", 1, 4, 19.99, 200),
        ("Monogram Leather Jacket", 1, 5, 1250.00, 5),
        ("Essential Trench Coat", 1, 6, 990.00, 8),
        # Cosmetics
        ("Rouge Dior Lipstick", 2, 7, 45.00, 85),
        ("Advanced Night Repair Serum", 2, 8, 78.00, 50),
        ("Shalimar Eau de Parfum", 2, 9, 135.00, 25),
        ("Matte Velvet Skin Foundation", 2, 10, 42.00, 60),
        ("Hypnose Mascara", 2, 11, 32.00, 110),
        ("Black Opium Perfume", 2, 12, 125.00, 40),
    ]

    cursor.executemany(
        """
        INSERT INTO products (name, category_id, brand_id, price, stock)
        VALUES (?, ?, ?, ?, ?)
        """,
        sample_products,
    )

    sample_orders = [
        (1, "Alice Martin", 2, "2026-08-12"),
        (2, "Bruno Chen", 1, "2026-08-15"),
        (3, "Clara Dubois", 3, "2026-08-18"),
        (4, "David Kim", 4, "2026-08-21"),
        (5, "Emma Rossi", 1, "2026-08-24"),
        (7, "Fatima El Amrani", 2, "2026-08-27"),
        (8, "Gabriel Lopez", 1, "2026-09-01"),
        (9, "Hana Suzuki", 1, "2026-09-04"),
        (11, "Ivan Petrov", 3, "2026-09-08"),
        (12, "Julie Moreau", 2, "2026-09-11"),
        (1, "Alice Martin", 1, "2026-09-14"),
        (6, "Karim Benali", 1, "2026-09-16"),
    ]

    cursor.executemany(
        """
        INSERT INTO orders (product_id, customer_name, quantity, order_date)
        VALUES (?, ?, ?, ?)
        """,
        sample_orders,
    )

    conn.commit()
    conn.close()
    print(
        "Database 'store.db' initialized with categories, brands, products, and orders."
    )


if __name__ == "__main__":
    init_db()
