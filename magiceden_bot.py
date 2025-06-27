import os
import asyncio
import logging
from datetime import datetime, timezone

import discord
from discord.ext import commands, tasks
from discord import app_commands

# NEU: aiohttp für asynchrone HTTP-Anfragen
import aiohttp
from flask import Flask

import firebase_admin
from firebase_admin import credentials, firestore

# --- Setup ---
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
logger = logging.getLogger("discord-bot")

try:
    cred = credentials.Certificate("serviceAccountKey.json")
    firebase_admin.initialize_app(cred)
    logger.info("✅ Firebase initialized with serviceAccountKey.json (Local Mode).")
except FileNotFoundError:
    firebase_admin.initialize_app()
    logger.info("✅ Firebase initialized without serviceAccountKey.json (Cloud Run Mode).")

db = firestore.client()
TOKEN = os.getenv("DISCORD_TOKEN")
CHANNEL_ID = int(os.getenv("DISCORD_CHANNEL_ID"))
intents = discord.Intents.default()
bot = commands.Bot(command_prefix="!", intents=intents)
tree = bot.tree
app = Flask(__name__)

@app.route("/")
def index(): return "✅ Magic Eden Discord Bot is active!"

# --- Firestore Helpers (unverändert) ---
def get_collections_from_db():
    doc = db.collection("bot_config").document("collections").get()
    return doc.to_dict().get("symbols", []) if doc.exists else []

def get_last_seen_timestamp(symbol: str) -> int:
    doc = db.collection("bot_state").document(symbol).get()
    return doc.to_dict().get("last_seen_timestamp", int(datetime.now(timezone.utc).timestamp())) if doc.exists else int(datetime.now(timezone.utc).timestamp())

def set_last_seen_timestamp(symbol: str, timestamp: int):
    db.collection("bot_state").document(symbol).set({"last_seen_timestamp": timestamp})

# --- Asynchrone API Calls mit aiohttp ---
async def fetch_json(session, url):
    """Hilfsfunktion für GET-Anfragen mit aiohttp."""
    try:
        async with session.get(url, timeout=30) as response:
            response.raise_for_status()
            return await response.json()
    except asyncio.TimeoutError:
        logger.error(f"API Timeout für URL: {url}")
    except aiohttp.ClientError as e:
        logger.error(f"API Client Error für URL {url}: {e}")
    return None

async def get_activities(session, symbol: str):
    url = f"https://api-mainnet.magiceden.dev/v2/collections/{symbol}/activities?offset=0&limit=500"
    return await fetch_json(session, url)

async def get_token_metadata(session, mint: str):
    url = f"https://api-mainnet.magiceden.dev/v2/tokens/{mint}"
    return await fetch_json(session, url)

# --- Embed Generators (unverändert) ---
def create_embed(title, color, activity, name, image_url):
    embed = discord.Embed(
        title=f"{title}: {name}", color=color,
        timestamp=datetime.fromtimestamp(activity.get("blockTime"), tz=timezone.utc))
    link = f"https://magiceden.io/item-details/{activity.get('tokenMint', '')}"
    embed.add_field(name="Collection", value=activity.get("collection", "N/A"), inline=True)
    embed.add_field(name="Seller", value=f"`{activity.get('seller')}`", inline=True)
    embed.add_field(name="🔗 Link", value=f"[View on Magic Eden]({link})", inline=False)
    if image_url: embed.set_thumbnail(url=image_url)
    embed.set_footer(text="Magic Eden Bot")
    return embed

async def send_new_listing_embed(channel, activity, metadata):
    embed = create_embed("✅ New Listing", discord.Color.green(), activity, metadata.get("name", "Unknown Item"), metadata.get("image"))
    embed.add_field(name="💰 Price", value=f"**{activity.get('price')} SOL**", inline=False)
    await channel.send(embed=embed)

async def send_price_update_embed(channel, activity, metadata, old_price):
    embed = create_embed("🔄 Price Update", discord.Color.blue(), activity, metadata.get("name", "Unknown Item"), metadata.get("image"))
    embed.add_field(name="💸 Price Change", value=f"`{old_price}` SOL -> **{activity.get('price')} SOL**", inline=False)
    await channel.send(embed=embed)

# --- Core Logic ---
async def process_collection(session, symbol: str):
    channel = bot.get_channel(CHANNEL_ID)
    if not channel: return

    last_timestamp = get_last_seen_timestamp(symbol)
    activities = await get_activities(session, symbol)
    if not activities: return
    
    new_activities = [act for act in activities if act.get("blockTime", 0) > last_timestamp]
    new_activities.sort(key=lambda x: x.get('blockTime', 0))

    if not new_activities:
        if activities: set_last_seen_timestamp(symbol, activities[0]['blockTime'])
        return

    logger.info(f"[{symbol}] Found {len(new_activities)} new activity/activities. Processing...")
    for activity in new_activities:
        activity_type = activity.get("type")
        mint = activity.get("tokenMint")
        if not mint: continue
        listing_ref = db.collection(f"listings_{symbol}").document(mint)
        if activity_type == "list":
            listing_doc = listing_ref.get()
            new_price = activity.get("price")
            metadata = await get_token_metadata(session, mint) or {}
            if not listing_doc.exists:
                await send_new_listing_embed(channel, activity, metadata)
                listing_ref.set({"price": new_price, "seller": activity.get("seller")})
            else:
                old_price = listing_doc.to_dict().get("price")
                if old_price != new_price:
                    await send_price_update_embed(channel, activity, metadata, old_price)
                    listing_ref.update({"price": new_price, "seller": activity.get("seller")})
        elif activity_type == "delist":
            listing_ref.delete()
        await asyncio.sleep(1)
    set_last_seen_timestamp(symbol, new_activities[-1]['blockTime'])

# --- Background Loop & Bot Events ---
@tasks.loop(seconds=60)
async def monitor_collections():
    async with aiohttp.ClientSession() as session:
        for symbol in get_collections_from_db():
            try:
                await process_collection(session, symbol)
            except Exception as e:
                logger.error(f"[ERROR in {symbol}] Unexpected error: {e}", exc_info=True)

@bot.event
async def on_ready():
    logger.info(f"✅ Bot is logged in as {bot.user}")
    await tree.sync()
    # KORREKTUR: Task nur starten, wenn er nicht bereits läuft
    if not monitor_collections.is_running():
        monitor_collections.start()

# --- Commands (unverändert) ---
# ... (alle /addcollection, /status etc. Befehle bleiben hier unverändert) ...
@tree.command(name="status", description="Checks if the bot is online and operational.")
async def status(interaction: discord.Interaction):
    await interaction.response.send_message("✅ Bot is online and the monitoring loop is running.", ephemeral=True)

@tree.command(name="collections", description="Lists all currently monitored collections.")
async def list_collections(interaction: discord.Interaction):
    collections = get_collections_from_db()
    msg = "No collections are currently being monitored."
    if collections:
        msg = "The following collections are being monitored:\n- " + "\n- ".join(collections)
    await interaction.response.send_message(msg, ephemeral=True)

@tree.command(name="addcollection", description="Adds a new collection to monitor.")
@app_commands.describe(symbol="The collection symbol from Magic Eden (e.g., 'degods')")
async def add_collection(interaction: discord.Interaction, symbol: str):
    symbol = symbol.lower().strip()
    doc_ref = db.collection("bot_config").document("collections")
    doc = doc_ref.get()
    collections = doc.to_dict().get("symbols", []) if doc.exists else []
    if symbol in collections:
        return await interaction.response.send_message(f"⚠️ `{symbol}` is already being monitored.", ephemeral=True)
    collections.append(symbol)
    doc_ref.set({"symbols": collections})
    set_last_seen_timestamp(symbol, int(datetime.now(timezone.utc).timestamp()))
    await interaction.response.send_message(f"✅ `{symbol}` has been added.", ephemeral=True)

@tree.command(name="removecollection", description="Removes a collection from being monitored.")
@app_commands.describe(symbol="The collection symbol from Magic Eden.")
async def remove_collection(interaction: discord.Interaction, symbol: str):
    symbol = symbol.lower().strip()
    doc_ref = db.collection("bot_config").document("collections")
    doc = doc_ref.get()
    if not doc.exists:
        return await interaction.response.send_message("⚠️ No collections are being tracked.", ephemeral=True)
    collections = doc.to_dict().get("symbols", [])
    if symbol not in collections:
        return await interaction.response.send_message(f"⚠️ `{symbol}` is not being monitored.", ephemeral=True)
    collections.remove(symbol)
    doc_ref.set({"symbols": collections})
    await interaction.response.send_message(f"❌ `{symbol}` will no longer be monitored.", ephemeral=True)

# --- Run ---
if __name__ == "__main__":
    if not TOKEN:
        logger.critical("DISCORD_TOKEN not found.")
    else:
        async def main():
            flask_task = asyncio.to_thread(app.run, host="0.0.0.0", port=int(os.environ.get("PORT", 8080)), debug=False)
            async with bot:
                await asyncio.gather(flask_task, bot.start(TOKEN))
        asyncio.run(main())