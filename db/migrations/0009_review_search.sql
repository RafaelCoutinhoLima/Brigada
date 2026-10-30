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
CREATE TABLE IF NOT EXISTS review_embeddings (
    review_id     bigint  NOT NULL REFERENCES reviews (id) ON DELETE CASCADE,
    restaurant_id integer NOT NULL,
    model         text    NOT NULL,
    embedding     vector  NOT NULL,
    PRIMARY KEY (review_id, model)
);
CREATE INDEX IF NOT EXISTS review_embeddings_lookup_idx
    ON review_embeddings (restaurant_id, model);

-- The placeholder column from 0001 had a guessed dimension and was never filled.
ALTER TABLE reviews DROP COLUMN IF EXISTS embedding;
