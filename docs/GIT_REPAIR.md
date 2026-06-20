# Git Repair

This workspace currently contains a `.git` directory that `git status` does not
recognize as a valid repository. Do not use destructive reset commands until you
have copied anything important out of the folder.

Safe Windows repair path:

```powershell
cd C:\Users\MattFields\Dawnstrike
Copy-Item .env .env.local.backup -ErrorAction SilentlyContinue
Rename-Item .git .git.broken-$(Get-Date -Format yyyyMMddHHmmss) -ErrorAction Stop
git init
git add .gitignore pyproject.toml README.md app.py intraday_scanner tests docs sample_data scripts
git status --short
```

Before committing, confirm these are not staged:

- `.env`
- `.streamlit/secrets.toml`
- `outputs/`
- `data/*.sqlite`
- logs, caches, API keys, webhook URLs, broker credentials

Dawnstrike is research/watchlist software. Do not add broker trading credentials
or order-execution code during Git repair.

