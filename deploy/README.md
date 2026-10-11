# Run the paper trader 24/7, free

The paper trader runs on Google Cloud's free-tier **e2-micro** server in us-east1 (South Carolina). The free tier covers one e2-micro per month, permanently, in us-east1, us-central1 or us-west1. A 1-minute live test used about 2.4% of one CPU core and 69 MB of memory, well inside the e2-micro. It is paper trading only: no wallet, no keys, no money.

## 1. Create a Google Cloud account

1. Go to https://console.cloud.google.com/ and sign in with a Google account. Accept the free trial; Google asks for a card to check your identity.
2. **Set a $1 budget alert** at https://console.cloud.google.com/billing/budgets, so Google emails you if anything ever starts costing money.

## 2. Create the server

Go to https://console.cloud.google.com/compute/instances. Click "Enable" if asked, then **Create instance**, and set:

- **Name:** `crypto-bot`.
- **Region:** `us-east1` (South Carolina), zone `us-east1-b`.
- **Machine type:** E2 → `e2-micro`.
- **Boot disk:** Change → **Ubuntu 24.04 LTS** (x86/64), disk type **Standard persistent disk**, size **30 GB**. The default "Balanced" disk is not free.
- **Firewall:** leave both boxes unticked; nothing needs to reach the server.

Click **Create**.

## 3. Open a terminal on the server

In the instance list, click **SSH** next to `crypto-bot`. A terminal opens in your browser; no key files are needed.

## Or: Oracle Cloud Always Free (preferred if you have an account)

Oracle's Always Free tier is permanent; only its $300 trial credit ends after 30 days. Its Ampere machine has dedicated CPU and runs in Ashburn, Virginia, close to Solana's infrastructure.

1. Check that the region shown at the top right of https://cloud.oracle.com is **US East (Ashburn)**. That's your home region, and Always Free machines run only there.
2. Go to Menu → Compute → Instances → **Create instance** and set:
   - **Image:** Canonical Ubuntu 24.04.
   - **Shape:** Ampere `VM.Standard.A1.Flex`, 1 OCPU, 6 GB. It must show **"Always Free-eligible"**.
   - **SSH keys:** "Generate a key pair for me" → **Save private key**.
3. Click **Create**. If it says "Out of capacity", retry later or pick another availability domain. Copy the **Public IP address**.
4. From your Mac's Terminal, run `chmod 600` on the key file, then:
   `ssh -i ~/Downloads/YOUR-KEY.key ubuntu@PUBLIC_IP`
5. Continue with step 4 below.

## 4. Install and start (on the server)

```bash
sudo apt-get update -qq && sudo apt-get install -y -qq git curl ca-certificates && git clone -b claude/focused-pasteur-1utc3l https://github.com/envyllcompanies-lang/crypto-bot.git ~/crypto-bot && bash ~/crypto-bot/deploy/setup.sh
```

This installs uv, Python 3.12 and the pinned packages, then runs the tests. It also starts two things:

- the paper trader, as a service that restarts after a crash or a reboot;
- an update check every 15 minutes: new commits on GitHub are pulled and tested, and the trader restarts only if the tests pass;
- a daily job at 00:10 UTC that rebuilds the tracked-wallet list from the last day of recordings (`data/reports/<date>-tracked.md`; set `CRYPTOBOT_TRACKED_DAYS` in `.env` for more days), writes `data/reports/<date>-paper.md` and deletes raw events older than 14 days. It is capped at 450 MB of memory so it can't starve the trader; if the list build hits the cap, the previous list stays in use.

## Everyday commands (on the server)

| What | Command |
|---|---|
| Is it running? | `systemctl status crypto-bot` |
| Live log | `journalctl -u crypto-bot -f` |
| Report so far | `cd ~/crypto-bot && ~/.local/bin/uv run python -m cryptobot.m6_report` |
| Update now (it also updates itself every 15 minutes) | `bash ~/crypto-bot/deploy/setup.sh` |
| Update log | `journalctl -u crypto-bot-update` |
| Stop / start | `sudo systemctl stop crypto-bot` / `sudo systemctl start crypto-bot` |

## Optional: daily report pushed to GitHub

To read the daily report on GitHub (on the `paper-results` branch) instead of logging in:

1. On the server, run:

   ```bash
   ssh-keygen -t ed25519 -N "" -f ~/.ssh/crypto_bot_deploy -C crypto-bot-server && printf 'Host github.com\n  IdentityFile ~/.ssh/crypto_bot_deploy\n  IdentitiesOnly yes\n  StrictHostKeyChecking accept-new\n' >> ~/.ssh/config && cat ~/.ssh/crypto_bot_deploy.pub
   ```

2. Copy the line it prints. It's a public key, so it's safe to share.
3. Add it at https://github.com/envyllcompanies-lang/crypto-bot/settings/keys → **Add deploy key**, and tick **Allow write access**.
4. Back on the server:

   ```bash
   git -C ~/crypto-bot remote set-url origin git@github.com:envyllcompanies-lang/crypto-bot.git && echo CRYPTOBOT_PUSH_REPORTS=1 >> ~/crypto-bot/.env
   ```

## Things to know

- **Cost check:** the free tier covers the machine, a 30 GB standard disk and 1 GB a month of outbound traffic. The trader mostly downloads, and incoming traffic is free. Google's pricing for the server's public IPv4 address has changed over time, so look at the billing page after a day; the budget alert will also email you.
- **Disk:** raw events take roughly 0.8 GB a day, and 14 days are kept. Change that with `CRYPTOBOT_KEEP_DAYS=N` in `~/crypto-bot/.env`. Trades, decisions and reports are kept.
- **Restarts:** each restart starts a new run, and Final Stretch waits 40 minutes again.
- **Narrative judge (optional, free):** the trader can ask a free LLM tier to rate each candidate token's narrative. Get a free key at https://console.groq.com/keys (Groq, used first) and/or https://aistudio.google.com/apikey (Gemini, the fallback), then type it in yourself on the server, replacing `...` with the key:

  ```bash
  echo 'GROQ_API_KEY=...' >> ~/crypto-bot/.env && sudo systemctl restart crypto-bot
  ```

  For Gemini, use `GEMINI_API_KEY=...` the same way. The log line `narrative judge: groq` confirms it (`journalctl -u crypto-bot | grep narrative`); the key itself is never printed. Without a key the narrative is "not evaluated". Free tiers have daily limits; once one runs out, narratives are "not evaluated" until it resets.
- **Faster feed:** to use a faster data feed later, put `CRYPTOBOT_WS_URL=...` in `~/crypto-bot/.env` yourself and run `sudo systemctl restart crypto-bot`. The URL is never printed.
