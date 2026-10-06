# 🚀 Production Migration Guide: Migrating to Centralized Local Bot API

This guide provides the exact, production-safe procedure to migrate the YouTube Downloader bot from an old embedded/standalone Local Bot API setup to our centralized, shared Telegram Local Bot API stack running on the Docker network `telegram-bots` at `http://telegram-bot-api:8081`.

---

## ⚠️ Important Safety Rules

1. **NO Automatic logOut**:
   The bot never issues an automatic `logOut` call. Telegram enforces a strict 10-minute cooldown if a bot session is terminated or switched improperly.
2. **Prior Session Conflict Prevention**:
   A bot token can only be active on ONE Telegram Bot API server at a time (either Telegram Cloud or a specific Local Bot API server instance).
3. **HTTP Multipart Uploads**:
   The bot connects to the centralized server with `is_local=False`. All media uploads up to 2000 MB are sent via standard HTTP multipart upload over the internal Docker network. **No shared disk volume or `/transfer` folder is needed.**

---

## 📋 Step-by-Step Production Migration Sequence

### Step 1: Stop the Running Bot
Before modifying configuration or touching tokens, stop the running bot application containers:

```bash
docker compose down
```

---

### Step 2: Log Out the Bot Token from the OLD Local Bot API
If your bot was previously active on an old embedded or dedicated Local Bot API instance, you **must** call Telegram's `logOut` method through that old instance to release the session back to Telegram.

If the old local server container is still accessible (e.g. running on port 8081 locally):
```bash
curl -X POST "http://localhost:8081/bot<YOUR_BOT_TOKEN>/logOut"
```

Expected response:
```json
{"ok": true, "result": true}
```

> [!NOTE]
> If switching from official **Telegram Cloud API** (`https://api.telegram.org`), you can either call:
> ```bash
> curl -X POST "https://api.telegram.org/bot<YOUR_BOT_TOKEN>/logOut"
> ```
> Or if the bot was only using Cloud polling, wait until polling stops before switching.

---

### Step 3: Ensure the External Docker Network Exists
Verify that the shared `telegram-bots` network exists on your host:

```bash
docker network ls | grep telegram-bots
```

If it does not exist, create it:
```bash
docker network create telegram-bots
```

Verify that the centralized `telegram-bot-api` container is running and attached to `telegram-bots`:
```bash
docker ps --filter name=telegram-bot-api
```

---

### Step 4: Configure the YouTube Downloader Bot
Update your `.env` file (or configure via the Web Admin Panel):

```env
# Operating Mode: local or cloud
TELEGRAM_API_MODE=local

# Centralized Bot API Endpoint on external network
TELEGRAM_API_BASE_URL=http://telegram-bot-api:8081

# Bot Token (from @BotFather)
BOT_TOKEN=123456789:ABCdefGHIjklMNOpqrSTUvwxYZ

# Private Cache Channel ID
TELEGRAM_CACHE_CHANNEL_ID=-1001234567890
```

> [!IMPORTANT]
> Notice that `TELEGRAM_API_ID` and `TELEGRAM_API_HASH` are **NO LONGER NEEDED** for this bot. Only the centralized server requires them.

---

### Step 5: Start the Bot Stack
Start the updated bot stack:

```bash
docker compose up -d
```

Check the startup logs:
```bash
docker compose logs -f bot worker web
```

---

### Step 6: Verify Health & End-to-End Functionality
Verify each of the following in order:

1. **Bot Identity (`getMe`)**:
   Verify logs indicate successful bot connection, or test through the Web Admin dashboard (**Telegram Settings -> Test Token** and **Test Local API**).
2. **Update Delivery & Commands**:
   Send `/start` to the bot in Telegram. Ensure the welcome message and inline buttons respond immediately.
3. **Downloads & Uploads**:
   Send a YouTube link (e.g. video or Shorts). Select a resolution or MP3 format.
4. **Cache & Delivery**:
   Ensure the media downloads, transcodes, uploads to the private cache channel, and delivers to the user with full audio metadata and thumbnail.

---

### Step 7: Clean Up Obsolete Containers and Artifacts
Once end-to-end operation is verified on the centralized server, remove any legacy standalone Local Bot API containers, temporary volumes, or directories:

```bash
# Remove old unused local bot api containers if still present
docker rm -f <old_local_bot_api_container_name> 2>/dev/null || true

# Prune old transfer / upload temp volumes if present
docker volume rm -f youtubedl_telegram_upload_tmp 2>/dev/null || true
```
