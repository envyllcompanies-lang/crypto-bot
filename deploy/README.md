# Run the paper trader 24/7, free

The paper trader runs on an Oracle Cloud "Always Free" server in Ashburn, Virginia. Ashburn is close to much of Solana's infrastructure, and the free Ampere machine has dedicated CPU. It is paper trading only: no wallet, no keys, no money.

## 1. Create an Oracle Cloud account

1. Sign up at https://www.oracle.com/cloud/free/.
2. **Home region: US East (Ashburn).** You can't change it later, and Always Free machines run only in your home region.
3. Oracle asks for a card to check your identity. Always Free resources aren't charged. Don't upgrade the account unless you decide to.

## 2. Create the server

In https://cloud.oracle.com go to Menu → Compute → Instances → **Create instance** and set:

- **Name:** `crypto-bot`.
- **Image:** Canonical Ubuntu 24.04.
- **Shape:** Change shape → Ampere → `VM.Standard.A1.Flex`, **1 OCPU, 6 GB** memory.
- **Networking:** keep the defaults, which give a public IPv4 address.
- **SSH keys:** "Generate a key pair for me" → **Save private key**. You need this file to log in.

Click **Create**. If it says "Out of capacity", try again later or pick another availability domain. Then copy the **Public IP address** from the instance page.

## 3. Log in from your Mac

In Terminal, use the key file you downloaded and the Public IP address from step 2:

```bash
chmod 600 ~/Downloads/ssh-key-YYYY-MM-DD.key
```

```bash
ssh -i ~/Downloads/ssh-key-YYYY-MM-DD.key ubuntu@PUBLIC_IP
```

## 4. Install and start (on the server)

```bash
git clone -b claude/focused-pasteur-1utc3l https://github.com/envyllcompanies-lang/crypto-bot.git ~/crypto-bot && bash ~/crypto-bot/deploy/setup.sh
```

This installs uv, Python 3.12 and the pinned packages, then runs the tests. It also starts two things:

- the paper trader, as a service that restarts after a crash or a reboot;
- a daily job at 00:10 UTC that writes `data/reports/<date>-paper.md` and deletes raw events older than 14 days.

## Everyday commands (on the server)

| What | Command |
|---|---|
| Is it running? | `systemctl status crypto-bot` |
| Live log | `journalctl -u crypto-bot -f` |
| Report so far | `cd ~/crypto-bot && ~/.local/bin/uv run python -m cryptobot.m6_report` |
| Update to the latest code | `bash ~/crypto-bot/deploy/setup.sh` |
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

- **Disk:** raw events take roughly 0.8 GB a day, and 14 days are kept. Change that with `CRYPTOBOT_KEEP_DAYS=N` in `~/crypto-bot/.env`. Trades, decisions and reports are kept.
- **Restarts:** each restart starts a new run, and Final Stretch waits 40 minutes again.
- **Idle reclamation:** Oracle's docs say it may reclaim Always Free machines that look idle over 7 days, meaning CPU, network and memory all under 20%. A 1-OCPU machine keeps the trader's share of the CPU higher. If Oracle emails an idle warning, tell Claude.
- **Faster feed:** to use a faster data feed later, put `CRYPTOBOT_WS_URL=...` in `~/crypto-bot/.env` yourself and run `sudo systemctl restart crypto-bot`. The URL is never printed.
