CREATE EXTENSION IF NOT EXISTS vector;

CREATE TABLE restaurants (
    id integer PRIMARY KEY,
    name text NOT NULL,
    cuisine text NOT NULL,
    city text NOT NULL
);

CREATE TABLE menu_items (
    id integer PRIMARY KEY,
    restaurant_id integer NOT NULL REFERENCES restaurants (id),
    name text NOT NULL,
    category text NOT NULL,
    price numeric(10, 2) NOT NULL CHECK (price > 0),
    active boolean NOT NULL DEFAULT true
);

CREATE TABLE orders (
    id bigint PRIMARY KEY,
    restaurant_id integer NOT NULL REFERENCES restaurants (id),
    created_at timestamptz NOT NULL,
    total numeric(10, 2) NOT NULL,
    channel text NOT NULL CHECK (channel IN ('app', 'telefone', 'balcao'))
);

CREATE TABLE order_items (
    order_id bigint REFERENCES orders (id) ON DELETE CASCADE,
    menu_item_id integer REFERENCES menu_items (id),
    qty integer NOT NULL CHECK (qty > 0),
    unit_price numeric(10, 2) NOT NULL,
    PRIMARY KEY (order_id, menu_item_id)
);

CREATE TABLE deliveries (
    order_id bigint PRIMARY KEY REFERENCES orders (id) ON DELETE CASCADE,
    promised_minutes integer NOT NULL,
    actual_minutes integer NOT NULL
);

CREATE TABLE reviews (
    id bigint PRIMARY KEY,
    restaurant_id integer NOT NULL REFERENCES restaurants (id),
    menu_item_id integer REFERENCES menu_items (id),
    rating smallint NOT NULL CHECK (rating BETWEEN 1 AND 5),
    text text NOT NULL,
    created_at timestamptz NOT NULL,
    -- Embedding dimension will be revisited when hybrid RAG is implemented.
    embedding vector(1024)
);

CREATE INDEX orders_restaurant_created_idx ON orders (restaurant_id, created_at);
CREATE INDEX reviews_restaurant_created_idx ON reviews (restaurant_id, created_at);
CREATE INDEX reviews_menu_item_idx ON reviews (menu_item_id);
CREATE INDEX order_items_menu_item_idx ON order_items (menu_item_id);
