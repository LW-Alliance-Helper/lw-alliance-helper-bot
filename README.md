# Squad Powers Survey Bot

A Discord bot that watches a channel for Subo survey response embeds, parses the data, and syncs it to a Google Sheet — updating existing rows or creating new ones based on Discord ID.

---

## File Structure

```
discord-bot/
├── bot.py                  # Main bot — Discord event handling & embed parsing
├── sheets.py               # Google Sheets read/write logic
├── requirements.txt        # Python dependencies
├── .env.example            # Environment variable template
├── .env                    # Your actual secrets (never commit this)
└── service_account.json    # Google service account key (never commit this)
```

---

## Step 1 — Create a Discord Bot

1. Go to https://discord.com/developers/applications and click **New Application**
2. Name it (e.g. "Squad Powers Bot") and click **Create**
3. Go to the **Bot** tab on the left
4. Click **Reset Token** and copy the token → this is your `DISCORD_TOKEN`
5. Under **Privileged Gateway Intents**, enable:
   - ✅ **Message Content Intent**
6. Go to **OAuth2 → URL Generator**:
   - Scopes: `bot`
   - Bot Permissions: `Read Messages/View Channels`, `Read Message History`
7. Copy the generated URL, open it in your browser, and invite the bot to your server

---

## Step 2 — Get Your Channel ID

1. In Discord, go to **User Settings → Advanced** and enable **Developer Mode**
2. Right-click the channel you want to watch → **Copy Channel ID**
3. Paste it as `WATCHED_CHANNEL_ID` in your `.env`

---

## Step 3 — Set Up Google Sheets Access

### Create a Service Account
1. Go to https://console.cloud.google.com
2. Create a new project (or use an existing one)
3. Go to **APIs & Services → Library** and enable:
   - **Google Sheets API**
4. Go to **APIs & Services → Credentials**
5. Click **Create Credentials → Service Account**
6. Give it a name, click **Create and Continue**, then **Done**
7. Click on the service account you just created
8. Go to the **Keys** tab → **Add Key → Create New Key → JSON**
9. Download the JSON file and rename it `service_account.json`
10. Place it in the same folder as `bot.py`

### Share Your Sheet with the Service Account
1. Open the downloaded JSON — find the `client_email` field (looks like `name@project.iam.gserviceaccount.com`)
2. Open your Google Sheet
3. Click **Share** and paste that email address — give it **Editor** access

### Get Your Spreadsheet ID
From your sheet's URL:
```
https://docs.google.com/spreadsheets/d/SPREADSHEET_ID_IS_HERE/edit
```

---

## Step 4 — Configure Environment Variables

```bash
cp .env.example .env
```

Edit `.env` and fill in all four values:

```env
DISCORD_TOKEN=your-discord-bot-token
WATCHED_CHANNEL_ID=1234567890123456789
SPREADSHEET_ID=your-spreadsheet-id
SHEET_NAME=Sheet1
GOOGLE_SERVICE_ACCOUNT_FILE=service_account.json
```

---

## Step 5 — Install & Run

```bash
# Create a virtual environment (recommended)
python3 -m venv venv
source venv/bin/activate        # Windows: venv\Scripts\activate

# Install dependencies
pip install -r requirements.txt

# Run the bot
python bot.py
```

You should see:
```
[INFO] Logged in as Squad Powers Bot#1234 (ID: ...)
[INFO] Watching channel ID: ...
```

---

## How It Works

When Subo posts a survey response in the watched channel, the bot:

1. Detects the message (author is a bot, title contains "New Response")
2. Parses **embed #1** description to extract:
   - Discord ID (from the `<@123456789>` mention)
   - Username (display name, with the mention stripped)
3. Reads each **question embed** (#2–#10) to get squad powers, types, gorilla level, and drone level
4. Looks up the Discord ID in **column B** of your sheet:
   - **Found** → updates that row in place
   - **Not found** → appends a new row
5. Stamps **Date Modified** with today's date

### Sheet Column Mapping

| Column | Data |
|--------|------|
| A | Username |
| B | Discord ID ← lookup key |
| C | 1st Squad |
| D | 1st Squad Type |
| E | 2nd Squad |
| F | 2nd Squad Type |
| G | 3rd Squad |
| H | 3rd Squad Type |
| I | Gorilla Level |
| J | Drone Level |
| K | Date Modified |

---

## Keeping It Running (Optional)

### On a Linux server with systemd

Create `/etc/systemd/system/squadbot.service`:

```ini
[Unit]
Description=Squad Powers Discord Bot
After=network.target

[Service]
User=youruser
WorkingDirectory=/path/to/discord-bot
ExecStart=/path/to/discord-bot/venv/bin/python bot.py
Restart=always
RestartSec=10

[Install]
WantedBy=multi-user.target
```

Then:
```bash
sudo systemctl enable squadbot
sudo systemctl start squadbot
sudo systemctl status squadbot
```

### On Railway / Render / Heroku
- Set each `.env` value as an environment variable in the platform's dashboard
- Upload `service_account.json` or paste its contents as a single-line env var
- Set the start command to: `python bot.py`

---

## Troubleshooting

| Problem | Fix |
|---------|-----|
| Bot doesn't see messages | Check **Message Content Intent** is enabled in Developer Portal |
| "Could not extract Discord ID" | Check Subo's embed description format hasn't changed |
| Google Sheets 403 error | Make sure the service account email has Editor access to the sheet |
| Wrong row updated | Confirm Discord IDs in column B are plain numbers (no spaces/formatting) |
