-- Hybrid search over reviews: Portuguese full-text (keyword) + embeddings (vector).

CREATE EXTENSION IF NOT EXISTS unaccent;

-- unaccent() is STABLE (it depends on a dictionary); a generated column needs IMMUTABLE. This
-- wrapper pins the dictionary, which is what makes declaring it immutable honest.
CREATE OR REPLACE FUNCTION brigada_unaccent(text) RETURNS text
LANGUAGE sql IMMUTABLE PARALLEL SAFE STRICT
AS $$ SELECT public.unaccent('public.unaccent'::regdictionary, $1) $$;

ALTER TABLE reviews ADD COLUMN IF NOT EXISTS search tsvector
    GENERATED ALWAYS AS (to_tsvector('portuguese', brigada_unaccent(text))) STORED;
CREATE INDEX IF NOT EXISTS reviews_search_idx ON reviews USING gin (search);

-- Embeddings live outside reviews: seed --reset truncates reviews, and vectors from two
-- models must never be compared, so every row says which model produced it. No dimension on
-- the column (models differ); at a few hundred rows per restaurant an exact scan is enough.
-- The (review, restaurant) pair is a composite FK: a vector filed under another restaurant
-- would take a top-k slot there (and be dropped later), so the database refuses it.
DO $$ BEGIN
    ALTER TABLE reviews ADD CONSTRAINT reviews_id_restaurant_key UNIQUE (id, restaurant_id);
EXCEPTION WHEN duplicate_table OR duplicate_object THEN NULL;  -- already there: re-runnable
END $$;
CREATE TABLE IF NOT EXISTS review_embeddings (
    review_id     bigint  NOT NULL,
    restaurant_id integer NOT NULL,
    model         text    NOT NULL,
    embedding     vector  NOT NULL,
    PRIMARY KEY (review_id, model),
    FOREIGN KEY (review_id, restaurant_id) REFERENCES reviews (id, restaurant_id)
        ON DELETE CASCADE
);
CREATE INDEX IF NOT EXISTS review_embeddings_lookup_idx
    ON review_embeddings (restaurant_id, model);

-- The placeholder column from 0001 had a guessed dimension and was never filled.
ALTER TABLE reviews DROP COLUMN IF EXISTS embedding;
