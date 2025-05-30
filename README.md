# 🤖 Magic Eden Discord Listings Bot

Get **real-time Discord alerts** for new NFT listings on Magic Eden – fully automated and customizable!  
Perfect for degen collectors, alpha groups or DAO servers.  

---

## 🧩 Features

- 🔔 Discord alerts for new listings
- 📦 Supports multiple Magic Eden collections
- 🧠 Avoids duplicates (seen listing cache)
- 👤 Displays seller wallet address
- ✨ Slash commands: `/status`, `/latest`, `/addcollection`, `/uptime`, etc.
- 🧼 Automatically clears outdated entries after 24h
- 💾 JSON-based tracking for persistent state
- 🧪 Fully compatible with cloud hosting (e.g., Google Cloud Run)

---

## ⚙️ Setup

### 1️⃣ Requirements

- A Discord server (with permission to add bots)
- A Discord bot token from [Discord Developer Portal](https://discord.com/developers/applications)
- Python 3.10+
- Git

---

### 2️⃣ Clone this repo

```bash
git clone https://github.com/YOUR_USERNAME/magiceden-discord-bot.git
cd magiceden-discord-bot
```

---

### 3️⃣ Install dependencies

```bash
pip install -r requirements.txt
```

📄 `requirements.txt`:

```txt
discord.py
python-dotenv
requests
```

---

### 4️⃣ Create your `.env` file

```env
DISCORD_TOKEN=your_discord_bot_token
DISCORD_CHANNEL_ID=your_channel_id
COLLECTIONS=meatbags,meatbags_geocache
```

✅ Get your Discord Channel ID by right-clicking the text channel → “Copy ID” (enable Developer Mode in settings).

---

### 5️⃣ Run the bot locally

```bash
python magiceden_bot.py
```

The bot will connect, start monitoring, and auto-sync all slash commands.

---

## 🧙 Slash Commands

These commands are available via Discord:

| Command | Description |
|--------|-------------|
| `/status` | Show status for all monitored collections |
| `/collections` | List currently monitored collections |
| `/addcollection <slug>` | Add a collection (non-persistent) |
| `/removecollection <slug>` | Remove a collection (non-persistent) |
| `/seen <collection>` | Show how many listings are cached |
| `/latest <collection>` | Show latest listing for a collection |
| `/resetseen <collection>` | Clear seen-listing cache |
| `/uptime` | Show bot uptime |
| `/help` | Show this command list |

---

## ☁️ Hosting on Google Cloud Run (Optional)

To host this bot 24/7 using Cloud Run:

### 🔧 Create Dockerfile

```dockerfile
FROM python:3.10-slim

WORKDIR /app
COPY . .

RUN pip install --no-cache-dir -r requirements.txt
CMD ["python", "magiceden_bot.py"]
```

### ⚙️ Deploy to Cloud Run

1. Enable APIs: Cloud Run, Cloud Build
2. Use the following commands:

```bash
gcloud builds submit --tag gcr.io/YOUR_PROJECT_ID/magiceden-discord-bot

gcloud run deploy magiceden-discord-bot \
  --image gcr.io/YOUR_PROJECT_ID/magiceden-discord-bot \
  --platform managed \
  --region europe-west1 \
  --memory 512Mi \
  --timeout=900 \
  --allow-unauthenticated \
  --env-vars-file .env
```

✅ Use UptimeRobot to ping the service every 5–10 mins to keep it alive.

---

## 🧼 File Structure

```bash
magiceden_bot.py        # Main bot script
.env                    # Your secret token and config
requirements.txt        # Dependencies
seen_<collection>.json  # Per-collection cache
```

✅ `.gitignore` recommendation:

```
.env
__pycache__/
seen_*.json
```

---

## 🧑‍💻 Author / License

Created by [Wealthior](https://x.com/wealthior) – open tools for collectors, founders & degens.  
Feel free to fork, remix, and extend. Attribution appreciated 🙏

License: MIT – free to use, but not liable for market floors 💀

---

Happy sniping ✌️