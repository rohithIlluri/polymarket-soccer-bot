"""
approve_contracts.py — Run ONCE before starting the bot.
Submits two on-chain approvals to Polygon (~0.01 POL gas).
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

print("Approving USDC collateral...")
client.approve_collateral()
print("Approving conditional tokens...")
client.approve_conditional()
print("Done. Wait ~30s for Polygon confirmation, then run generate_keys.py")
