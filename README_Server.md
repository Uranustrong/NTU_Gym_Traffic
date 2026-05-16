# Gym Fetch Server

Fetch current NTU sports center occupancy from <https://rent.pe.ntu.edu.tw/> and store it locally for later history analysis.

## Environment

This project only uses Python standard-library modules. A virtual environment is optional, not required.

```bash
cd /tmp2/b12902066/Gym_Fetch
python3 --version
chmod +x fetch_counts.py
```

`requirements.txt` is kept so the project still has a normal Python shape, but there is nothing to install right now. If you prefer using a virtual environment anyway:

```bash
python3 -m venv .venv
source .venv/bin/activate
python3 -m pip install -r requirements.txt
```

If Python fails with `CERTIFICATE_VERIFY_FAILED` and mentions `Missing Subject Key Identifier`, the script already works around that by disabling OpenSSL strict certificate-chain checks while keeping normal certificate and hostname verification enabled.

## Try One Fetch

```bash
python3 fetch_counts.py
```

It creates `gym_counts.sqlite3` and appends one row per venue.

Inspect saved records:

```bash
sqlite3 gym_counts.sqlite3 \
  'SELECT fetched_at, source_updated_at, venue, current_count FROM occupancy ORDER BY fetched_at DESC LIMIT 10;'
```

Export CSV:

```bash
python3 fetch_counts.py --csv occupancy.csv
```

## Run Every 5 Minutes With cron

The sports center opening hours are:

- Monday-Friday: 08:00-22:00
- Saturday: 09:00-22:00
- Sunday: 09:00-18:00

Use `crontab -e` and add these lines. They run on exact 5-minute boundaries during opening hours.

```cron
*/5 8-21 * * 1-5 cd /tmp2/b12902066/Gym_Fetch && /usr/bin/python3 fetch_counts.py --open-hours-only >/dev/null 2>&1
*/5 9-21 * * 6 cd /tmp2/b12902066/Gym_Fetch && /usr/bin/python3 fetch_counts.py --open-hours-only >/dev/null 2>&1
*/5 9-17 * * 0 cd /tmp2/b12902066/Gym_Fetch && /usr/bin/python3 fetch_counts.py --open-hours-only >/dev/null 2>&1
```

This records 08:00 through 21:55 on weekdays, 09:00 through 21:55 on Saturday, and 09:00 through 17:55 on Sunday. It does not fetch at the closing minute itself.

Fetch output is written to daily files in `logs/`, such as `logs/2026-05-14.log`.

Check whether cron is writing data:

```bash
tail -f "logs/$(date +%F).log"
sqlite3 gym_counts.sqlite3 'SELECT COUNT(*) FROM occupancy;'
```

## Run With tmux

`tmux` is useful when you want to start a long-running fetch loop manually, keep it alive after disconnecting SSH, and come back later to inspect the output.

Start a named tmux session:

```bash
tmux new -s gym-fetch
cd /tmp2/b12902066/Gym_Fetch
```

Inside tmux, run the built-in loop. It waits until the next exact 5-minute boundary before fetching. During closed hours, it waits until the next opening time instead of waking up every 5 minutes.

```bash
python3 fetch_counts.py --loop --open-hours-only
```

If the site returns a temporary page without occupancy data, the script retries twice with a 10-second delay before skipping that timestamp.

Detach from tmux without stopping the loop:

```text
Ctrl-b d
```

Come back to the running session:

```bash
tmux attach -t gym-fetch
```

Stop the loop from inside tmux:

```text
Ctrl-c
```

Or stop the whole tmux session from outside:

```bash
tmux kill-session -t gym-fetch
```

Check whether data is still being saved:

```bash
tail -f "logs/$(date +%F).log"
sqlite3 gym_counts.sqlite3 'SELECT fetched_at, venue, current_count FROM occupancy ORDER BY id DESC LIMIT 10;'
```

For long-term unattended collection, `cron` is usually more reliable because it starts a fresh job every 5 minutes and can survive reboots if cron is enabled. `tmux` is better for interactive testing and short-running experiments.

## Useful Queries

Average occupancy by hour:

```bash
sqlite3 gym_counts.sqlite3 '
SELECT venue, strftime("%H", fetched_at) AS hour, ROUND(AVG(current_count), 1) AS avg_people
FROM occupancy
GROUP BY venue, hour
ORDER BY venue, hour;
'
```

Recent records:

```bash
sqlite3 gym_counts.sqlite3 '
SELECT fetched_at, venue, current_count
FROM occupancy
ORDER BY fetched_at DESC
LIMIT 20;
'
```

## SSL Strict Mode Note

The site can work with `curl` but fail in Python with an error like:

```text
CERTIFICATE_VERIFY_FAILED: Missing Subject Key Identifier
```

This happens because recent Python/OpenSSL combinations may enable stricter X.509 certificate-chain checks. One of those strict checks expects certificate-chain extensions such as Subject Key Identifier to be present. Some institutional websites still serve certificate chains that browsers and `curl` accept, but OpenSSL strict mode rejects.

The script handles this in `make_ssl_context()`:

```python
context = ssl.create_default_context()
context.verify_flags &= ~ssl.VERIFY_X509_STRICT
```

This does not disable HTTPS verification. Python still verifies the certificate chain against trusted CAs and still checks that the hostname matches `rent.pe.ntu.edu.tw`. It only disables OpenSSL's extra strict extension checks, which is narrower and safer than using an unverified SSL context.
