import os
import json
import asyncio
import discord
import requests
from datetime import datetime, timedelta
from dotenv import load_dotenv
from discord import app_commands
from discord.ext import commands

load_dotenv()

DISCORD_TOKEN = os.getenv("DISCORD_TOKEN")
CHANNEL_ID = int(os.getenv("DISCORD_CHANNEL_ID"))
COLLECTIONS = os.getenv("COLLECTIONS").split(",")
LIMIT = 100
CHECK_INTERVAL = 60

SEEN_EXPIRY_HOURS = 24

intents = discord.Intents.default()
client = discord.Client(intents=intents)
tree = app_commands.CommandTree(client)
last_checked = {}
uptime_start = datetime.now()

# --- File-Handling ---
def get_seen_file(collection):
    return f"seen_{collection}.json"

def load_seen(collection):
    try:
        with open(get_seen_file(collection), "r") as f:
            return json.load(f)
    except:
        return {}

def save_seen(collection, seen_dict):
    with open(get_seen_file(collection), "w") as f:
        json.dump(seen_dict, f)

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
    seller = token.get("owner", "unknown wallet")
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

# --- Main logic ---
async def monitor_listings():
    await client.wait_until_ready()
    channel = client.get_channel(CHANNEL_ID)
    seen_map = {c: load_seen(c) for c in COLLECTIONS}

    while not client.is_closed():
        for collection in COLLECTIONS:
            try:
                listings = fetch_listings(collection)
                now = datetime.now()
                updated_seen = seen_map[collection]
                last_checked[collection] = now
                for nft in listings:
                    mint = nft["token"]["mintAddress"]
                    price = nft["price"]
                    seen_key = f"{mint}_{price}"

                    last_seen_str = updated_seen.get(seen_key)
                    last_seen = datetime.strptime(last_seen_str, "%Y-%m-%dT%H:%M:%S") if last_seen_str else None

                    if not last_seen or (now - last_seen) > timedelta(hours=SEEN_EXPIRY_HOURS):
                        await send_listing(nft, collection, channel)
                        updated_seen[seen_key] = now.strftime("%Y-%m-%dT%H:%M:%S")

                # only the last 500 keys to save for security issues
                if len(updated_seen) > 500:
                    updated_seen = dict(sorted(updated_seen.items(), key=lambda x: x[1], reverse=True)[:500])

                seen_map[collection] = updated_seen
                save_seen(collection, updated_seen)

            except Exception as e:
                print(f"[Error in {collection}] {e}")

        await asyncio.sleep(CHECK_INTERVAL)

@client.event
async def on_ready():
    print(f"✅ Logged in as {client.user}")
    await tree.sync()
    client.loop.create_task(monitor_listings())

# --- Slash Commands ---
@tree.command(name="status", description="Shows the actuals state of the Magic Eden Bot")
async def status_command(interaction: discord.Interaction):
    lines = [f"✅ **Bot runs!**"]
    for collection in COLLECTIONS:
        ts = last_checked.get(collection)
        ts_str = ts.strftime('%Y-%m-%d %H:%M:%S') if ts else "Never checked"
        lines.append(f"📦 `{collection}` last checked: **{ts_str}**")
        try:
            with open(get_seen_file(collection), "r") as f:
                data = json.load(f)
                lines.append(f"🔁 Listings: {len(data)}")
        except:
            lines.append(f"⚠️ No data loaded for {collection}")
    await interaction.response.send_message("\n".join(lines), ephemeral=True)
    
@tree.command(name="collections", description="List all monitored collections")
async def collections_command(interaction: discord.Interaction):
    await interaction.response.send_message(f"📦 Currently monitored collections:\n- " + "\n- ".join(COLLECTIONS), ephemeral=True)

@tree.command(name="seen", description="Show how many listings are tracked for a collection")
async def seen_command(interaction: discord.Interaction, collection: str):
    try:
        with open(get_seen_file(collection), "r") as f:
            data = json.load(f)
            await interaction.response.send_message(f"✅ `{collection}` tracks {len(data)} listings.", ephemeral=True)
    except:
        await interaction.response.send_message(f"❌ Collection `{collection}` not found.", ephemeral=True)

@tree.command(name="uptime", description="Show bot uptime")
async def uptime_command(interaction: discord.Interaction):
    uptime = datetime.now() - uptime_start
    await interaction.response.send_message(f"⏱ Bot uptime: {str(uptime).split('.')[0]}", ephemeral=True)

@tree.command(name="latest", description="Show the most recent listing for a collection")
@app_commands.describe(collection="Collection slug (e.g. meatbags)")
async def latest_command(interaction: discord.Interaction, collection: str):
    try:
        await interaction.response.defer(thinking=True)  # Gibt dir mehr Zeit

        if collection not in COLLECTIONS:
            await interaction.followup.send(f"❌ Collection `{collection}` is not being monitored.")
            return

        listings = fetch_listings(collection)
        if not listings:
            await interaction.followup.send(f"❌ No listings found for `{collection}`.")
            return

        nft = listings[0]
        token = nft["token"]
        name = token.get("name", "Unnamed NFT")
        price = nft["price"]
        mint = token["mintAddress"]
        image = token["image"]
        seller = token.get("owner", "unknown wallet")
        url = f"https://magiceden.io/item-details/{mint}"

        embed = discord.Embed(
            title=f"[{collection}] {name}",
            description=f"💰 **Price:** {price} SOL\n👤 **Seller:** `{seller}`\n[🔗 View on Magic Eden]({url})",
            color=0x00ffcc,
            timestamp=datetime.now()
        )
        embed.set_thumbnail(url=image)
        embed.set_footer(text="Magic Eden Monitor")

        await interaction.followup.send(embed=embed)

    except Exception as e:
        print(f"[Error in latest_command] {e}")
        try:
            await interaction.followup.send(f"❌ Could not fetch latest listing for `{collection}`.\nError: {e}")
        except:
            pass

@tree.command(name="resetseen", description="Clear seen listing cache for a collection")
async def reset_seen_command(interaction: discord.Interaction, collection: str):
    try:
        save_seen(collection, {})
        await interaction.response.send_message(f"🧹 Seen cache for `{collection}` cleared.", ephemeral=True)
    except:
        await interaction.response.send_message(f"❌ Failed to reset seen cache for `{collection}`.", ephemeral=True)

@tree.command(name="addcollection", description="Add a collection to the monitoring list (non-persistent)")
async def add_collection(interaction: discord.Interaction, collection: str):
    if collection in COLLECTIONS:
        await interaction.response.send_message(f"ℹ️ `{collection}` is already being monitored.", ephemeral=True)
    else:
        COLLECTIONS.append(collection)
        await interaction.response.send_message(f"➕ Collection `{collection}` added to monitoring.", ephemeral=True)

@tree.command(name="removecollection", description="Remove a collection from the monitoring list (non-persistent)")
async def remove_collection(interaction: discord.Interaction, collection: str):
    if collection not in COLLECTIONS:
        await interaction.response.send_message(f"❌ `{collection}` is not in the monitoring list.", ephemeral=True)
    else:
        COLLECTIONS.remove(collection)
        await interaction.response.send_message(f"➖ Collection `{collection}` removed from monitoring.", ephemeral=True)

@tree.command(name="help", description="Show all available bot commands")
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
