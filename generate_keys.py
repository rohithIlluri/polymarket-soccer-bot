"""
generate_keys.py — Run ONCE to derive Polymarket L2 API credentials.
Output: copy the printed lines into your .env file.
"""
import os
from dotenv import load_dotenv
from py_clob_client.client import ClobClient

load_dotenv()

client = ClobClient(
    "https://clob.polymarket.com",
    key=os.getenv("POLYGON_PRIVATE_KEY"),
    chain_id=137,
)

creds = client.create_or_derive_api_creds()

print("\nAdd these to your .env:\n")
print(f"POLY_API_KEY={creds.api_key}")
print(f"POLY_SECRET={creds.api_secret}")
print(f"POLY_PASSPHRASE={creds.api_passphrase}")
