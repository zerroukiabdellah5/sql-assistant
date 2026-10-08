import random
import sqlite3
import sys


# ============================================================
# SEED DEMO DATA
# ============================================================
# Data-only expansion of the demo dataset. Builds a NEW database
# file using exactly the schema shipped in database.py, preserves
# the original 12/12/12/2 rows, and adds a much larger coherent
# catalog with months of orders.
#
# Usage:
#   python seed_demo_data.py <output>.db
#
# The application's store.db is never touched by this script; it
# reads no input file, so a validated output can copy over store.db
# later (keep the backup).
# ============================================================

ORIGINAL_CATEGORIES = [
    "Clothing",
    "Cosmetics",
]

ORIGINAL_BRANDS = [
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
]

ORIGINAL_PRODUCTS = [
    ("Air Max 90 Sneakers", "Clothing", "Nike", 129.99, 45),
    ("Classic Fit Hoodie", "Clothing", "Adidas", 65.00, 120),
    ("Slim Fit Denim Jeans", "Clothing", "Levi's", 89.50, 30),
    ("Basic Cotton T-Shirt", "Clothing", "Zara", 19.99, 200),
    ("Monogram Leather Jacket", "Clothing", "Gucci", 1250.00, 5),
    ("Essential Trench Coat", "Clothing", "Burberry", 990.00, 8),
    ("Rouge Dior Lipstick", "Cosmetics", "Dior", 45.00, 85),
    ("Advanced Night Repair Serum", "Cosmetics", "Estee Lauder", 78.00, 50),
    ("Shalimar Eau de Parfum", "Cosmetics", "Guerlain", 135.00, 25),
    ("Matte Velvet Skin Foundation", "Cosmetics", "Make Up For Ever", 42.00, 60),
    ("Hypnose Mascara", "Cosmetics", "Lancome", 32.00, 110),
    ("Black Opium Perfume", "Cosmetics", "Yves Saint Laurent", 125.00, 40),
]

ORIGINAL_ORDERS = [
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

# ------------------------------------------------------------
# EXPANDED CATALOG (name, category, brand, price, stock)
# ------------------------------------------------------------

NEW_CATEGORIES = [
    "Electronics",
    "Smartphones",
    "Laptops",
    "Accessories",
    "Sportswear",
    "Shoes",
    "Food",
    "Drinks",
    "Cleaning",
    "Personal Care",
    "Home",
    "Office",
    "Gaming",
]

NEW_BRANDS = [
    ("Apple", "USA"),
    ("Samsung", "South Korea"),
    ("Sony", "Japan"),
    ("Lenovo", "China"),
    ("HP", "USA"),
    ("Dell", "USA"),
    ("Xiaomi", "China"),
    ("LG", "South Korea"),
    ("Bosch", "Germany"),
    ("Philips", "Netherlands"),
    ("Whirlpool", "USA"),
    ("Nespresso", "Switzerland"),
    ("Nestle", "Switzerland"),
    ("Coca-Cola", "USA"),
    ("PepsiCo", "USA"),
    ("Danone", "France"),
    ("L'Oreal", "France"),
    ("Unilever", "UK"),
    ("Procter & Gamble", "USA"),
    ("Colgate-Palmolive", "USA"),
    ("Puma", "Germany"),
    ("New Balance", "USA"),
    ("ASUS", "Taiwan"),
    ("Logitech", "Switzerland"),
    ("JBL", "USA"),
    ("Braun", "Germany"),
    ("Reebok", "UK"),
    ("Under Armour", "USA"),
]

PRODUCT_CATALOG = [
    # Electronics
    ("Sony Bravia 43 Inch 4K Smart TV", "Electronics", "Sony", 549.00, 18),
    ("Samsung 65 Inch Neo QLED TV", "Electronics", "Samsung", 1099.00, 12),
    ("LG 50 Inch NanoCell TV", "Electronics", "LG", 649.00, 15),
    ("LG CordZero Vacuum Cleaner", "Electronics", "LG", 399.00, 25),
    ("Xiaomi Robot Vacuum S12", "Electronics", "Xiaomi", 289.00, 33),
    ("Philips Air Fryer XL 6.5L", "Electronics", "Philips", 149.00, 60),
    ("JBL Flip 6 Bluetooth Speaker", "Electronics", "JBL", 129.00, 80),
    ("JBL Charge 5 Speaker", "Electronics", "JBL", 179.00, 55),
    ("Sony WH-1000XM5 Headphones", "Electronics", "Sony", 349.00, 40),
    ("Samsung Galaxy Buds2 Pro", "Electronics", "Samsung", 199.00, 70),
    ("Samsung HW-Q600C Soundbar", "Electronics", "Samsung", 449.00, 22),
    ("Philips Titanium Steam Iron", "Electronics", "Philips", 69.00, 48),
    ("Bosch Hand Blender MFQ4030", "Electronics", "Bosch", 39.00, 64),
    ("Sony XB100 Portable Speaker", "Electronics", "Sony", 59.00, 90),
    # Smartphones
    ("Apple iPhone 15 128GB", "Smartphones", "Apple", 949.00, 40),
    ("Apple iPhone 15 Pro 256GB", "Smartphones", "Apple", 1249.00, 25),
    ("Apple iPhone SE 64GB", "Smartphones", "Apple", 549.00, 30),
    ("Samsung Galaxy S24 Ultra", "Smartphones", "Samsung", 1199.00, 35),
    ("Samsung Galaxy A55", "Smartphones", "Samsung", 449.00, 55),
    ("Samsung Galaxy Z Flip6", "Smartphones", "Samsung", 1099.00, 12),
    ("Xiaomi 14 Pro", "Smartphones", "Xiaomi", 899.00, 28),
    ("Xiaomi Redmi Note 13 Pro", "Smartphones", "Xiaomi", 349.00, 70),
    # Laptops
    ("Apple MacBook Air 13 M3", "Laptops", "Apple", 1099.00, 30),
    ("Apple MacBook Pro 14", "Laptops", "Apple", 1999.00, 15),
    ("Dell XPS 13", "Laptops", "Dell", 1249.00, 20),
    ("Dell Inspiron 15", "Laptops", "Dell", 649.00, 42),
    ("HP Spectre x360", "Laptops", "HP", 1299.00, 18),
    ("HP Pavilion 15", "Laptops", "HP", 699.00, 38),
    ("Lenovo ThinkPad X1 Carbon", "Laptops", "Lenovo", 1549.00, 22),
    ("Lenovo IdeaPad Slim 5", "Laptops", "Lenovo", 579.00, 45),
    ("ASUS Zenbook 14", "Laptops", "ASUS", 749.00, 26),
    ("ASUS ROG Strix G16", "Laptops", "ASUS", 1499.00, 14),
    # Accessories
    ("Apple AirPods Pro 2", "Accessories", "Apple", 249.00, 85),
    ("Apple MagSafe Charger", "Accessories", "Apple", 39.00, 120),
    ("Apple USB-C Fast Charge Cable", "Accessories", "Apple", 19.00, 200),
    ("Samsung 45W Fast Charger", "Accessories", "Samsung", 29.00, 160),
    ("Xiaomi 20000mAh Power Bank", "Accessories", "Xiaomi", 45.00, 90),
    ("Logitech MX Master 3S Mouse", "Accessories", "Logitech", 99.00, 60),
    ("Logitech K380 Keyboard", "Accessories", "Logitech", 40.00, 75),
    ("Logitech C920 HD Webcam", "Accessories", "Logitech", 79.00, 50),
    ("Sony Extra Bass Earbuds", "Accessories", "Sony", 59.00, 95),
    ("JBL Tune 510BT Headphones", "Accessories", "JBL", 39.00, 110),
    ("HP 24 Inch Desktop Monitor", "Accessories", "HP", 119.00, 35),
    ("LG 27 Inch IPS Monitor", "Accessories", "LG", 229.00, 28),
    ("Dell Wireless Keyboard KM3320W", "Accessories", "Dell", 35.00, 0),
    ("Samsung EVO Plus 128GB SD Card", "Accessories", "Samsung", 18.00, 140),
    ("JBL Go 3 Speaker", "Accessories", "JBL", 29.00, 105),
    ("Xiaomi Mi Band 8", "Accessories", "Xiaomi", 39.00, 65),
    # Sportswear
    ("Nike Dri-FIT Training Tee", "Sportswear", "Nike", 28.00, 150),
    ("Nike Club Hoodie", "Sportswear", "Nike", 55.00, 90),
    ("Nike Pro Leggings", "Sportswear", "Nike", 45.00, 80),
    ("Nike Dri-FIT Running Shorts", "Sportswear", "Nike", 25.00, 140),
    ("Adidas Own The Run Jacket", "Sportswear", "Adidas", 60.00, 58),
    ("Adidas Essentials 3-Stripes Tee", "Sportswear", "Adidas", 30.00, 170),
    ("Adidas Tech Fleece Hoodie", "Sportswear", "Adidas", 85.00, 45),
    ("Puma Trainer T-Shirt", "Sportswear", "Puma", 22.00, 130),
    ("Puma Terry Sweatpants", "Sportswear", "Puma", 40.00, 75),
    ("New Balance Short-Sleeve Tee", "Sportswear", "New Balance", 26.00, 120),
    ("New Balance Joggers Graphite", "Sportswear", "New Balance", 48.00, 60),
    ("Under Armour HeatGear Shorts", "Sportswear", "Under Armour", 28.00, 100),
    ("Under Armour Tech Tee", "Sportswear", "Under Armour", 30.00, 85),
    ("Reebok Workout CrossFit Tee", "Sportswear", "Reebok", 24.00, 0),
    ("Reebok Fleece Zip Hoodie", "Sportswear", "Reebok", 52.00, 55),
    # Shoes
    ("Nike Air Max 270", "Shoes", "Nike", 149.99, 42),
    ("Nike Revolution 6 Running", "Shoes", "Nike", 64.99, 95),
    ("Nike Pegasus 41", "Shoes", "Nike", 119.99, 38),
    ("Adidas Ultraboost Light", "Shoes", "Adidas", 179.99, 30),
    ("Adidas Stan Smith", "Shoes", "Adidas", 89.99, 60),
    ("Adidas Samba OG", "Shoes", "Adidas", 99.99, 26),
    ("Puma Caven 2.0", "Shoes", "Puma", 69.99, 50),
    ("Puma RS-X", "Shoes", "Puma", 89.99, 33),
    ("New Balance 550", "Shoes", "New Balance", 109.99, 25),
    ("New Balance Fresh Foam 1080", "Shoes", "New Balance", 149.99, 22),
    ("Reebok Classic Leather", "Shoes", "Reebok", 79.99, 8),
    ("Reebok Nano X4", "Shoes", "Reebok", 129.99, 16),
    # Food
    ("Nestle Milk Chocolate Bar 100g", "Food", "Nestle", 2.50, 500),
    ("Nestle KitKat 4-Finger Bar", "Food", "Nestle", 1.80, 600),
    ("Nestle Smarties Tube", "Food", "Nestle", 2.20, 0),
    ("Nestle Milo Cereal 375g", "Food", "Nestle", 6.50, 90),
    ("Nestle Maggi Instant Noodles", "Food", "Nestle", 0.90, 0),
    ("Nestle Fitness Cereal 375g", "Food", "Nestle", 4.20, 95),
    ("Danone Oat Yogurt 4-Pack", "Food", "Danone", 3.40, 120),
    ("Danone Greek Yogurt 500g", "Food", "Danone", 2.90, 140),
    ("Danone Activia Strawberry 8-Pack", "Food", "Danone", 4.80, 80),
    ("Unilever Knorr Stock Cubes 60g", "Food", "Unilever", 1.90, 300),
    ("Unilever Lipton Green Tea 20 Bags", "Food", "Unilever", 3.60, 150),
    ("Unilever Hellmann's Mayonnaise 430g", "Food", "Unilever", 3.20, 110),
    ("Unilever Ben & Jerry's Cookie Dough 465ml", "Food", "Unilever", 6.40, 70),
    # Drinks
    ("Coca-Cola Classic 1.5L", "Drinks", "Coca-Cola", 2.00, 400),
    ("Coca-Cola Zero 6-Pack 330ml", "Drinks", "Coca-Cola", 4.50, 300),
    ("Fanta Orange 1.5L", "Drinks", "Coca-Cola", 1.90, 250),
    ("Sprite 1.5L", "Drinks", "Coca-Cola", 1.90, 230),
    ("Pepsi Cola 1.5L", "Drinks", "PepsiCo", 1.80, 260),
    ("7UP Lemonade 1.5L", "Drinks", "PepsiCo", 1.90, 180),
    ("Lipton Ice Tea Peach 1.5L", "Drinks", "PepsiCo", 2.10, 5),
    ("Pepsi Max 1.5L", "Drinks", "PepsiCo", 1.90, 0),
    ("Nestle Nescafe Classic 200g", "Drinks", "Nestle", 5.90, 140),
    ("Nestle Nescafe Gold 100g", "Drinks", "Nestle", 8.90, 60),
    ("Danone Volvic Water 6x1.5L", "Drinks", "Danone", 3.90, 90),
    # Cleaning
    ("Unilever Domestos Bleach 750ml", "Cleaning", "Unilever", 2.40, 220),
    ("Unilever Cif Cream Cleaner 500ml", "Cleaning", "Unilever", 2.10, 200),
    ("Unilever Cif Power Spray 750ml", "Cleaning", "Unilever", 3.20, 150),
    ("Unilever Cif Bathroom Spray", "Cleaning", "Unilever", 2.90, 120),
    ("Unilever Surf Washing Powder 3kg", "Cleaning", "Unilever", 8.40, 75),
    ("Procter & Gamble Tide Liquid Detergent 1.5L", "Cleaning", "Procter & Gamble", 7.90, 130),
    ("Procter & Gamble Ariel Pods 40-Count", "Cleaning", "Procter & Gamble", 9.90, 110),
    ("Procter & Gamble Febreze Air Freshener", "Cleaning", "Procter & Gamble", 4.20, 90),
    ("Procter & Gamble Mr. Clean All-Purpose 1L", "Cleaning", "Procter & Gamble", 3.40, 140),
    ("Procter & Gamble Fairy Dish Soap 450ml", "Cleaning", "Procter & Gamble", 2.60, 240),
    ("Colgate-Palmolive Ajax Floor Cleaner 1L", "Cleaning", "Colgate-Palmolive", 2.80, 170),
    ("Colgate-Palmolive Axion Dish Liquid 500ml", "Cleaning", "Colgate-Palmolive", 1.80, 0),
    # Personal Care
    ("L'Oreal Paris Elseve Shampoo 400ml", "Personal Care", "L'Oreal", 5.20, 160),
    ("L'Oreal Paris Elseve Conditioner 400ml", "Personal Care", "L'Oreal", 5.50, 130),
    ("L'Oreal Men Expert Face Wash 150ml", "Personal Care", "L'Oreal", 4.80, 110),
    ("L'Oreal Paris Total Repair 5 Mask", "Personal Care", "L'Oreal", 6.90, 85),
    ("Gillette Mach3 Razor", "Personal Care", "Procter & Gamble", 12.90, 95),
    ("Gillette Fusion5 Razor", "Personal Care", "Procter & Gamble", 15.50, 80),
    ("Oral-B Pro-Expert Toothbrush 2-Pack", "Personal Care", "Procter & Gamble", 6.20, 190),
    ("Old Spice Deodorant Original", "Personal Care", "Procter & Gamble", 4.90, 140),
    ("Unilever Dove Body Wash 750ml", "Personal Care", "Unilever", 4.20, 150),
    ("Unilever Dove Bar Soap 4-Pack", "Personal Care", "Unilever", 3.40, 0),
    ("Unilever Axe Body Spray 150ml", "Personal Care", "Unilever", 5.90, 100),
    ("Colgate Total Original Toothpaste", "Personal Care", "Colgate-Palmolive", 3.90, 220),
    ("Colgate Sensitive Toothpaste 100ml", "Personal Care", "Colgate-Palmolive", 4.40, 130),
    ("Philips Sonicare ProtectiveClean", "Personal Care", "Philips", 89.00, 35),
    ("Braun Series 7 Shaver", "Personal Care", "Braun", 199.00, 12),
    # Home
    ("Philips Electric Kettle 1.7L", "Home", "Philips", 29.00, 85),
    ("Philips Platinum Mixer Grinder", "Home", "Philips", 79.00, 40),
    ("Bosch Series 4 Washing Machine", "Home", "Bosch", 649.00, 14),
    ("Bosch Dishwasher Series 2", "Home", "Bosch", 599.00, 10),
    ("Bosch Coffee Machine Tassimo", "Home", "Bosch", 89.00, 36),
    ("LG Refrigerator Top Freezer 350L", "Home", "LG", 899.00, 9),
    ("Samsung Microwave Oven 28L", "Home", "Samsung", 249.00, 22),
    ("Samsung Side-by-Side Refrigerator 500L", "Home", "Samsung", 1199.00, 7),
    ("Whirlpool Microwave 25L", "Home", "Whirlpool", 179.00, 18),
    ("Whirlpool Steam Iron", "Home", "Whirlpool", 45.00, 60),
    ("Nespresso Vertuo Next", "Home", "Nespresso", 129.00, 30),
    ("Nespresso Essenza Mini", "Home", "Nespresso", 179.00, 20),
    ("Xiaomi Smart Air Fryer 5.5L", "Home", "Xiaomi", 89.00, 45),
    ("Xiaomi Smart Scale 2", "Home", "Xiaomi", 29.00, 70),
    # Office
    ("HP DeskJet Ink Advantage 4155", "Office", "HP", 89.00, 32),
    ("HP LaserJet M111w", "Office", "HP", 139.00, 15),
    ("HP Color LaserJet 100A", "Office", "HP", 189.00, 8),
    ("Lenovo USB-C Docking Station", "Office", "Lenovo", 169.00, 12),
    ("Logitech Zone Wireless Headset", "Office", "Logitech", 119.00, 22),
    ("Logitech Mechanical Keyboard K845", "Office", "Logitech", 79.00, 28),
    ("Philips Automatic Paper Shredder", "Office", "Philips", 64.00, 0),
    ("Dell 27 Inch 4K UltraSharp Monitor", "Office", "Dell", 449.00, 10),
    # Gaming
    ("Sony PlayStation 5 Slim", "Gaming", "Sony", 499.00, 20),
    ("Sony DualSense Wireless Controller", "Gaming", "Sony", 69.00, 55),
    ("Sony DualSense Edge Controller", "Gaming", "Sony", 199.00, 10),
    ("Sony Pulse 3D Wireless Headset", "Gaming", "Sony", 99.00, 30),
    ("Samsung Odyssey G5 32 Inch Curved Monitor", "Gaming", "Samsung", 329.00, 18),
    ("LG UltraGear 27 Inch 144Hz", "Gaming", "LG", 299.00, 25),
    ("ASUS TUF Gaming 24 Inch 165Hz", "Gaming", "ASUS", 219.00, 23),
    ("ASUS ROG Strix Mechanical Keyboard", "Gaming", "ASUS", 139.00, 16),
    ("Logitech G502 X Gaming Mouse", "Gaming", "Logitech", 79.00, 45),
    ("Logitech G435 Wireless Headset", "Gaming", "Logitech", 59.00, 50),
    ("HP OMEN 27 Inch Gaming Monitor", "Gaming", "HP", 349.00, 0),
]

# ------------------------------------------------------------
# FICTIONAL CUSTOMER POOL
# ------------------------------------------------------------

# Order volume tiers: frequent customers carry more weight than
# regular ones, so repeat shopping feels natural.
FREQUENT_CUSTOMERS = [
    "Sara Haddad", "Yacine Mansouri", "Lina Amara", "Mehdi Rahmani",
    "Rania Gharbi", "Amira Saidi", "Ines Belkacem", "Meriem Ben Salah",
    "Chloe Laurent", "Olivia Smith", "Yuki Tanaka", "Arjun Sharma",
    "Priya Patel", "Liam Johnson", "Lena Fischer", "Lucas Garcia",
    "Amina Cherif", "Zineb Alaoui",
]

REGULAR_CUSTOMERS = [
    "Alice Martin", "Bruno Chen", "Clara Dubois", "David Kim",
    "Emma Rossi", "Fatima El Amrani", "Gabriel Lopez", "Hana Suzuki",
    "Ivan Petrov", "Julie Moreau", "Karim Benali", "Houda Meziane",
    "Salim Khelifi", "Youssef Tabet", "Walid Boucherit", "Samir Ziani",
    "Sonia Mahfoud", "Khalid Ameziane", "Insaf Berrahal", "Aymen Saoud",
    "Karim Bensaid", "Rim Aouiche", "Nassim Belkheir", "Dalila Boussaid",
    "Tarek Hamdani", "Lynda Kaci", "Fares Bouguerra", "Wissam Taleb",
    "Zahia Merbah", "Yasmine Ferhat", "Mourad Sellami", "Amel Chikhi",
    "Hakim Laribi", "Bilal Hattab", "Nesrine Boumediene", "Sabrina Louaifi",
    "Idir Benhamou", "Anissa Rahal", "Kamel Dridi", "Nawel Zerdoumi",
    "Farida Boukhalfa", "Soumeya Belaid", "Redha Messaoud", "Djamila Kaddour",
    "Samia Filali", "Aziz Belhadi", "Toufik Rahmouni", "Leila Bouzid",
    "Khadija Amrani", "Hamid Benjelloun", "Fouad Tazi", "Salma El Idrissi",
    "Driss Bennani", "Najat Berrada", "Omar El Khattabi", "Latifa Ghazi",
    "Abderrahim Chahbi", "Mouna Sbai", "Rachida Benali", "Ghita Lahlou",
    "Yassir Bennaye", "Sana Bouziane", "Hamza Lachhab", "Imane Kabbaj",
    "Karim El Messaoudi", "Rita Fernandes", "Mateo Rossi", "Sofia Kowalczyk",
    "Lukas Weber", "Emma Fischer", "Noah Dubois", "Ethan Wright",
    "Ava Brown", "Mia Davis", "Noah Williams", "Daniel Rodrigues",
    "Sofia Pereira", "Marco Bianchi", "Giulia Romano", "Filip Novak",
    "Olena Kovalenko", "Dmitri Volkov", "Anastasia Petrova", "Tomasz Nowak",
    "Agnieszka Mazur", "Jan Kowalski", "Petra Svobodova", "Milan Horvat",
    "Ivana Jovanovic", "Marko Petrovic", "Jana Kovarova", "Ondrej Novotny",
    "Haruto Sato", "Min-jun Park", "Seo-yeon Choi", "Jun-seo Lee",
    "Wei Zhang", "Mei Li", "Rahul Gupta", "Ananya Singh",
]

# Customers that only ever order during the early part of the year,
# so "customers with no recent purchases" is a truthful answer.
EARLY_ONLY_CUSTOMERS = [
    "Ahmed Benali", "Nadia Cherif", "Sofiane Guerroudj", "Riad Nacer",
    "Selma Othmani", "Malik Djouadi", "Rachid Hammadi", "Adel Ghomri",
    "Hocine Mebarki", "Mustapha El Fassi",
]

# Products that must never appear in any order.
NEVER_SOLD = {
    "Dell Wireless Keyboard KM3320W",
    "Reebok Workout CrossFit Tee",
    "Nestle Smarties Tube",
    "Colgate-Palmolive Axion Dish Liquid 500ml",
    "Unilever Dove Bar Soap 4-Pack",
    "Philips Automatic Paper Shredder",
}

# Product substrings that get above-average order volume, spread
# across categories so top-sellers are not all from one shelf.
BEST_SELLER_BOOSTS = [
    "Nescafe Classic", "Chocolate Bar 100g", "KitKat", "Air Max 90",
    "Air Max 270", "Ultraboost", "AirPods Pro 2", "Pegasus 41",
    "Basic Cotton T-Shirt", "Hypnose Mascara", "Galaxy A55",
    "Coca-Cola Classic 1.5L", "Fanta Orange", "MacBook Air",
    "iPhone 15 128GB", "Dri-FIT Training Tee", "PlayStation 5 Slim",
    "Activia", "Logitech G502", "Oat Yogurt",
]

MONTH_WEIGHTS = [
    ("2026-01", 1.00),
    ("2026-02", 1.35),
    ("2026-03", 1.00),
    ("2026-04", 0.85),
    ("2026-05", 1.15),
    ("2026-06", 1.00),
    ("2026-07", 0.70),
    ("2026-08", 1.40),
    ("2026-09", 1.30),
]

QUANTITY_WEIGHTS = [(1, 0.34), (2, 0.24), (3, 0.18), (4, 0.10), (5, 0.07), (6, 0.07)]

TARGET_ORDERS = 1235  # new orders in addition to the original 12
EARLY_ORDERS = 45     # of those, forced into 2026-01..2026-03


def build(path):
    conn = sqlite3.connect(path)
    conn.execute("PRAGMA foreign_keys = ON")
    cur = conn.cursor()

    cur.execute("DROP TABLE IF EXISTS orders")
    cur.execute("DROP TABLE IF EXISTS products")
    cur.execute("DROP TABLE IF EXISTS brands")
    cur.execute("DROP TABLE IF EXISTS categories")

    cur.execute(
        """
        CREATE TABLE categories (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL UNIQUE
        )
        """
    )
    cur.execute(
        """
        CREATE TABLE brands (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL UNIQUE,
            country TEXT NOT NULL
        )
        """
    )
    cur.execute(
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
    cur.execute(
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

    # Categories: originals first, then the expanded set.
    category_ids = {}
    for name in ORIGINAL_CATEGORIES + NEW_CATEGORIES:
        cur.execute("INSERT INTO categories (name) VALUES (?)", (name,))
        category_ids[name] = cur.lastrowid

    # Brands: originals first, then the expanded set.
    brand_ids = {}
    for name, country in ORIGINAL_BRANDS + NEW_BRANDS:
        cur.execute(
            "INSERT INTO brands (name, country) VALUES (?, ?)",
            (name, country),
        )
        brand_ids[name] = cur.lastrowid

    # Products: originals first, then the expanded catalog.
    product_rows = ORIGINAL_PRODUCTS + PRODUCT_CATALOG
    product_id_by_name = {}
    product_stock = {}
    for name, category, brand, price, stock in product_rows:
        cur.execute(
            """
            INSERT INTO products (name, category_id, brand_id, price, stock)
            VALUES (?, ?, ?, ?, ?)
            """,
            (name, category_ids[category], brand_ids[brand], price, stock),
        )
        product_id_by_name[name] = cur.lastrowid
        product_stock[name] = stock

    # Weighted product pool (order volume bias).
    rng = random.Random(4242)
    product_weight = []
    for name in product_id_by_name:
        if name in NEVER_SOLD:
            product_weight.append(0.0)
            continue
        weight = 1.0
        for boost in BEST_SELLER_BOOSTS:
            if boost in name:
                weight *= 3.0
        product_weight.append(weight)

    product_pool = list(product_id_by_name)
    total_weight = sum(product_weight)

    def pick_product():
        r = rng.random() * total_weight
        acc = 0.0
        for name, weight in zip(product_pool, product_weight):
            acc += weight
            if r <= acc:
                return name
        return product_pool[-1]

    # Quantity distribution.
    quantities = [q for q, _ in QUANTITY_WEIGHTS]
    q_probs = [w for _, w in QUANTITY_WEIGHTS]

    # Weighted month draw.
    months = [m for m, _ in MONTH_WEIGHTS]
    month_probs = [w for _, w in MONTH_WEIGHTS]

    # Customer pool for the main (spread) orders.
    main_pool = FREQUENT_CUSTOMERS + REGULAR_CUSTOMERS
    freq = set(FREQUENT_CUSTOMERS)
    main_customer_weight = [4.0 if name in freq else 1.0 for name in main_pool]
    main_customer_probs = [w / sum(main_customer_weight) for w in main_customer_weight]

    new_orders = []  # (product_name, customer, quantity, date)

    for _ in range(EARLY_ORDERS):
        month = rng.choice(["2026-01", "2026-02", "2026-03"])
        day = rng.randint(1, 28)
        new_orders.append(
            (pick_product(), rng.choice(EARLY_ONLY_CUSTOMERS),
             rng.choices(quantities, weights=q_probs, k=1)[0],
             "%s-%02d" % (month, day))
        )

    for _ in range(TARGET_ORDERS - EARLY_ORDERS):
        month = rng.choices(months, weights=month_probs, k=1)[0]
        day = rng.randint(1, 28)
        new_orders.append(
            (pick_product(), rng.choices(main_pool, weights=main_customer_probs, k=1)[0],
             rng.choices(quantities, weights=q_probs, k=1)[0],
             "%s-%02d" % (month, day))
        )

    # Original orders first (their product ids match the original file),
    # then the generated orders resolved by product name.
    for product_id, customer, quantity, date in ORIGINAL_ORDERS:
        cur.execute(
            "INSERT INTO orders (product_id, customer_name, quantity, order_date) VALUES (?, ?, ?, ?)",
            (product_id, customer, quantity, date),
        )

    for name, customer, quantity, date in new_orders:
        cur.execute(
            "INSERT INTO orders (product_id, customer_name, quantity, order_date) VALUES (?, ?, ?, ?)",
            (product_id_by_name[name], customer, quantity, date),
        )

    conn.commit()
    conn.close()

    return product_rows, new_orders


def summarize(path):
    conn = sqlite3.connect(path)
    counts = {
        table: conn.execute("SELECT COUNT(*) FROM %s" % table).fetchone()[0]
        for table in ("categories", "brands", "products", "orders")
    }
    distinct_customers = conn.execute(
        "SELECT COUNT(DISTINCT customer_name) FROM orders"
    ).fetchone()[0]
    date_range = conn.execute(
        "SELECT MIN(order_date), MAX(order_date) FROM orders"
    ).fetchone()
    integrity = conn.execute("PRAGMA integrity_check").fetchone()[0]
    foreign_keys = conn.execute("PRAGMA foreign_key_check").fetchall()
    never_sold = conn.execute(
        """
        SELECT COUNT(*) FROM products p
        WHERE NOT EXISTS (SELECT 1 FROM orders o WHERE o.product_id = p.id)
        """
    ).fetchone()[0]
    total_value = conn.execute(
        "SELECT ROUND(SUM(o.quantity * p.price), 2) FROM orders o JOIN products p ON p.id = o.product_id"
    ).fetchone()[0]
    conn.close()

    print("Table counts: %s" % counts)
    print("Distinct customers: %s" % distinct_customers)
    print("Order date range: %s .. %s" % date_range)
    print("PRAGMA integrity_check: %s" % integrity)
    print("PRAGMA foreign_key_check rows: %s" % len(foreign_keys))
    print("Products never sold: %s" % never_sold)
    print("Total order value (computed): %.2f" % total_value)

    assert integrity == "ok"
    assert not foreign_keys, "foreign key violations: %r" % foreign_keys[:5]
    assert counts["categories"] >= 10 and counts["categories"] <= 20
    assert counts["brands"] >= 20 and counts["brands"] <= 50
    assert counts["products"] >= 100 and counts["products"] <= 300
    assert distinct_customers >= 50 and distinct_customers <= 150
    assert counts["orders"] >= 500 and counts["orders"] <= 1500
    return counts


if __name__ == "__main__":
    target = sys.argv[1] if len(sys.argv) > 1 else "store.db.demo"
    print("Building %s ..." % target)
    build(target)
    print("Validating %s ..." % target)
    summarize(target)
    print("Done.")