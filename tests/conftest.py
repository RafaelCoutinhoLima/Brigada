import os

# Tests never read the developer's .env (model provider, keys): they run on the defaults plus
# whatever the command line sets (DATABASE_URL), exactly like CI. Must run before brigada.config
# is imported, which decides the env file at class definition.
os.environ["BRIGADA_NO_DOTENV"] = "1"
