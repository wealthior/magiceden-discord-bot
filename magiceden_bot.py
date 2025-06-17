import os
import asyncio
import sqlite3
import discord
import requests
from datetime import datetime, timedelta
from dotenv import load_dotenv
from discord import app_commands

# --- Load ENV ---
load_dotenv()
DISCORD_TOKEN = os.getenv("DISCORD_TOKEN")
CHANNEL_ID = int(os.getenv("DISCORD_CHANNEL_ID"))
COLLECTIONS = os.getenv("COLLECTIONS", "").split(",")
LIMIT = 100
CHECK_INTERVAL = 60
SEEN_EXPIRY_HOURS = 24

# --- Discord Setup ---
intents = discord.Intents.default()
client = discord.Client(intents=intents)
tree = app_commands.CommandTree(client)
last_checked = {}
uptime_start = datetime.now()

# --- SQLite DB Setup ---
conn = sqlite3.connect("seen.db")
cur = conn.cursor()
cur.execute("""
CREATE TABLE IF NOT EXISTS seen_listings (
    collection TEXT,
    mint TEXT,
    price REAL,
    timestamp TEXT,
    PRIMARY KEY (collection, mint, price)
)
""")
conn.commit()

def was_seen(collection, mint, price):
    cur.execute("""
        SELECT timestamp FROM seen_listings
        WHERE collection=? AND mint=? AND price=?
    """, (collection, mint, price))
    row = cur.fetchone()
    if row:
        ts = datetime.strptime(row[0], "%Y-%m-%dT%H:%M:%S")
        if datetime.now() - ts < timedelta(hours=SEEN_EXPIRY_HOURS):
            return True
    return False

def mark_seen(collection, mint, price):
    cur.execute("""
        INSERT OR REPLACE INTO seen_listings (collection, mint, price, timestamp)
        VALUES (?, ?, ?, ?)
    """, (collection, mint, price, datetime.now().strftime("%Y-%m-%dT%H:%M:%S")))
    conn.commit()

def reset_seen(collection):
    cur.execute("DELETE FROM seen_listings WHERE collection=?", (collection,))
    conn.commit()

def count_seen(collection):
    cur.execute("SELECT COUNT(*) FROM seen_listings WHERE collection=?", (collection,))
    return cur.fetchone()[0]

# --- API Call ---
def fetch_listings(collection):
    url = f"https://api-mainnet.magiceden.dev/v2/collections/{collection}/listings?offset=0&limit={LIMIT}"
    res = requests.get(url)
    res.raise_for_status()
    return res.json()

# --- Discord Notify ---
async def send_listing(nft, collection, channel):
    token = nft["token"]
    name = token.get("name", "Unnamed NFT")
    price = nft["price"]
    mint = token["mintAddress"]
    image = token["image"]
    seller = token.get("mintAuthority", "unknown wallet")
    url = f"https://magiceden.io/item-details/{mint}"

    embed = discord.Embed(
        title=f"[{collection}] {name}",
        description=f"💰 **Price:** {price} SOL\n👤 **Seller:** `{seller}`\n[🔗 View on Magic Eden]({url})",
        color=0x00ffcc,
        timestamp=datetime.now()
    )
    embed.set_thumbnail(url=image)
    embed.set_footer(text="Magic Eden Monitor")
    await channel.send(embed=embed)

# --- Main Monitor Loop ---
async def monitor_listings():
    await client.wait_until_ready()
    channel = client.get_channel(CHANNEL_ID)

    if channel is None:
        print("❌ ERROR: Could not find Discord channel. Check permissions and channel ID.")
        return

    while not client.is_closed():
        for collection in COLLECTIONS:
            try:
                listings = fetch_listings(collection)
                now = datetime.now()
                last_checked[collection] = now

                for nft in listings:
                    mint = nft["token"]["mintAddress"]
                    price = nft["price"]

                    if not was_seen(collection, mint, price):
                        await send_listing(nft, collection, channel)
                        mark_seen(collection, mint, price)

            except Exception as e:
                print(f"[Error in {collection}] {e}")

        await asyncio.sleep(CHECK_INTERVAL)

@client.event
async def on_ready():
    print(f"✅ Logged in as {client.user}")
    await tree.sync()
    client.loop.create_task(monitor_listings())

# --- Slash Commands ---
@tree.command(name="status", description="Show bot and collection status")
async def status_command(interaction: discord.Interaction):
    lines = [f"✅ **Bot runs!**"]
    for collection in COLLECTIONS:
        ts = last_checked.get(collection)
        ts_str = ts.strftime('%Y-%m-%d %H:%M:%S') if ts else "Never checked"
        listings = count_seen(collection)
        lines.append(f"📦 `{collection}` last checked: **{ts_str}** | Seen: `{listings}`")
    await interaction.response.send_message("\n".join(lines), ephemeral=True)

@tree.command(name="collections", description="List all monitored collections")
async def collections_command(interaction: discord.Interaction):
    if not COLLECTIONS:
        await interaction.response.send_message("❌ No collections currently monitored.", ephemeral=True)
    else:
        await interaction.response.send_message(
            f"📦 Currently monitored collections:\n- " + "\n- ".join(COLLECTIONS),
            ephemeral=True
        )

@tree.command(name="seen", description="Show listing count for a collection")
async def seen_command(interaction: discord.Interaction, collection: str):
    try:
        count = count_seen(collection)
        await interaction.response.send_message(f"✅ `{collection}` tracks {count} listings.", ephemeral=True)
    except:
        await interaction.response.send_message(f"❌ Failed to get listings for `{collection}`.", ephemeral=True)

@tree.command(name="uptime", description="Show bot uptime")
async def uptime_command(interaction: discord.Interaction):
    uptime = datetime.now() - uptime_start
    await interaction.response.send_message(f"⏱ Bot uptime: {str(uptime).split('.')[0]}", ephemeral=True)

@tree.command(name="latest", description="Show the latest listing for a collection")
@app_commands.describe(collection="Collection slug (e.g. meatbags)")
async def latest_command(interaction: discord.Interaction, collection: str):
    try:
        await interaction.response.defer(thinking=True)
        listings = fetch_listings(collection)
        if not listings:
            await interaction.followup.send(f"❌ No listings found for `{collection}`.")
            return
        await send_listing(listings[0], collection, interaction.channel)
    except Exception as e:
        await interaction.followup.send(f"❌ Could not fetch latest listing for `{collection}`.\nError: {e}")

@tree.command(name="resetseen", description="Clear seen listing cache for a collection")
async def reset_seen_command(interaction: discord.Interaction, collection: str):
    try:
        reset_seen(collection)
        await interaction.response.send_message(f"🧹 Seen cache for `{collection}` cleared.", ephemeral=True)
    except:
        await interaction.response.send_message(f"❌ Failed to reset seen cache for `{collection}`.", ephemeral=True)

@tree.command(name="addcollection", description="Add a collection to monitoring")
async def add_collection(interaction: discord.Interaction, collection: str):
    if collection in COLLECTIONS:
        await interaction.response.send_message(f"ℹ️ `{collection}` is already monitored.", ephemeral=True)
    else:
        COLLECTIONS.append(collection)
        await interaction.response.send_message(f"➕ Added `{collection}` to monitoring list.", ephemeral=True)

@tree.command(name="removecollection", description="Remove a collection from monitoring")
async def remove_collection(interaction: discord.Interaction, collection: str):
    if collection not in COLLECTIONS:
        await interaction.response.send_message(f"❌ `{collection}` is not being monitored.", ephemeral=True)
    else:
        COLLECTIONS.remove(collection)
        await interaction.response.send_message(f"➖ Removed `{collection}` from monitoring list.", ephemeral=True)

@tree.command(name="help", description="List all bot commands")
async def help_command(interaction: discord.Interaction):
    help_text = """
📘 **Magic Eden Discord Bot Commands**

/status - Show current bot and collection status  
/collections - List all monitored collections  
/addcollection <slug> - Add a new collection (non-persistent)  
/removecollection <slug> - Remove a collection (non-persistent)  
/seen <collection> - Show number of known listings  
/latest <collection> - Show latest NFT listing  
/resetseen <collection> - Clear known listings for collection  
/uptime - Show how long the bot has been running  
/help - Show this command list
    """
    await interaction.response.send_message(help_text, ephemeral=True)

client.run(DISCORD_TOKEN)
