# Gym Fetch

Fetch current NTU sports center occupancy from <https://rent.pe.ntu.edu.tw/> and keep the data available for local analysis and Grafana dashboards.

This repository has two operating modes:

- [README_Server.md](README_Server.md): remote collector setup for `b12902066@ws7.csie.ntu.edu.tw:/tmp2/b12902066/Gym_Fetch`, where cron or tmux runs `fetch_counts.py`.
- [README_Local.md](README_Local.md): local macOS mirror setup for `/Users/songhejun/Downloads/My_Project/Fetch_Gym`, including `rsync.sh`, PostgreSQL sync, and Grafana.

Common local commands:

```bash
./rsync.sh pull-data
./rsync.sh push-code
python3 -m unittest test_sync_to_postgres.py
```
