import os

# Tests never read the developer's .env (model provider, keys): they run on the defaults plus
# whatever the command line sets (DATABASE_URL), exactly like CI. Must run before brigada.config
# is imported, which decides the env file at class definition.
os.environ["BRIGADA_NO_DOTENV"] = "1"
# An exported key must not make search tests call the real embedding API: without it the
# server falls back to keyword search, and the tests that need vectors pass their own embedder.
os.environ.pop("OPENROUTER_API_KEY", None)
