import os
import asyncio
import logging
from datetime import datetime, timezone

import discord
from discord.ext import commands, tasks
from discord import app_commands
from discord.ui import Select, View

import aiohttp
from flask import Flask

import firebase_admin
from firebase_admin import credentials, firestore

# --- Setup ---
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
logger = logging.getLogger("discord-bot")

try:
    # For local testing with a service account file
    cred = credentials.Certificate("serviceAccountKey.json")
    firebase_admin.initialize_app(cred)
    logger.info("Firebase initialized with serviceAccountKey.json (Local Mode).")
except FileNotFoundError:
    # For Google Cloud Run, where authentication is handled automatically
    firebase_admin.initialize_app()
    logger.info("Firebase initialized without serviceAccountKey.json (Cloud Run Mode).")

db = firestore.client()
TOKEN = os.getenv("DISCORD_TOKEN")
CHANNEL_ID = int(os.getenv("DISCORD_CHANNEL_ID"))

# --- Cooldown configuration for price updates (in seconds) ---
# Prevents spam from bots that update prices too frequently.
# 900 seconds = 15 minutes. Set to 0 to disable.
PRICE_UPDATE_COOLDOWN_SECONDS = 900

# Define intents, what the bot is allowed to receive from Discord
intents = discord.Intents.default()
intents.dm_messages = True  # Required to send direct messages (DMs) for alerts

bot = commands.Bot(command_prefix="!", intents=intents)
tree = bot.tree
app = Flask(__name__)

# --- Webserver for Cloud Run Health Check ---
@app.route("/")
def index():
    return "✅ Magic Eden Discord Bot is active!"

# --- Firestore Helper Functions ---
def get_collections_from_db():
    """Fetches the list of collections to monitor from Firestore."""
    doc = db.collection("bot_config").document("collections").get()
    return doc.to_dict().get("symbols", []) if doc.exists else []

def get_last_seen_timestamp(symbol: str) -> int:
    """Gets the timestamp of the last seen activity for a collection."""
    doc = db.collection("bot_state").document(symbol).get()
    # Returns the saved timestamp or the current time if no data exists
    return doc.to_dict().get("last_seen_timestamp", int(datetime.now(timezone.utc).timestamp())) if doc.exists else int(datetime.now(timezone.utc).timestamp())

def set_last_seen_timestamp(symbol: str, timestamp: int):
    """Saves the timestamp of the last seen activity."""
    db.collection("bot_state").document(symbol).set({"last_seen_timestamp": timestamp})

# --- Asynchronous API Calls with aiohttp ---
async def fetch_json(session, url):
    """A central helper function for API GET requests."""
    try:
        async with session.get(url, timeout=20) as response:
            if response.status == 404: return None  # Explicitly handle 404 as "not found"
            response.raise_for_status()  # Raise an exception for other HTTP errors
            return await response.json()
    except asyncio.TimeoutError:
        logger.error(f"API Timeout for URL: {url}")
    except aiohttp.ClientError as e:
        logger.error(f"API Client Error for URL {url}: {e}")
    return None

async def get_activities(session, symbol: str):
    """Fetches the last 500 activities for a collection."""
    url = f"https://api-mainnet.magiceden.dev/v2/collections/{symbol}/activities?offset=0&limit=500"
    return await fetch_json(session, url)

async def get_token_metadata(session, mint: str):
    """Fetches metadata (name, image, etc.) for a single NFT."""
    url = f"https://api-mainnet.magiceden.dev/v2/tokens/{mint}"
    return await fetch_json(session, url)

async def get_collection_stats(session, symbol: str):
    """Fetches statistics (floor price, etc.) for a collection."""
    url = f"https://api-mainnet.magiceden.dev/v2/collections/{symbol}/stats"
    return await fetch_json(session, url)

async def get_token_listings(session, mint: str):
    """Checks if a single NFT is currently listed."""
    url = f"https://api-mainnet.magiceden.dev/v2/tokens/{mint}/listings"
    return await fetch_json(session, url)

async def get_wallet_activities(session, wallet: str):
    """Fetches the latest activities of a wallet."""
    url = f"https://api-mainnet.magiceden.dev/v2/wallets/{wallet}/activities?offset=0&limit=10"
    return await fetch_json(session, url)

# --- Embed Creation Functions ---
def create_embed(title, color, activity, name, image_url):
    """Base function to create a standardized Discord embed."""
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

# --- Optimized: Prefer image from activity data to avoid extra API call ---
async def send_new_listing_embed(channel, activity, metadata):
    """Sends a message for a new listing."""
    name = metadata.get("name", "Unknown")
    image_url = activity.get("image") or metadata.get("image")
    embed = create_embed("✅ New Listing", discord.Color.green(), activity, name, image_url)
    embed.add_field(name="💰 Price", value=f"**{activity.get('price')} SOL**", inline=False)
    await channel.send(embed=embed)

# --- Optimized: Prefer image from activity data to avoid extra API call ---
async def send_price_update_embed(channel, activity, metadata, old_price):
    """Sends a message for a price update."""
    name = metadata.get("name", "Unknown")
    image_url = activity.get("image") or metadata.get("image")
    embed = create_embed("🔄 Price Update", discord.Color.blue(), activity, name, image_url)
    embed.add_field(name="💸 Price Change", value=f"`{old_price}` SOL -> **{activity.get('price')} SOL**", inline=False)
    await channel.send(embed=embed)


# --- Core Bot Logic (UPDATED with Cooldown) ---
async def process_collection_listings(session, symbol: str):
    """Processes listing, delisting, and price update activities with a cooldown."""
    channel = bot.get_channel(CHANNEL_ID)
    if not channel: return

    last_timestamp = get_last_seen_timestamp(symbol)
    activities = await get_activities(session, symbol)
    if not activities:
        logger.info(f"[{symbol}] No activities returned from API.")
        return
    
    new_activities = [act for act in activities if act.get("blockTime", 0) > last_timestamp]
    
    if not new_activities:
        logger.info(f"[{symbol}] No new activities since timestamp {last_timestamp}.")
        return

    new_activities.sort(key=lambda x: x.get('blockTime', 0))
    logger.info(f"[{symbol}] Found {len(new_activities)} new activity/activities. Processing...")

    for activity in new_activities:
        activity_type = activity.get("type")
        mint = activity.get("tokenMint")
        if not mint: continue
        
        listing_ref = db.collection(f"listings_{symbol}").document(mint)
        activity_timestamp = activity.get("blockTime")

        if activity_type == "list":
            listing_doc = listing_ref.get()
            new_price = activity.get("price")
            
            # This data will be saved to Firestore for the specific NFT
            update_payload = {
                "price": new_price, 
                "seller": activity.get("seller"),
                "last_update_timestamp": activity_timestamp
            }

            if not listing_doc.exists:
                # This is a brand new listing for an NFT we haven't seen before.
                metadata = await get_token_metadata(session, mint) or {}
                await send_new_listing_embed(channel, activity, metadata)
                listing_ref.set(update_payload)
            else:
                # We have seen this NFT before, it's a price update or relist.
                old_data = listing_doc.to_dict()
                old_price = old_data.get("price")
                last_update_time = old_data.get("last_update_timestamp", 0)

                # COOLDOWN CHECK: Skip notification if the update is too frequent
                if activity_timestamp - last_update_time < PRICE_UPDATE_COOLDOWN_SECONDS:
                    logger.info(f"[{symbol}] Skipping frequent price update for mint {mint}.")
                    listing_ref.update(update_payload) # Update DB to keep price current
                    continue # Move to the next activity without sending a notification

                if old_price != new_price:
                    metadata = await get_token_metadata(session, mint) or {}
                    await send_price_update_embed(channel, activity, metadata, old_price)
                    listing_ref.update(update_payload)
        
        elif activity_type == "delist":
            listing_ref.delete()
        
        await asyncio.sleep(1)
    
    # IMPORTANT: Only update the overall collection timestamp after processing all new activities
    newest_processed_timestamp = new_activities[-1]['blockTime']
    set_last_seen_timestamp(symbol, newest_processed_timestamp)
    logger.info(f"[{symbol}] Collection timestamp updated to {newest_processed_timestamp}.")

async def check_price_alerts(session):
    """Checks if any user-defined price alerts have been triggered."""
    logger.info("Checking price alerts...")
    alerts_ref = db.collection("user_alerts")
    triggered_alerts_to_remove = []

    for alert_doc in alerts_ref.stream():
        user_id_str = alert_doc.id
        user_alerts = alert_doc.to_dict().get("alerts", [])
        if not user_alerts: continue

        for i, alert in enumerate(user_alerts):
            symbol, target_price = alert.get("symbol"), alert.get("price")
            stats = await get_collection_stats(session, symbol)
            if not stats or "floorPrice" not in stats: continue
            
            floor_price_sol = stats["floorPrice"] / 1_000_000_000

            if floor_price_sol <= target_price:
                logger.info(f"Price alert triggered for user {user_id_str} on {symbol}!")
                try:
                    user = await bot.fetch_user(int(user_id_str))
                    embed = discord.Embed(title="🔔 Price Alert Triggered!",
                                          description=f"The floor price for **{symbol.upper()}** has dropped to **{floor_price_sol:.4f} SOL**.",
                                          color=discord.Color.gold())
                    embed.add_field(name="Your target price was", value=f"below **{target_price} SOL**")
                    await user.send(embed=embed)
                    triggered_alerts_to_remove.append((user_id_str, i))
                except Exception as e:
                    logger.error(f"Could not send DM to user {user_id_str}: {e}")

    # Delete triggered alerts to prevent spam, iterating backwards to avoid index issues
    triggered_alerts_to_remove.sort(key=lambda x: x[1], reverse=True)
    for user_id, index in triggered_alerts_to_remove:
        doc_ref = db.collection("user_alerts").document(user_id)
        current_alerts = doc_ref.get().to_dict().get("alerts", [])
        if index < len(current_alerts):
            current_alerts.pop(index)
            doc_ref.set({"alerts": current_alerts})

# --- Background Task (UPDATED for better reliability) ---
@tasks.loop(minutes=2)
async def monitor_loop():
    """Main loop that regularly executes both core functions."""
    async with aiohttp.ClientSession() as session:
        logger.info("Starting monitoring run...")
        
        # 1. Check listings. Use a for-loop with error handling to avoid a single failure stopping all checks.
        for symbol in get_collections_from_db():
            try:
                await process_collection_listings(session, symbol)
            except Exception as e:
                logger.error(f"[ERROR in Listing-Check for {symbol}]: {e}", exc_info=True)

        # 2. Check price alerts. Also add error handling for robustness.
        try:
            await check_price_alerts(session)
        except Exception as e:
            logger.error(f"[ERROR in Alert-Check]: {e}", exc_info=True)

        logger.info("Monitoring run finished.")


# --- Bot Events ---
@bot.event
async def on_ready():
    logger.info(f"✅ Bot is logged in as {bot.user}")
    await tree.sync()
    if not monitor_loop.is_running():
        monitor_loop.start()

# --- Slash Commands ---

@tree.command(name="status", description="Checks if the bot is online.")
async def status(interaction: discord.Interaction):
    await interaction.response.send_message("✅ Bot is online and the monitoring loop is running.", ephemeral=True)

@tree.command(name="floor", description="Shows the current floor price of a collection.")
@app_commands.describe(symbol="The collection symbol from Magic Eden.")
async def floor(interaction: discord.Interaction, symbol: str):
    await interaction.response.defer(ephemeral=True)
    async with aiohttp.ClientSession() as session:
        stats = await get_collection_stats(session, symbol)
        if stats and "floorPrice" in stats:
            floor_sol = stats['floorPrice'] / 1_000_000_000
            await interaction.followup.send(f"The floor price for **{symbol.upper()}** is **{floor_sol:.4f} SOL**.")
        else:
            await interaction.followup.send(f"Could not find the floor price for `{symbol}`.")

@tree.command(name="stats", description="Shows detailed statistics for a collection.")
@app_commands.describe(symbol="The collection symbol from Magic Eden.")
async def stats(interaction: discord.Interaction, symbol: str):
    await interaction.response.defer(ephemeral=True)
    async with aiohttp.ClientSession() as session:
        stats = await get_collection_stats(session, symbol)
        if stats:
            embed = discord.Embed(title=f"Statistics for {symbol.upper()}", color=discord.Color.dark_orange())
            embed.add_field(name="Floor Price", value=f"**{stats.get('floorPrice', 0) / 1_000_000_000:.4f} SOL**")
            embed.add_field(name="Listed Count", value=f"{stats.get('listedCount', 'N/A')}")
            embed.add_field(name="Total Supply", value=f"{stats.get('totalSupply', 'N/A')}")
            embed.add_field(name="Total Volume", value=f"**{stats.get('volumeAll', 0) / 1_000_000_000:.2f} SOL**")
            await interaction.followup.send(embed=embed)
        else:
            await interaction.followup.send(f"Could not find statistics for `{symbol}`.")

@tree.command(name="lookup", description="Looks up a specific NFT by its mint address.")
@app_commands.describe(mint="The mint address of the NFT.")
async def lookup(interaction: discord.Interaction, mint: str):
    await interaction.response.defer(ephemeral=True)
    async with aiohttp.ClientSession() as session:
        metadata = await get_token_metadata(session, mint)
        listings = await get_token_listings(session, mint)
        if not metadata:
            return await interaction.followup.send(f"Could not find an NFT with the mint address `{mint}`.")

        embed = discord.Embed(title=f"Lookup: {metadata.get('name', 'Unknown')}", color=discord.Color.dark_teal())
        if metadata.get("image"): embed.set_thumbnail(url=metadata.get("image"))
        
        listing_price = "Not Listed"
        if listings:
            listing_price = f"**{listings[0].get('price')} SOL**"
        embed.add_field(name="Current Price", value=listing_price, inline=False)

        attributes = metadata.get("attributes", [])
        if attributes:
            attr_string = "\n".join([f"**{attr['trait_type']}**: {attr['value']}" for attr in attributes])
            embed.add_field(name="Attributes", value=attr_string, inline=False)
        
        await interaction.followup.send(embed=embed)

@tree.command(name="link", description="Returns the direct Magic Eden link for a collection.")
@app_commands.describe(symbol="The collection symbol from Magic Eden.")
async def link(interaction: discord.Interaction, symbol: str):
    url = f"https://magiceden.io/collections/{symbol.lower().strip()}"
    await interaction.response.send_message(f"Here is the link for **{symbol.upper()}**:\n{url}", ephemeral=True)

@tree.command(name="wallet", description="Shows the latest activities of a wallet.")
@app_commands.describe(wallet="The wallet address.")
async def wallet(interaction: discord.Interaction, wallet: str):
    await interaction.response.defer(ephemeral=True)
    async with aiohttp.ClientSession() as session:
        activities = await get_wallet_activities(session, wallet)
        if not activities:
            return await interaction.followup.send(f"Could not find any activities for the wallet `{wallet}`.")
        
        embed = discord.Embed(title=f"Last 5 Activities for Wallet", description=f"`{wallet}`", color=discord.Color.purple())
        for act in activities[:5]:
            act_type = act.get('type', 'Unknown').capitalize()
            price = act.get('price', 0)
            collection = act.get('collection', 'N/A')
            embed.add_field(name=f"{act_type} on `{collection}`", value=f"Price: **{price} SOL**", inline=False)
        
        await interaction.followup.send(embed=embed)

# --- Alert Command Group ---
alert_group = app_commands.Group(name="alert", description="Manage your personal price alerts.")

@alert_group.command(name="add", description="Add a new price alert.")
@app_commands.describe(symbol="Collection symbol", price="Target price in SOL (e.g., 1.5)")
async def alert_add(interaction: discord.Interaction, symbol: str, price: float):
    user_id = str(interaction.user.id)
    doc_ref = db.collection("user_alerts").document(user_id)
    new_alert = {"symbol": symbol.lower().strip(), "price": price}
    
    doc = doc_ref.get()
    if doc.exists:
        user_alerts = doc.to_dict().get("alerts", [])
        if any(a['symbol'] == new_alert['symbol'] for a in user_alerts):
            return await interaction.response.send_message(f"You already have an alert for `{symbol}`. Remove it first with `/alert remove`.", ephemeral=True)
        user_alerts.append(new_alert)
        doc_ref.update({"alerts": user_alerts})
    else:
        doc_ref.set({"alerts": [new_alert]})
        
    await interaction.response.send_message(f"✅ Alert set! I will notify you via DM if the floor of **{symbol.upper()}** drops below **{price} SOL**.", ephemeral=True)

@alert_group.command(name="list", description="Lists your active price alerts.")
async def alert_list(interaction: discord.Interaction):
    user_id = str(interaction.user.id)
    doc = db.collection("user_alerts").document(user_id).get()
    if not doc.exists or not doc.to_dict().get("alerts"):
        return await interaction.response.send_message("You have no active alerts.", ephemeral=True)
    
    alerts = doc.to_dict().get("alerts")
    embed = discord.Embed(title="Your Active Price Alerts", color=discord.Color.gold())
    for alert in alerts:
        embed.add_field(name=alert['symbol'].upper(), value=f"Target: < {alert['price']} SOL", inline=False)
    await interaction.response.send_message(embed=embed, ephemeral=True)

@alert_group.command(name="remove", description="Removes one of your price alerts.")
async def alert_remove(interaction: discord.Interaction):
    user_id = str(interaction.user.id)
    doc_ref = db.collection("user_alerts").document(user_id)
    doc = doc_ref.get()
    if not doc.exists or not doc.to_dict().get("alerts"):
        return await interaction.response.send_message("You have no alerts that could be removed.", ephemeral=True)

    alerts = doc.to_dict().get("alerts")
    options = [discord.SelectOption(label=f"{alert['symbol'].upper()} (< {alert['price']} SOL)", value=str(i)) for i, alert in enumerate(alerts)]
    select = Select(placeholder="Choose an alert to remove...", options=options)

    async def select_callback(callback_interaction: discord.Interaction):
        chosen_index = int(select.values[0])
        # Refetch alerts to ensure data is current before modifying
        current_alerts_doc = doc_ref.get()
        if not current_alerts_doc.exists:
            return await callback_interaction.response.edit_message(content="Error: Could not find your alerts.", view=None)
        
        current_alerts = current_alerts_doc.to_dict().get("alerts", [])
        if chosen_index < len(current_alerts):
            removed_alert = current_alerts.pop(chosen_index)
            doc_ref.set({"alerts": current_alerts})
            await callback_interaction.response.edit_message(content=f"❌ Alert for **{removed_alert['symbol'].upper()}** has been removed.", view=None)
        else:
            await callback_interaction.response.edit_message(content="Error: This alert no longer exists.", view=None)

    select.callback = select_callback
    view = View(); view.add_item(select)
    await interaction.response.send_message("Choose an alert from the list to remove it:", view=view, ephemeral=True)

tree.add_command(alert_group)

# --- Administrative Commands ---
admin_group = app_commands.Group(name="admin", description="Administrative commands for the bot.")

@admin_group.command(name="addcollection", description="Adds a collection to monitor.")
@app_commands.describe(symbol="The collection symbol from Magic Eden.")
async def add_collection(interaction: discord.Interaction, symbol: str):
    # A permission check for admins could be added here
    symbol = symbol.lower().strip()
    doc_ref = db.collection("bot_config").document("collections")
    doc = doc_ref.get()
    collections = doc.to_dict().get("symbols", []) if doc.exists else []
    if symbol in collections:
        return await interaction.response.send_message(f"⚠️ `{symbol}` is already being monitored.", ephemeral=True)
    collections.append(symbol)
    doc_ref.set({"symbols": collections})
    set_last_seen_timestamp(symbol, int(datetime.now(timezone.utc).timestamp()))
    await interaction.response.send_message(f"✅ Collection `{symbol}` has been added.", ephemeral=True)

@admin_group.command(name="removecollection", description="Removes a collection from monitoring.")
@app_commands.describe(symbol="The collection symbol.")
async def remove_collection(interaction: discord.Interaction, symbol: str):
    symbol = symbol.lower().strip()
    doc_ref = db.collection("bot_config").document("collections")
    if not (doc := doc_ref.get()).exists:
        return await interaction.response.send_message("⚠️ No collections are being tracked.", ephemeral=True)
    collections = doc.to_dict().get("symbols", [])
    if symbol not in collections:
        return await interaction.response.send_message(f"⚠️ `{symbol}` is not being monitored.", ephemeral=True)
    collections.remove(symbol)
    doc_ref.set({"symbols": collections})
    await interaction.response.send_message(f"❌ Collection `{symbol}` has been removed.", ephemeral=True)

@admin_group.command(name="listcollections", description="Lists all monitored collections.")
async def list_collections(interaction: discord.Interaction):
    collections = get_collections_from_db()
    msg = "No collections are currently being monitored."
    if collections:
        msg = "The following collections are being monitored:\n- " + "\n- ".join(collections)
    await interaction.response.send_message(msg, ephemeral=True)

tree.add_command(admin_group)


# --- Bot Startup ---
if __name__ == "__main__":
    if not TOKEN:
        logger.critical("DISCORD_TOKEN not found in environment variables. Bot cannot start.")
    else:
        async def main():
            # Start the Flask webserver in a separate thread
            flask_task = asyncio.to_thread(app.run, host="0.0.0.0", port=int(os.environ.get("PORT", 8080)), debug=False)
            # Start the Discord bot
            async with bot:
                await asyncio.gather(flask_task, bot.start(TOKEN))
        
        try:
            asyncio.run(main())
        except KeyboardInterrupt:
            logger.info("Bot is shutting down.")