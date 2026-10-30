-- An approved purchase becomes an order; receiving it is what changes the stock.
-- Without this, approving left no trace and the same shortage was proposed again.

-- Receipts need stock_movements ids. The seed writes explicit small ids, so the sequence starts
-- far above them: no lock, no max(id) + 1, no collision with a reseed (RESTART IDENTITY goes
-- back to this START, not to 1).
CREATE SEQUENCE IF NOT EXISTS stock_movements_id_seq START 1000000000 OWNED BY stock_movements.id;
ALTER TABLE stock_movements ALTER COLUMN id SET DEFAULT nextval('stock_movements_id_seq');
CREATE TABLE IF NOT EXISTS purchase_orders (
    id               bigserial PRIMARY KEY,
    restaurant_id    integer NOT NULL,
    ingredient_id    integer NOT NULL,
    qty              numeric(12, 3) NOT NULL CHECK (qty > 0),
    status           text NOT NULL DEFAULT 'ordered'
                     CHECK (status IN ('ordered', 'received', 'cancelled')),
    source_action_id bigint UNIQUE REFERENCES pending_actions (id),
    ordered_at       timestamptz NOT NULL DEFAULT now(),
    received_at      timestamptz,
    received_by      text,
    unit_cost        numeric(10, 2) CHECK (unit_cost IS NULL OR unit_cost > 0),
    movement_id      bigint UNIQUE REFERENCES stock_movements (id),
    cancelled_at     timestamptz,
    cancelled_by     text,
    FOREIGN KEY (restaurant_id, ingredient_id) REFERENCES ingredients (restaurant_id, id),
    CHECK ((status = 'received') = (received_at IS NOT NULL AND unit_cost IS NOT NULL
                                    AND movement_id IS NOT NULL)),
    CHECK ((status = 'cancelled') = (cancelled_at IS NOT NULL))
);
CREATE INDEX IF NOT EXISTS purchase_orders_open_idx
    ON purchase_orders (restaurant_id, ingredient_id) WHERE status = 'ordered';
