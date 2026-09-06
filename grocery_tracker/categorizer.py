"""
categorizer.py — Keyword-based grocery product categorizer.

Categories:
    meat        — beef, pork, chicken, turkey, lamb, seafood, deli meats
    seafood     — fish, shrimp, crab, lobster, scallops, oysters
    dairy       — milk, cheese, yogurt, butter, cream, eggs
    produce     — fruits, vegetables, fresh herbs
    deli        — prepared meats, sliced meats, hot bar, sushi
    bakery      — bread, rolls, pastries, cakes, pies
    frozen      — anything frozen
    pantry      — canned goods, dry goods, oils, condiments, pasta, rice
    snacks      — chips, crackers, nuts, candy, cookies, popcorn
    beverages   — juice, soda, water, coffee, tea, sports drinks
    alcohol     — beer, wine, spirits, cider
    prepared    — ready meals, rotisserie, deli prepared foods
    health      — vitamins, supplements, protein powder
    other       — anything that doesn't match

Rules:
  - Each product gets exactly one category
  - More specific categories take priority over broader ones
    (e.g. "frozen chicken" → frozen, not meat)
  - Categories are checked in order — first match wins
  - All matching is case-insensitive substring match on canonical_name
"""

import logging
import re
from typing import Optional

from grocery_tracker.db import db_cursor

logger = logging.getLogger(__name__)

# ── Category keyword definitions ──────────────────────────────────────────────
# Order matters — checked top to bottom, first match wins.
# Put more specific categories before broader ones.

CATEGORIES = [

    # ── Alcohol (before beverages) ────────────────────────────────────────────
    ("alcohol", [
        "beer", "lager", "ale", "ipa", "stout", "pilsner", "hard seltzer",
        "white claw", "truly ", "bud light", "budweiser", "coors", "miller lite",
        "corona", "heineken", "stella artois", "modelo", "dos equis",
        "wine", "chardonnay", "cabernet", "merlot", "pinot", "sauvignon",
        "riesling", "prosecco", "champagne", "rosé", "rose wine",
        "vodka", "whiskey", "whisky", "bourbon", "scotch", "gin ", "rum ",
        "tequila", "brandy", "cognac", "liqueur", "vermouth",
        "hard cider", "spiked", "malt beverage",
        "six pack", "12 pack", "24 pack", "case of beer",
    ]),

    # ── Frozen (before meat/produce so "frozen chicken" → frozen) ─────────────
    ("frozen", [
        "frozen", "ice cream", "gelato", "sorbet", "sherbet",
        "popsicle", "ice pop", "frozen yogurt", "fro-yo",
        "frozen pizza", "frozen meal", "frozen entree", "frozen dinner",
        "frozen vegetable", "frozen fruit", "frozen berry",
        "frozen shrimp", "frozen fish", "frozen chicken",
        "tater tot", "hash brown", "frozen waffle", "frozen pancake",
        "edamame", "pot pie", "frozen burrito", "hot pocket",
    ]),

    # ── Seafood (before meat) ─────────────────────────────────────────────────
    ("seafood", [
        "shrimp", "prawn", "lobster", "crab", "scallop", "oyster", "clam",
        "mussel", "squid", "octopus", "calamari",
        "salmon", "tuna", "tilapia", "cod ", "halibut", "flounder",
        "mahi", "grouper", "snapper", "catfish", "trout", "bass ",
        "swordfish", "sea bass", "anchovy", "sardine", "herring",
        "fish fillet", "fish steak", "seafood", "crab cake",
        "fish taco", "fish sandwich",
    ]),

    # ── Deli (before meat — sliced/prepared meats) ────────────────────────────
    ("deli", [
        "deli", "sliced turkey", "sliced ham", "sliced chicken",
        "sliced beef", "lunch meat", "lunchmeat", "cold cut",
        "bologna", "salami", "pepperoni slice", "prosciutto",
        "mortadella", "pastrami", "corned beef", "roast beef slice",
        "liverwurst", "head cheese",
        "rotisserie chicken", "rotisserie",
        "prepared meal", "ready meal", "heat and eat",
        "sushi", "poke bowl",
    ]),

    # ── Meat ──────────────────────────────────────────────────────────────────
    ("meat", [
        # Beef
        "ground beef", "beef chuck", "chuck roast", "beef roast",
        "sirloin", "ribeye", "rib eye", "t-bone", "porterhouse",
        "beef tenderloin", "filet mignon", "flank steak", "skirt steak",
        "strip steak", "new york strip", "brisket", "beef short rib",
        "beef stew", "beef kabob", "beef patty", "burger patty",
        "steak", "beef ",
        # Pork
        "pork chop", "pork loin", "pork tenderloin", "pork shoulder",
        "pork belly", "pork rib", "baby back rib", "spare rib",
        "ham steak", "ham hock", "pulled pork", "pork roast",
        "ground pork", "pork burger", "pork ",
        "bacon", "sausage", "bratwurst", "brat ", "kielbasa",
        "andouille", "chorizo", "italian sausage", "breakfast sausage",
        "hot dog", "frank ", "wiener",
        # Chicken & Poultry
        "chicken breast", "chicken thigh", "chicken leg", "chicken wing",
        "chicken drumstick", "chicken quarter", "chicken tender",
        "whole chicken", "chicken cutlet", "ground chicken",
        "chicken ", "turkey breast", "turkey thigh", "ground turkey",
        "whole turkey", "turkey ",
        "duck ", "lamb ", "veal ", "bison ", "venison",
        # Generic
        "ground meat", "meat ",
    ]),

    # ── Dairy & Eggs ──────────────────────────────────────────────────────────
    ("dairy", [
        "milk", "whole milk", "2% milk", "skim milk", "oat milk",
        "almond milk", "soy milk", "lactaid", "half and half",
        "heavy cream", "whipping cream", "sour cream", "cream cheese",
        "cottage cheese", "ricotta", "mascarpone",
        "cheddar", "mozzarella", "swiss cheese", "provolone",
        "parmesan", "parmigiano", "romano", "asiago", "brie", "gouda",
        "pepper jack", "colby", "american cheese", "string cheese",
        "shredded cheese", "sliced cheese", "cheese blend", "cheese ",
        "butter", "margarine", "ghee",
        "yogurt", "greek yogurt", "kefir",
        "egg ", "eggs", "egg white", "egg yolk",
        "whipped cream", "cool whip",
    ]),

    # ── Produce — Fruits ──────────────────────────────────────────────────────
    ("produce", [
        # Fruits
        "apple", "banana", "orange", "lemon", "lime", "grapefruit",
        "strawberry", "blueberry", "raspberry", "blackberry",
        "grape ", "watermelon", "cantaloupe", "honeydew",
        "peach", "plum", "nectarine", "apricot", "cherry",
        "mango", "pineapple", "papaya", "kiwi", "pomegranate",
        "avocado", "pear ", "fig ", "date ", "coconut",
        "clementine", "mandarin", "tangerine",
        # Vegetables
        "tomato", "lettuce", "spinach", "kale", "arugula", "cabbage",
        "broccoli", "cauliflower", "brussels sprout", "asparagus",
        "green bean", "snap pea", "snow pea", "pea pod",
        "bell pepper", "jalapeño", "jalapeno", "serrano", "habanero",
        "cucumber", "zucchini", "squash", "eggplant", "okra",
        "carrot", "celery", "onion", "shallot", "leek", "scallion",
        "green onion", "garlic", "ginger root",
        "potato", "sweet potato", "yam ", "turnip", "parsnip", "beet ",
        "corn ", "artichoke", "fennel", "bok choy", "swiss chard",
        "mushroom", "portobello", "shiitake",
        "herbs", "basil", "cilantro", "parsley", "dill ", "mint ",
        "rosemary", "thyme ", "sage ",
        "salad mix", "spring mix", "romaine", "iceberg",
        "fresh vegetable", "fresh fruit", "produce",
    ]),

    # ── Bakery ────────────────────────────────────────────────────────────────
    ("bakery", [
        "bread", "white bread", "wheat bread", "sourdough", "rye bread",
        "pumpernickel", "brioche", "ciabatta", "baguette", "focaccia",
        "pita bread", "naan", "tortilla", "wrap ",
        "bagel", "english muffin", "croissant", "pretzel roll",
        "dinner roll", "slider roll", "hamburger bun", "hot dog bun",
        "muffin", "scone", "biscuit",
        "cake", "cupcake", "donut", "doughnut", "danish",
        "pie ", "tart ", "eclair", "cannoli",
        "cookie", "brownie", "bar cake",
        "pastry", "bakery",
    ]),

    # ── Beverages (non-alcohol) ───────────────────────────────────────────────
    ("beverages", [
        "juice", "orange juice", "apple juice", "cranberry juice",
        "grape juice", "lemonade", "limeade", "fruit punch",
        "sparkling water", "seltzer", "club soda", "tonic water",
        "mineral water", "spring water", "water ",
        "soda", "cola", "pepsi", "coca-cola", "sprite", "7up",
        "dr pepper", "mountain dew", "ginger ale", "root beer",
        "energy drink", "red bull", "monster energy", "bang energy",
        "sports drink", "gatorade", "powerade", "propel",
        "coffee", "espresso", "cold brew", "k-cup", "ground coffee",
        "instant coffee", "coffee pod",
        "tea ", "green tea", "black tea", "herbal tea", "iced tea",
        "kombucha", "kefir drink",
        "smoothie", "protein shake", "meal replacement drink",
        "coconut water", "aloe drink",
        "hot chocolate", "cocoa mix",
    ]),

    # ── Snacks ────────────────────────────────────────────────────────────────
    ("snacks", [
        "chip", "potato chip", "tortilla chip", "corn chip",
        "popcorn", "pork rind", "rice cake", "puffed",
        "cracker", "graham cracker", "saltine", "ritz",
        "pretzel", "pita chip",
        "nut ", "nuts", "peanut", "almond", "cashew", "walnut",
        "pecan", "pistachio", "macadamia", "mixed nut", "trail mix",
        "granola bar", "protein bar", "energy bar", "kind bar",
        "fruit snack", "fruit roll", "fruit leather",
        "gummy", "gummies", "candy", "chocolate bar", "chocolate chip",
        "m&m", "skittles", "starburst", "twizzler", "licorice",
        "caramel", "toffee",
        "cookie", "oreo", "chips ahoy", "nutter butter",
        "popcorner", "veggie straw", "veggie chip",
        "jerky", "beef jerky", "meat stick", "chomps",
        "dip ", "salsa", "guacamole", "hummus", "queso",
        "snack mix", "chex mix", "party mix",
        "pudding", "gelatin", "jell-o",
    ]),

    # ── Pantry ────────────────────────────────────────────────────────────────
    ("pantry", [
        # Canned & jarred
        "canned", "can of", "tomato sauce", "tomato paste", "diced tomato",
        "crushed tomato", "tomato soup", "chicken soup", "beef broth",
        "chicken broth", "vegetable broth", "stock ", "bouillon",
        "canned bean", "black bean", "kidney bean", "chickpea",
        "canned corn", "canned pea", "canned tuna", "canned salmon",
        "canned sardine", "canned chicken",
        # Dry goods
        "pasta", "spaghetti", "penne", "fettuccine", "linguine",
        "rigatoni", "rotini", "farfalle", "orzo", "macaroni", "noodle",
        "rice ", "white rice", "brown rice", "jasmine rice", "basmati",
        "quinoa", "couscous", "farro", "barley", "lentil",
        "flour", "sugar", "brown sugar", "powdered sugar",
        "baking soda", "baking powder", "yeast", "cornstarch",
        "oat", "oatmeal", "granola",
        "cereal", "corn flake", "wheaties", "cheerio",
        # Oils & condiments
        "olive oil", "vegetable oil", "canola oil", "coconut oil",
        "avocado oil", "cooking spray", "shortening",
        "vinegar", "soy sauce", "worcestershire", "fish sauce",
        "hot sauce", "sriracha", "tabasco",
        "ketchup", "mustard", "mayonnaise", "relish", "pickles",
        "bbq sauce", "steak sauce", "teriyaki", "hoisin",
        "honey", "maple syrup", "agave", "corn syrup", "molasses",
        "jam", "jelly", "preserve", "peanut butter", "almond butter",
        "nutella", "tahini",
        "salt ", "pepper ", "spice", "seasoning", "herb mix",
        "garlic powder", "onion powder", "paprika", "cumin",
        "chili powder", "cayenne", "turmeric", "cinnamon",
        # Other pantry
        "coffee filter", "tea bag",
        "soup mix", "gravy mix", "sauce mix", "seasoning packet",
        "mac and cheese", "macaroni and cheese",
        "stuffing mix", "bread crumb", "panko",
        "tortilla chip", "taco shell",
    ]),

    # ── Prepared / Ready-to-eat ───────────────────────────────────────────────
    ("prepared", [
        "meal kit", "hello fresh", "home chef",
        "soup container", "prepared soup",
        "sandwich ", "wrap meal", "burrito",
        "pizza ", "flatbread pizza",
        "spring roll", "egg roll", "dumpling",
        "hummus tray", "veggie tray", "fruit tray", "cheese tray",
        "party platter",
    ]),

    # ── Health & wellness ─────────────────────────────────────────────────────
    ("health", [
        "vitamin", "supplement", "probiotic", "prebiotic",
        "protein powder", "whey protein", "plant protein",
        "collagen", "omega", "fish oil", "multivitamin",
        "electrolyte", "hydration mix",
        "diet ", "low calorie", "keto", "paleo",
        "organic formula", "infant formula", "baby food",
    ]),
]

# Flat lookup for fast category retrieval after classification
_CATEGORY_LOOKUP: dict[str, str] = {}


def classify(canonical_name: str) -> str:
    """
    Return the category for a product canonical name.
    Returns 'other' if no category matches.
    """
    name = canonical_name.lower()

    for category, keywords in CATEGORIES:
        for kw in keywords:
            if kw in name:
                return category

    return "other"


def classify_all_products(force: bool = False) -> int:
    """
    Classify all products in the DB that have no category (or all if force=True).
    Updates the products.category column in place.
    Returns the number of products updated.
    """
    with db_cursor() as cur:
        if force:
            cur.execute("SELECT id, canonical_name FROM products")
        else:
            cur.execute(
                "SELECT id, canonical_name FROM products "
                "WHERE category IS NULL OR category = '' OR category = 'uncategorized'"
            )
        rows = cur.fetchall()

    if not rows:
        logger.info("No products need categorization")
        return 0

    updates = []
    counts: dict[str, int] = {}

    for row in rows:
        cat = classify(row["canonical_name"])
        updates.append((cat, row["id"]))
        counts[cat] = counts.get(cat, 0) + 1

    with db_cursor() as cur:
        cur.executemany(
            "UPDATE products SET category = ? WHERE id = ?", updates
        )

    logger.info(
        "Categorized %d products: %s",
        len(updates),
        ", ".join(f"{k}={v}" for k, v in sorted(counts.items(), key=lambda x: -x[1]))
    )
    return len(updates)


def category_counts() -> dict[str, int]:
    """Return counts of products per category."""
    with db_cursor() as cur:
        cur.execute("""
            SELECT category, COUNT(*) as n
            FROM products
            GROUP BY category
            ORDER BY n DESC
        """)
        return {r["category"] or "uncategorized": r["n"] for r in cur.fetchall()}


# ── CLI ───────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    import sys
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s %(levelname)s — %(message)s")

    if len(sys.argv) > 1 and sys.argv[1] == "test":
        # Test a specific product name
        name = " ".join(sys.argv[2:])
        print(f"'{name}' → {classify(name)}")

    elif len(sys.argv) > 1 and sys.argv[1] == "audit":
        # Show all products and their assigned categories
        from grocery_tracker.db import db_cursor as _cur
        with _cur() as cur:
            cur.execute("SELECT canonical_name, category FROM products ORDER BY category, canonical_name")
            current_cat = None
            for r in cur.fetchall():
                if r["category"] != current_cat:
                    current_cat = r["category"]
                    print(f"\n── {current_cat or 'uncategorized'} ──")
                print(f"  {r['canonical_name']}")

    else:
        n = classify_all_products(force="--force" in sys.argv)
        print(f"Categorized {n} products")
        print()
        counts = category_counts()
        for cat, n in counts.items():
            print(f"  {cat:<15} {n:>4}")
