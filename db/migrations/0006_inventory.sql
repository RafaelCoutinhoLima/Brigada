-- Inventory: ingredients, recipes (fichas técnicas), stock movements and physical counts.
-- Every child table carries restaurant_id in its foreign keys, so a recipe or a movement can
-- never point at another restaurant's ingredient or dish.

DO $$ BEGIN
    ALTER TABLE menu_items ADD CONSTRAINT menu_items_restaurant_item_key UNIQUE (restaurant_id, id);
EXCEPTION WHEN duplicate_object OR duplicate_table THEN NULL;
END $$;

CREATE TABLE IF NOT EXISTS ingredients (
    id integer PRIMARY KEY,
    restaurant_id integer NOT NULL REFERENCES restaurants,
    name text NOT NULL,
    unit text NOT NULL CHECK (unit IN ('kg', 'l', 'un')),
    -- Supplier lead time and safety stock, both in days of consumption.
    lead_time_days integer NOT NULL CHECK (lead_time_days >= 0),
    safety_days integer NOT NULL CHECK (safety_days >= 0),
    UNIQUE (restaurant_id, id),
    UNIQUE (restaurant_id, name)
);

CREATE TABLE IF NOT EXISTS recipes (
    restaurant_id integer NOT NULL,
    menu_item_id integer NOT NULL,
    ingredient_id integer NOT NULL,
    qty numeric(10, 3) NOT NULL CHECK (qty > 0),
    PRIMARY KEY (menu_item_id, ingredient_id),
    FOREIGN KEY (restaurant_id, menu_item_id) REFERENCES menu_items (restaurant_id, id),
    FOREIGN KEY (restaurant_id, ingredient_id) REFERENCES ingredients (restaurant_id, id)
);

CREATE TABLE IF NOT EXISTS stock_movements (
    id bigint PRIMARY KEY,
    restaurant_id integer NOT NULL,
    ingredient_id integer NOT NULL,
    -- purchase (+), sale (-, from recipes x items sold), waste (-, registered loss),
    -- count_adjust (+/-, aligns the books with a physical count: unexplained loss).
    kind text NOT NULL CHECK (kind IN ('purchase', 'sale', 'waste', 'count_adjust')),
    qty numeric(12, 3) NOT NULL,
    unit_cost numeric(10, 2) CHECK (unit_cost IS NULL OR unit_cost > 0),
    at timestamptz NOT NULL,
    FOREIGN KEY (restaurant_id, ingredient_id) REFERENCES ingredients (restaurant_id, id),
    CHECK ((kind = 'purchase') = (unit_cost IS NOT NULL)),
    CHECK ((kind = 'purchase' AND qty > 0) OR (kind IN ('sale', 'waste') AND qty < 0)
           OR kind = 'count_adjust')
);

CREATE TABLE IF NOT EXISTS stock_counts (
    id bigint PRIMARY KEY,
    restaurant_id integer NOT NULL,
    ingredient_id integer NOT NULL,
    counted_qty numeric(12, 3) NOT NULL CHECK (counted_qty >= 0),
    -- What the books said right before the count; counted - expected = unexplained loss.
    expected_qty numeric(12, 3) NOT NULL,
    at timestamptz NOT NULL,
    FOREIGN KEY (restaurant_id, ingredient_id) REFERENCES ingredients (restaurant_id, id)
);

CREATE INDEX IF NOT EXISTS stock_movements_lookup_idx
    ON stock_movements (restaurant_id, ingredient_id, at);
CREATE INDEX IF NOT EXISTS stock_counts_lookup_idx ON stock_counts (restaurant_id, at);

-- Purchase proposals go through the same human approval path as the other write tools.
ALTER TABLE pending_actions DROP CONSTRAINT IF EXISTS pending_actions_kind_check;
ALTER TABLE pending_actions ADD CONSTRAINT pending_actions_kind_check
    CHECK (kind IN ('pause_menu_item', 'create_promotion', 'propose_purchase'));
