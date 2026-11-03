-- Restaurants from the real datasets in the lakehouse (ADR 0009) live in Postgres only as a
-- registry row: runs, audit and API keys need the id, but their orders exist only in the
-- lakehouse. The Postgres tools refuse them instead of answering "zero sales".
ALTER TABLE restaurants ADD COLUMN IF NOT EXISTS data_source text NOT NULL DEFAULT 'brigada';
ALTER TABLE restaurants ADD COLUMN IF NOT EXISTS lakehouse_source text;

DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint WHERE conname = 'restaurants_data_source_check'
    ) THEN
        ALTER TABLE restaurants ADD CONSTRAINT restaurants_data_source_check CHECK (
            (data_source = 'brigada' AND lakehouse_source IS NULL)
            OR (data_source = 'lakehouse' AND lakehouse_source IS NOT NULL)
        );
    END IF;
END
$$;
